import pytest

from higgshole.mcp_server import (
    TOOL_DESCRIPTIONS,
    TOOL_NAMES,
    TOOL_SCHEMAS,
    build_tools,
    handle_list_tools,
)

EXPECTED = {
    "list_models",
    "generate_image",
    "generate_video",
    "get_job",
    "upload_asset",
    "list_media",
    "get_media",
    "delete_media",
    "list_projects",
    "create_project",
    "get_budget",
}


def test_exactly_the_eleven_specified_tools_are_declared():
    # Spec section 6.2 enumerates eleven tools; a twelfth would be business
    # logic that the REST API does not also expose.
    assert set(TOOL_NAMES) == EXPECTED
    assert len(TOOL_NAMES) == 11


def test_every_tool_carries_a_description_and_a_schema():
    for name in TOOL_NAMES:
        assert TOOL_DESCRIPTIONS[name].strip(), name
        assert name in TOOL_SCHEMAS, name


def test_every_input_schema_is_a_closed_object_schema():
    for name, schema in TOOL_SCHEMAS.items():
        assert schema["type"] == "object", name
        assert isinstance(schema["properties"], dict), name
        assert schema["additionalProperties"] is False, name


def test_every_required_field_is_also_declared_as_a_property():
    for name, schema in TOOL_SCHEMAS.items():
        for field in schema.get("required", []):
            assert field in schema["properties"], f"{name}.{field}"


def test_generate_image_declares_its_generation_parameters():
    schema = TOOL_SCHEMAS["generate_image"]

    assert schema["required"] == ["model", "prompt"]
    for field in (
        "project",
        "aspect_ratio",
        "resolution",
        "size",
        "quality",
        "output_format",
        "seed",
        "input_reference_asset_ids",
        "n",
    ):
        assert field in schema["properties"], field


def test_generate_image_pins_n_to_one():
    # Spec section 5.5: batch generation is not supported.
    n_schema = TOOL_SCHEMAS["generate_image"]["properties"]["n"]

    assert n_schema["maximum"] == 1
    assert n_schema["default"] == 1


def test_generate_video_declares_both_frame_slots():
    props = TOOL_SCHEMAS["generate_video"]["properties"]

    assert "first_frame_asset_id" in props
    assert "last_frame_asset_id" in props
    assert "generate_audio" in props
    assert "duration" in props


def test_get_job_declares_a_bounded_long_poll():
    props = TOOL_SCHEMAS["get_job"]["properties"]

    assert props["wait_seconds"]["default"] == 0
    assert props["wait_seconds"]["minimum"] == 0
    assert props["wait_seconds"]["maximum"] >= 1


def test_list_models_constrains_kind_to_the_two_media_types():
    assert TOOL_SCHEMAS["list_models"]["properties"]["kind"]["enum"] == ["image", "video"]


@pytest.mark.parametrize("name", ["list_projects", "get_budget"])
def test_argument_free_tools_declare_no_properties(name):
    assert TOOL_SCHEMAS[name]["properties"] == {}
    assert TOOL_SCHEMAS[name].get("required", []) == []


async def test_the_list_tools_handler_returns_mcp_tool_objects():
    tools = await handle_list_tools()

    assert {tool.name for tool in tools} == EXPECTED
    assert all(tool.inputSchema["type"] == "object" for tool in tools)
    assert tools == build_tools()
