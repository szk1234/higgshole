import pytest

from higgshole.orclient.errors import (
    AuthError,
    IndeterminateError,
    InsufficientCreditsError,
    InvalidRequestError,
    ModerationError,
    OpenRouterError,
    ProviderError,
    RateLimitError,
    error_from_response,
)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, InvalidRequestError),
        (401, AuthError),
        (402, InsufficientCreditsError),
        (429, RateLimitError),
        (500, ProviderError),
        (502, ProviderError),
    ],
)
def test_status_codes_map_to_types(status, expected):
    error = error_from_response(status, {"error": {"message": "boom", "code": status}})

    assert isinstance(error, expected)
    assert error.status_code == status
    assert "boom" in str(error)


def test_moderation_refusal_is_distinct_from_generic_bad_request():
    error = error_from_response(
        400, {"error": {"message": "Content policy violation", "code": 400}}
    )

    assert isinstance(error, ModerationError)


def test_unparseable_body_still_yields_an_error():
    error = error_from_response(503, None)

    assert isinstance(error, ProviderError)
    assert error.status_code == 503


def test_all_errors_share_a_base_type():
    for error_type in (
        AuthError,
        IndeterminateError,
        InsufficientCreditsError,
        InvalidRequestError,
        ModerationError,
        ProviderError,
        RateLimitError,
    ):
        assert issubclass(error_type, OpenRouterError)


def test_indeterminate_error_records_that_a_charge_may_have_occurred():
    error = IndeterminateError("connection reset after submit")

    assert error.may_have_charged is True
