from pathlib import Path

import pytest

UNIT_PATH = Path(__file__).resolve().parents[2] / "deploy" / "higgshole.service.example"

MEDIA_ROOT = "/var/lib/higgshole/media"
STATE_DIR = "/var/lib/higgshole/state"


def _directives() -> dict[str, list[str]]:
    """Parse a unit file into {key: [values]}, tolerating repeated keys."""
    parsed: dict[str, list[str]] = {}
    for raw in UNIT_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, _, value = line.partition("=")
        parsed.setdefault(key.strip(), []).append(value.strip())
    return parsed


def test_the_unit_exists_and_declares_the_three_sections():
    text = UNIT_PATH.read_text(encoding="utf-8")

    for section in ("[Unit]", "[Service]", "[Install]"):
        assert section in text, section


def test_user_and_install_directory_are_placeholders():
    # Spec section 9: no machine-specific values are committed.
    directives = _directives()

    assert directives["User"] == ["@USER@"]
    assert any("@INSTALL_DIR@" in value for value in directives["ExecStart"])
    assert directives["WorkingDirectory"] == ["@INSTALL_DIR@"]


def test_the_environment_file_is_optional():
    # The leading '-' lets the service start before an operator has written
    # /etc/higgshole/higgshole.env, rather than failing at boot.
    assert _directives()["EnvironmentFile"] == ["-/etc/higgshole/higgshole.env"]


@pytest.mark.parametrize(
    ("directive", "value"),
    [
        ("NoNewPrivileges", "yes"),
        ("PrivateTmp", "yes"),
        ("ProtectSystem", "strict"),
        ("ProtectHome", "yes"),
        ("ProtectKernelTunables", "yes"),
    ],
)
def test_required_hardening_directives_are_set(directive, value):
    assert _directives()[directive] == [value]


def test_address_families_are_restricted_to_ip_and_unix():
    families = set(_directives()["RestrictAddressFamilies"][0].split())

    assert families == {"AF_INET", "AF_INET6", "AF_UNIX"}


def test_writable_paths_are_limited_to_the_media_root_and_state_directory():
    # ProtectSystem=strict makes everything read-only; these two are the only
    # places the service legitimately writes (spec sections 5.1, 5.2).
    writable = set(_directives()["ReadWritePaths"][0].split())

    assert writable == {MEDIA_ROOT, STATE_DIR}


def test_media_root_and_database_path_are_configured_to_those_paths():
    environment = " ".join(_directives()["Environment"])

    assert f"HIGGSHOLE_MEDIA_ROOT={MEDIA_ROOT}" in environment
    assert f"HIGGSHOLE_DB_PATH={STATE_DIR}/higgshole.db" in environment


def test_exactly_one_uvicorn_worker_is_requested():
    # Spec section 9: multiple workers would each reattach a poller to the
    # same job at boot and the reservation lock is process-local.
    exec_start = _directives()["ExecStart"][0]

    assert "--workers 1" in exec_start
    assert "uvicorn" in exec_start


def test_the_single_worker_requirement_is_explained_in_the_file():
    text = UNIT_PATH.read_text(encoding="utf-8").lower()

    assert "worker" in text
    assert "poller" in text or "reservation" in text


def test_the_service_restarts_and_waits_for_the_network():
    directives = _directives()

    assert directives["Restart"] == ["always"]
    assert "network-online.target" in " ".join(directives["After"])


def test_no_machine_specific_path_or_identity_is_committed():
    text = UNIT_PATH.read_text(encoding="utf-8")

    for forbidden in ("/home/", "/Users/", "~/", "sk-or-v1-"):
        assert forbidden not in text, forbidden
