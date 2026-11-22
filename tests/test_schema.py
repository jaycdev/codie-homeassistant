"""Converting Assist's voluptuous schemas into the Codie's restricted JSON Schema."""

from __future__ import annotations

import json
from typing import Any

import probatio
import pytest

from custom_components.codie.schema import (
    MAX_DEPTH,
    MAX_PROPERTIES,
    MAX_SCHEMA_BYTES,
    SchemaLimitError,
    check_limits,
    from_voluptuous,
    restrict,
)


def test_keeps_only_supported_keywords() -> None:
    schema = {
        "type": "object",
        "title": "Slots",
        "properties": {
            "brightness": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "default": 50,
                "format": "int32",
            },
            "name": {"type": "string", "pattern": "^a", "minLength": 1},
        },
        "required": ["name"],
    }

    assert restrict(schema) == {
        "type": "object",
        "properties": {
            "brightness": {"type": "integer", "minimum": 0, "maximum": 100},
            "name": {"type": "string", "minLength": 1},
        },
        "required": ["name"],
        "additionalProperties": False,
    }


def test_objects_get_closed_and_empty_lists_are_dropped() -> None:
    schema = {
        "type": "object",
        "properties": {"mode": {"type": "string", "enum": []}},
        "required": [],
    }

    assert restrict(schema) == {
        "type": "object",
        "properties": {"mode": {"type": "string"}},
        "additionalProperties": False,
    }


def test_union_collapses_to_its_simplest_branch() -> None:
    schema = {
        "type": "object",
        "properties": {
            "volume": {
                "description": "How loud",
                "anyOf": [{"type": "array", "items": {"type": "integer"}}, {"type": "integer"}],
            },
            "target": {"oneOf": [{"type": "object", "properties": {}}, {"type": "string"}]},
        },
    }

    result = restrict(schema)

    assert result["properties"]["volume"] == {"type": "integer", "description": "How loud"}
    assert result["properties"]["target"] == {"type": "string"}


def test_arrays_keep_their_item_schema() -> None:
    schema = {
        "type": "object",
        "properties": {"domain": {"type": "array", "items": {"type": "string"}, "maxItems": 3}},
    }

    assert restrict(schema)["properties"]["domain"] == {
        "type": "array",
        "items": {"type": "string"},
        "maxItems": 3,
    }


def test_converts_a_voluptuous_schema() -> None:
    schema = probatio.Schema(
        {
            probatio.Required("temperature"): probatio.Coerce(float),
            probatio.Optional("name"): str,
            probatio.Optional("level"): probatio.All(probatio.Coerce(int), probatio.Range(0, 100)),
            probatio.Optional("mode"): probatio.In(["a", "b"]),
        }
    )

    assert from_voluptuous(schema) == {
        "type": "object",
        "properties": {
            "temperature": {"type": "number"},
            "name": {"type": "string"},
            "level": {"type": "integer", "minimum": 0, "maximum": 100},
            "mode": {"type": "string", "enum": ["a", "b"]},
        },
        "required": ["temperature"],
        "additionalProperties": False,
    }


def test_empty_schema_is_an_empty_closed_object() -> None:
    assert from_voluptuous(probatio.Schema({})) == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def test_limits_accept_a_schema_at_the_edge() -> None:
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(MAX_DEPTH - 1):
        schema = {"type": "object", "properties": {"x": schema}, "additionalProperties": False}

    check_limits(schema)


def test_limits_reject_a_schema_that_is_too_deep() -> None:
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(MAX_DEPTH):
        schema = {"type": "object", "properties": {"x": schema}, "additionalProperties": False}

    with pytest.raises(SchemaLimitError, match="depth"):
        check_limits(schema)


def test_limits_reject_too_many_properties() -> None:
    schema = {
        "type": "object",
        "properties": {f"p{i}": {"type": "string"} for i in range(MAX_PROPERTIES + 1)},
        "additionalProperties": False,
    }

    with pytest.raises(SchemaLimitError, match="properties"):
        check_limits(schema)


def test_limits_count_properties_across_nested_objects() -> None:
    inner = {
        "type": "object",
        "properties": {f"p{i}": {"type": "string"} for i in range(MAX_PROPERTIES)},
        "additionalProperties": False,
    }
    schema = {"type": "object", "properties": {"inner": inner}, "additionalProperties": False}

    with pytest.raises(SchemaLimitError, match="properties"):
        check_limits(schema)


def test_limits_reject_an_oversized_schema() -> None:
    schema = {
        "type": "object",
        "properties": {"x": {"type": "string", "description": "d" * MAX_SCHEMA_BYTES}},
        "additionalProperties": False,
    }
    assert len(json.dumps(schema).encode()) > MAX_SCHEMA_BYTES

    with pytest.raises(SchemaLimitError, match="bytes"):
        check_limits(schema)
