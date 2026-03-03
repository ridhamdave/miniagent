from typing import Any, Literal, Optional

from pydantic import BaseModel


class ErrorShape(BaseModel):
    """
    All errors over the wire use this shape — never raw Python tracebacks.
    (OpenClaw: errorShape() in src/gateway/protocol/schema/error-codes.ts)
    """

    code: str  # e.g. "invalid_request", "internal_error", "aborted"
    message: str
    details: Optional[Any] = None
    retryable: Optional[bool] = None


class RequestFrame(BaseModel):
    """
    Client → Server.
    Direct mirror of OpenClaw's RequestFrameSchema (src/gateway/protocol/schema/frames.ts).

    The `id` field is client-generated and must be echoed back in the ResponseFrame.
    This is the correlation ID for request/response pairing — the same id can receive
    multiple ResponseFrames (see the double-response pattern for "agent").
    """

    type: Literal["req"]
    id: str  # Client-generated UUID; echoed in ResponseFrame
    method: str  # RPC method name, e.g. "agent", "chat.history"
    params: Optional[Any] = None


class ResponseFrame(BaseModel):
    """
    Server → Client (one specific client, not broadcast).
    Matches the id from the RequestFrame it answers.

    The "agent" method sends TWO ResponseFrames with the same id:
      1st: {ok:true, payload:{run_id, status:"accepted"}}  — immediate ack
      2nd: {ok:true, payload:{run_id, status:"ok"}}        — when pipeline finishes
    This double-response lets the UI show "running..." immediately while the
    full result arrives later without blocking the handler coroutine.
    (OpenClaw source: src/gateway/server-methods/agent.ts lines 599 + 654)
    """

    type: Literal["res"]
    id: str
    ok: bool
    payload: Optional[Any] = None
    error: Optional[ErrorShape] = None


class EventFrame(BaseModel):
    """
    Server → ALL connected clients (broadcast).
    NOT correlated to any specific RequestFrame id.

    The `seq` field is a global monotonic counter incremented for every broadcast.
    A client that reconnects and sees seq=50 when it last saw seq=47 knows it
    missed 3 events. (OpenClaw: server-broadcast.ts line 66)
    """

    type: Literal["event"]
    event: str  # e.g. "agent-event", "tick", "connect.challenge"
    payload: Optional[Any] = None
    seq: Optional[int] = None


class HelloFeatures(BaseModel):
    methods: list[str]  # All registered RPC method names
    events: list[str]  # All event names the server can emit


class HelloOk(BaseModel):
    """
    Server → Client, sent as the ResponseFrame payload after a valid "connect" request.
    Tells the client what the server supports — so clients can adapt gracefully
    when the server is an older version.
    (OpenClaw: HelloOkSchema in src/gateway/protocol/schema/frames.ts)
    """

    type: Literal["hello-ok"]
    server_version: str
    conn_id: str
    features: HelloFeatures
