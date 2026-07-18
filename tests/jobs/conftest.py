"""Fixtures for the job engine tests.

This file adds fixtures and nothing else. The autouse network guard lives in
tests/conftest.py and must not be shadowed here.
"""

from __future__ import annotations

import pytest

from tests.jobs.fakes import Harness, fake_metadata_for, fake_thumbnail


@pytest.fixture
def stub_media(monkeypatch):
    """Replace probing, embedding and thumbnailing at the runner's seam.

    The real implementations shell out to ffprobe/ffmpeg and decode images
    with Pillow. Neither is what these tests are about, and requiring ffmpeg
    on every developer machine to test a state machine is a poor trade.
    """
    monkeypatch.setattr("higgshole.jobs.runner.probe_media", fake_metadata_for)
    monkeypatch.setattr(
        "higgshole.jobs.runner.embed_params", lambda path, payload: None
    )
    monkeypatch.setattr("higgshole.jobs.runner.make_image_thumbnail", fake_thumbnail)
    monkeypatch.setattr("higgshole.jobs.runner.make_video_thumbnail", fake_thumbnail)
    monkeypatch.setattr("higgshole.jobs.runner.make_video_poster", fake_thumbnail)


@pytest.fixture
def harness(tmp_path, stub_media):
    built = Harness(tmp_path)
    yield built
    built.db.close()
