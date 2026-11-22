"""Turn Assist's probatio schemas into the Codie's restricted JSON Schema.

The Codie accepts an object root with the types object, array, string, integer, number,
boolean and null; `properties`, `required`, `additionalProperties: false`, `items`, `enum`,
inclusive numeric bounds, length and item bounds, and `description`. It rejects references,
unions, `allOf`, type lists, patterns, exclusive bounds, more than 6 levels, more than 64
properties, and more than 8 KiB.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Any, TypeGuard

import probatio

MAX_DEPTH = 6
MAX_PROPERTIES = 64
MAX_SCHEMA_BYTES = 8 * 1024

_KEPT_KEYWORDS = frozenset(
    {
        "type",
        "description",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
    }
)
_UNIONS = ("anyOf", "oneOf")
# Union branches with only these keys narrow the parent instead of offering another type.
_CONSTRAINTS = frozenset({"required"})
_FLOORS = frozenset({"minimum", "minLength", "minItems", "exclusiveMinimum"})
_CEILINGS = frozenset({"maximum", "maxLength", "maxItems", "exclusiveMaximum"})
# Lower is simpler: a union or type list collapses to its lowest-ranked member, first on a tie.
_TYPE_RANK = {"string": 0, "integer": 0, "number": 0, "boolean": 0, "null": 1, "array": 2}
_OPENAPI_VERSION = "3.1.0"


class SchemaLimitError(ValueError):
    """A schema is outside what the Codie accepts."""


def from_probatio(
    schema: probatio.Schema, custom_serializer: Callable[[Any], Any] | None = None
) -> dict[str, Any]:
    """Convert an Assist tool's parameter schema; `custom_serializer` renders its selectors."""
    converted = probatio.to_openapi(
        schema, custom_serializer=custom_serializer, openapi_version=_OPENAPI_VERSION
    )
    result = restrict(converted)
    check_limits(result)
    return result


def restrict(schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a JSON Schema into the supported subset."""
    schema = _normalize(_fold_all_of(schema))
    for union in _UNIONS:
        if union in schema and (branch := _pick_branch(schema[union])) is not None:
            rest = {key: value for key, value in schema.items() if key != union}
            return restrict(_merge([rest, branch]))

    result = {key: value for key, value in schema.items() if key in _KEPT_KEYWORDS}
    # A nullable enum lists null; the type has already dropped it.
    if enum := [value for value in result.get("enum", []) if value is not None]:
        result["enum"] = enum
    else:
        result.pop("enum", None)
    _restrict_bounds(schema, result)

    if result.get("type") == "object" or "properties" in schema:
        result["type"] = "object"
        result["properties"] = {
            name: restrict(sub) for name, sub in schema.get("properties", {}).items()
        }
        if required := [
            name for name in schema.get("required", []) if name in result["properties"]
        ]:
            result["required"] = required
        result["additionalProperties"] = False
    elif result.get("type") == "array" and isinstance(schema.get("items"), dict):
        result["items"] = restrict(schema["items"])
    return result


def check_limits(schema: dict[str, Any]) -> None:
    """Raise `SchemaLimitError` unless `schema` fits the Codie's depth, size and property caps."""
    depth, properties = _measure(schema)
    if depth > MAX_DEPTH:
        raise SchemaLimitError(f"schema depth {depth} exceeds {MAX_DEPTH}")
    if properties > MAX_PROPERTIES:
        raise SchemaLimitError(f"{properties} properties exceed {MAX_PROPERTIES}")
    size = len(json.dumps(schema, separators=(",", ":"), ensure_ascii=False).encode())
    if size > MAX_SCHEMA_BYTES:
        raise SchemaLimitError(f"schema is {size} bytes, over {MAX_SCHEMA_BYTES} bytes")


def _simplest_type(types: list[str]) -> str:
    """Null is only the answer when it is the only type."""
    return min(
        [name for name in types if name != "null"] or types,
        key=lambda name: _TYPE_RANK.get(name, 3),
        default="",
    )


def _branch_type(branch: dict[str, Any]) -> str:
    type_ = branch.get("type", "")
    return _simplest_type(type_) if isinstance(type_, list) else str(type_)


def _rank(branch: dict[str, Any]) -> int:
    return _TYPE_RANK.get(_branch_type(branch), 3)


def _pick_branch(branches: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The simplest branch of a union, or None if the branches only add constraints.

    An object whose `anyOf` lists alternative `required` sets is not a union of types.
    """
    candidates = [b for b in branches if isinstance(b, dict) and not (b and set(b) <= _CONSTRAINTS)]
    typed = [b for b in candidates if _branch_type(b) != "null"] or candidates
    return min(typed, key=_rank, default=None)


def _normalize(schema: dict[str, Any]) -> dict[str, Any]:
    """Collapse a type list and turn OpenAPI 3.0's boolean exclusive flags into 3.1 values."""
    schema = dict(schema)
    if isinstance(schema.get("type"), list):
        if type_ := _simplest_type(schema["type"]):
            schema["type"] = type_
        else:
            del schema["type"]
    for inclusive, exclusive in (("minimum", "exclusiveMinimum"), ("maximum", "exclusiveMaximum")):
        if isinstance(flag := schema.get(exclusive), bool):
            del schema[exclusive]
            if flag and inclusive in schema:
                schema[exclusive] = schema.pop(inclusive)
    return schema


def _merge(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine schemas that must all hold: the first wins, except that bounds keep the tighter."""
    merged: dict[str, Any] = {}
    for part in map(_normalize, parts):
        for key, value in part.items():
            if key not in merged:
                merged[key] = value
            elif key == "type" and {merged[key], value} == {"integer", "number"}:
                merged[key] = "integer"
            elif key in _FLOORS and _is_number(merged[key]) and _is_number(value):
                merged[key] = max(merged[key], value)
            elif key in _CEILINGS and _is_number(merged[key]) and _is_number(value):
                merged[key] = min(merged[key], value)
            elif key == "enum":
                merged[key] = [item for item in merged[key] if item in value]
            elif key == "required":
                merged[key] = [*merged[key], *(name for name in value if name not in merged[key])]
            elif key == "properties":
                merged[key] = {**value, **merged[key]}
    return merged


def _fold_all_of(schema: dict[str, Any]) -> dict[str, Any]:
    if "allOf" not in schema:
        return schema
    rest = {key: value for key, value in schema.items() if key != "allOf"}
    return _merge([rest, *(_fold_all_of(part) for part in schema["allOf"])])


def _is_number(value: Any) -> TypeGuard[float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _next_above(value: float) -> int:
    return math.floor(value) + 1


def _next_below(value: float) -> int:
    return math.ceil(value) - 1


def _restrict_bounds(schema: dict[str, Any], result: dict[str, Any]) -> None:
    """Fold exclusive bounds into `result`'s inclusive ones, which are all the Codie takes.

    An integer's exclusive bound becomes the next integer inside it. Any other type drops the
    bound, so it is never loosened into an inclusive one.
    """
    if result.get("type") != "integer":
        return
    for inclusive, exclusive, tighter, step in (
        ("minimum", "exclusiveMinimum", max, _next_above),
        ("maximum", "exclusiveMaximum", min, _next_below),
    ):
        bound = schema.get(exclusive)
        if _is_number(bound) and math.isfinite(bound):
            result[inclusive] = tighter(result.get(inclusive, step(bound)), step(bound))


def _measure(schema: dict[str, Any]) -> tuple[int, int]:
    """Return the schema's depth (a leaf is 1) and its total property count."""
    children: list[dict[str, Any]] = list(schema.get("properties", {}).values())
    if isinstance(schema.get("items"), dict):
        children.append(schema["items"])
    depth = 1
    properties = len(schema.get("properties", {}))
    for child in children:
        child_depth, child_properties = _measure(child)
        depth = max(depth, child_depth + 1)
        properties += child_properties
    return depth, properties
