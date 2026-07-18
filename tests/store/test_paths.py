from datetime import UTC, datetime
from pathlib import Path

import pytest

from higgshole.config import Settings
from higgshole.store.paths import (
    DEFAULT_PROJECT_SLUG,
    ID_LENGTH,
    MediaPaths,
    PathTraversalError,
    is_valid_id,
    media_filename,
    new_id,
    project_slug,
    slugify,
    timestamp_prefix,
)

WHEN = datetime(2026, 7, 18, 14, 30, 22, tzinfo=UTC)


@pytest.fixture
def paths(tmp_path):
    return MediaPaths(tmp_path / "media")


def test_new_id_is_twelve_lowercase_hex():
    value = new_id()

    assert len(value) == ID_LENGTH == 12
    assert all(c in "0123456789abcdef" for c in value)


def test_new_ids_are_distinct():
    assert len({new_id() for _ in range(500)}) == 500


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("a3f21c9d4e07", True),
        ("A3F21C9D4E07", False),  # uppercase is not the canonical form
        ("a3f21c9d4e0", False),  # eleven characters
        ("a3f21c9d4e077", False),  # thirteen characters
        ("../../../etc", False),  # a crafted path segment
        ("", False),
    ],
)
def test_is_valid_id(candidate, expected):
    assert is_valid_id(candidate) is expected


def test_slugify_normalises_and_collapses():
    assert slugify("Neon City Street  at Night, Rain!") == "neon-city-street-at-night-rain"


def test_slugify_strips_diacritics_via_nfkd():
    assert slugify("café über straße") == "cafe-uber-strae"


def test_slugify_truncates_to_sixty_characters():
    result = slugify("word " * 60)

    assert len(result) <= 60
    assert not result.endswith("-")


def test_slugify_returns_empty_when_nothing_survives():
    assert slugify("!!! ??? ***") == ""


def test_project_slug_falls_back_to_unsorted():
    assert project_slug("!!!") == DEFAULT_PROJECT_SLUG
    assert project_slug("My Project") == "my-project"


def test_timestamp_prefix_formats_utc():
    assert timestamp_prefix(WHEN) == "20260718-143022"


def test_media_filename_includes_slug():
    name = media_filename(
        timestamp="20260718-143022", gen_id="a3f21c9d4e07", slug="neon-city", ext="png"
    )

    assert name == "20260718-143022_a3f21c9d4e07_neon-city.png"


def test_media_filename_omits_empty_slug():
    name = media_filename(
        timestamp="20260718-143022", gen_id="a3f21c9d4e07", slug="", ext="png"
    )

    assert name == "20260718-143022_a3f21c9d4e07.png"


def test_ensure_project_tree_creates_all_directories(paths):
    project = paths.ensure_project_tree("demo")

    assert (project / "images").is_dir()
    assert (project / "videos").is_dir()
    assert (project / "uploads").is_dir()
    assert paths.thumbs_dir("demo").is_dir()
    # Idempotent.
    assert paths.ensure_project_tree("demo") == project


@pytest.mark.parametrize(
    ("kind", "folder", "ext"),
    [("image", "images", "png"), ("video", "videos", "mp4")],
)
def test_allocate_output_places_images_and_videos_correctly(paths, kind, folder, ext):
    allocated = paths.allocate_output(
        project_slug="demo",
        kind=kind,
        gen_id="a3f21c9d4e07",
        prompt="Neon City Street",
        ext=ext,
        when=WHEN,
    )

    assert allocated.media_path.parent == paths.root / "projects" / "demo" / folder
    assert allocated.media_path.name == f"20260718-143022_a3f21c9d4e07_neon-city-street.{ext}"
    assert allocated.relative_media_path == Path(
        f"projects/demo/{folder}/20260718-143022_a3f21c9d4e07_neon-city-street.{ext}"
    )


def test_allocate_output_returns_sidecar_and_part_paths(paths):
    allocated = paths.allocate_output(
        project_slug="demo",
        kind="image",
        gen_id="a3f21c9d4e07",
        prompt="cat",
        ext="png",
        when=WHEN,
    )

    assert allocated.sidecar_path == allocated.media_path.with_suffix(".json")
    assert allocated.part_path.name == allocated.media_path.name + ".part"
    assert allocated.media_path.parent.is_dir()


def test_allocate_upload_lands_in_uploads(paths):
    allocated = paths.allocate_upload(
        project_slug="demo",
        asset_id="0c118b4e77aa",
        original_name="My Reference Photo.PNG",
        ext="png",
        when=WHEN,
    )

    assert allocated.media_path.parent == paths.uploads_dir("demo")
    assert allocated.media_path.name == (
        "20260718-143022_0c118b4e77aa_my-reference-photo-png.png"
    )


def test_thumb_and_poster_paths_are_sharded_by_project(paths):
    thumb = paths.thumb_path(project_slug="demo", gen_id="a3f21c9d4e07")
    poster = paths.poster_path(project_slug="demo", gen_id="a3f21c9d4e07")

    assert thumb == paths.root / "thumbs" / "demo" / "a3f21c9d4e07.webp"
    assert poster == paths.root / "thumbs" / "demo" / "a3f21c9d4e07_poster.webp"


def test_resolve_within_root_accepts_a_contained_path(paths):
    paths.ensure_project_tree("demo")
    target = paths.images_dir("demo") / "x.png"
    target.write_bytes(b"x")

    assert paths.resolve_within_root("projects/demo/images/x.png") == target.resolve()


@pytest.mark.parametrize(
    "attempt",
    [
        "../../etc/passwd",
        "projects/demo/../../../../etc/passwd",
        "/etc/passwd",
    ],
)
def test_resolve_within_root_rejects_traversal(paths, attempt):
    with pytest.raises(PathTraversalError):
        paths.resolve_within_root(attempt)


def test_is_within_root_rejects_a_sibling_directory(tmp_path):
    paths = MediaPaths(tmp_path / "media")

    assert paths.is_within_root(tmp_path / "media" / "a" / "b") is True
    assert paths.is_within_root(tmp_path / "media-elsewhere" / "b") is False


def test_from_settings_uses_the_media_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HIGGSHOLE_MEDIA_ROOT", str(tmp_path / "custom"))

    assert MediaPaths.from_settings(Settings()).root == (tmp_path / "custom").resolve()
