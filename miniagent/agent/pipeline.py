"""
AgentPipeline — Anthropic streaming + recursive tool call loop.

OpenClaw equivalent: runEmbeddedPiAgent() in src/agents/pi-embedded.ts.
miniagent uses the Anthropic SDK directly to make every step of the loop
explicit and learnable.

The loop:
  run(message)
    ↓ load session history
    ↓ append user message to JSONL
    ↓ emit lifecycle:started
    ↓ _run_turn(messages)         ← recursive tool call loop
         ↓ stream from Claude
         ↓ text deltas → emit assistant events
         ↓ tool_use blocks → execute_tool() → emit tool events → recurse
         ↓ end_turn → return
    ↓ append assistant message to JSONL
    ↓ emit lifecycle:complete
    ↓ return final text
"""

from ..config.loader import get_config
from ..config.types import AgentConfig
from ..protocol.methods import AgentParams
from .events import AgentEventEmitter
from .tools import BrowserClient, execute_tool, get_tool_definitions


class AgentPipeline:
    """
    Core agent execution loop.

    Can be constructed in two ways:
    1. Full mode (used by gateway handler):
         AgentPipeline(run_id, session_key, emitter, session_store, params)
         Matches DESIGN.md's constructor signature exactly.

    2. Minimal mode (used by tests / standalone usage):
         AgentPipeline(emitter, browser_client, config)
         Matches the task spec's simplified constructor.

    Both modes share the same _run_turn() logic. The difference is where
    session history comes from and how results are persisted.
    """

    def __init__(
        self,
        run_id: str,
        session_key: str,
        emitter: AgentEventEmitter,
        session_store: object | None,  # SessionStore | None
        params: AgentParams | None,
        *,
        config: AgentConfig | None = None,
        browser_client: BrowserClient | None = None,
    ) -> None:
        self.run_id = run_id
        self.session_key = session_key
        self.emitter = emitter
        self.session_store = session_store
        self.params = params
        self.browser_client = browser_client

        # Config: use provided config or fall back to get_config().agent
        if config is not None:
            self.cfg_agent = config
        else:
            self.cfg_agent = get_config().agent

        # Anthropic client — imported here so tests can mock anthropic.AsyncAnthropic
        import anthropic

        self._anthropic_module = anthropic
        self.client = anthropic.AsyncAnthropic()
        self._seq = 0

    @classmethod
    def from_minimal(
        cls,
        emitter: AgentEventEmitter,
        browser_client: BrowserClient,
        config: AgentConfig,
        run_id: str = "run-default",
        session_key: str = "default",
    ) -> "AgentPipeline":
        """
        Alternative constructor matching the task spec's simplified signature:
          AgentPipeline(emitter, browser_client, config)
        Returns a pipeline without session store (no JSONL persistence).
        """
        instance = cls.__new__(cls)
        instance.run_id = run_id
        instance.session_key = session_key
        instance.emitter = emitter
        instance.session_store = None
        instance.params = None
        instance.browser_client = browser_client
        instance.cfg_agent = config

        import anthropic

        instance._anthropic_module = anthropic
        instance.client = anthropic.AsyncAnthropic()
        instance._seq = 0
        return instance

    async def run(self, message: str, history: list[dict] | None = None) -> str:
        """
        Entry point. Returns final assistant text.

        Parameters
        ----------
        message:
            The new user message to process.
        history:
            Optional pre-built message history. If provided and session_store is None,
            history is used directly. If session_store is set, history is loaded from store.
        """
        if self.session_store is not None:
            # Full mode: load from JSONL store
            messages = await self.session_store.load_messages(self.session_key)
            await self.session_store.append_message(
                self.session_key, "user", message, self.run_id
            )
        elif history is not None:
            messages = list(history)
        else:
            messages = []

        messages.append({"role": "user", "content": message})

        await self._emit("lifecycle", {"status": "started", "run_id": self.run_id})

        final_text, _new_messages = await self._run_turn(messages)

        if self.session_store is not None:
            await self.session_store.append_message(
                self.session_key, "assistant", final_text, self.run_id
            )

        await self._emit("lifecycle", {"status": "complete", "run_id": self.run_id})
        return final_text

    async def _run_turn(self, messages: list[dict]) -> tuple[str, list[dict]]:
        """
        Single turn of the agentic loop.
        Returns (accumulated_text, new_messages_to_add).

        Recursion happens when stop_reason == "tool_use":
          append assistant message (with tool_use blocks) +
          user message (with tool_result blocks) → call _run_turn again
        This continues until stop_reason == "end_turn".
        """
        accumulated_text = ""
        tool_uses: list = []
        new_messages: list[dict] = []

        # Stream from Claude
        async with self.client.messages.stream(
            model=self.cfg_agent.model,
            max_tokens=self.cfg_agent.max_tokens,
            system=self.cfg_agent.system_prompt,
            messages=messages,
            tools=get_tool_definitions(),
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        delta = event.delta.text
                        accumulated_text += delta
                        await self._emit("assistant", {"delta": delta})

            final_msg = await stream.get_final_message()
            stop_reason = final_msg.stop_reason

            # Collect tool_use blocks from final message content
            for block in final_msg.content:
                if block.type == "tool_use":
                    tool_uses.append(block)

        # end_turn: done
        if stop_reason == "end_turn":
            new_messages.append({"role": "assistant", "content": accumulated_text})
            await self._emit("done", {"text": accumulated_text})
            return accumulated_text, new_messages

        # tool_use: execute tools, recurse
        if stop_reason == "tool_use":
            tool_results = await self._handle_tool_calls(tool_uses)

            new_messages.append({"role": "assistant", "content": final_msg.content})
            new_messages.append({"role": "user", "content": tool_results})

            next_text, more = await self._run_turn(messages + new_messages)
            return accumulated_text + next_text, new_messages + more

        return accumulated_text, new_messages

    async def _handle_tool_calls(self, tool_uses: list) -> list[dict]:
        """
        Execute each tool_use, emit events, return list of tool_result content blocks.
        Runs tool calls sequentially.
        """
        tool_results = []
        for block in tool_uses:
            tool_name = block.name
            tool_input = block.input

            await self._emit(
                "tool",
                {"phase": "start", "tool_name": tool_name, "input": tool_input},
            )
            # Also emit in the simplified format the task spec requires
            await self.emitter.emit(
                self.run_id,
                "tool_call",
                {"name": tool_name, "input": tool_input},
                self.session_key,
            )

            try:
                if self.browser_client is not None:
                    # Use the injected browser client (test/standalone mode)
                    result = await self.browser_client.call(tool_name, tool_input)
                else:
                    # Use the global execute_tool which creates its own BrowserClient
                    result = await execute_tool(tool_name, tool_input)

                await self._emit(
                    "tool",
                    {"phase": "result", "tool_name": tool_name, "result": result},
                )
                await self.emitter.emit(
                    self.run_id,
                    "tool_result",
                    {"name": tool_name, "result": result},
                    self.session_key,
                )
            except Exception as e:
                result = {"error": str(e)}
                await self._emit(
                    "tool",
                    {
                        "phase": "result",
                        "tool_name": tool_name,
                        "result": result,
                        "error": True,
                    },
                )
                await self.emitter.emit(
                    self.run_id,
                    "tool_result",
                    {"name": tool_name, "result": result, "error": True},
                    self.session_key,
                )

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result),
                }
            )

        return tool_results

    async def _emit(self, stream: str, data: dict) -> None:
        """Emit one agent event. Auto-increments per-run seq."""
        await self.emitter.emit(self.run_id, stream, data, self.session_key)
