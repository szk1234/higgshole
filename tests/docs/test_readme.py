from pathlib import Path

README = Path(__file__).resolve().parents[2] / "README.md"


def test_the_readme_no_longer_claims_implementation_has_not_started():
    text = README.read_text(encoding="utf-8")

    assert "implementation not yet started" not in text
    assert "design complete" not in text


def test_the_readme_states_a_running_status():
    text = README.read_text(encoding="utf-8").lower()

    assert "status:" in text
    assert "implemented" in text or "working" in text


def test_the_readme_documents_how_to_run_it():
    text = README.read_text(encoding="utf-8")

    assert "uv sync" in text
    assert "uv run pytest" in text
    assert "127.0.0.1:8077" in text


def test_the_readme_lists_every_mcp_tool():
    from higgshole.mcp_server import TOOL_NAMES

    text = README.read_text(encoding="utf-8")

    for name in TOOL_NAMES:
        assert f"`{name}`" in text, name


def test_the_readme_links_the_deployment_and_mcp_guides():
    text = README.read_text(encoding="utf-8")

    assert "docs/deployment.md" in text
    assert "docs/mcp.md" in text


def test_the_readme_commits_no_machine_specific_path_or_key():
    text = README.read_text(encoding="utf-8")

    for forbidden in ("/home/", "/Users/", "sk-or-v1-a", "sk-or-v1-0"):
        assert forbidden not in text, forbidden
