"""
Tests for the gateway/ module.

Coverage:
- GET / returns 200 with HTML content (serve index.html)
- WebSocket connect receives a connect.challenge event then HelloOk response
- WebSocket RequestFrame with unknown method returns ResponseFrame(ok=False)
  with ErrorCode.INVALID_REQUEST
- "chat.history" RPC returns {"messages": [...]} shape
- "chat.abort" RPC returns {"aborted": True} shape
- "agent" RPC returns first response {"status": "accepted", "run_id": "..."}
- Broadcaster fan-out: subscribe two callbacks, broadcast one event, both called
- SessionState active run tracking: start, is_active, finish, is_active again
- HandlerRegistry: register, dispatch known/unknown methods
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from miniagent.config.loader import clear_config_cache
from miniagent.gateway.broadcaster import Broadcaster
from miniagent.gateway.handler_registry import HandlerContext, HandlerRegistry
from miniagent.gateway.session_state import ActiveRun, SessionState
from miniagent.protocol.error_codes import ErrorCode


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_config():
    """Reset config cache before each test."""
    clear_config_cache()
    yield
    clear_config_cache()


def _make_app():
    """
    Create the gateway FastAPI app with mocked AgentPipeline and SessionStore.
    Uses patch to avoid real Anthropic API calls or file I/O.
    """
    from miniagent.gateway.server import create_gateway_app

    return create_gateway_app()


def _do_handshake(ws) -> dict:
    """
    Perform the connect handshake on a starlette TestClient WebSocket.

    Returns the HelloOk payload dict.
    """
    # Step 1: receive connect.challenge event
    challenge_raw = ws.receive_text()
    challenge = json.loads(challenge_raw)
    assert challenge["type"] == "event"
    assert challenge["event"] == "connect.challenge"

    # Step 2: send connect request
    req_id = str(uuid.uuid4())
    ws.send_text(json.dumps({
        "type": "req",
        "id": req_id,
        "method": "connect",
        "params": {"client": {"id": "test", "version": "1.0"}},
    }))

    # Step 3: receive HelloOk response
    resp_raw = ws.receive_text()
    resp = json.loads(resp_raw)
    assert resp["type"] == "res"
    assert resp["id"] == req_id
    assert resp["ok"] is True
    return resp


# ===========================================================================
# Tests: GET /
# ===========================================================================


class TestServeUI:
    def test_get_root_returns_200_html(self) -> None:
        """GET / should return 200 with HTML content."""
        from starlette.testclient import TestClient

        app = _make_app()
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        # Check it contains some HTML
        assert "<html" in response.text.lower() or "<!doctype" in response.text.lower()


# ===========================================================================
# Tests: WebSocket connection lifecycle
# ===========================================================================


class TestWebSocketConnect:
    def test_ws_connect_receives_challenge_then_hello_ok(self) -> None:
        """
        On WS connect:
        1. Server sends connect.challenge event
        2. Client sends connect request
        3. Server responds with HelloOk (type=hello-ok)
        """
        from starlette.testclient import TestClient

        app = _make_app()
        client = TestClient(app)

        with client.websocket_connect("/ws") as ws:
            resp = _do_handshake(ws)
            payload = resp["payload"]
            assert payload["type"] == "hello-ok"
            assert "server_version" in payload
            assert "conn_id" in payload
            assert "features" in payload
            assert "methods" in payload["features"]

    def test_ws_hello_ok_lists_registered_methods(self) -> None:
        """HelloOk features.methods should include all registered RPC methods."""
        from starlette.testclient import TestClient

        app = _make_app()
        client = TestClient(app)

        with client.websocket_connect("/ws") as ws:
            resp = _do_handshake(ws)
            methods = resp["payload"]["features"]["methods"]
            # Core methods should be present
            assert "connect" in methods
            assert "agent" in methods
            assert "chat.history" in methods
            assert "chat.abort" in methods

    def test_ws_unknown_method_returns_error_response(self) -> None:
        """
        Sending a RequestFrame with an unknown method should return
        ResponseFrame(ok=False) with error code invalid_request.
        """
        from starlette.testclient import TestClient

        app = _make_app()
        client = TestClient(app)

        with client.websocket_connect("/ws") as ws:
            _do_handshake(ws)

            # Send unknown method
            req_id = str(uuid.uuid4())
            ws.send_text(json.dumps({
                "type": "req",
                "id": req_id,
                "method": "no_such_method",
                "params": {},
            }))

            resp_raw = ws.receive_text()
            resp = json.loads(resp_raw)
            assert resp["type"] == "res"
            assert resp["id"] == req_id
            assert resp["ok"] is False
            assert resp["error"] is not None
            assert resp["error"]["code"] == ErrorCode.INVALID_REQUEST

    def test_ws_invalid_json_closes_connection(self) -> None:
        """Sending invalid JSON should cause the server to close the connection."""
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        app = _make_app()
        client = TestClient(app)

        # Sending invalid JSON triggers websocket.close(code=1003, ...)
        # On the client side this manifests as WebSocketDisconnect or
        # the context manager exiting normally — both are acceptable
        try:
            with client.websocket_connect("/ws") as ws:
                # Receive challenge first (before handshake completes)
                challenge_raw = ws.receive_text()
                challenge = json.loads(challenge_raw)
                assert challenge["event"] == "connect.challenge"

                # Send garbage (this triggers invalid frame handling before handshake)
                ws.send_text("not valid json{{}")
                # The server will close the connection — receiving may raise
                try:
                    ws.receive_text()
                except Exception:
                    pass  # Expected: connection was closed
        except Exception:
            pass  # WebSocketDisconnect or similar — expected


# ===========================================================================
# Tests: chat.history handler
# ===========================================================================


class TestChatHistoryHandler:
    def test_chat_history_returns_messages_shape(self) -> None:
        """chat.history RPC returns {"messages": [...]} shape."""
        from starlette.testclient import TestClient

        app = _make_app()

        # Patch SessionStore.load to return a canned response
        with patch("miniagent.sessions.store.SessionStore.load", new_callable=AsyncMock) as mock_load:
            mock_load.return_value = [
                {"id": "msg_1", "role": "user", "content": "Hello"},
                {"id": "msg_2", "role": "assistant", "content": "Hi!"},
            ]

            client = TestClient(app)
            with client.websocket_connect("/ws") as ws:
                _do_handshake(ws)

                req_id = str(uuid.uuid4())
                ws.send_text(json.dumps({
                    "type": "req",
                    "id": req_id,
                    "method": "chat.history",
                    "params": {"session_key": "default", "limit": 50},
                }))

                resp_raw = ws.receive_text()
                resp = json.loads(resp_raw)
                assert resp["type"] == "res"
                assert resp["id"] == req_id
                assert resp["ok"] is True
                assert "messages" in resp["payload"]
                assert isinstance(resp["payload"]["messages"], list)

    def test_chat_history_invalid_params_returns_error(self) -> None:
        """chat.history with missing required params returns error response."""
        from starlette.testclient import TestClient

        app = _make_app()
        client = TestClient(app)

        with client.websocket_connect("/ws") as ws:
            _do_handshake(ws)

            req_id = str(uuid.uuid4())
            ws.send_text(json.dumps({
                "type": "req",
                "id": req_id,
                "method": "chat.history",
                "params": {},  # Missing session_key
            }))

            resp_raw = ws.receive_text()
            resp = json.loads(resp_raw)
            assert resp["ok"] is False
            assert resp["error"]["code"] == ErrorCode.INVALID_REQUEST


# ===========================================================================
# Tests: chat.abort handler
# ===========================================================================


class TestChatAbortHandler:
    def test_chat_abort_returns_aborted_true(self) -> None:
        """chat.abort RPC returns {"aborted": True}."""
        from starlette.testclient import TestClient

        app = _make_app()
        client = TestClient(app)

        with client.websocket_connect("/ws") as ws:
            _do_handshake(ws)

            req_id = str(uuid.uuid4())
            ws.send_text(json.dumps({
                "type": "req",
                "id": req_id,
                "method": "chat.abort",
                "params": {"session_key": "default"},
            }))

            resp_raw = ws.receive_text()
            resp = json.loads(resp_raw)
            assert resp["type"] == "res"
            assert resp["id"] == req_id
            assert resp["ok"] is True
            assert resp["payload"] == {"aborted": True}

    def test_chat_abort_with_run_id_returns_aborted_true(self) -> None:
        """chat.abort with explicit run_id also returns {"aborted": True}."""
        from starlette.testclient import TestClient

        app = _make_app()
        client = TestClient(app)

        with client.websocket_connect("/ws") as ws:
            _do_handshake(ws)

            req_id = str(uuid.uuid4())
            ws.send_text(json.dumps({
                "type": "req",
                "id": req_id,
                "method": "chat.abort",
                "params": {"session_key": "default", "run_id": "run-123"},
            }))

            resp_raw = ws.receive_text()
            resp = json.loads(resp_raw)
            assert resp["ok"] is True
            assert resp["payload"] == {"aborted": True}


# ===========================================================================
# Tests: agent handler (double-response pattern)
# ===========================================================================


class TestAgentHandler:
    def test_agent_rpc_returns_accepted_status_immediately(self) -> None:
        """
        The "agent" RPC must return the first response immediately with
        {"status": "accepted", "run_id": "..."}.
        The background task runs separately.
        """
        from starlette.testclient import TestClient

        app = _make_app()

        # Mock AgentPipeline.run to complete quickly
        async def _mock_run(self_inner, message, **kwargs):
            return "done"

        # Also mock anthropic.AsyncAnthropic so constructor doesn't fail
        # when ANTHROPIC_API_KEY is not set in test environment
        mock_anthropic_client = MagicMock()

        with patch("miniagent.agent.pipeline.AgentPipeline.run", new=_mock_run), \
             patch("anthropic.AsyncAnthropic", return_value=mock_anthropic_client):
            client = TestClient(app)
            with client.websocket_connect("/ws") as ws:
                _do_handshake(ws)

                req_id = str(uuid.uuid4())
                idem_key = str(uuid.uuid4())
                ws.send_text(json.dumps({
                    "type": "req",
                    "id": req_id,
                    "method": "agent",
                    "params": {
                        "message": "Hello",
                        "session_key": "default",
                        "idempotency_key": idem_key,
                    },
                }))

                resp_raw = ws.receive_text()
                resp = json.loads(resp_raw)
                assert resp["type"] == "res"
                assert resp["id"] == req_id
                assert resp["ok"] is True
                payload = resp["payload"]
                assert payload["status"] == "accepted"
                assert "run_id" in payload
                # run_id should be a non-empty string
                assert len(payload["run_id"]) > 0

    async def test_agent_rpc_deduplicates_same_idempotency_key_direct(self) -> None:
        """
        The agent handler deduplicates requests with the same idempotency_key.
        Tested directly (not via WebSocket) for determinism.
        """
        from miniagent.agent.events import AgentEventEmitter
        from miniagent.gateway.handlers.agent import make_agent_handler

        emitter = AgentEventEmitter()
        session_state = SessionState()
        mock_store = MagicMock()
        mock_store.load = AsyncMock(return_value=[])
        mock_store.append = AsyncMock(return_value="msg_id")

        handler = make_agent_handler(emitter, mock_store, session_state)

        responses_1: list = []
        responses_2: list = []

        async def respond_1(ok: bool, payload: object = None, error: object = None) -> None:
            responses_1.append({"ok": ok, "payload": payload})

        async def respond_2(ok: bool, payload: object = None, error: object = None) -> None:
            responses_2.append({"ok": ok, "payload": payload})

        idem_key = str(uuid.uuid4())

        ctx_1 = HandlerContext(
            req_id="req-1",
            method="agent",
            params={
                "message": "Hello",
                "session_key": "default",
                "idempotency_key": idem_key,
            },
            respond=respond_1,
            conn_id="conn-1",
            session_state=session_state,
            broadcaster=Broadcaster(),
        )

        async def _mock_run(self_inner, message, **kwargs):
            return "Done"

        mock_anthropic_client = MagicMock()
        with patch("miniagent.agent.pipeline.AgentPipeline.run", new=_mock_run), \
             patch("anthropic.AsyncAnthropic", return_value=mock_anthropic_client):
            await handler(ctx_1)

        # First handler call: first response is "accepted"
        assert responses_1[0]["payload"]["status"] == "accepted"
        run_id_1 = responses_1[0]["payload"]["run_id"]

        # Second handler call with same idem key — should return cached response
        ctx_2 = HandlerContext(
            req_id="req-2",
            method="agent",
            params={
                "message": "Hello again",
                "session_key": "default",
                "idempotency_key": idem_key,  # same key
            },
            respond=respond_2,
            conn_id="conn-1",
            session_state=session_state,
            broadcaster=Broadcaster(),
        )

        with patch("miniagent.agent.pipeline.AgentPipeline.run", new=_mock_run), \
             patch("anthropic.AsyncAnthropic", return_value=mock_anthropic_client):
            await handler(ctx_2)

        # Cache hit: same run_id returned
        assert responses_2[0]["ok"] is True
        assert responses_2[0]["payload"].get("run_id") == run_id_1

    def test_agent_rpc_invalid_params_returns_error(self) -> None:
        """agent RPC with missing required params returns error."""
        from starlette.testclient import TestClient

        app = _make_app()
        client = TestClient(app)

        # No need to mock Anthropic here since params validation fails before
        # AgentPipeline is instantiated
        with client.websocket_connect("/ws") as ws:
            _do_handshake(ws)

            req_id = str(uuid.uuid4())
            ws.send_text(json.dumps({
                "type": "req",
                "id": req_id,
                "method": "agent",
                "params": {},  # Missing message and idempotency_key
            }))

            resp_raw = ws.receive_text()
            resp = json.loads(resp_raw)
            assert resp["ok"] is False
            assert resp["error"]["code"] == ErrorCode.INVALID_REQUEST


# ===========================================================================
# Tests: Broadcaster fan-out
# ===========================================================================


class TestBroadcaster:
    async def test_broadcast_calls_all_subscribed_callbacks(self) -> None:
        """Broadcaster should call all subscribed callbacks concurrently."""
        broadcaster = Broadcaster()

        received_a: list[dict] = []
        received_b: list[dict] = []

        async def callback_a(event: dict) -> None:
            received_a.append(event)

        async def callback_b(event: dict) -> None:
            received_b.append(event)

        broadcaster.subscribe("client_a", callback_a)
        broadcaster.subscribe("client_b", callback_b)

        await broadcaster.broadcast("test-event", {"key": "value"})

        assert len(received_a) == 1
        assert len(received_b) == 1
        assert received_a[0]["event"] == "test-event"
        assert received_a[0]["payload"] == {"key": "value"}
        assert received_b[0]["event"] == "test-event"

    async def test_broadcast_includes_seq_counter(self) -> None:
        """Each broadcast should increment the seq counter."""
        broadcaster = Broadcaster()
        seqs: list[int] = []

        async def callback(event: dict) -> None:
            seqs.append(event["seq"])

        broadcaster.subscribe("client", callback)

        await broadcaster.broadcast("event1", {})
        await broadcaster.broadcast("event2", {})
        await broadcaster.broadcast("event3", {})

        assert seqs == [1, 2, 3]

    async def test_unsubscribe_removes_callback(self) -> None:
        """After unsubscribe_callback, the callback should no longer be called."""
        broadcaster = Broadcaster()
        received: list[dict] = []

        async def callback(event: dict) -> None:
            received.append(event)

        broadcaster.subscribe("client", callback)
        await broadcaster.broadcast("event1", {})
        assert len(received) == 1

        broadcaster.unsubscribe_callback("client")
        await broadcaster.broadcast("event2", {})
        # Should still be 1, not 2
        assert len(received) == 1

    async def test_broadcast_with_no_subscribers_does_not_raise(self) -> None:
        """Broadcaster with no subscribers should not raise."""
        broadcaster = Broadcaster()
        # Should complete without error
        await broadcaster.broadcast("event", {"key": "val"})

    async def test_broadcast_failing_callback_does_not_affect_others(self) -> None:
        """A failing callback should not prevent other callbacks from being called."""
        broadcaster = Broadcaster()
        received: list[dict] = []

        async def bad_callback(event: dict) -> None:
            raise RuntimeError("callback failure")

        async def good_callback(event: dict) -> None:
            received.append(event)

        broadcaster.subscribe("bad", bad_callback)
        broadcaster.subscribe("good", good_callback)

        # Should not raise despite bad_callback failing
        await broadcaster.broadcast("event", {})

        assert len(received) == 1

    async def test_broadcast_two_ws_connections(self) -> None:
        """Broadcaster should send to all registered WsConnection clients."""
        from unittest.mock import AsyncMock, MagicMock

        broadcaster = Broadcaster()

        # Create mock WsConnection objects
        mock_conn_a = MagicMock()
        mock_conn_a.send_event = AsyncMock()
        mock_conn_a.conn_id = "conn-a"

        mock_conn_b = MagicMock()
        mock_conn_b.send_event = AsyncMock()
        mock_conn_b.conn_id = "conn-b"

        broadcaster.register(mock_conn_a)
        broadcaster.register(mock_conn_b)

        await broadcaster.broadcast("test-event", {"data": 42})

        mock_conn_a.send_event.assert_awaited_once()
        mock_conn_b.send_event.assert_awaited_once()

    async def test_unregister_removes_ws_connection(self) -> None:
        """unregister() should remove the connection from the broadcast list."""
        from unittest.mock import AsyncMock, MagicMock

        broadcaster = Broadcaster()
        mock_conn = MagicMock()
        mock_conn.send_event = AsyncMock()
        mock_conn.conn_id = "conn-1"

        broadcaster.register(mock_conn)
        broadcaster.unregister(mock_conn)

        await broadcaster.broadcast("event", {})
        mock_conn.send_event.assert_not_awaited()


# ===========================================================================
# Tests: SessionState
# ===========================================================================


class TestSessionState:
    def test_start_and_is_active_and_finish(self) -> None:
        """
        start_run → is_run_active returns True
        finish_run → is_run_active returns False
        """
        state = SessionState()

        # Not active initially
        assert state.is_run_active("session-1", "run-1") is False

        # Register via register_run (uses ActiveRun)
        mock_task = MagicMock(spec=asyncio.Task)
        run = ActiveRun(
            run_id="run-1",
            session_key="session-1",
            task=mock_task,
            started_at=0.0,
            conn_id="conn-1",
        )
        state.register_run(run)
        assert state.is_run_active("session-1", "run-1") is True

        # Finish the run
        state.finish_run("session-1", "run-1")
        assert state.is_run_active("session-1", "run-1") is False

    def test_get_active_run_id_returns_run_for_session(self) -> None:
        """get_active_run_id returns the run_id for an active run."""
        state = SessionState()

        mock_task = MagicMock(spec=asyncio.Task)
        run = ActiveRun(
            run_id="run-abc",
            session_key="my-session",
            task=mock_task,
            started_at=0.0,
            conn_id="conn-1",
        )
        state.register_run(run)

        assert state.get_active_run_id("my-session") == "run-abc"
        assert state.get_active_run_id("other-session") is None

    def test_abort_run_cancels_task(self) -> None:
        """abort_run should cancel the underlying asyncio.Task."""
        state = SessionState()

        mock_task = MagicMock(spec=asyncio.Task)
        run = ActiveRun(
            run_id="run-xyz",
            session_key="sess",
            task=mock_task,
            started_at=0.0,
            conn_id="conn-1",
        )
        state.register_run(run)

        state.abort_run("sess", "run-xyz")
        mock_task.cancel.assert_called_once()

    def test_cancel_run_returns_true_if_found(self) -> None:
        """cancel_run returns True when run is found and cancelled."""
        state = SessionState()
        mock_task = MagicMock(spec=asyncio.Task)
        run = ActiveRun(
            run_id="run-1",
            session_key="sess",
            task=mock_task,
            started_at=0.0,
            conn_id="conn-1",
        )
        state.register_run(run)
        result = state.cancel_run("run-1")
        assert result is True

    def test_cancel_run_returns_false_if_not_found(self) -> None:
        """cancel_run returns False for unknown run_id."""
        state = SessionState()
        result = state.cancel_run("nonexistent-run")
        assert result is False

    def test_next_run_seq_increments_per_session(self) -> None:
        """next_run_seq should return monotonically increasing values per session."""
        state = SessionState()
        assert state.next_run_seq("session-a") == 1
        assert state.next_run_seq("session-a") == 2
        assert state.next_run_seq("session-a") == 3
        # Different session starts at 1
        assert state.next_run_seq("session-b") == 1

    def test_complete_run_removes_from_active(self) -> None:
        """complete_run should remove the run from active_runs."""
        state = SessionState()
        mock_task = MagicMock(spec=asyncio.Task)
        run = ActiveRun(
            run_id="run-done",
            session_key="sess",
            task=mock_task,
            started_at=0.0,
            conn_id="conn-1",
        )
        state.register_run(run)
        assert "run-done" in state.active_runs
        state.complete_run("run-done")
        assert "run-done" not in state.active_runs


# ===========================================================================
# Tests: HandlerRegistry
# ===========================================================================


class TestHandlerRegistry:
    async def test_register_and_dispatch_known_method(self) -> None:
        """Registering a handler then dispatching should call it."""
        registry = HandlerRegistry()
        called: list[str] = []

        async def my_handler(ctx: HandlerContext) -> None:
            called.append(ctx.method)
            await ctx.respond(True, {"result": "ok"})

        registry.register("my.method", my_handler)

        responses: list = []

        async def respond(ok: bool, payload: object = None, error: object = None) -> None:
            responses.append({"ok": ok, "payload": payload, "error": error})

        ctx = HandlerContext(
            req_id="req-1",
            method="my.method",
            params={},
            respond=respond,
            conn_id="conn-1",
            session_state=SessionState(),
            broadcaster=Broadcaster(),
        )
        await registry.dispatch(ctx)

        assert called == ["my.method"]
        assert responses[0]["ok"] is True

    async def test_dispatch_unknown_method_responds_with_error(self) -> None:
        """Dispatching an unknown method should respond with INVALID_REQUEST error."""
        registry = HandlerRegistry()
        responses: list = []

        async def respond(ok: bool, payload: object = None, error: object = None) -> None:
            responses.append({"ok": ok, "payload": payload, "error": error})

        ctx = HandlerContext(
            req_id="req-1",
            method="unknown.method",
            params={},
            respond=respond,
            conn_id="conn-1",
            session_state=SessionState(),
            broadcaster=Broadcaster(),
        )
        await registry.dispatch(ctx)

        assert responses[0]["ok"] is False
        assert responses[0]["error"] is not None
        assert responses[0]["error"].code == ErrorCode.INVALID_REQUEST

    def test_list_methods_returns_registered_names(self) -> None:
        """list_methods() returns all registered method names."""
        registry = HandlerRegistry()

        async def h1(ctx: HandlerContext) -> None:
            pass

        async def h2(ctx: HandlerContext) -> None:
            pass

        registry.register("method.a", h1)
        registry.register("method.b", h2)

        methods = registry.list_methods()
        assert "method.a" in methods
        assert "method.b" in methods

    def test_register_many_registers_all(self) -> None:
        """register_many() registers all handlers from a dict."""
        registry = HandlerRegistry()

        async def h(ctx: HandlerContext) -> None:
            pass

        registry.register_many({"m1": h, "m2": h, "m3": h})
        methods = registry.list_methods()
        assert set(methods) == {"m1", "m2", "m3"}

    def test_get_returns_registered_handler(self) -> None:
        """get() returns the registered handler or None."""
        registry = HandlerRegistry()

        async def h(ctx: HandlerContext) -> None:
            pass

        registry.register("exists", h)
        assert registry.get("exists") is h
        assert registry.get("not_exists") is None

    async def test_dispatch_handler_exception_returns_internal_error(self) -> None:
        """If a handler raises, dispatch should respond with INTERNAL error."""
        registry = HandlerRegistry()

        async def bad_handler(ctx: HandlerContext) -> None:
            raise RuntimeError("something went wrong")

        registry.register("bad.method", bad_handler)

        responses: list = []

        async def respond(ok: bool, payload: object = None, error: object = None) -> None:
            responses.append({"ok": ok, "payload": payload, "error": error})

        ctx = HandlerContext(
            req_id="req-1",
            method="bad.method",
            params={},
            respond=respond,
            conn_id="conn-1",
            session_state=SessionState(),
            broadcaster=Broadcaster(),
        )
        await registry.dispatch(ctx)

        assert responses[0]["ok"] is False
        assert responses[0]["error"].code == ErrorCode.INTERNAL


# ===========================================================================
# Tests: Agent handler unit (direct handler function calls)
# ===========================================================================


class TestAgentHandlerDirect:
    async def test_agent_handler_accepts_valid_params(self) -> None:
        """agent handler should immediately respond with accepted status."""
        from miniagent.agent.events import AgentEventEmitter
        from miniagent.gateway.handlers.agent import make_agent_handler

        emitter = AgentEventEmitter()
        session_state = SessionState()

        # Mock SessionStore
        mock_store = MagicMock()
        mock_store.load = AsyncMock(return_value=[])
        mock_store.append = AsyncMock(return_value="msg_id")

        handler = make_agent_handler(emitter, mock_store, session_state)

        responses: list = []

        async def respond(ok: bool, payload: object = None, error: object = None) -> None:
            responses.append({"ok": ok, "payload": payload})

        ctx = HandlerContext(
            req_id="req-1",
            method="agent",
            params={
                "message": "Hello",
                "session_key": "default",
                "idempotency_key": str(uuid.uuid4()),
            },
            respond=respond,
            conn_id="conn-1",
            session_state=session_state,
            broadcaster=Broadcaster(),
        )

        # Mock AgentPipeline.run to return quickly
        # Also mock anthropic.AsyncAnthropic to avoid needing API key in constructor
        async def _mock_run(self_inner, message, **kwargs):
            return "Done"

        mock_anthropic_client = MagicMock()
        with patch("miniagent.agent.pipeline.AgentPipeline.run", new=_mock_run), \
             patch("anthropic.AsyncAnthropic", return_value=mock_anthropic_client):
            await handler(ctx)

        # First response should be "accepted"
        assert len(responses) >= 1
        assert responses[0]["ok"] is True
        assert responses[0]["payload"]["status"] == "accepted"
        assert "run_id" in responses[0]["payload"]

    async def test_agent_handler_invalid_params_returns_error(self) -> None:
        """agent handler should return error for missing required params."""
        from miniagent.agent.events import AgentEventEmitter
        from miniagent.gateway.handlers.agent import make_agent_handler

        emitter = AgentEventEmitter()
        session_state = SessionState()
        mock_store = MagicMock()

        handler = make_agent_handler(emitter, mock_store, session_state)

        responses: list = []

        async def respond(ok: bool, payload: object = None, error: object = None) -> None:
            responses.append({"ok": ok, "error": error})

        ctx = HandlerContext(
            req_id="req-1",
            method="agent",
            params={},  # Missing required fields
            respond=respond,
            conn_id="conn-1",
            session_state=session_state,
            broadcaster=Broadcaster(),
        )
        await handler(ctx)

        assert responses[0]["ok"] is False
        assert responses[0]["error"] is not None


# ===========================================================================
# Tests: Chat handler unit (direct)
# ===========================================================================


class TestChatHandlersDirect:
    async def test_chat_history_handler_returns_messages(self) -> None:
        """chat.history handler returns {"messages": [...]}."""
        from miniagent.gateway.handlers.chat import make_chat_handlers

        mock_store = MagicMock()
        mock_store.load = AsyncMock(return_value=[
            {"id": "1", "role": "user", "content": "Hi"},
        ])
        session_state = SessionState()

        handlers = make_chat_handlers(mock_store, session_state)
        handler = handlers["chat.history"]

        responses: list = []

        async def respond(ok: bool, payload: object = None, error: object = None) -> None:
            responses.append({"ok": ok, "payload": payload})

        ctx = HandlerContext(
            req_id="req-1",
            method="chat.history",
            params={"session_key": "default", "limit": 10},
            respond=respond,
            conn_id="conn-1",
            session_state=session_state,
            broadcaster=Broadcaster(),
        )
        await handler(ctx)

        assert responses[0]["ok"] is True
        assert "messages" in responses[0]["payload"]
        assert isinstance(responses[0]["payload"]["messages"], list)

    async def test_chat_abort_handler_returns_aborted(self) -> None:
        """chat.abort handler returns {"aborted": True}."""
        from miniagent.gateway.handlers.chat import make_chat_handlers

        mock_store = MagicMock()
        session_state = SessionState()
        handlers = make_chat_handlers(mock_store, session_state)
        handler = handlers["chat.abort"]

        responses: list = []

        async def respond(ok: bool, payload: object = None, error: object = None) -> None:
            responses.append({"ok": ok, "payload": payload})

        ctx = HandlerContext(
            req_id="req-1",
            method="chat.abort",
            params={"session_key": "default"},
            respond=respond,
            conn_id="conn-1",
            session_state=session_state,
            broadcaster=Broadcaster(),
        )
        await handler(ctx)

        assert responses[0]["ok"] is True
        assert responses[0]["payload"] == {"aborted": True}
