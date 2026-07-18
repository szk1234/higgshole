"""The two generation state machines (spec section 4.3).

Image and video have different shapes and deliberately do not share a machine:

    image:  PENDING -> GENERATING -> WRITING -> COMPLETE
    video:  PENDING -> SUBMITTED -> RUNNING -> DOWNLOADING -> COMPLETE

with REJECTED and FAILED branches on both. Only video rows can ever occupy
SUBMITTED, RUNNING or DOWNLOADING, which is what makes boot-time reattachment
(jobs/resume.py) safe to scope by state.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from higgshole.budget.estimator import (
    Estimate,
    EstimateUnavailable,
    estimate_image_cost,
    estimate_video_cost,
)
from higgshole.budget.gate import BudgetGate, Reservation
from higgshole.catalog.validation import (
    Severity,
    ValidationIssue,
    validate_image_request,
    validate_video_request,
)
from higgshole.config import MediaKind, Settings
from higgshole.jobs.clock import Clock, RealClock
from higgshole.jobs.events import EventPublisher, JobEvent
from higgshole.jobs.references import (
    build_input_references,
    build_video_frame_images,
)
from higgshole.orclient.client import OpenRouterClient
from higgshole.store.db import (
    AssetKind,
    Database,
    ErrorReason,
    GenerationKind,
    GenerationRow,
    GenerationState,
    InputRole,
    utc_now_iso,
)
from higgshole.store.files import (
    SIDECAR_VERSION,
    atomic_write_bytes,
    discard_part,
    file_size,
    write_sidecar,
)
from higgshole.store.metadata import (
    embed_params,
    extension_for,
    make_image_thumbnail,
    make_video_poster,
    make_video_thumbnail,
    probe_media,
)
from higgshole.store.paths import MediaPaths

logger = logging.getLogger(__name__)

#: Parameters that are never forwarded to the provider. ``n`` is fixed at 1
#: (spec section 5.5) and is rejected by validation rather than transmitted.
_NON_WIRE_PARAMS: frozenset[str] = frozenset({"n"})


@dataclass(frozen=True)
class GenerationRequest:
    """A validated, project-resolved request.

    Built by web/api.py. The runner never parses HTTP input, so the same
    engine serves the REST API and any future caller unchanged.
    """

    kind: GenerationKind
    project_id: str
    project_slug: str
    model: str
    prompt: str
    params: dict[str, Any]
    #: (asset_id, role) pairs in the order the operator supplied them.
    inputs: tuple[tuple[str, InputRole], ...] = ()


@dataclass(frozen=True)
class GenerationOutcome:
    """What a runner returns to its caller."""

    generation_id: str
    state: GenerationState
    file_path: str | None
    asset_id: str | None
    cost: Decimal | None
    error_reason: ErrorReason | None
    error_detail: str | None


@dataclass(frozen=True)
class RetryPolicy:
    """Spec section 4.4.

    Submission is never blindly retried: POST /images is synchronous and
    non-idempotent, so a retry risks a second charge. Only 429-before-dispatch
    and idempotent GETs (poll, download) use this.
    """

    max_retries: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped at max_delay_s.

        Full jitter rather than a fixed schedule because several pollers may
        back off from the same 429 at the same instant; sampling from
        [0, ceiling] de-synchronises them.
        """
        exponent = max(0, attempt)
        ceiling = min(self.max_delay_s, self.base_delay_s * (2**exponent))
        return random.uniform(0.0, ceiling)


#: Provider job status -> (internal state, error reason). Spec section 4.3.
_STATUS_MAP: dict[str, tuple[GenerationState, ErrorReason | None]] = {
    "pending": (GenerationState.RUNNING, None),
    "in_progress": (GenerationState.RUNNING, None),
    "completed": (GenerationState.DOWNLOADING, None),
    "failed": (GenerationState.FAILED, ErrorReason.PROVIDER_FAILED),
    "cancelled": (GenerationState.FAILED, ErrorReason.PROVIDER_CANCELLED),
    "expired": (GenerationState.FAILED, ErrorReason.PROVIDER_EXPIRED),
}


def map_provider_status(status: str) -> tuple[GenerationState, ErrorReason | None]:
    """Provider job status -> (internal state, error reason).

    Unrecognised statuses map to (RUNNING, None): over-polling is bounded by
    the wall-clock ceiling and self-corrects, while treating a live job as
    terminal loses a paid generation irrecoverably (spec section 2.4).
    """
    return _STATUS_MAP.get(status, (GenerationState.RUNNING, None))


class JobRunner:
    """Shared plumbing for both machines.

    Database calls are made directly on the event loop thread rather than
    through a worker: each is a sub-millisecond local SQLite statement, and a
    sqlite3 connection is bound to its creating thread, so hopping threads
    would require reopening it per call for no measurable gain.
    """

    #: Which API key the subclass draws from (spec section 8).
    media_kind: MediaKind = "image"

    def __init__(
        self,
        *,
        db: Database,
        paths: MediaPaths,
        gate: BudgetGate,
        catalog: Any,
        settings: Settings,
        client_factory: Callable[[MediaKind], OpenRouterClient],
        events: EventPublisher,
        clock: Clock | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.db = db
        self.paths = paths
        self.gate = gate
        self.catalog = catalog
        self.settings = settings
        self.client_factory = client_factory
        self.events = events
        self.clock = clock or RealClock()
        self.retry_policy = retry_policy or RetryPolicy(
            max_retries=settings.max_retries
        )
        #: Output paths whose .part file must be discarded if the job fails.
        self._parts: dict[str, Path] = {}
        #: Provider-side generation IDs, recorded for the sidecar.
        self._provider_generation_ids: dict[str, str] = {}

    # -- events ----------------------------------------------------------

    def _publish(
        self,
        gen_id: str,
        kind: GenerationKind,
        state: GenerationState,
        reason: ErrorReason | None = None,
        detail: str | None = None,
    ) -> None:
        self.events.publish(
            JobEvent(
                generation_id=gen_id,
                kind=kind,
                state=state,
                error_reason=reason,
                detail=detail,
                at=utc_now_iso(),
            )
        )

    def _transition(
        self,
        gen_id: str,
        kind: GenerationKind,
        state: GenerationState,
        *,
        reason: ErrorReason | None = None,
        detail: str | None = None,
        completed_at: str | None = None,
    ) -> GenerationRow:
        row = self.db.set_generation_state(
            gen_id,
            state,
            error_reason=reason,
            error_detail=detail,
            completed_at=completed_at,
        )
        self._publish(gen_id, kind, state, reason, detail)
        return row

    # -- creation and validation ------------------------------------------

    async def create_pending(self, request: GenerationRequest) -> GenerationRow:
        """Insert the generation in PENDING and record its inputs."""
        row = self.db.create_generation(
            project_id=request.project_id,
            kind=request.kind,
            model=request.model,
            prompt=request.prompt,
            params=dict(request.params),
            state=GenerationState.PENDING,
        )
        for position, (asset_id, role) in enumerate(request.inputs):
            self.db.add_generation_input(
                generation_id=row.id,
                asset_id=asset_id,
                role=role,
                position=position,
            )
        self._publish(row.id, request.kind, GenerationState.PENDING)
        return row

    @staticmethod
    def _unknown_model_issue(model_id: str) -> ValidationIssue:
        return ValidationIssue(
            parameter="model",
            value=model_id,
            severity=Severity.HARD,
            message=(
                f"{model_id} is not in the cached catalogue. Refresh the "
                "catalogue from Settings, or check the model identifier."
            ),
        )

    @staticmethod
    def _frame_types(request: GenerationRequest) -> list[str]:
        return [
            str(role)
            for _, role in request.inputs
            if role in (InputRole.FIRST_FRAME, InputRole.LAST_FRAME)
        ]

    @staticmethod
    def _reference_count(request: GenerationRequest) -> int:
        return sum(
            1 for _, role in request.inputs if role is InputRole.INPUT_REFERENCE
        )

    async def validate(self, request: GenerationRequest) -> list[ValidationIssue]:
        """Run catalog.validation against cached capabilities.

        Ordering is fixed: local validation -> budget gate -> dispatch
        (spec section 4.3), so an invalid combination costs nothing rather
        than becoming a failed paid request.
        """
        if request.kind is GenerationKind.IMAGE:
            model = await self.catalog.get_image_model(request.model)
            if model is None:
                return [self._unknown_model_issue(request.model)]
            return validate_image_request(
                model,
                n=int(request.params.get("n", 1)),
                quality=request.params.get("quality"),
                reference_count=self._reference_count(request),
                daily_cap_set=self.gate.cap_is_set,
            )

        model = await self.catalog.get_video_model(request.model)
        if model is None:
            return [self._unknown_model_issue(request.model)]
        return validate_video_request(
            model,
            resolution=request.params.get("resolution"),
            aspect_ratio=request.params.get("aspect_ratio"),
            duration=request.params.get("duration"),
            frame_types=self._frame_types(request),
        )

    async def estimate(self, request: GenerationRequest) -> Estimate:
        """The advisory pre-flight cost, or an explicit reason it is unknown.

        Never returns a fabricated number: where the axes do not resolve, the
        gate falls back to the pessimistic ceiling instead (spec section 3.3).
        """
        if request.kind is GenerationKind.IMAGE:
            model = await self.catalog.get_image_model(request.model)
            if model is None:
                return Estimate(
                    amount=None,
                    reason=EstimateUnavailable.NO_PRICING_DATA,
                    detail=f"{request.model} is not in the cached catalogue.",
                )
            pricing = await self.catalog.get_image_pricing(request.model)
            return estimate_image_cost(
                model,
                pricing,
                width=request.params.get("width"),
                height=request.params.get("height"),
                quality=request.params.get("quality"),
                reference_count=self._reference_count(request),
            )

        model = await self.catalog.get_video_model(request.model)
        if model is None:
            return Estimate(
                amount=None,
                reason=EstimateUnavailable.NO_PRICING_DATA,
                detail=f"{request.model} is not in the cached catalogue.",
            )
        return estimate_video_cost(
            model,
            duration=request.params.get("duration"),
            resolution=request.params.get("resolution"),
            aspect_ratio=request.params.get("aspect_ratio"),
            generate_audio=bool(request.params.get("generate_audio")),
            has_frame_images=bool(self._frame_types(request)),
        )

    async def reject(
        self, gen_id: str, reason: ErrorReason, detail: str
    ) -> GenerationOutcome:
        """Move to REJECTED and emit an event.

        No reservation is settled because rejection happens before or at the
        gate: either nothing was reserved, or the gate refused to reserve.
        """
        row = self.db.get_generation(gen_id)
        kind = row.kind if row is not None else GenerationKind.IMAGE
        self._transition(
            gen_id, kind, GenerationState.REJECTED, reason=reason, detail=detail
        )
        return GenerationOutcome(
            generation_id=gen_id,
            state=GenerationState.REJECTED,
            file_path=None,
            asset_id=None,
            cost=None,
            error_reason=reason,
            error_detail=detail,
        )

    # -- resolving stored inputs into provider payloads --------------------

    def _resolved_inputs(self, gen_id: str) -> list[tuple[Any, InputRole]]:
        resolved: list[tuple[Any, InputRole]] = []
        for link in self.db.list_generation_inputs(gen_id):
            asset = self.db.get_asset(link.asset_id)
            if asset is not None:
                resolved.append((asset, link.role))
        return resolved

    def input_references_for(self, gen_id: str) -> list[str]:
        return build_input_references(
            self._resolved_inputs(gen_id),
            self.paths,
            transport=self.settings.reference_transport,
        )

    def frame_images_for(self, gen_id: str) -> list[tuple[str, str]]:
        return build_video_frame_images(
            self._resolved_inputs(gen_id),
            self.paths,
            transport=self.settings.reference_transport,
        )

    @staticmethod
    def wire_params(params: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in params.items()
            if key not in _NON_WIRE_PARAMS and value is not None
        }

    # -- completion --------------------------------------------------------

    def _sidecar_payload(
        self,
        *,
        row: GenerationRow,
        project_slug: str,
        relative: str,
        metadata: Any,
        cost: Decimal | None,
        completed_at: str,
    ) -> dict[str, Any]:
        inputs: list[dict[str, Any]] = []
        for link in self.db.list_generation_inputs(row.id):
            asset = self.db.get_asset(link.asset_id)
            inputs.append(
                {
                    "asset_id": link.asset_id,
                    "role": str(link.role),
                    "position": link.position,
                    "relative_path": None if asset is None else asset.file_path,
                }
            )
        return {
            "sidecar_version": SIDECAR_VERSION,
            "id": row.id,
            "kind": str(row.kind),
            "project_slug": project_slug,
            "model": row.model,
            "prompt": row.prompt,
            "params": dict(row.params),
            "inputs": inputs,
            "provider": {
                "job_id": row.provider_job_id,
                "generation_id": self._provider_generation_ids.get(row.id),
            },
            "media": {
                "relative_path": relative,
                "mime_type": metadata.mime_type,
                "bytes": metadata.bytes,
                "width": metadata.width,
                "height": metadata.height,
                "duration_s": metadata.duration_s,
            },
            "cost": {
                "amount_usd": None if cost is None else str(cost),
                "known": cost is not None,
            },
            "created_at": row.created_at,
            "completed_at": completed_at,
        }

    def _write_derived_images(
        self, *, row: GenerationRow, project_slug: str, media_path: Path
    ) -> None:
        """Thumbnail and, for video, a poster frame.

        Wrapped: a thumbnail failure degrades the library grid, and failing a
        paid generation over it would be indefensible.
        """
        try:
            thumb = self.paths.thumb_path(project_slug=project_slug, gen_id=row.id)
            if row.kind is GenerationKind.VIDEO:
                poster = self.paths.poster_path(
                    project_slug=project_slug, gen_id=row.id
                )
                poster_meta = make_video_poster(media_path, poster)
                self.db.create_asset(
                    kind=AssetKind.POSTER,
                    file_path=poster.relative_to(self.paths.root).as_posix(),
                    mime_type=poster_meta.mime_type,
                    bytes_=poster_meta.bytes,
                    generation_id=row.id,
                    width=poster_meta.width,
                    height=poster_meta.height,
                )
                thumb_meta = make_video_thumbnail(media_path, thumb)
            else:
                thumb_meta = make_image_thumbnail(media_path, thumb)

            self.db.create_asset(
                kind=AssetKind.THUMBNAIL,
                file_path=thumb.relative_to(self.paths.root).as_posix(),
                mime_type=thumb_meta.mime_type,
                bytes_=thumb_meta.bytes,
                generation_id=row.id,
                width=thumb_meta.width,
                height=thumb_meta.height,
            )
        except Exception:
            logger.warning("thumbnailing failed for %s", row.id, exc_info=True)

    async def finalise_success(
        self,
        *,
        gen_id: str,
        data: bytes,
        media_type: str,
        cost: Decimal | None,
        reservation: Reservation | None,
    ) -> GenerationOutcome:
        """The single completion path for both machines."""
        row = self.db.get_generation(gen_id)
        project = self.db.get_project(row.project_id)

        try:
            allocated = self.paths.allocate_output(
                project_slug=project.slug,
                kind=row.kind,
                gen_id=gen_id,
                prompt=row.prompt,
                ext=extension_for(media_type),
            )
        except Exception as exc:
            return await self.finalise_failure(
                gen_id=gen_id,
                reason=ErrorReason.WRITE_FAILED,
                detail=f"could not allocate an output path: {exc}",
                reservation=reservation,
            )

        self._parts[gen_id] = allocated.media_path
        try:
            atomic_write_bytes(allocated.media_path, data)
            metadata = probe_media(allocated.media_path)
        except Exception as exc:
            return await self.finalise_failure(
                gen_id=gen_id,
                reason=ErrorReason.WRITE_FAILED,
                detail=str(exc),
                reservation=reservation,
            )
        self._parts.pop(gen_id, None)

        relative = allocated.relative_media_path.as_posix()
        completed_at = utc_now_iso()
        payload = self._sidecar_payload(
            row=row,
            project_slug=project.slug,
            relative=relative,
            metadata=metadata,
            cost=cost,
            completed_at=completed_at,
        )

        # The sidecar is written before the tag is embedded so that an
        # embedding failure can never leave the authoritative record unwritten.
        # media.bytes therefore records the file as generated; the asset row
        # below re-stats the file so the served length is always the truth.
        write_sidecar(allocated.sidecar_path, payload)
        try:
            embed_params(allocated.media_path, payload)
        except Exception:
            logger.warning(
                "embedding parameters failed for %s", gen_id, exc_info=True
            )

        asset = self.db.create_asset(
            kind=AssetKind.OUTPUT,
            file_path=relative,
            mime_type=metadata.mime_type,
            bytes_=file_size(allocated.media_path),
            generation_id=gen_id,
            width=metadata.width,
            height=metadata.height,
            duration_s=metadata.duration_s,
        )
        self._write_derived_images(
            row=row, project_slug=project.slug, media_path=allocated.media_path
        )

        self.db.set_generation_file(gen_id, relative)
        self._transition(
            gen_id, row.kind, GenerationState.COMPLETE, completed_at=completed_at
        )

        if reservation is not None:
            await self.gate.release(reservation, actual_cost=cost, succeeded=True)

        return GenerationOutcome(
            generation_id=gen_id,
            state=GenerationState.COMPLETE,
            file_path=relative,
            asset_id=asset.id,
            cost=cost,
            error_reason=None,
            error_detail=None,
        )

    async def finalise_failure(
        self,
        *,
        gen_id: str,
        reason: ErrorReason,
        detail: str,
        reservation: Reservation | None,
    ) -> GenerationOutcome:
        """FAILED plus gate.release(succeeded=False). Discards any .part file."""
        part_owner = self._parts.pop(gen_id, None)
        if part_owner is not None:
            discard_part(part_owner)

        row = self.db.get_generation(gen_id)
        kind = row.kind if row is not None else GenerationKind.IMAGE
        self._transition(
            gen_id, kind, GenerationState.FAILED, reason=reason, detail=detail
        )

        if reservation is not None:
            await self.gate.release(reservation, actual_cost=None, succeeded=False)

        return GenerationOutcome(
            generation_id=gen_id,
            state=GenerationState.FAILED,
            file_path=None,
            asset_id=None,
            cost=None,
            error_reason=reason,
            error_detail=detail,
        )


class ImageJobRunner(JobRunner):
    """PENDING -> GENERATING -> WRITING -> COMPLETE (spec section 4.3)."""

    media_kind: MediaKind = "image"


class VideoJobRunner(JobRunner):
    """PENDING -> SUBMITTED -> RUNNING -> DOWNLOADING -> COMPLETE."""

    media_kind: MediaKind = "video"
