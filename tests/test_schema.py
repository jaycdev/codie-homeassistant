"""Converting Assist's probatio schemas into the Codie's restricted JSON Schema."""

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
    from_probatio,
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


def test_a_null_union_collapses_to_the_other_type() -> None:
    schema = {"anyOf": [{"type": "null"}, {"type": "string"}], "description": "Name"}

    assert restrict(schema) == {"type": "string", "description": "Name"}


def test_a_type_list_collapses_to_its_simplest_type() -> None:
    assert restrict({"type": ["null", "string"]}) == {"type": "string"}
    assert restrict({"type": ["array", "integer", "null"]}) == {"type": "integer"}
    assert restrict({"type": ["number", "string"]}) == {"type": "number"}
    assert restrict({"type": ["null"]}) == {"type": "null"}
    assert restrict({"anyOf": [{"type": ["null", "boolean"]}, {"type": "array"}]}) == {
        "type": "boolean"
    }


def test_null_is_dropped_from_an_enum() -> None:
    assert restrict({"type": ["string", "null"], "enum": ["a", None]}) == {
        "type": "string",
        "enum": ["a"],
    }
    assert restrict({"type": ["null"], "enum": [None]}) == {"type": "null"}


def test_integer_exclusive_bounds_become_inclusive() -> None:
    assert restrict({"type": "integer", "exclusiveMinimum": 0, "exclusiveMaximum": 10}) == {
        "type": "integer",
        "minimum": 1,
        "maximum": 9,
    }
    assert restrict({"type": "integer", "exclusiveMinimum": 0.5, "exclusiveMaximum": 9.5}) == {
        "type": "integer",
        "minimum": 1,
        "maximum": 9,
    }


def test_the_tighter_of_an_inclusive_and_an_exclusive_bound_wins() -> None:
    both = {"type": "integer", "minimum": 5, "exclusiveMinimum": 0, "maximum": 20}

    assert restrict({**both, "exclusiveMaximum": 10}) == {
        "type": "integer",
        "minimum": 5,
        "maximum": 9,
    }
    assert restrict({**both, "minimum": 0, "exclusiveMinimum": 5, "maximum": 6}) == {
        "type": "integer",
        "minimum": 6,
        "maximum": 6,
    }


def test_number_exclusive_bounds_are_dropped() -> None:
    schema = {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 10, "maximum": 20}

    assert restrict(schema) == {"type": "number", "maximum": 20}


def test_the_boolean_exclusive_form_never_loosens_a_bound() -> None:
    assert restrict({"type": "integer", "minimum": 0, "exclusiveMinimum": True}) == {
        "type": "integer",
        "minimum": 1,
    }
    assert restrict({"type": "integer", "maximum": 10, "exclusiveMaximum": True}) == {
        "type": "integer",
        "maximum": 9,
    }
    assert restrict({"type": "number", "minimum": 0, "exclusiveMinimum": True, "maximum": 5}) == {
        "type": "number",
        "maximum": 5,
    }
    assert restrict({"type": "number", "minimum": 0, "exclusiveMinimum": False}) == {
        "type": "number",
        "minimum": 0,
    }


def test_a_nullable_array_or_object_is_not_collapsed_to_null() -> None:
    items = {"type": "array", "items": {"type": "string"}}

    assert restrict({"type": ["array", "null"], "items": {"type": "string"}}) == items
    assert restrict({"anyOf": [items, {"type": "null"}]}) == items
    assert restrict({"anyOf": [{"type": "null"}, {}]}) == {}
    assert restrict({"type": ["object", "null"], "properties": {"a": {"type": "string"}}}) == {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": False,
    }
    assert restrict({"anyOf": [{"type": "null"}, {"type": "null"}]}) == {"type": "null"}


def test_collapsing_a_type_list_comes_before_the_exclusive_bound_rule() -> None:
    assert restrict({"type": ["integer", "null"], "exclusiveMinimum": 0}) == {
        "type": "integer",
        "minimum": 1,
    }


def test_all_of_is_folded_into_one_schema() -> None:
    schema = {
        "description": "How far",
        "allOf": [{"type": "integer"}, {"type": "number", "minimum": 0, "maximum": 100}],
    }

    assert restrict(schema) == {
        "type": "integer",
        "description": "How far",
        "minimum": 0,
        "maximum": 100,
    }


def test_all_of_keeps_the_tighter_bounds_and_the_common_enum() -> None:
    schema = {
        "allOf": [
            {"type": "integer", "minimum": 0, "maximum": 100, "enum": [1, 2, 3]},
            {"minimum": 10, "maximum": 20, "enum": [3, 2, 9]},
            {"minLength": 1, "maxLength": 9},
            {"minLength": 2, "maxLength": 5, "minItems": 1, "maxItems": 4},
        ]
    }

    assert restrict(schema) == {
        "type": "integer",
        "minimum": 10,
        "maximum": 20,
        "enum": [2, 3],
        "minLength": 2,
        "maxLength": 5,
        "minItems": 1,
        "maxItems": 4,
    }


def test_all_of_exclusive_bounds_follow_the_merged_type() -> None:
    assert restrict(
        {"allOf": [{"type": "number", "exclusiveMinimum": 0}, {"type": "integer"}]}
    ) == {"type": "integer", "minimum": 1}
    assert restrict({"allOf": [{"type": "number", "exclusiveMinimum": 0}, {"maximum": 5}]}) == {
        "type": "number",
        "maximum": 5,
    }
    assert restrict(
        {"allOf": [{"type": "integer", "minimum": 0, "exclusiveMinimum": True}, {"minimum": 0}]}
    ) == {"type": "integer", "minimum": 1}


def test_all_of_inside_properties_and_arrays_is_folded() -> None:
    schema = {
        "type": "object",
        "properties": {
            "level": {"allOf": [{"type": "integer"}, {"minimum": 0}]},
            "levels": {"type": "array", "items": {"allOf": [{"type": "integer"}, {"maximum": 9}]}},
        },
    }

    assert restrict(schema)["properties"] == {
        "level": {"type": "integer", "minimum": 0},
        "levels": {"type": "array", "items": {"type": "integer", "maximum": 9}},
    }


def test_an_object_whose_union_only_adds_required_sets_stays_an_object() -> None:
    properties = {"a": {"type": "string"}, "b": {"type": "string"}, "c": {"type": "integer"}}
    schema = {
        "type": "object",
        "properties": properties,
        "required": ["c"],
        "additionalProperties": False,
        "anyOf": [{"required": ["a"]}, {"required": ["b"]}],
    }

    assert restrict(schema) == {
        "type": "object",
        "properties": properties,
        "required": ["c"],
        "additionalProperties": False,
    }


def test_a_collapsed_union_keeps_the_constraints_beside_it() -> None:
    schema = {
        "description": "Level",
        "anyOf": [{"type": "integer", "minimum": 5}, {"type": "number"}],
        "minimum": 0,
        "maximum": 10,
    }

    assert restrict(schema) == {
        "type": "integer",
        "description": "Level",
        "minimum": 5,
        "maximum": 10,
    }
    assert restrict(
        {"anyOf": [{"type": "integer"}, {"type": "number"}], "exclusiveMinimum": 0}
    ) == {"type": "integer", "minimum": 1}
    assert restrict({"oneOf": [{"type": "string"}, {"type": "null"}], "enum": ["a", None]}) == {
        "type": "string",
        "enum": ["a"],
    }


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


def test_converts_a_probatio_schema() -> None:
    schema = probatio.Schema(
        {
            probatio.Required("temperature"): probatio.Coerce(float),
            probatio.Optional("name"): str,
            probatio.Optional("level"): probatio.All(probatio.Coerce(int), probatio.Range(0, 100)),
            probatio.Optional("mode"): probatio.In(["a", "b"]),
        }
    )

    assert from_probatio(schema) == {
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


def test_converts_nullable_and_exclusive_probatio_validators() -> None:
    schema = probatio.Schema(
        {
            probatio.Optional("name"): probatio.Any(None, str),
            probatio.Optional("mode"): probatio.In(["a", None]),
            probatio.Optional("count"): probatio.All(
                probatio.Coerce(int), probatio.Range(min=0, min_included=False)
            ),
            probatio.Optional("amount"): probatio.All(
                probatio.Coerce(float), probatio.Range(min=0, min_included=False)
            ),
        }
    )

    assert from_probatio(schema)["properties"] == {
        "name": {"type": "string"},
        "mode": {"type": "string", "enum": ["a"]},
        "count": {"type": "integer", "minimum": 1},
        "amount": {"type": "number"},
    }


def test_converts_all_of_nullable_collections_and_alternative_required_keys() -> None:
    schema = probatio.Schema(
        {
            probatio.Optional("percent"): probatio.All(
                probatio.Coerce(int), probatio.Range(0, 100), probatio.Range(10, 20)
            ),
            probatio.Optional("names"): probatio.Any([str], None),
            probatio.Optional("level"): probatio.All(
                probatio.Any(int, float), probatio.Range(0, 10)
            ),
            probatio.Optional("steps"): probatio.All(
                probatio.Any(int, float), probatio.Range(min=0, min_included=False)
            ),
        }
    )

    assert from_probatio(schema)["properties"] == {
        "percent": {"type": "integer", "minimum": 10, "maximum": 20},
        "names": {"type": "array", "items": {"type": "string"}},
        "level": {"type": "integer", "minimum": 0, "maximum": 10},
        "steps": {"type": "integer", "minimum": 1},
    }

    either = probatio.Schema(
        {probatio.Required(probatio.Any("a", "b")): str, probatio.Optional("c"): int}
    )

    assert from_probatio(either) == {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}, "c": {"type": "integer"}},
        "additionalProperties": False,
    }


def test_the_serializer_renders_what_probatio_does_not_know() -> None:
    class Opaque:
        pass

    def serializer(schema: Any) -> Any:
        return {"type": "integer"} if isinstance(schema, Opaque) else probatio.UNSUPPORTED

    schema = probatio.Schema({probatio.Optional("x"): Opaque()})

    assert from_probatio(schema)["properties"] == {"x": {}}
    assert from_probatio(schema, serializer)["properties"] == {"x": {"type": "integer"}}


def test_empty_schema_is_an_empty_closed_object() -> None:
    assert from_probatio(probatio.Schema({})) == {
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
