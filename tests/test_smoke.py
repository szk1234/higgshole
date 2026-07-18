import socket

import httpx
import pytest


def test_package_importable():
    import higgshole

    assert higgshole.__version__ == "0.1.0"


def test_the_network_guard_blocks_a_direct_socket_connection():
    with pytest.raises(RuntimeError, match="real network connection"):
        socket.create_connection(("example.com", 443))


async def test_an_unintercepted_http_request_cannot_succeed():
    """The guard reaches httpx too.

    asyncio wraps the guard's RuntimeError in an ExceptionGroup while trying
    each resolved address, so the assertion is that the request fails rather
    than that a specific type escapes.
    """
    with pytest.raises(BaseException) as caught:  # noqa: B017,PT011
        async with httpx.AsyncClient() as client:
            await client.get("https://example.com")

    assert "real network connection" in _flatten(caught.value)


def _flatten(error: BaseException) -> str:
    """Render an exception and everything nested inside it as text."""
    parts = [str(error)]
    for nested in getattr(error, "exceptions", ()):
        parts.append(_flatten(nested))
    if error.__cause__ is not None:
        parts.append(_flatten(error.__cause__))
    return " | ".join(parts)
