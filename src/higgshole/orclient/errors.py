"""Typed errors mapped from OpenRouter HTTP responses.

Callers branch on type rather than status code, so the mapping lives here
once. Spec section 10 defines the operator-facing behaviour for each.
"""

from __future__ import annotations

# Substrings that identify a content-policy refusal rather than a malformed
# request. Both surface as HTTP 400, but they mean very different things to
# the operator, so they get different types.
_MODERATION_MARKERS = ("content policy", "moderation", "safety")


class OpenRouterError(Exception):
    """Base type for every provider-originated failure."""

    #: Whether a request that raised this may still have been billed.
    may_have_charged: bool = False

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class InvalidRequestError(OpenRouterError):
    """The request was rejected as malformed or unsupported (HTTP 400)."""


class ModerationError(OpenRouterError):
    """The provider refused on content-policy grounds."""


class AuthError(OpenRouterError):
    """The API key is missing, malformed, or unrecognised (HTTP 401)."""


class InsufficientCreditsError(OpenRouterError):
    """The key's credit limit is exhausted (HTTP 402).

    This is the provider-enforced spend guard described in spec section 3.2,
    and must be surfaced distinctly from the local daily cap.
    """


class RateLimitError(OpenRouterError):
    """Too many requests (HTTP 429). Retryable with backoff."""


class ProviderError(OpenRouterError):
    """An upstream failure (HTTP 5xx)."""


class IndeterminateError(OpenRouterError):
    """A request failed after being sent, so its billing state is unknown.

    Never retried automatically: image generation is synchronous and
    non-idempotent, so a retry risks a second charge (spec section 4.4).
    """

    may_have_charged = True


def _message_of(body: dict | None) -> str:
    if not body:
        return "no response body"
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message", "unknown error"))
    if isinstance(error, str):
        return error
    return "unknown error"


def error_from_response(status_code: int, body: dict | None) -> OpenRouterError:
    """Build the appropriate error for an HTTP response."""
    message = _message_of(body)

    if status_code == 400:
        lowered = message.lower()
        if any(marker in lowered for marker in _MODERATION_MARKERS):
            return ModerationError(message, status_code=status_code)
        return InvalidRequestError(message, status_code=status_code)
    if status_code == 401:
        return AuthError(message, status_code=status_code)
    if status_code == 402:
        return InsufficientCreditsError(message, status_code=status_code)
    if status_code == 429:
        return RateLimitError(message, status_code=status_code)
    if status_code >= 500:
        return ProviderError(message, status_code=status_code)
    return OpenRouterError(message, status_code=status_code)
