from higgshole.catalog.validation import (
    Severity,
    has_hard_failure,
    validate_image_request,
    validate_video_request,
)
from higgshole.orclient.types import ImageModel, VideoModel

# Kling declares only 720p but prices 480p and 1080p — spec section 2.7.
KLING = VideoModel.from_api(
    {
        "id": "kwaivgi/kling-v3.0-pro",
        "supported_resolutions": ["720p"],
        "supported_aspect_ratios": ["16:9", "9:16"],
        "supported_durations": [3, 5, 10],
        "supported_frame_images": ["first_frame", "last_frame"],
        "pricing_skus": {
            "text_to_video_duration_seconds_480p": "0.112",
            "image_to_video_duration_seconds_1080p": "0.112",
        },
    }
)

SORA = VideoModel.from_api(
    {
        "id": "openai/sora-2-pro",
        "supported_resolutions": ["720p", "1080p"],
        "supported_durations": [4, 8],
        "supported_frame_images": [],
    }
)

HAILUO = VideoModel.from_api(
    {
        "id": "minimax/hailuo-2.3",
        "supported_resolutions": ["1080p"],
        "supported_durations": [6, 10],
        "supported_frame_images": ["first_frame"],
    }
)

GPT_IMAGE = ImageModel.from_api(
    {
        "id": "openai/gpt-image-2",
        "supported_parameters": {
            "quality": {"type": "enum", "values": ["auto", "low", "medium", "high"]},
            "n": {"type": "range", "min": 1, "max": 10},
            "input_references": {"type": "range", "min": 0, "max": 16},
        },
    }
)

RECRAFT = ImageModel.from_api(
    {
        "id": "recraft/recraft-v4.1",
        "supported_parameters": {
            "input_references": {"type": "range", "min": 0, "max": 1}
        },
    }
)


def test_a_declared_value_produces_no_issue():
    assert validate_video_request(KLING, resolution="720p", duration=5) == []


def test_an_undeclared_but_priced_value_is_advisory():
    issues = validate_video_request(KLING, resolution="1080p")

    assert len(issues) == 1
    assert issues[0].severity is Severity.ADVISORY
    assert has_hard_failure(issues) is False


def test_a_value_absent_from_both_lists_is_a_hard_failure():
    issues = validate_video_request(KLING, resolution="8K")

    assert issues[0].severity is Severity.HARD
    assert has_hard_failure(issues) is True


def test_an_unsupported_duration_is_a_hard_failure():
    issues = validate_video_request(KLING, duration=7)

    assert has_hard_failure(issues) is True
    assert "7" in issues[0].value


def test_a_model_accepting_no_frames_rejects_any_reference():
    issues = validate_video_request(SORA, frame_types=["first_frame"])

    assert has_hard_failure(issues) is True
    assert (
        "sora" in issues[0].message.lower()
        or "no reference" in issues[0].message.lower()
    )


def test_a_first_frame_only_model_rejects_a_last_frame():
    issues = validate_video_request(HAILUO, frame_types=["last_frame"])

    assert has_hard_failure(issues) is True


def test_a_first_frame_only_model_accepts_a_first_frame():
    assert validate_video_request(HAILUO, frame_types=["first_frame"]) == []


def test_an_unsupported_aspect_ratio_is_a_hard_failure():
    issues = validate_video_request(KLING, aspect_ratio="21:9")

    assert has_hard_failure(issues) is True


def test_multiple_problems_are_all_reported():
    issues = validate_video_request(KLING, resolution="8K", duration=99)

    assert len(issues) == 2


def test_too_many_image_references_is_a_hard_failure():
    issues = validate_image_request(RECRAFT, reference_count=3)

    assert has_hard_failure(issues) is True
    assert "1" in issues[0].message


def test_reference_count_within_the_limit_is_accepted():
    assert validate_image_request(GPT_IMAGE, reference_count=5) == []


def test_batch_generation_is_rejected():
    # Spec section 5.5: n is fixed at 1.
    issues = validate_image_request(GPT_IMAGE, n=4)

    assert has_hard_failure(issues) is True


def test_auto_quality_is_rejected_when_a_daily_cap_is_configured():
    # Spec section 3.5: auto quality on token-billed models is unbounded.
    issues = validate_image_request(GPT_IMAGE, quality="auto", daily_cap_set=True)

    assert has_hard_failure(issues) is True


def test_auto_quality_is_permitted_with_no_cap_configured():
    assert validate_image_request(GPT_IMAGE, quality="auto", daily_cap_set=False) == []


def test_an_unsupported_quality_value_is_a_hard_failure():
    issues = validate_image_request(GPT_IMAGE, quality="ultra")

    assert has_hard_failure(issues) is True
