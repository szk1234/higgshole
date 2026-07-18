import pytest

from higgshole.orclient.client import OpenRouterClient

BASE_URL = "https://openrouter.ai/api/v1"


@pytest.fixture
async def client():
    """A client pointed at the real base URL, with all traffic intercepted."""
    async with OpenRouterClient(api_key="sk-or-v1-test", base_url=BASE_URL) as c:
        yield c


@pytest.fixture(autouse=True)
def _forbid_real_network(request, monkeypatch):
    """Fail any test that attempts a real network connection.

    Tests must intercept HTTP with respx. A real request would be slow,
    flaky, and — against a generation API — billable.
    """
    if request.node.get_closest_marker("live"):
        return

    import socket

    def _blocked(*args, **kwargs):
        raise RuntimeError(
            "This test attempted a real network connection. Use respx to "
            "intercept it, or mark the test with @pytest.mark.live."
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


@pytest.fixture
def db():
    """A migrated in-memory database, closed after the test."""
    from higgshole.store.db import Database

    database = Database.in_memory()
    database.migrate()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def media_paths(tmp_path):
    """A media root under tmp_path, with the default project tree created."""
    from higgshole.store.paths import MediaPaths

    paths = MediaPaths(tmp_path / "media")
    paths.ensure_project_tree("unsorted")
    return paths
