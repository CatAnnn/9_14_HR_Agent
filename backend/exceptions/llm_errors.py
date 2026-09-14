from __future__ import annotations


_RACEABLE_STRUCTURED_OUTPUT_CODES = frozenset(
    {
        "business_validation",
        "conflicting_content_objects",
        "conflicting_tool_arguments",
        "conflicting_tool_calls",
        "conflicting_wrapped_objects",
        "malformed_json",
        "missing_output",
        "schema_validation",
        "timeout",
        "truncated_stream",
    }
)
_RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 425, 429})


class LLMError(Exception):
    """Raised when model invocation or structured output validation fails."""


class ModelInvocationError(LLMError):
    """Raised for a classified model transport or gateway failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ):
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        super().__init__(f"model_invocation.{code}: {message}")


class StructuredOutputError(LLMError):
    """Raised when a single-call structured response cannot be validated safely."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"structured_output.{code}: {message}")


def is_retryable_http_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_HTTP_STATUS_CODES or status_code >= 500


def is_raceable_model_error(error: BaseException) -> bool:
    if isinstance(error, StructuredOutputError):
        return error.code in _RACEABLE_STRUCTURED_OUTPUT_CODES
    if isinstance(error, ModelInvocationError):
        return error.retryable
    return False


def model_error_code(error: BaseException) -> str:
    if isinstance(error, (ModelInvocationError, StructuredOutputError)):
        return error.code
    return type(error).__name__
