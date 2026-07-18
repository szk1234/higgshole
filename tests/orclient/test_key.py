import httpx
import pytest
import respx

from higgshole.orclient.client import looks_like_openrouter_key
from higgshole.orclient.errors import ProviderError

BASE_URL = "https://openrouter.ai/api/v1"


@respx.mock
async def test_key_status_returns_authoritative_budget_figures(client):
    # Spec section 3.2: this call is free and is the source of truth for
    # remaining budget, in preference to the local ledger.
    respx.get(f"{BASE_URL}/key").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "limit": 100,
                    "limit_remaining": 74.5,
                    "limit_reset": "monthly",
                    "usage": 25.5,
                    "usage_daily": 25.5,
                    "is_free_tier": False,
                }
            },
        )
    )

    status = await client.get_key_status()

    assert str(status.limit_remaining) == "74.5"
    assert str(status.usage_daily) == "25.5"


@respx.mock
async def test_validate_key_is_true_for_a_working_key(client):
    respx.get(f"{BASE_URL}/key").mock(
        return_value=httpx.Response(200, json={"data": {"limit": None}})
    )

    assert await client.validate_key() is True


@respx.mock
async def test_validate_key_is_false_for_a_rejected_key(client):
    respx.get(f"{BASE_URL}/key").mock(
        return_value=httpx.Response(401, json={"error": {"message": "User not found."}})
    )

    assert await client.validate_key() is False


@respx.mock
async def test_validate_key_propagates_non_auth_failures(client):
    # A provider outage must not be reported to the operator as a bad key.
    respx.get(f"{BASE_URL}/key").mock(
        return_value=httpx.Response(503, json={"error": {"message": "down"}})
    )

    with pytest.raises(ProviderError):
        await client.validate_key()


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("sk-or-v1-abcdef0123456789", True),
        ("sk-proj-abcdef0123456789", False),  # an OpenAI key
        ("sk-or-abc", False),  # missing the v1 segment
        ("sk-or-v1-", False),  # prefix with no payload
        ("", False),
        ("   ", False),
    ],
)
def test_key_shape_is_checked_before_submission(candidate, expected):
    # The server's messages do not clearly distinguish a foreign key from an
    # absent one, so the shape check happens locally (spec section 7).
    assert looks_like_openrouter_key(candidate) is expected
