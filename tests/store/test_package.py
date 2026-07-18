import subprocess
import sys

from higgshole import store
from higgshole.store import (
    RESUMABLE_STATES,
    SIDECAR_VERSION,
    TERMINAL_STATES,
    AssetKind,
    AssetRow,
    Database,
    ErrorReason,
    GenerationKind,
    GenerationRow,
    GenerationState,
    InputRole,
    LedgerKind,
    LedgerRow,
    MediaFilter,
    MediaMetadata,
    MediaPaths,
    PathTraversalError,
    ProjectRow,
    atomic_write_bytes,
    embed_params,
    new_id,
    probe_media,
    project_slug,
    read_sidecar,
    slugify,
    utc_now_iso,
    write_sidecar,
)


def test_re_exports_are_importable():
    exported = {
        RESUMABLE_STATES,
        SIDECAR_VERSION,
        TERMINAL_STATES,
        AssetKind,
        AssetRow,
        Database,
        ErrorReason,
        GenerationKind,
        GenerationRow,
        GenerationState,
        InputRole,
        LedgerKind,
        LedgerRow,
        MediaFilter,
        MediaMetadata,
        MediaPaths,
        PathTraversalError,
        ProjectRow,
    }

    assert len(exported) == 18
    for name in (
        atomic_write_bytes,
        embed_params,
        new_id,
        probe_media,
        project_slug,
        read_sidecar,
        slugify,
        utc_now_iso,
        write_sidecar,
    ):
        assert callable(name)
    assert set(store.__all__) >= {"Database", "MediaPaths", "utc_now_iso"}


def test_store_does_not_import_orclient():
    # Spec section 4.1: store/ touches disk and database only. Checked in a
    # fresh interpreter because this test session has already imported
    # orclient for other tests.
    script = (
        "import sys; import higgshole.store; "
        "leaked = [m for m in sys.modules if m.startswith('higgshole.orclient')]; "
        "assert not leaked, leaked"
    )

    result = subprocess.run([sys.executable, "-c", script], capture_output=True)

    assert result.returncode == 0, result.stderr.decode()


def test_enum_values_match_the_contract():
    assert GenerationKind.IMAGE.value == "image"
    assert GenerationState.DOWNLOADING.value == "DOWNLOADING"
    assert ErrorReason.PROVIDER_CANCELLED.value == "provider_cancelled"
    assert AssetKind.POSTER.value == "poster"
    assert InputRole.FIRST_FRAME.value == "first_frame"
    assert LedgerKind.REVERSAL.value == "reversal"
