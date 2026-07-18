"""Generation state machines and everything that orchestrates a job."""

from .clock import Clock, RealClock
from .events import EventPublisher, JobEvent, NullEventPublisher
from .references import (
    DEFAULT_MAX_DATA_URI_BYTES,
    ReferenceTooLargeError,
    ReferenceTransport,
    UnsupportedTransportError,
    build_input_references,
    build_reference,
    build_video_frame_images,
    encode_data_uri,
    video_references_supported,
)
from .resume import ResumeReport, reservation_for, resume_pending_jobs
from .runner import (
    GenerationOutcome,
    GenerationRequest,
    ImageJobRunner,
    JobRunner,
    RetryPolicy,
    VideoJobRunner,
    map_provider_status,
)

__all__ = [
    "DEFAULT_MAX_DATA_URI_BYTES",
    "Clock",
    "EventPublisher",
    "GenerationOutcome",
    "GenerationRequest",
    "ImageJobRunner",
    "JobEvent",
    "JobRunner",
    "NullEventPublisher",
    "RealClock",
    "ReferenceTooLargeError",
    "ReferenceTransport",
    "ResumeReport",
    "RetryPolicy",
    "UnsupportedTransportError",
    "VideoJobRunner",
    "build_input_references",
    "build_reference",
    "build_video_frame_images",
    "encode_data_uri",
    "map_provider_status",
    "reservation_for",
    "resume_pending_jobs",
    "video_references_supported",
]
