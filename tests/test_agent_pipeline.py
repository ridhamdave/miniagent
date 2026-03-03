"""
Tests for the agent/ module.

Mocks:
- anthropic.AsyncAnthropic (no real API calls)
- aiohttp.ClientSession (no real HTTP calls)

Covers:
- AgentEventEmitter: subscribe, unsubscribe, emit, multiple subscribers
- BrowserClient: each method makes the right HTTP call, returns response dict
- AgentPipeline.run(): text-only response, tool_use response, event sequence
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniagent.agent.events import AgentEventEmitter, AgentEventPayload
from miniagent.agent.pipeline import AgentPipeline
from miniagent.agent.tools import BrowserClient, get_tool_definitions
from miniagent.config.types import AgentConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_text_delta(text: str) -> SimpleNamespace:
    """Build a fake content_block_delta event with a text delta."""
    delta = SimpleNamespace(text=text)
    return SimpleNamespace(type="content_block_delta", delta=delta)


def _make_tool_use_block(tool_id: str, name: str, input_: dict) -> SimpleNamespace:
    """Build a fake tool_use content block."""
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_)


def _make_text_block(text: str) -> SimpleNamespace:
    """Build a fake text content block."""
    return SimpleNamespace(type="text", text=text)


class _FakeStreamContext:
    """
    Async context manager that simulates anthropic client.messages.stream().

    It yields events from `events_to_yield` when iterated, and returns
    `final_message` from get_final_message().
    """

    def __init__(self, events_to_yield: list, final_message: SimpleNamespace) -> None:
        self._events = events_to_yield
        self._final_message = final_message

    async def __aenter__(self) -> "_FakeStreamContext":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def __aiter__(self) -> AsyncIterator[SimpleNamespace]:
        return self._aiter_impl()

    async def _aiter_impl(self):
        for event in self._events:
            yield event

    async def get_final_message(self) -> SimpleNamespace:
        return self._final_message


def _build_text_only_pipeline(
    emitter: AgentEventEmitter,
    text_chunks: list[str],
    config: AgentConfig | None = None,
) -> tuple[AgentPipeline, MagicMock]:
    """
    Build an AgentPipeline whose Anthropic client returns only text tokens,
    then end_turn.

    Returns (pipeline, mock_anthropic_client).
    """
    if config is None:
        config = AgentConfig(
            model="claude-opus-4-6",
            max_tokens=1024,
            system_prompt="You are helpful.",
        )

    events = [_make_text_delta(chunk) for chunk in text_chunks]
    final_text = "".join(text_chunks)
    final_message = SimpleNamespace(
        stop_reason="end_turn",
        content=[_make_text_block(final_text)],
    )
    stream_ctx = _FakeStreamContext(events, final_message)

    mock_messages = MagicMock()
    mock_messages.stream = MagicMock(return_value=stream_ctx)

    mock_client = MagicMock()
    mock_client.messages = mock_messages

    pipeline = AgentPipeline.from_minimal(
        emitter=emitter,
        browser_client=BrowserClient("http://localhost:18790"),
        config=config,
    )
    pipeline.client = mock_client
    return pipeline, mock_client


def _build_tool_use_pipeline(
    emitter: AgentEventEmitter,
    tool_id: str,
    tool_name: str,
    tool_input: dict,
    tool_result: dict,
    second_text: str = "Done.",
    config: AgentConfig | None = None,
) -> tuple[AgentPipeline, BrowserClient, MagicMock]:
    """
    Build an AgentPipeline whose first Anthropic call returns a tool_use block,
    and second call returns plain text (end_turn).

    Returns (pipeline, mock_browser_client, mock_anthropic_client).
    """
    if config is None:
        config = AgentConfig(
            model="claude-opus-4-6",
            max_tokens=1024,
            system_prompt="You are helpful.",
        )

    # First stream: stop_reason=tool_use, content has tool_use block
    first_tool_block = _make_tool_use_block(tool_id, tool_name, tool_input)
    first_message = SimpleNamespace(
        stop_reason="tool_use",
        content=[first_tool_block],
    )
    first_stream = _FakeStreamContext([], first_message)

    # Second stream: stop_reason=end_turn, content has text
    second_events = [_make_text_delta(second_text)]
    second_message = SimpleNamespace(
        stop_reason="end_turn",
        content=[_make_text_block(second_text)],
    )
    second_stream = _FakeStreamContext(second_events, second_message)

    call_count = {"n": 0}

    def _stream_side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return first_stream
        return second_stream

    mock_messages = MagicMock()
    mock_messages.stream = MagicMock(side_effect=_stream_side_effect)

    mock_client = MagicMock()
    mock_client.messages = mock_messages

    # Mock BrowserClient
    mock_browser = MagicMock(spec=BrowserClient)
    mock_browser.call = AsyncMock(return_value=tool_result)

    pipeline = AgentPipeline.from_minimal(
        emitter=emitter,
        browser_client=mock_browser,
        config=config,
    )
    pipeline.client = mock_client
    return pipeline, mock_browser, mock_client


# ===========================================================================
# Tests: AgentEventEmitter
# ===========================================================================


class TestAgentEventEmitter:
    async def test_emit_calls_single_subscriber(self) -> None:
        emitter = AgentEventEmitter()
        received: list[AgentEventPayload] = []

        async def on_event(evt: AgentEventPayload) -> None:
            received.append(evt)

        emitter.subscribe(on_event)
        await emitter.emit("run-1", "assistant", {"delta": "Hello"})

        assert len(received) == 1
        evt = received[0]
        assert evt.run_id == "run-1"
        assert evt.stream == "assistant"
        assert evt.data == {"delta": "Hello"}
        assert evt.seq == 1

    async def test_emit_increments_seq_per_run(self) -> None:
        emitter = AgentEventEmitter()
        received: list[AgentEventPayload] = []

        async def on_event(evt: AgentEventPayload) -> None:
            received.append(evt)

        emitter.subscribe(on_event)
        await emitter.emit("run-1", "assistant", {"delta": "A"})
        await emitter.emit("run-1", "assistant", {"delta": "B"})
        await emitter.emit("run-2", "lifecycle", {"status": "started"})

        assert received[0].seq == 1
        assert received[1].seq == 2
        # run-2 starts its own seq counter
        assert received[2].seq == 1
        assert received[2].run_id == "run-2"

    async def test_emit_calls_multiple_subscribers(self) -> None:
        emitter = AgentEventEmitter()
        results_a: list[str] = []
        results_b: list[str] = []

        async def sub_a(evt: AgentEventPayload) -> None:
            results_a.append(evt.data.get("delta", ""))

        async def sub_b(evt: AgentEventPayload) -> None:
            results_b.append(evt.data.get("delta", ""))

        emitter.subscribe(sub_a)
        emitter.subscribe(sub_b)
        await emitter.emit("run-1", "assistant", {"delta": "hi"})

        assert results_a == ["hi"]
        assert results_b == ["hi"]

    async def test_unsubscribe_stops_delivery(self) -> None:
        emitter = AgentEventEmitter()
        received: list[AgentEventPayload] = []

        async def on_event(evt: AgentEventPayload) -> None:
            received.append(evt)

        sub_id = emitter.subscribe(on_event)
        await emitter.emit("run-1", "assistant", {"delta": "first"})
        emitter.unsubscribe(sub_id)
        await emitter.emit("run-1", "assistant", {"delta": "second"})

        assert len(received) == 1
        assert received[0].data["delta"] == "first"

    async def test_unsubscribe_only_removes_specified_subscriber(self) -> None:
        emitter = AgentEventEmitter()
        received_a: list[AgentEventPayload] = []
        received_b: list[AgentEventPayload] = []

        async def sub_a(evt: AgentEventPayload) -> None:
            received_a.append(evt)

        async def sub_b(evt: AgentEventPayload) -> None:
            received_b.append(evt)

        sub_a_id = emitter.subscribe(sub_a)
        emitter.subscribe(sub_b)

        emitter.unsubscribe(sub_a_id)
        await emitter.emit("run-1", "lifecycle", {"status": "started"})

        assert len(received_a) == 0
        assert len(received_b) == 1

    async def test_unsubscribe_nonexistent_id_is_noop(self) -> None:
        emitter = AgentEventEmitter()
        # Should not raise
        emitter.unsubscribe("nonexistent-id-12345")

    async def test_session_key_passed_through(self) -> None:
        emitter = AgentEventEmitter()
        received: list[AgentEventPayload] = []

        async def on_event(evt: AgentEventPayload) -> None:
            received.append(evt)

        emitter.subscribe(on_event)
        await emitter.emit("run-1", "lifecycle", {"status": "started"}, "session-xyz")

        assert received[0].session_key == "session-xyz"

    async def test_on_method_subscribes(self) -> None:
        """Test the on() convenience method (no sub_id returned)."""
        emitter = AgentEventEmitter()
        received: list[AgentEventPayload] = []

        async def on_event(evt: AgentEventPayload) -> None:
            received.append(evt)

        emitter.on(on_event)
        await emitter.emit("run-1", "assistant", {"delta": "test"})
        assert len(received) == 1

    async def test_emit_returns_exceptions_without_raising(self) -> None:
        """A failing subscriber should not prevent other subscribers from receiving the event."""
        emitter = AgentEventEmitter()
        received: list[AgentEventPayload] = []

        async def bad_sub(evt: AgentEventPayload) -> None:
            raise RuntimeError("subscriber error")

        async def good_sub(evt: AgentEventPayload) -> None:
            received.append(evt)

        emitter.subscribe(bad_sub)
        emitter.subscribe(good_sub)

        # Should not raise even though bad_sub throws
        await emitter.emit("run-1", "assistant", {"delta": "hello"})
        assert len(received) == 1


# ===========================================================================
# Tests: BrowserClient
# ===========================================================================


class TestBrowserClient:
    """Tests that BrowserClient methods make the right HTTP calls via aiohttp."""

    def _make_mock_session(self, response_data: dict) -> MagicMock:
        """Build a mock aiohttp.ClientSession whose requests return response_data."""
        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value=response_data)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.close = AsyncMock()
        return mock_session

    async def test_navigate_posts_to_navigate(self) -> None:
        client = BrowserClient("http://localhost:18790")
        expected = {"ok": True, "url": "https://example.com", "title": "Example"}
        mock_session = self._make_mock_session(expected)
        client._session = mock_session

        result = await client.navigate("https://example.com")

        mock_session.post.assert_called_once_with(
            "http://localhost:18790/navigate", json={"url": "https://example.com"}
        )
        assert result == expected

    async def test_screenshot_gets_screenshot(self) -> None:
        client = BrowserClient("http://localhost:18790")
        expected = {"image_b64": "abc123", "mime_type": "image/png"}
        mock_session = self._make_mock_session(expected)
        client._session = mock_session

        result = await client.screenshot()

        mock_session.get.assert_called_once_with("http://localhost:18790/screenshot")
        assert result == expected

    async def test_click_posts_to_click(self) -> None:
        client = BrowserClient("http://localhost:18790")
        expected = {"ok": True}
        mock_session = self._make_mock_session(expected)
        client._session = mock_session

        result = await client.click("button#submit", double=False)

        mock_session.post.assert_called_once_with(
            "http://localhost:18790/click",
            json={"ref": "button#submit", "double_click": False},
        )
        assert result == expected

    async def test_click_double_click(self) -> None:
        client = BrowserClient("http://localhost:18790")
        expected = {"ok": True}
        mock_session = self._make_mock_session(expected)
        client._session = mock_session

        result = await client.click("a.link", double=True)

        mock_session.post.assert_called_once_with(
            "http://localhost:18790/click",
            json={"ref": "a.link", "double_click": True},
        )
        assert result == expected

    async def test_type_text_posts_to_type(self) -> None:
        client = BrowserClient("http://localhost:18790")
        expected = {"ok": True}
        mock_session = self._make_mock_session(expected)
        client._session = mock_session

        result = await client.type_text("input#search", "hello world")

        mock_session.post.assert_called_once_with(
            "http://localhost:18790/type",
            json={"ref": "input#search", "text": "hello world"},
        )
        assert result == expected

    async def test_get_text_without_ref(self) -> None:
        client = BrowserClient("http://localhost:18790")
        expected = {"text": "page content"}
        mock_session = self._make_mock_session(expected)
        client._session = mock_session

        result = await client.get_text()

        mock_session.get.assert_called_once_with(
            "http://localhost:18790/text", params={}
        )
        assert result == expected

    async def test_get_text_with_ref(self) -> None:
        client = BrowserClient("http://localhost:18790")
        expected = {"text": "element text"}
        mock_session = self._make_mock_session(expected)
        client._session = mock_session

        result = await client.get_text(ref="#main-content")

        mock_session.get.assert_called_once_with(
            "http://localhost:18790/text", params={"ref": "#main-content"}
        )
        assert result == expected

    async def test_scroll_posts_to_scroll(self) -> None:
        client = BrowserClient("http://localhost:18790")
        expected = {"ok": True}
        mock_session = self._make_mock_session(expected)
        client._session = mock_session

        result = await client.scroll("down", 500)

        mock_session.post.assert_called_once_with(
            "http://localhost:18790/scroll",
            json={"direction": "down", "amount": 500},
        )
        assert result == expected

    async def test_close_closes_session(self) -> None:
        client = BrowserClient("http://localhost:18790")
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()
        client._session = mock_session

        await client.close()

        mock_session.close.assert_awaited_once()
        assert client._session is None

    async def test_close_when_no_session_is_noop(self) -> None:
        client = BrowserClient("http://localhost:18790")
        # Should not raise when session was never created
        await client.close()

    async def test_call_unknown_tool_raises(self) -> None:
        client = BrowserClient("http://localhost:18790")
        with pytest.raises(ValueError, match="Unknown tool"):
            await client.call("launch_missiles", {})


# ===========================================================================
# Tests: get_tool_definitions
# ===========================================================================


class TestGetToolDefinitions:
    def test_returns_list_of_six_tools(self) -> None:
        tools = get_tool_definitions()
        assert len(tools) == 6

    def test_tool_names_are_correct(self) -> None:
        tools = get_tool_definitions()
        names = [t["name"] for t in tools]
        assert "navigate" in names
        assert "screenshot" in names
        assert "click" in names
        assert "type_text" in names
        assert "get_text" in names
        assert "scroll" in names

    def test_each_tool_has_required_fields(self) -> None:
        tools = get_tool_definitions()
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            schema = tool["input_schema"]
            assert schema["type"] == "object"
            assert "properties" in schema


# ===========================================================================
# Tests: AgentPipeline.run() — text-only response
# ===========================================================================


class TestAgentPipelineTextOnly:
    async def test_run_returns_accumulated_text(self) -> None:
        emitter = AgentEventEmitter()
        pipeline, _ = _build_text_only_pipeline(emitter, ["Hello", ", ", "world!"])
        result = await pipeline.run("Say hello")
        assert result == "Hello, world!"

    async def test_run_emits_token_events(self) -> None:
        emitter = AgentEventEmitter()
        events: list[AgentEventPayload] = []

        async def capture(evt: AgentEventPayload) -> None:
            events.append(evt)

        emitter.subscribe(capture)
        pipeline, _ = _build_text_only_pipeline(emitter, ["Hi", "!"])
        await pipeline.run("Say hi")

        assistant_events = [e for e in events if e.stream == "assistant"]
        deltas = [e.data["delta"] for e in assistant_events]
        assert "Hi" in deltas
        assert "!" in deltas

    async def test_run_emits_lifecycle_started(self) -> None:
        emitter = AgentEventEmitter()
        events: list[AgentEventPayload] = []

        async def capture(evt: AgentEventPayload) -> None:
            events.append(evt)

        emitter.subscribe(capture)
        pipeline, _ = _build_text_only_pipeline(emitter, ["hi"])
        await pipeline.run("hi")

        lifecycle_events = [e for e in events if e.stream == "lifecycle"]
        statuses = [e.data.get("status") for e in lifecycle_events]
        assert "started" in statuses

    async def test_run_emits_lifecycle_complete(self) -> None:
        emitter = AgentEventEmitter()
        events: list[AgentEventPayload] = []

        async def capture(evt: AgentEventPayload) -> None:
            events.append(evt)

        emitter.subscribe(capture)
        pipeline, _ = _build_text_only_pipeline(emitter, ["hi"])
        await pipeline.run("hi")

        lifecycle_events = [e for e in events if e.stream == "lifecycle"]
        statuses = [e.data.get("status") for e in lifecycle_events]
        assert "complete" in statuses

    async def test_run_emits_done_event(self) -> None:
        emitter = AgentEventEmitter()
        events: list[AgentEventPayload] = []

        async def capture(evt: AgentEventPayload) -> None:
            events.append(evt)

        emitter.subscribe(capture)
        pipeline, _ = _build_text_only_pipeline(emitter, ["Hello, world!"])
        await pipeline.run("Say hello")

        done_events = [e for e in events if e.stream == "done"]
        assert len(done_events) == 1
        assert done_events[0].data["text"] == "Hello, world!"

    async def test_event_order_tokens_before_done(self) -> None:
        """Token events must arrive before the done event."""
        emitter = AgentEventEmitter()
        streams_in_order: list[str] = []

        async def capture(evt: AgentEventPayload) -> None:
            streams_in_order.append(evt.stream)

        emitter.subscribe(capture)
        pipeline, _ = _build_text_only_pipeline(emitter, ["A", "B", "C"])
        await pipeline.run("write abc")

        # Extract only assistant/done streams
        assistant_and_done = [s for s in streams_in_order if s in ("assistant", "done")]
        # All "assistant" deltas must come before "done"
        if "done" in assistant_and_done:
            done_idx = assistant_and_done.index("done")
            for s in assistant_and_done[:done_idx]:
                assert s == "assistant"

    async def test_run_with_history(self) -> None:
        """Test that history messages are passed to the stream call."""
        emitter = AgentEventEmitter()
        pipeline, mock_client = _build_text_only_pipeline(emitter, ["OK"])

        history = [
            {"role": "user", "content": "previous message"},
            {"role": "assistant", "content": "previous response"},
        ]
        await pipeline.run("new message", history=history)

        # Check that stream was called with messages including history
        call_args = mock_client.messages.stream.call_args
        messages_arg = call_args.kwargs["messages"]
        # The messages should contain history + new user message
        assert len(messages_arg) >= 3  # 2 history + 1 new user message

    async def test_run_empty_history(self) -> None:
        emitter = AgentEventEmitter()
        pipeline, mock_client = _build_text_only_pipeline(emitter, ["Response"])
        result = await pipeline.run("Hello", history=[])
        assert result == "Response"


# ===========================================================================
# Tests: AgentPipeline.run() — tool_use response
# ===========================================================================


class TestAgentPipelineToolUse:
    async def test_run_calls_browser_client_for_tool(self) -> None:
        emitter = AgentEventEmitter()
        pipeline, mock_browser, _ = _build_tool_use_pipeline(
            emitter=emitter,
            tool_id="tool-123",
            tool_name="navigate",
            tool_input={"url": "https://google.com"},
            tool_result={"ok": True, "url": "https://google.com", "title": "Google"},
            second_text="I navigated to Google.",
        )

        result = await pipeline.run("Go to google")

        mock_browser.call.assert_awaited_once_with(
            "navigate", {"url": "https://google.com"}
        )
        assert "I navigated to Google." in result

    async def test_run_emits_tool_call_event(self) -> None:
        emitter = AgentEventEmitter()
        events: list[AgentEventPayload] = []

        async def capture(evt: AgentEventPayload) -> None:
            events.append(evt)

        emitter.subscribe(capture)
        pipeline, _, _ = _build_tool_use_pipeline(
            emitter=emitter,
            tool_id="tool-456",
            tool_name="screenshot",
            tool_input={},
            tool_result={"image_b64": "abc", "mime_type": "image/png"},
        )

        await pipeline.run("Take a screenshot")

        tool_call_events = [e for e in events if e.stream == "tool_call"]
        assert len(tool_call_events) >= 1
        assert tool_call_events[0].data["name"] == "screenshot"

    async def test_run_emits_tool_result_event(self) -> None:
        emitter = AgentEventEmitter()
        events: list[AgentEventPayload] = []

        async def capture(evt: AgentEventPayload) -> None:
            events.append(evt)

        emitter.subscribe(capture)
        tool_result = {"image_b64": "xyz", "mime_type": "image/png"}
        pipeline, _, _ = _build_tool_use_pipeline(
            emitter=emitter,
            tool_id="tool-789",
            tool_name="screenshot",
            tool_input={},
            tool_result=tool_result,
        )

        await pipeline.run("Take a screenshot")

        tool_result_events = [e for e in events if e.stream == "tool_result"]
        assert len(tool_result_events) >= 1
        assert tool_result_events[0].data["name"] == "screenshot"
        assert tool_result_events[0].data["result"] == tool_result

    async def test_run_recurses_after_tool_use(self) -> None:
        """After tool_use, pipeline must recurse and call stream() again."""
        emitter = AgentEventEmitter()
        pipeline, _, mock_client = _build_tool_use_pipeline(
            emitter=emitter,
            tool_id="t1",
            tool_name="navigate",
            tool_input={"url": "https://example.com"},
            tool_result={"ok": True},
        )

        await pipeline.run("Visit example.com")

        # stream() must have been called twice: first for tool_use, then for end_turn
        assert mock_client.messages.stream.call_count == 2

    async def test_run_final_text_includes_post_tool_text(self) -> None:
        emitter = AgentEventEmitter()
        pipeline, _, _ = _build_tool_use_pipeline(
            emitter=emitter,
            tool_id="t1",
            tool_name="scroll",
            tool_input={"direction": "down", "amount": 300},
            tool_result={"ok": True},
            second_text="Scrolled down by 300px.",
        )

        result = await pipeline.run("Scroll down")
        assert "Scrolled down by 300px." in result

    async def test_event_sequence_for_tool_use(self) -> None:
        """lifecycle:started → tool_call → tool_result → done → lifecycle:complete"""
        emitter = AgentEventEmitter()
        streams: list[str] = []

        async def capture(evt: AgentEventPayload) -> None:
            streams.append(evt.stream)

        emitter.subscribe(capture)
        pipeline, _, _ = _build_tool_use_pipeline(
            emitter=emitter,
            tool_id="t1",
            tool_name="navigate",
            tool_input={"url": "https://example.com"},
            tool_result={"ok": True},
            second_text="Done.",
        )

        await pipeline.run("Go somewhere")

        # Check ordering constraints
        assert "lifecycle" in streams
        assert "tool_call" in streams
        assert "tool_result" in streams
        assert "done" in streams

        # lifecycle:started before tool_call
        first_lifecycle_idx = streams.index("lifecycle")
        first_tool_call_idx = streams.index("tool_call")
        assert first_lifecycle_idx < first_tool_call_idx

        # tool_call before tool_result
        tool_call_idx = streams.index("tool_call")
        tool_result_idx = streams.index("tool_result")
        assert tool_call_idx < tool_result_idx

        # done before lifecycle:complete (done is emitted in _run_turn, complete after)
        done_idx = streams.index("done")
        last_lifecycle_idx = len(streams) - 1 - streams[::-1].index("lifecycle")
        assert done_idx < last_lifecycle_idx

    async def test_tool_error_emits_tool_result_with_error_flag(self) -> None:
        """When the browser client raises, a tool_result event with error=True is emitted."""
        emitter = AgentEventEmitter()
        events: list[AgentEventPayload] = []

        async def capture(evt: AgentEventPayload) -> None:
            events.append(evt)

        emitter.subscribe(capture)

        # Build a pipeline where the browser client raises on call()
        config = AgentConfig(model="claude-opus-4-6", max_tokens=1024, system_prompt="")
        first_tool_block = _make_tool_use_block("t1", "navigate", {"url": "bad"})
        first_message = SimpleNamespace(
            stop_reason="tool_use",
            content=[first_tool_block],
        )
        first_stream = _FakeStreamContext([], first_message)

        second_events = [_make_text_delta("Recovered.")]
        second_message = SimpleNamespace(
            stop_reason="end_turn",
            content=[_make_text_block("Recovered.")],
        )
        second_stream = _FakeStreamContext(second_events, second_message)

        call_count = {"n": 0}

        def _stream_side_effect(*args, **kwargs):
            call_count["n"] += 1
            return first_stream if call_count["n"] == 1 else second_stream

        mock_messages = MagicMock()
        mock_messages.stream = MagicMock(side_effect=_stream_side_effect)
        mock_client = MagicMock()
        mock_client.messages = mock_messages

        mock_browser = MagicMock(spec=BrowserClient)
        mock_browser.call = AsyncMock(side_effect=RuntimeError("network error"))

        pipeline = AgentPipeline.from_minimal(
            emitter=emitter,
            browser_client=mock_browser,
            config=config,
        )
        pipeline.client = mock_client

        await pipeline.run("Navigate to bad URL")

        error_result_events = [
            e for e in events
            if e.stream == "tool_result" and e.data.get("error") is True
        ]
        assert len(error_result_events) >= 1
        assert "network error" in error_result_events[0].data["result"].get("error", "")
