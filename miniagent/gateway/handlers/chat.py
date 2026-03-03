"""
Chat handlers: "chat.history" and "chat.abort".

OpenClaw equivalent: src/gateway/server-methods/chat.ts

- chat.history: loads message history from SessionStore
- chat.abort:   cancels an active agent run via SessionState
"""

from __future__ import annotations

from ...protocol.error_codes import ErrorCode, error_shape
from ...protocol.methods import ChatAbortParams, ChatHistoryParams
from ...sessions.store import SessionStore
from ..handler_registry import HandlerContext, HandlerFn
from ..session_state import SessionState


def make_chat_handlers(
    session_store: SessionStore,
    session_state: SessionState,
) -> dict[str, HandlerFn]:
    """
    Factory returns dict of chat handlers.
    Closes over session_store and session_state.
    """

    async def chat_history(ctx: HandlerContext) -> None:
        """Load and return conversation history for a session."""
        try:
            params = ChatHistoryParams.model_validate(ctx.params)
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INVALID_REQUEST, str(e)))
            return

        try:
            # SessionStore.load() returns list of dicts (JSONL entries)
            limit = params.limit if params.limit is not None else 50
            messages = await session_store.load(params.session_key, limit=limit)
            await ctx.respond(True, {"messages": messages})
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INTERNAL, str(e)))

    async def chat_abort(ctx: HandlerContext) -> None:
        """Abort an active agent run for a session."""
        try:
            params = ChatAbortParams.model_validate(ctx.params)
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INVALID_REQUEST, str(e)))
            return

        try:
            session_key = params.session_key
            run_id = params.run_id

            if run_id is not None:
                # Abort a specific run
                session_state.abort_run(session_key, run_id)
            else:
                # Abort the most recent active run for this session
                active_run_id = session_state.get_active_run_id(session_key)
                if active_run_id is not None:
                    session_state.abort_run(session_key, active_run_id)

            await ctx.respond(True, {"aborted": True})
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INTERNAL, str(e)))

    return {
        "chat.history": chat_history,
        "chat.abort": chat_abort,
    }
