from decimal import Decimal
from pathlib import Path

import pytest

from higgshole.config import Settings


def test_defaults_use_xdg_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for var in ("HIGGSHOLE_MEDIA_ROOT", "HIGGSHOLE_DB_PATH"):
        monkeypatch.delenv(var, raising=False)

    settings = Settings()

    assert settings.media_root == tmp_path / "data" / "higgshole" / "media"
    assert settings.db_path == tmp_path / "state" / "higgshole" / "higgshole.db"


def test_defaults_fall_back_to_home_when_xdg_unset(monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("HIGGSHOLE_MEDIA_ROOT", raising=False)

    settings = Settings()

    assert settings.media_root == Path.home() / ".local/share/higgshole/media"


def test_binds_to_loopback_by_default(monkeypatch):
    monkeypatch.delenv("HIGGSHOLE_BIND_HOST", raising=False)

    assert Settings().bind_host == "127.0.0.1"


def test_env_vars_override_defaults(monkeypatch):
    monkeypatch.setenv("HIGGSHOLE_BIND_PORT", "9000")
    monkeypatch.setenv("HIGGSHOLE_DAILY_CAP_USD", "12.50")

    settings = Settings()

    assert settings.bind_port == 9000
    assert settings.daily_cap_usd == Decimal("12.50")


def test_no_daily_cap_by_default(monkeypatch):
    monkeypatch.delenv("HIGGSHOLE_DAILY_CAP_USD", raising=False)

    assert Settings().daily_cap_usd is None


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("image", "sk-or-v1-img"), ("video", "sk-or-v1-vid")],
)
def test_per_kind_key_selection(monkeypatch, kind, expected):
    monkeypatch.setenv("HIGGSHOLE_OPENROUTER_API_KEY", "sk-or-v1-default")
    monkeypatch.setenv("HIGGSHOLE_OPENROUTER_API_KEY_IMAGE", "sk-or-v1-img")
    monkeypatch.setenv("HIGGSHOLE_OPENROUTER_API_KEY_VIDEO", "sk-or-v1-vid")

    assert Settings().openrouter_api_key_for(kind) == expected


def test_per_kind_key_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("HIGGSHOLE_OPENROUTER_API_KEY", "sk-or-v1-default")
    monkeypatch.delenv("HIGGSHOLE_OPENROUTER_API_KEY_IMAGE", raising=False)
    monkeypatch.delenv("HIGGSHOLE_OPENROUTER_API_KEY_VIDEO", raising=False)

    assert Settings().openrouter_api_key_for("image") == "sk-or-v1-default"
