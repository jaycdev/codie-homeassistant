"""Turn Assist's voluptuous schemas into the Codie's restricted JSON Schema.

The Codie accepts an object root with the types object, array, string, integer, number,
boolean and null; `properties`, `required`, `additionalProperties: false`, `items`, `enum`,
numeric, length and item bounds, and `description`. It rejects references, unions and patterns,
more than 6 levels, more than 64 properties, and more than 8 KiB.
"""

from __future__ import annotations

import json
from typing import Any

import voluptuous as vol
import voluptuous_openapi

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
# Lower is simpler: a union collapses to the lowest-ranked branch, the first one on a tie.
_TYPE_RANK = {"string": 0, "integer": 0, "number": 0, "boolean": 0, "null": 1, "array": 2}


class SchemaLimitError(ValueError):
    """A schema is outside what the Codie accepts."""


def from_voluptuous(schema: vol.Schema) -> dict[str, Any]:
    """Convert an Assist tool's parameter schema."""
    converted = voluptuous_openapi.convert(schema)
    result = restrict(converted)
    check_limits(result)
    return result


def restrict(schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a JSON Schema into the supported subset."""
    for union in _UNIONS:
        if union in schema:
            branch = min(schema[union], key=_rank)
            schema = {**branch, **{k: v for k, v in schema.items() if k == "description"}}
            break

    result = {key: value for key, value in schema.items() if key in _KEPT_KEYWORDS}
    if not result.get("enum"):
        result.pop("enum", None)

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


def _rank(branch: dict[str, Any]) -> int:
    return _TYPE_RANK.get(branch.get("type", ""), 3)


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
