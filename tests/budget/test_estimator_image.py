from decimal import Decimal

from higgshole.budget.estimator import EstimateUnavailable, estimate_image_cost
from higgshole.orclient.types import ImageModel

RECRAFT = ImageModel.from_api(
    {
        "id": "recraft/recraft-v4.1",
        "supported_parameters": {"input_references": {"type": "range", "min": 0, "max": 1}},
    }
)

RIVERFLOW = ImageModel.from_api(
    {
        "id": "riverflow/riverflow-v2-pro",
        "supported_parameters": {"input_references": {"type": "range", "min": 0, "max": 5}},
    }
)

FLUX = ImageModel.from_api({"id": "black-forest-labs/flux.2-pro"})

GPT_IMAGE = ImageModel.from_api(
    {
        "id": "openai/gpt-image-2",
        "supported_parameters": {
            "quality": {"type": "enum", "values": ["auto", "low", "medium", "high"]}
        },
    }
)

FLAT = [{"billable": "output_image", "unit": "image", "cost_usd": 0.04}]

RIVERFLOW_PRICING = [
    {"billable": "output_image", "unit": "image", "cost_usd": 0.06},
    {"billable": "input_reference", "unit": "image", "cost_usd": 0.20},
]

MEGAPIXEL = [{"billable": "output_image", "unit": "megapixel", "cost_usd": 0.03}]

# Spec section 3.1: OpenAI GPT-Image, Gemini and MAI price in tokens.
TOKEN = [{"billable": "output_image", "unit": "token", "cost_usd": 3e-05}]

VARIANTS = [
    {"billable": "output_image", "unit": "image", "cost_usd": 0.04, "variant": "standard"},
    {"billable": "output_image", "unit": "image", "cost_usd": 0.08, "variant": "hd"},
]

WEIRD = [{"billable": "output_image", "unit": "furlong", "cost_usd": 0.04}]


def test_recraft_flat_image_price_is_exact():
    estimate = estimate_image_cost(RECRAFT, FLAT)

    assert estimate.amount == Decimal("0.04")
    assert estimate.reason is None


def test_riverflow_input_references_are_added():
    estimate = estimate_image_cost(RIVERFLOW, RIVERFLOW_PRICING, reference_count=1)

    assert estimate.amount == Decimal("0.26")


def test_five_references_add_a_dollar():
    # Spec section 3.2: input-side items are material and must be included.
    estimate = estimate_image_cost(RIVERFLOW, RIVERFLOW_PRICING, reference_count=5)

    assert estimate.amount == Decimal("1.06")


def test_megapixel_pricing_uses_the_requested_dimensions():
    estimate = estimate_image_cost(FLUX, MEGAPIXEL, width=1920, height=1080)

    assert estimate.amount == Decimal("0.062208")


def test_megapixel_pricing_without_dimensions_is_a_missing_axis():
    estimate = estimate_image_cost(FLUX, MEGAPIXEL)

    assert estimate.amount is None
    assert estimate.reason is EstimateUnavailable.MISSING_AXIS


def test_token_priced_models_are_not_estimable():
    estimate = estimate_image_cost(GPT_IMAGE, TOKEN, quality="high")

    assert estimate.amount is None
    assert estimate.reason is EstimateUnavailable.TOKEN_PRICED


def test_quality_variants_select_the_matching_line_item():
    estimate = estimate_image_cost(RECRAFT, VARIANTS, quality="hd")

    assert estimate.amount == Decimal("0.08")


def test_an_unmatched_quality_variant_has_no_matching_sku():
    estimate = estimate_image_cost(RECRAFT, VARIANTS, quality="ultra")

    assert estimate.reason is EstimateUnavailable.NO_MATCHING_SKU


def test_ambiguous_variants_without_a_quality_are_reported():
    estimate = estimate_image_cost(RECRAFT, VARIANTS)

    assert estimate.reason is EstimateUnavailable.AMBIGUOUS_AXES


def test_empty_pricing_reports_no_pricing_data():
    estimate = estimate_image_cost(RECRAFT, [])

    assert estimate.reason is EstimateUnavailable.NO_PRICING_DATA


def test_an_unknown_unit_is_reported():
    estimate = estimate_image_cost(RECRAFT, WEIRD)

    assert estimate.reason is EstimateUnavailable.UNKNOWN_UNIT


def test_zero_references_add_nothing():
    estimate = estimate_image_cost(RIVERFLOW, RIVERFLOW_PRICING, reference_count=0)

    assert estimate.amount == Decimal("0.06")


def test_estimate_never_returns_a_fabricated_zero():
    # Spec section 3.4: zero would let the daily cap silently never trip.
    for estimate in (
        estimate_image_cost(GPT_IMAGE, TOKEN),
        estimate_image_cost(RECRAFT, []),
        estimate_image_cost(FLUX, MEGAPIXEL),
    ):
        assert estimate.amount is None
        assert estimate.amount != Decimal("0")
