from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs"
DEPLOYMENT = DOCS / "deployment.md"
MCP = DOCS / "mcp.md"


def test_deployment_covers_creating_the_service_account():
    text = DEPLOYMENT.read_text(encoding="utf-8")

    assert "useradd" in text
    assert "--system" in text or "-r " in text


def test_deployment_covers_the_media_and_state_directories():
    text = DEPLOYMENT.read_text(encoding="utf-8")

    assert "/var/lib/higgshole/media" in text
    assert "/var/lib/higgshole/state" in text
    assert "install -d" in text or "mkdir -p" in text


def test_deployment_explains_installing_the_unit():
    text = DEPLOYMENT.read_text(encoding="utf-8")

    assert "higgshole.service.example" in text
    assert "@USER@" in text and "@INSTALL_DIR@" in text
    assert "systemctl enable" in text


def test_deployment_documents_local_overrides_via_systemctl_edit():
    # Overrides belong in a drop-in, not in an edited copy of the unit, so an
    # upgrade does not silently discard them.
    text = DEPLOYMENT.read_text(encoding="utf-8")

    assert "systemctl edit higgshole" in text
    assert "override.conf" in text


def test_deployment_states_the_environment_file_holds_the_api_key():
    text = DEPLOYMENT.read_text(encoding="utf-8")

    assert "/etc/higgshole/higgshole.env" in text
    assert "HIGGSHOLE_OPENROUTER_API_KEY" in text
    assert "chmod 600" in text or "0600" in text


def test_mcp_doc_shows_a_stdio_client_registration_block():
    text = MCP.read_text(encoding="utf-8")

    assert "mcpServers" in text
    assert "higgshole-mcp" in text
    assert '"command"' in text


def test_mcp_doc_names_the_api_base_environment_variable():
    text = MCP.read_text(encoding="utf-8")

    assert "HIGGSHOLE_API_BASE" in text
    assert "http://127.0.0.1:8077" in text


def test_mcp_doc_lists_all_eleven_tools():
    from higgshole.mcp_server import TOOL_NAMES

    text = MCP.read_text(encoding="utf-8")

    for name in TOOL_NAMES:
        assert f"`{name}`" in text, name


def test_mcp_doc_states_that_video_generation_does_not_block():
    text = MCP.read_text(encoding="utf-8").lower()

    assert "generate_video" in text
    assert "does not block" in text or "not block" in text


@pytest.mark.parametrize("path", [DEPLOYMENT, MCP])
def test_no_machine_specific_path_or_key_is_committed(path):
    text = path.read_text(encoding="utf-8")

    for forbidden in ("/home/", "/Users/", "sk-or-v1-a", "sk-or-v1-0"):
        assert forbidden not in text, f"{path.name}: {forbidden}"
