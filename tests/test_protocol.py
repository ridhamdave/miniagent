"""
Round-trip Pydantic parse/serialize tests for the protocol module.

No I/O, no mocking — pure Pydantic validation.
"""

import pytest
from pydantic import ValidationError

from miniagent.protocol import (
    AgentParams,
    BrowserClickParams,
    BrowserGetTextParams,
    BrowserNavigateParams,
    BrowserTypeParams,
    ChatAbortParams,
    ChatHistoryParams,
    ErrorCode,
    ErrorShape,
    EventFrame,
    HelloFeatures,
    HelloOk,
    RequestFrame,
    ResponseFrame,
    error_shape,
)


# ---------------------------------------------------------------------------
# ErrorShape
# ---------------------------------------------------------------------------


class TestErrorShape:
    def test_minimal_round_trip(self) -> None:
        data = {"code": "invalid_request", "message": "Bad params"}
        obj = ErrorShape.model_validate(data)
        assert obj.code == "invalid_request"
        assert obj.message == "Bad params"
        assert obj.details is None
        assert obj.retryable is None
        dumped = obj.model_dump()
        assert dumped["code"] == "invalid_request"
        assert dumped["message"] == "Bad params"

    def test_full_round_trip(self) -> None:
        data = {
            "code": "internal_error",
            "message": "Something broke",
            "details": {"traceback": "line 42"},
            "retryable": True,
        }
        obj = ErrorShape.model_validate(data)
        assert obj.retryable is True
        assert obj.details == {"traceback": "line 42"}
        dumped = obj.model_dump()
        assert dumped["retryable"] is True

    def test_missing_required_fields_raise(self) -> None:
        with pytest.raises(ValidationError):
            ErrorShape.model_validate({"code": "invalid_request"})  # missing message

        with pytest.raises(ValidationError):
            ErrorShape.model_validate({"message": "oops"})  # missing code


# ---------------------------------------------------------------------------
# RequestFrame
# ---------------------------------------------------------------------------


class TestRequestFrame:
    def test_minimal_round_trip(self) -> None:
        data = {"type": "req", "id": "abc-123", "method": "agent"}
        obj = RequestFrame.model_validate(data)
        assert obj.type == "req"
        assert obj.id == "abc-123"
        assert obj.method == "agent"
        assert obj.params is None
        dumped = obj.model_dump()
        assert dumped["type"] == "req"

    def test_with_params(self) -> None:
        data = {
            "type": "req",
            "id": "req-1",
            "method": "agent",
            "params": {"message": "hello", "idempotency_key": "idem-1"},
        }
        obj = RequestFrame.model_validate(data)
        assert obj.params == {"message": "hello", "idempotency_key": "idem-1"}

    def test_wrong_type_literal_raises(self) -> None:
        with pytest.raises(ValidationError):
            RequestFrame.model_validate(
                {"type": "res", "id": "x", "method": "agent"}
            )

    def test_missing_required_fields_raise(self) -> None:
        with pytest.raises(ValidationError):
            RequestFrame.model_validate({"type": "req", "method": "agent"})  # no id

        with pytest.raises(ValidationError):
            RequestFrame.model_validate({"type": "req", "id": "x"})  # no method


# ---------------------------------------------------------------------------
# ResponseFrame
# ---------------------------------------------------------------------------


class TestResponseFrame:
    def test_ok_response_round_trip(self) -> None:
        data = {
            "type": "res",
            "id": "req-1",
            "ok": True,
            "payload": {"run_id": "run-abc", "status": "accepted"},
        }
        obj = ResponseFrame.model_validate(data)
        assert obj.type == "res"
        assert obj.ok is True
        assert obj.payload == {"run_id": "run-abc", "status": "accepted"}
        assert obj.error is None
        dumped = obj.model_dump()
        assert dumped["ok"] is True

    def test_error_response_round_trip(self) -> None:
        data = {
            "type": "res",
            "id": "req-2",
            "ok": False,
            "error": {"code": "invalid_request", "message": "bad params"},
        }
        obj = ResponseFrame.model_validate(data)
        assert obj.ok is False
        assert isinstance(obj.error, ErrorShape)
        assert obj.error.code == "invalid_request"

    def test_wrong_type_literal_raises(self) -> None:
        with pytest.raises(ValidationError):
            ResponseFrame.model_validate(
                {"type": "req", "id": "x", "ok": True}
            )

    def test_missing_required_fields_raise(self) -> None:
        with pytest.raises(ValidationError):
            ResponseFrame.model_validate({"type": "res", "ok": True})  # no id

        with pytest.raises(ValidationError):
            ResponseFrame.model_validate({"type": "res", "id": "x"})  # no ok


# ---------------------------------------------------------------------------
# EventFrame
# ---------------------------------------------------------------------------


class TestEventFrame:
    def test_minimal_round_trip(self) -> None:
        data = {"type": "event", "event": "tick"}
        obj = EventFrame.model_validate(data)
        assert obj.type == "event"
        assert obj.event == "tick"
        assert obj.payload is None
        assert obj.seq is None

    def test_with_seq_and_payload(self) -> None:
        data = {
            "type": "event",
            "event": "agent-event",
            "payload": {"run_id": "run-1", "delta": "hello"},
            "seq": 42,
        }
        obj = EventFrame.model_validate(data)
        assert obj.seq == 42
        assert obj.payload["run_id"] == "run-1"
        dumped = obj.model_dump()
        assert dumped["seq"] == 42

    def test_connect_challenge_event(self) -> None:
        data = {
            "type": "event",
            "event": "connect.challenge",
            "payload": {"nonce": "abc", "ts": 1234567890},
            "seq": 1,
        }
        obj = EventFrame.model_validate(data)
        assert obj.event == "connect.challenge"

    def test_wrong_type_literal_raises(self) -> None:
        with pytest.raises(ValidationError):
            EventFrame.model_validate({"type": "req", "event": "tick"})

    def test_missing_event_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            EventFrame.model_validate({"type": "event"})  # missing event


# ---------------------------------------------------------------------------
# HelloFeatures + HelloOk
# ---------------------------------------------------------------------------


class TestHelloOk:
    def test_round_trip(self) -> None:
        data = {
            "type": "hello-ok",
            "server_version": "0.1.0",
            "conn_id": "conn-abc",
            "features": {
                "methods": ["connect", "agent", "chat.history"],
                "events": ["agent-event", "tick", "connect.challenge"],
            },
        }
        obj = HelloOk.model_validate(data)
        assert obj.type == "hello-ok"
        assert obj.server_version == "0.1.0"
        assert obj.conn_id == "conn-abc"
        assert isinstance(obj.features, HelloFeatures)
        assert "agent" in obj.features.methods
        dumped = obj.model_dump()
        assert dumped["features"]["methods"] == ["connect", "agent", "chat.history"]

    def test_wrong_type_literal_raises(self) -> None:
        with pytest.raises(ValidationError):
            HelloOk.model_validate(
                {
                    "type": "bad-type",
                    "server_version": "0.1.0",
                    "conn_id": "x",
                    "features": {"methods": [], "events": []},
                }
            )

    def test_missing_fields_raise(self) -> None:
        with pytest.raises(ValidationError):
            HelloOk.model_validate(
                {
                    "type": "hello-ok",
                    "server_version": "0.1.0",
                    # missing conn_id and features
                }
            )


# ---------------------------------------------------------------------------
# ErrorCode enum + error_shape factory
# ---------------------------------------------------------------------------


class TestErrorCode:
    def test_string_values(self) -> None:
        assert ErrorCode.INVALID_REQUEST == "invalid_request"
        assert ErrorCode.UNAVAILABLE == "unavailable"
        assert ErrorCode.INTERNAL == "internal_error"
        assert ErrorCode.ABORTED == "aborted"

    def test_is_str(self) -> None:
        # StrEnum members must behave as plain strings
        assert isinstance(ErrorCode.INTERNAL, str)


class TestErrorShapeFactory:
    def test_basic(self) -> None:
        shape = error_shape(ErrorCode.INVALID_REQUEST, "bad request")
        assert isinstance(shape, ErrorShape)
        assert shape.code == "invalid_request"
        assert shape.message == "bad request"
        assert shape.retryable is False

    def test_with_details_and_retryable(self) -> None:
        shape = error_shape(
            ErrorCode.UNAVAILABLE,
            "service down",
            details={"service": "claude"},
            retryable=True,
        )
        assert shape.code == "unavailable"
        assert shape.retryable is True
        assert shape.details == {"service": "claude"}

    def test_aborted(self) -> None:
        shape = error_shape(ErrorCode.ABORTED, "run cancelled")
        assert shape.code == "aborted"

    def test_internal(self) -> None:
        shape = error_shape(ErrorCode.INTERNAL, "unexpected error")
        assert shape.code == "internal_error"


# ---------------------------------------------------------------------------
# AgentParams
# ---------------------------------------------------------------------------


class TestAgentParams:
    def test_minimal_round_trip(self) -> None:
        data = {"message": "Hello Claude", "idempotency_key": "idem-uuid-1"}
        obj = AgentParams.model_validate(data)
        assert obj.message == "Hello Claude"
        assert obj.idempotency_key == "idem-uuid-1"
        assert obj.session_key == "default"  # default
        assert obj.thinking is None

    def test_full_round_trip(self) -> None:
        data = {
            "message": "Do research",
            "session_key": "my-session",
            "idempotency_key": "idem-2",
            "thinking": "high",
        }
        obj = AgentParams.model_validate(data)
        assert obj.session_key == "my-session"
        assert obj.thinking == "high"
        dumped = obj.model_dump()
        assert dumped["thinking"] == "high"

    def test_missing_required_raises(self) -> None:
        with pytest.raises(ValidationError):
            AgentParams.model_validate({"message": "hi"})  # missing idempotency_key

        with pytest.raises(ValidationError):
            AgentParams.model_validate({"idempotency_key": "x"})  # missing message


# ---------------------------------------------------------------------------
# ChatHistoryParams
# ---------------------------------------------------------------------------


class TestChatHistoryParams:
    def test_round_trip(self) -> None:
        data = {"session_key": "sess-1", "limit": 20}
        obj = ChatHistoryParams.model_validate(data)
        assert obj.session_key == "sess-1"
        assert obj.limit == 20

    def test_default_limit(self) -> None:
        obj = ChatHistoryParams.model_validate({"session_key": "sess-1"})
        assert obj.limit == 50

    def test_missing_session_key_raises(self) -> None:
        with pytest.raises(ValidationError):
            ChatHistoryParams.model_validate({"limit": 10})


# ---------------------------------------------------------------------------
# ChatAbortParams
# ---------------------------------------------------------------------------


class TestChatAbortParams:
    def test_with_run_id(self) -> None:
        obj = ChatAbortParams.model_validate(
            {"session_key": "sess-1", "run_id": "run-abc"}
        )
        assert obj.run_id == "run-abc"

    def test_without_run_id_defaults_none(self) -> None:
        obj = ChatAbortParams.model_validate({"session_key": "sess-1"})
        assert obj.run_id is None

    def test_missing_session_key_raises(self) -> None:
        with pytest.raises(ValidationError):
            ChatAbortParams.model_validate({"run_id": "run-1"})


# ---------------------------------------------------------------------------
# Browser param models
# ---------------------------------------------------------------------------


class TestBrowserNavigateParams:
    def test_round_trip(self) -> None:
        obj = BrowserNavigateParams.model_validate({"url": "https://example.com"})
        assert obj.url == "https://example.com"
        assert obj.model_dump() == {"url": "https://example.com"}

    def test_missing_url_raises(self) -> None:
        with pytest.raises(ValidationError):
            BrowserNavigateParams.model_validate({})


class TestBrowserClickParams:
    def test_defaults(self) -> None:
        obj = BrowserClickParams.model_validate({"ref": "button[name=Submit]"})
        assert obj.ref == "button[name=Submit]"
        assert obj.double_click is False

    def test_double_click(self) -> None:
        obj = BrowserClickParams.model_validate(
            {"ref": "some-ref", "double_click": True}
        )
        assert obj.double_click is True

    def test_missing_ref_raises(self) -> None:
        with pytest.raises(ValidationError):
            BrowserClickParams.model_validate({"double_click": False})


class TestBrowserTypeParams:
    def test_round_trip(self) -> None:
        data = {"ref": "input[name=q]", "text": "search query"}
        obj = BrowserTypeParams.model_validate(data)
        assert obj.ref == "input[name=q]"
        assert obj.text == "search query"
        dumped = obj.model_dump()
        assert dumped["text"] == "search query"

    def test_missing_fields_raise(self) -> None:
        with pytest.raises(ValidationError):
            BrowserTypeParams.model_validate({"ref": "x"})  # missing text

        with pytest.raises(ValidationError):
            BrowserTypeParams.model_validate({"text": "x"})  # missing ref


class TestBrowserGetTextParams:
    def test_default_none(self) -> None:
        obj = BrowserGetTextParams.model_validate({})
        assert obj.ref is None

    def test_with_ref(self) -> None:
        obj = BrowserGetTextParams.model_validate({"ref": "article"})
        assert obj.ref == "article"
