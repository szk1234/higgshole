"""Media byte serving.

This is a standalone Starlette application rather than a set of routes on the
main app. `web/app.py` dispatches to it BEFORE the parent's middleware stack
runs, which is the structural guarantee behind spec section 6.3: no middleware
anyone adds later can compress or re-length a 206 response.

`FileResponse` implements HTTP Range natively — 206 with Content-Range, suffix
ranges and 416 all come for free, so there is no custom byte-slicing code here
to get wrong.
"""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse
from starlette.routing import Route

from higgshole.store.db import Database
from higgshole.store.metadata import UnsupportedMediaError, mime_for
from higgshole.store.paths import MediaPaths, PathTraversalError

MEDIA_MOUNT_PATH: str = "/media"
THUMBS_MOUNT_PATH: str = "/thumbs"


def media_url_for(relative_path: str) -> str:
    """Turn a media-root-relative path into its HTTP URL.

    The single place media URLs are built, so API responses and templates
    cannot drift apart.
    """
    return f"{MEDIA_MOUNT_PATH}/{str(relative_path).lstrip('/')}"


def thumb_url_for(*, project_slug: str, gen_id: str) -> str:
    return f"{THUMBS_MOUNT_PATH}/{project_slug}/{gen_id}.webp"


def poster_url_for(*, project_slug: str, gen_id: str) -> str:
    return f"{THUMBS_MOUNT_PATH}/{project_slug}/{gen_id}_poster.webp"


def _file_response(paths: MediaPaths, relative: str | Path) -> FileResponse:
    """Resolve, contain, and serve one file.

    Containment failure is reported as 404, not 403: a 403 tells a caller that
    the crafted target exists, which is information the caller should not get.
    """
    try:
        target = paths.resolve_within_root(relative)
    except PathTraversalError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc

    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")

    try:
        media_type: str | None = mime_for(target)
    except UnsupportedMediaError:
        media_type = None

    return FileResponse(target, media_type=media_type)


async def serve_media(request: Request) -> FileResponse:
    """GET /media/{path:path} — any file beneath the media root."""
    paths: MediaPaths = request.app.state.paths
    return _file_response(paths, request.path_params["path"])


async def serve_thumb(request: Request) -> FileResponse:
    """GET /thumbs/{project_slug}/{filename}

    The project slug and filename are re-joined and then contained, so a
    crafted filename is caught by the same single guard as everything else.
    """
    paths: MediaPaths = request.app.state.paths
    slug = request.path_params["project_slug"]
    filename = request.path_params["filename"]
    relative = Path("thumbs") / slug / filename
    return _file_response(paths, relative)


def create_media_app(paths: MediaPaths, db: Database) -> Starlette:
    """Build the media sub-application.

    `db` is held for future needs (asset lookup by path) and to keep the
    signature stable; byte serving itself needs only the path allocator.
    """
    app = Starlette(
        routes=[
            Route(
                f"{MEDIA_MOUNT_PATH}/{{path:path}}",
                serve_media,
                methods=["GET", "HEAD"],
            ),
            Route(
                f"{THUMBS_MOUNT_PATH}/{{project_slug}}/{{filename}}",
                serve_thumb,
                methods=["GET", "HEAD"],
            ),
        ]
    )
    app.state.paths = paths
    app.state.db = db
    return app
