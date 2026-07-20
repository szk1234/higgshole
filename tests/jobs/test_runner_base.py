from decimal import Decimal

from higgshole.budget.estimator import Estimate
from higgshole.catalog.validation import has_hard_failure
from higgshole.store.db import (
    AssetKind,
    ErrorReason,
    GenerationState,
    InputRole,
    LedgerKind,
)
from tests.jobs.fakes import PNG_BYTES


async def _reserve(harness, gen_id):
    """Take a real reservation through the gate, as the runners do."""
    return await harness.gate.acquire(
        generation_id=gen_id,
        estimate=Estimate(amount=Decimal("0.10"), reason=None, detail="exact"),
    )


async def test_create_pending_inserts_a_pending_row(harness):
    row = await harness.image_runner.create_pending(harness.image_request())

    stored = harness.db.get_generation(row.id)
    assert stored.state is GenerationState.PENDING
    assert stored.model == "test/image-model"
    assert harness.events.states_for(row.id) == ["PENDING"]


async def test_create_pending_records_inputs_in_order(harness):
    first = harness.upload("first.png")
    second = harness.upload("second.png")

    row = await harness.image_runner.create_pending(
        harness.image_request(
            inputs=(
                (first, InputRole.INPUT_REFERENCE),
                (second, InputRole.INPUT_REFERENCE),
            )
        )
    )

    links = harness.db.list_generation_inputs(row.id)
    assert [link.asset_id for link in links] == [first, second]
    assert [link.position for link in links] == [0, 1]


async def test_validate_flags_an_unknown_model_as_hard(harness):
    issues = await harness.image_runner.validate(
        harness.image_request(model="nobody/nothing")
    )

    assert has_hard_failure(issues) is True
    assert issues[0].parameter == "model"


async def test_validate_passes_a_supported_image_request(harness):
    assert await harness.image_runner.validate(harness.image_request()) == []


async def test_validate_rejects_batch_generation(harness):
    # Spec section 5.5: n is fixed at 1.
    issues = await harness.image_runner.validate(
        harness.image_request(params={"n": 4, "quality": "high"})
    )

    assert has_hard_failure(issues) is True
    assert any(issue.parameter == "n" for issue in issues)


async def test_reject_moves_the_row_to_rejected_and_emits_an_event(harness):
    row = await harness.image_runner.create_pending(harness.image_request())

    outcome = await harness.image_runner.reject(
        row.id, ErrorReason.VALIDATION, "unsupported resolution"
    )

    assert outcome.state is GenerationState.REJECTED
    assert outcome.error_reason is ErrorReason.VALIDATION
    assert harness.db.get_generation(row.id).state is GenerationState.REJECTED
    assert harness.events.states_for(row.id) == ["PENDING", "REJECTED"]


async def test_finalise_success_writes_media_sidecar_and_asset(harness):
    row = await harness.image_runner.create_pending(harness.image_request())
    reservation = await _reserve(harness, row.id)

    outcome = await harness.image_runner.finalise_success(
        gen_id=row.id,
        data=PNG_BYTES,
        media_type="image/png",
        cost=Decimal("0.04"),
        reservation=reservation,
    )

    media_path = harness.paths.root / outcome.file_path
    assert media_path.read_bytes() == PNG_BYTES
    assert harness.paths.sidecar_path(media_path).exists()
    assert not media_path.with_suffix(media_path.suffix + ".part").exists()

    assets = harness.db.list_assets_for_generation(row.id)
    kinds = {asset.kind for asset in assets}
    assert AssetKind.OUTPUT in kinds
    assert AssetKind.THUMBNAIL in kinds


async def test_the_sidecar_byte_count_matches_the_file_after_embedding(harness):
    """The sidecar is what `rescan` rebuilds the database from (spec 5.3).

    Embedding parameters rewrites the media file and changes its length, so a
    sidecar written before that step records a stale size. Left uncorrected, a
    restore would populate every asset row with the pre-embed byte count.
    """
    import json

    row = await harness.image_runner.create_pending(harness.image_request())
    reservation = await _reserve(harness, row.id)

    outcome = await harness.image_runner.finalise_success(
        gen_id=row.id,
        data=PNG_BYTES,
        media_type="image/png",
        cost=Decimal("0.04"),
        reservation=reservation,
    )

    media_path = harness.paths.root / outcome.file_path
    sidecar = json.loads(harness.paths.sidecar_path(media_path).read_text())

    assert sidecar["media"]["bytes"] == media_path.stat().st_size


async def test_finalise_success_marks_the_row_complete_and_releases_the_reservation(
    harness,
):
    row = await harness.image_runner.create_pending(harness.image_request())
    reservation = await _reserve(harness, row.id)

    await harness.image_runner.finalise_success(
        gen_id=row.id,
        data=PNG_BYTES,
        media_type="image/png",
        cost=Decimal("0.04"),
        reservation=reservation,
    )

    stored = harness.db.get_generation(row.id)
    assert stored.state is GenerationState.COMPLETE
    assert stored.completed_at is not None
    assert stored.file_path == harness.db.get_generation(row.id).file_path

    kinds = [row_.kind for row_ in harness.db.list_ledger_for_generation(row.id)]
    assert LedgerKind.ACTUAL in kinds
    assert harness.ledger_total(row.id) == Decimal("0.04")


async def test_finalise_success_survives_a_metadata_embedding_failure(
    harness, monkeypatch
):
    # The sidecar is the authoritative record; embedding is a convenience, and
    # a metadata failure must never fail a paid generation.
    def _boom(path, payload):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr("higgshole.jobs.runner.embed_params", _boom)

    row = await harness.image_runner.create_pending(harness.image_request())
    reservation = await _reserve(harness, row.id)

    outcome = await harness.image_runner.finalise_success(
        gen_id=row.id,
        data=PNG_BYTES,
        media_type="image/png",
        cost=Decimal("0.04"),
        reservation=reservation,
    )

    assert outcome.state is GenerationState.COMPLETE


async def test_finalise_failure_marks_failed_and_reverses_the_reservation(harness):
    row = await harness.image_runner.create_pending(harness.image_request())
    reservation = await _reserve(harness, row.id)

    outcome = await harness.image_runner.finalise_failure(
        gen_id=row.id,
        reason=ErrorReason.DOWNLOAD_FAILED,
        detail="upstream 502",
        reservation=reservation,
    )

    assert outcome.state is GenerationState.FAILED
    assert outcome.error_reason is ErrorReason.DOWNLOAD_FAILED
    # A failed job nets to zero (spec section 3.3).
    assert harness.ledger_total(row.id) == Decimal("0")


async def test_finalise_failure_discards_a_stale_part_file(harness, monkeypatch):
    # An interrupted write leaves only the .part file, which is never renamed,
    # so a half-file can never be indexed as a complete asset (spec section 10).
    def _explode(path, data, **kwargs):
        part = path.with_name(path.name + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"half")
        raise OSError("disk full")

    monkeypatch.setattr("higgshole.jobs.runner.atomic_write_bytes", _explode)

    row = await harness.image_runner.create_pending(harness.image_request())
    reservation = await _reserve(harness, row.id)

    outcome = await harness.image_runner.finalise_success(
        gen_id=row.id,
        data=PNG_BYTES,
        media_type="image/png",
        cost=Decimal("0.04"),
        reservation=reservation,
    )

    assert outcome.state is GenerationState.FAILED
    assert outcome.error_reason is ErrorReason.WRITE_FAILED
    parts = list(harness.paths.root.rglob("*.part"))
    assert parts == []
