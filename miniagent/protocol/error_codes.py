from enum import StrEnum
from typing import Any, Optional

from .frames import ErrorShape


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal_error"
    ABORTED = "aborted"


def error_shape(
    code: ErrorCode,
    message: str,
    details: Optional[Any] = None,
    retryable: bool = False,
) -> ErrorShape:
    """Factory ensuring all errors have consistent shape."""
    return ErrorShape(code=code, message=message, details=details, retryable=retryable)
