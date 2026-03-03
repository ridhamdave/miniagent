"""
"agent" RPC handler — the double-response pattern.

This is the architectural heart of miniagent. OpenClaw equivalent:
agentHandlers["agent"] in src/gateway/server-methods/agent.ts

The double-response pattern:
  1. Validate params
  2. Check idempotency dedupe map → return cached response on hit
  3. Call respond(ok=True, {run_id, status:"accepted"})  ← FIRST response, immediate
  4. asyncio.create_task(run_pipeline(...))              ← fire-and-forget
     ↑ handler returns here; WebSocket is free for other messages
  5. [background] pipeline runs, emits events, calls respond again when done
  6. respond(ok=True, {run_id, status:"ok"})            ← SECOND response

Why the callback instead of return value:
- Step 3 and step 6 both send a ResponseFrame with the SAME req_id
- A return-value approach can only send once
- The callback approach lets background tasks call respond() at any time
"""

from __future__ import annotations

import asyncio
import time
import uuid

from ...agent.events import AgentEventEmitter
from ...agent.pipeline import AgentPipeline
from ...protocol.error_codes import ErrorCode, error_shape
from ...protocol.methods import AgentParams
from ...sessions.store import SessionStore
from ..handler_registry import HandlerContext, HandlerFn
from ..session_state import ActiveRun, SessionState


def make_agent_handler(
    emitter: AgentEventEmitter,
    session_store: SessionStore,
    session_state: SessionState,
) -> HandlerFn:
    """
    Factory returns the "agent" RPC handler.

    Closes over shared state: emitter, session_store, session_state.
    Returns a HandlerFn that can be registered with HandlerRegistry.
    """

    async def handler(ctx: HandlerContext) -> None:
        try:
            params = AgentParams.model_validate(ctx.params)
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INVALID_REQUEST, str(e)))
            return

        # Idempotency: if we've seen this key before, return the cached result
        idem_key = f"agent:{params.idempotency_key}"
        if cached := session_state.dedupe.get(idem_key):
            await ctx.respond(cached["ok"], cached.get("payload"), cached.get("error"))
            return

        run_id = str(uuid.uuid4())
        session_key = params.session_key or "default"

        # FIRST response: immediate ack
        accepted = {"run_id": run_id, "status": "accepted"}
        session_state.dedupe[idem_key] = {"ok": True, "payload": accepted}
        await ctx.respond(True, accepted)

        # Background task: run the actual agent pipeline
        async def _run() -> None:
            pipeline = AgentPipeline(
                run_id=run_id,
                session_key=session_key,
                emitter=emitter,
                session_store=session_store,
                params=params,
            )
            try:
                result_text = await pipeline.run(params.message)
                final = {"run_id": run_id, "status": "ok", "result": result_text}
                session_state.dedupe[idem_key] = {"ok": True, "payload": final}
                try:
                    await ctx.respond(True, final)
                except Exception:
                    pass  # WS may have closed before task completed
            except asyncio.CancelledError:
                err = error_shape(ErrorCode.ABORTED, "run was aborted")
                session_state.dedupe[idem_key] = {"ok": False, "error": err.model_dump()}
                try:
                    await ctx.respond(False, None, err)
                except Exception:
                    pass
            except Exception as e:
                err = error_shape(ErrorCode.INTERNAL, str(e))
                session_state.dedupe[idem_key] = {"ok": False, "error": err.model_dump()}
                try:
                    await ctx.respond(False, None, err)
                except Exception:
                    pass
            finally:
                session_state.complete_run(run_id)

        task = asyncio.create_task(_run())
        session_state.register_run(
            ActiveRun(
                run_id=run_id,
                session_key=session_key,
                task=task,
                started_at=time.time(),
                conn_id=ctx.conn_id,
            )
        )

    return handler
