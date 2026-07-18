from decimal import Decimal

import pytest

from higgshole.budget.estimator import (
    Estimate,
    EstimateUnavailable,
    estimate_video_cost,
    parse_sku_amount,
    reservation_amount,
)
from higgshole.orclient.types import VideoModel

# Spec section 3.1 item 3: mode- and resolution-qualified SKUs with no audio
# variants, alongside a bare with-audio SKU carrying a 50% surcharge.
KLING = VideoModel.from_api(
    {
        "id": "kwaivgi/kling-v3.0-pro",
        "supported_resolutions": ["720p"],
        "supported_durations": [5, 10],
        "supported_frame_images": ["first_frame", "last_frame"],
        "generate_audio": True,
        "pricing_skus": {
            "text_to_video_duration_seconds_480p": "0.028",
            "text_to_video_duration_seconds_720p": "0.056",
            "text_to_video_duration_seconds_1080p": "0.112",
            "image_to_video_duration_seconds_480p": "0.028",
            "image_to_video_duration_seconds_720p": "0.056",
            "image_to_video_duration_seconds_1080p": "0.112",
            "duration_seconds_with_audio": "0.168",
        },
    }
)

VEO = VideoModel.from_api(
    {
        "id": "google/veo-3.1",
        "supported_durations": [4, 6, 8],
        "generate_audio": True,
        "pricing_skus": {"duration_seconds_with_audio": "0.40"},
    }
)

# Spec section 3.1 item 4: audio-capable, bare SKU only.
WAN = VideoModel.from_api(
    {
        "id": "alibaba/wan-2.7",
        "supported_durations": [5],
        "generate_audio": True,
        "pricing_skus": {"duration_seconds": "0.050"},
    }
)

SILENT = VideoModel.from_api(
    {
        "id": "alibaba/happyhorse-1",
        "supported_durations": [5],
        "generate_audio": None,
        "pricing_skus": {"duration_seconds": "0.050"},
    }
)

# Spec section 3.1 item 1: no published tokens-per-second table.
SEEDANCE = VideoModel.from_api(
    {
        "id": "bytedance/seedance-1.5-pro",
        "supported_durations": [5, 10],
        "pricing_skus": {"video_tokens": "0.000007"},
    }
)

# Spec section 3.1 item 2: reading "7" as dollars is a 100x overestimate.
GROK = VideoModel.from_api(
    {
        "id": "x-ai/grok-imagine-video",
        "supported_durations": [5],
        "pricing_skus": {"cents_per_duration_seconds": "7"},
    }
)

# Spec section 3.1 item 5: the guide's hyphenated grammar matches no live model.
DOCUMENTED_BUT_UNREAL = VideoModel.from_api(
    {"id": "example/from-the-guide", "pricing_skus": {"per-video-second": "0.10"}}
)

UNPRICED = VideoModel.from_api({"id": "example/unpriced", "supported_durations": [5]})

TIED = VideoModel.from_api(
    {
        "id": "example/tied",
        "supported_durations": [5],
        "pricing_skus": {"duration_seconds": "0.10", "duration_seconds_pro": "0.20"},
    }
)


def test_veo_audio_only_sku_is_exact():
    estimate = estimate_video_cost(VEO, duration=8, generate_audio=True)

    assert estimate.amount == Decimal("3.20")
    assert estimate.reason is None
    assert estimate.sku_key == "duration_seconds_with_audio"


def test_kling_image_to_video_720p_without_audio_is_exact():
    estimate = estimate_video_cost(
        KLING, duration=5, resolution="720p", generate_audio=False, has_frame_images=True
    )

    assert estimate.amount == Decimal("0.28")
    assert estimate.sku_key == "image_to_video_duration_seconds_720p"


def test_kling_audio_with_resolution_is_ambiguous():
    # The audio SKU is unqualified while the mode/resolution SKUs carry no
    # audio variant. Most-specific-match would silently drop a 50% surcharge.
    estimate = estimate_video_cost(
        KLING, duration=5, resolution="720p", generate_audio=True, has_frame_images=True
    )

    assert estimate.amount is None
    assert estimate.reason is EstimateUnavailable.AMBIGUOUS_AXES


def test_kling_text_to_video_is_exact():
    estimate = estimate_video_cost(KLING, duration=5, resolution="1080p")

    assert estimate.amount == Decimal("0.56")
    assert estimate.sku_key == "text_to_video_duration_seconds_1080p"


def test_audio_capable_model_with_only_a_bare_sku_is_missing_axis():
    estimate = estimate_video_cost(WAN, duration=5, generate_audio=True)

    assert estimate.amount is None
    assert estimate.reason is EstimateUnavailable.MISSING_AXIS


def test_seedance_video_tokens_are_not_estimable():
    estimate = estimate_video_cost(SEEDANCE, duration=5)

    assert estimate.amount is None
    assert estimate.reason is EstimateUnavailable.VIDEO_TOKEN_PRICED


def test_grok_cents_per_prefix_divides_by_one_hundred():
    estimate = estimate_video_cost(GROK, duration=5)

    assert estimate.amount == Decimal("0.35")


def test_grok_is_not_read_as_dollars():
    estimate = estimate_video_cost(GROK, duration=5)

    assert estimate.amount != Decimal("35")


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("duration_seconds", "0.112", Decimal("0.112")),
        ("cents_per_duration_seconds", "7", Decimal("0.07")),
        ("cents_per_video_second", "0.5", Decimal("0.005")),
        ("duration_seconds_with_audio", "0.40", Decimal("0.40")),
    ],
)
def test_parse_sku_amount(key, raw, expected):
    assert parse_sku_amount(key, raw) == expected


def test_a_model_with_no_pricing_skus_reports_no_pricing_data():
    estimate = estimate_video_cost(UNPRICED, duration=5)

    assert estimate.reason is EstimateUnavailable.NO_PRICING_DATA


def test_an_unknown_unit_is_reported_as_such():
    estimate = estimate_video_cost(DOCUMENTED_BUT_UNREAL, duration=5)

    assert estimate.reason is EstimateUnavailable.UNKNOWN_UNIT


def test_a_missing_duration_is_a_missing_axis():
    estimate = estimate_video_cost(VEO, duration=None, generate_audio=True)

    assert estimate.reason is EstimateUnavailable.MISSING_AXIS


def test_an_unpriced_resolution_has_no_matching_sku():
    estimate = estimate_video_cost(KLING, duration=5, resolution="4K")

    assert estimate.reason is EstimateUnavailable.NO_MATCHING_SKU


def test_audio_requested_on_a_non_audio_model_has_no_matching_sku():
    estimate = estimate_video_cost(SILENT, duration=5, generate_audio=True)

    assert estimate.reason is EstimateUnavailable.NO_MATCHING_SKU


def test_estimate_amount_and_reason_are_mutually_exclusive():
    for estimate in (
        estimate_video_cost(VEO, duration=8, generate_audio=True),
        estimate_video_cost(SEEDANCE, duration=5),
        estimate_video_cost(UNPRICED, duration=5),
    ):
        assert (estimate.amount is None) != (estimate.reason is None)
        assert estimate.detail


def test_is_exact_reflects_the_amount():
    assert estimate_video_cost(VEO, duration=8, generate_audio=True).is_exact is True
    assert estimate_video_cost(SEEDANCE, duration=5).is_exact is False


def test_reservation_amount_uses_an_exact_estimate():
    estimate = estimate_video_cost(VEO, duration=8, generate_audio=True)

    assert reservation_amount(estimate, max_job_cost_usd=Decimal("2.00")) == (
        Decimal("3.20"),
        True,
    )


def test_reservation_amount_falls_back_to_the_ceiling():
    # Spec section 3.3: a non-estimable job reserves the pessimistic ceiling.
    estimate = estimate_video_cost(SEEDANCE, duration=5)

    assert reservation_amount(estimate, max_job_cost_usd=Decimal("2.00")) == (
        Decimal("2.00"),
        False,
    )


def test_two_equally_specific_conflicting_skus_are_ambiguous():
    estimate = estimate_video_cost(TIED, duration=5)

    assert isinstance(estimate, Estimate)
    assert estimate.reason is EstimateUnavailable.AMBIGUOUS_AXES
