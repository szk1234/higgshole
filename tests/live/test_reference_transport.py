"""Resolves spec open item 12.1.

Question: do video providers accept a base64 data URI in ``frame_images``?

The OpenAPI schema's ``url`` field is an unconstrained string, so schema-level
acceptance is near-certain; runtime acceptance is what is unknown. This test
answers it with one cheap generation, because the alternative — a public-URL
transport — needs a tunnel or object store and contradicts the trusted-network
premise (spec section 2.8).

COSTS MONEY. Requires HIGGSHOLE_LIVE_TESTS and a funded OpenRouter key.
"""

from __future__ import annotations

import base64
import io
from decimal import Decimal

import anyio
import pytest

from higgshole.config import get_settings
from higgshole.orclient import OpenRouterClient, VideoModel, is_terminal
from tests.live.gating import live_tests_enabled

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not live_tests_enabled(),
        reason="billable; set HIGGSHOLE_LIVE_TESTS=1 to run",
    ),
]

#: Refuse to submit anything estimated above this. A guard against a
#: catalogue change silently turning a $0.30 test into a $30 one.
LIVE_COST_CEILING_USD = Decimal("0.50")

POLL_INTERVAL_S = 10.0
POLL_CEILING_S = 600.0


#: Providers impose a minimum reference-image resolution. Measured against
#: alibaba/wan-2.7 on 2026-07-20, which rejected an 8x8 PNG with
#: "image resolution must be at least 240x240, got 8x8". 256 clears that with
#: a little headroom while still encoding to a couple of hundred bytes.
_REFERENCE_EDGE_PX = 256


def _reference_png_data_uri() -> str:
    """A solid PNG as a data URI, large enough for providers to accept.

    Solid colour compresses to a few hundred bytes, so the data URI stays
    small despite the pixel dimensions.
    """
    from PIL import Image

    buffer = io.BytesIO()
    Image.new(
        "RGB", (_REFERENCE_EDGE_PX, _REFERENCE_EDGE_PX), (32, 64, 128)
    ).save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/png;base64,{encoded}"


def _per_second_price(model: VideoModel) -> Decimal | None:
    """The plain per-second price, or None when the model is not so priced.

    Only bare ``duration_seconds`` keys are considered: anything qualified by
    audio, resolution or mode has axes this test does not resolve, and a
    misread axis is exactly the estimation trap of spec section 3.1.
    """
    raw = model.pricing_skus.get("duration_seconds")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (ValueError, ArithmeticError):
        return None


def _cheapest_first_frame_model(
    models: tuple[VideoModel, ...],
) -> tuple[VideoModel, int, Decimal]:
    """The cheapest (model, duration, estimated cost) accepting a first frame."""
    candidates: list[tuple[Decimal, VideoModel, int]] = []
    for model in models:
        if "first_frame" not in model.supported_frame_images:
            continue
        price = _per_second_price(model)
        if price is None or not model.supported_durations:
            continue
        duration = min(model.supported_durations)
        candidates.append((price * duration, model, duration))

    if not candidates:
        pytest.skip("no per-second-priced video model accepts a first_frame reference")

    cost, model, duration = min(candidates, key=lambda item: item[0])
    return model, duration, cost


async def test_video_frame_images_accept_a_base64_data_uri():
    settings = get_settings()
    api_key = settings.openrouter_api_key_for("video")
    if not api_key:
        pytest.skip("no video API key configured")

    async with OpenRouterClient(api_key) as client:
        models = await client.list_video_models()
        model, duration, estimate = _cheapest_first_frame_model(models)

        assert estimate <= LIVE_COST_CEILING_USD, (
            f"cheapest candidate {model.id} at {duration}s estimates ${estimate}, "
            f"above the ${LIVE_COST_CEILING_USD} ceiling; refusing to submit"
        )
        print(f"\nLIVE: submitting {model.id} at {duration}s, estimated ${estimate}")

        job = await client.submit_video(
            model=model.id,
            prompt="a slow gentle push in on a plain coloured surface",
            frame_images=[(_reference_png_data_uri(), "first_frame")],
            duration=duration,
        )
        print(f"LIVE: accepted at submit time, job {job.id}")

        elapsed = 0.0
        while not is_terminal(job.status) and elapsed < POLL_CEILING_S:
            await anyio.sleep(POLL_INTERVAL_S)
            elapsed += POLL_INTERVAL_S
            job = await client.get_video_job(job.id)

        print(
            f"LIVE: terminal status {job.status!r} after {elapsed:.0f}s, "
            f"cost {job.cost}, error {job.error!r}"
        )

    # Submission being accepted proves schema-level acceptance; a `completed`
    # status proves the provider actually fetched and used the data URI. A
    # `failed` status naming the reference is the negative answer, and is
    # recorded in the specification rather than swallowed.
    assert job.status == "completed", (
        f"data URI frame_images did NOT work on {model.id}: status={job.status}, "
        f"error={job.error!r}. Record this in spec section 2.8 and disable video "
        f"reference slots."
    )
