from decimal import Decimal

from higgshole.orclient.types import (
    ImageModel,
    KeyStatus,
    VideoJob,
    VideoModel,
    is_terminal,
)

# Trimmed from a live GET /api/v1/videos/models response.
VIDEO_MODEL_PAYLOAD = {
    "id": "kwaivgi/kling-v3.0-pro",
    "supported_resolutions": ["720p"],
    "supported_aspect_ratios": ["16:9", "9:16", "1:1"],
    "supported_durations": [3, 4, 5, 10],
    "supported_sizes": ["1280x720", "720x1280"],
    "supported_frame_images": ["first_frame", "last_frame"],
    "generate_audio": True,
    "seed": True,
    "pricing_skus": {
        "duration_seconds": "0.112",
        "duration_seconds_with_audio": "0.168",
        "image_to_video_duration_seconds_1080p": "0.112",
    },
    "allowed_passthrough_parameters": ["negative_prompt", "cfg_scale"],
}

IMAGE_MODEL_PAYLOAD = {
    "id": "openai/gpt-image-2",
    "name": "GPT Image 2",
    "supported_parameters": {
        "quality": {"type": "enum", "values": ["auto", "low", "medium", "high"]},
        "n": {"type": "range", "min": 1, "max": 10},
        "input_references": {"type": "range", "min": 0, "max": 16},
    },
    "supports_streaming": True,
}


def test_video_model_parses_capabilities():
    model = VideoModel.from_api(VIDEO_MODEL_PAYLOAD)

    assert model.id == "kwaivgi/kling-v3.0-pro"
    assert model.supported_durations == (3, 4, 5, 10)
    assert model.supported_frame_images == ("first_frame", "last_frame")
    assert model.generate_audio is True
    assert model.pricing_skus["duration_seconds_with_audio"] == "0.168"


def test_video_model_tolerates_absent_optional_fields():
    model = VideoModel.from_api({"id": "some/model"})

    assert model.supported_durations == ()
    assert model.supported_frame_images == ()
    assert model.generate_audio is None
    assert model.pricing_skus == {}


def test_video_model_reports_reference_support():
    assert VideoModel.from_api(VIDEO_MODEL_PAYLOAD).accepts_frame_images is True
    # Sora 2 Pro accepts none — spec section 2.7.
    assert VideoModel.from_api({"id": "openai/sora-2-pro"}).accepts_frame_images is False


def test_image_model_extracts_reference_limit():
    model = ImageModel.from_api(IMAGE_MODEL_PAYLOAD)

    assert model.id == "openai/gpt-image-2"
    assert model.max_input_references == 16
    assert model.quality_values == ("auto", "low", "medium", "high")


def test_image_model_without_reference_support_reports_zero():
    model = ImageModel.from_api({"id": "some/model", "supported_parameters": {}})

    assert model.max_input_references == 0
    assert model.quality_values == ()


def test_terminal_status_set_matches_the_specification():
    for status in ("completed", "failed", "cancelled", "expired"):
        assert is_terminal(status) is True
    for status in ("pending", "in_progress"):
        assert is_terminal(status) is False


def test_unknown_status_is_non_terminal_so_polling_continues():
    # Spec section 2.4: treating a live job as terminal loses a paid
    # generation; over-polling is bounded by the wall-clock ceiling.
    assert is_terminal("something_new") is False


def test_video_job_parses_a_completed_response():
    job = VideoJob.from_api(
        {
            "id": "abc123",
            "status": "completed",
            "generation_id": "gen-1",
            "unsigned_urls": ["https://storage.example.com/video.mp4"],
            "usage": {"cost": 0.25, "is_byok": False},
        }
    )

    assert job.id == "abc123"
    assert job.is_terminal is True
    assert job.cost == Decimal("0.25")
    assert job.result_urls == ("https://storage.example.com/video.mp4",)


def test_video_job_with_null_cost_reports_none_not_zero():
    job = VideoJob.from_api(
        {"id": "abc", "status": "completed", "usage": {"cost": None}}
    )

    assert job.cost is None


def test_video_job_without_usage_reports_none():
    job = VideoJob.from_api({"id": "abc", "status": "completed"})

    assert job.cost is None


def test_video_job_surfaces_the_error_string():
    job = VideoJob.from_api(
        {"id": "abc", "status": "failed", "error": "Content policy violation"}
    )

    assert job.is_terminal is True
    assert job.error == "Content policy violation"


def test_key_status_parses_authoritative_budget_figures():
    status = KeyStatus.from_api(
        {
            "data": {
                "limit": 100,
                "limit_remaining": 74.5,
                "limit_reset": "monthly",
                "usage": 25.5,
                "usage_daily": 25.5,
                "is_free_tier": False,
            }
        }
    )

    assert status.limit_remaining == Decimal("74.5")
    assert status.usage_daily == Decimal("25.5")
    assert status.is_free_tier is False


def test_key_status_handles_an_unlimited_key():
    status = KeyStatus.from_api({"data": {"limit": None, "limit_remaining": None}})

    assert status.limit is None
    assert status.limit_remaining is None
