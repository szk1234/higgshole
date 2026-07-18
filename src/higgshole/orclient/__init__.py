"""OpenRouter API client.

Performs no filesystem or database access, so the whole provider integration
is testable against recorded fixtures with no network and no spend.
"""

from .client import DEFAULT_BASE_URL, OpenRouterClient, looks_like_openrouter_key
from .errors import (
    AuthError,
    IndeterminateError,
    InsufficientCreditsError,
    InvalidRequestError,
    ModerationError,
    OpenRouterError,
    ProviderError,
    RateLimitError,
    error_from_response,
)
from .types import (
    TERMINAL_STATUSES,
    ImageModel,
    ImageResult,
    KeyStatus,
    VideoJob,
    VideoModel,
    is_terminal,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "TERMINAL_STATUSES",
    "AuthError",
    "ImageModel",
    "ImageResult",
    "IndeterminateError",
    "InsufficientCreditsError",
    "InvalidRequestError",
    "KeyStatus",
    "ModerationError",
    "OpenRouterClient",
    "OpenRouterError",
    "ProviderError",
    "RateLimitError",
    "VideoJob",
    "VideoModel",
    "error_from_response",
    "is_terminal",
    "looks_like_openrouter_key",
]
