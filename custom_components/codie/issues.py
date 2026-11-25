"""Repair issues: problems with a Codie that the user can fix, under Settings → Repairs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

ISSUE_CLOCK_SKEW = "clock_skew"
ISSUE_CATALOG_REJECTED = "catalog_rejected"

ISSUE_TOOL_MISSING: Final = "tool_missing"
ISSUE_TOOL_SCHEMA_LIMIT: Final = "tool_schema_limit"

type ToolIssue = Literal["tool_missing", "tool_schema_limit"]
_TOOL_ISSUES: tuple[ToolIssue, ...] = (ISSUE_TOOL_MISSING, ISSUE_TOOL_SCHEMA_LIMIT)


@dataclass(frozen=True)
class LeftOut:
    """Why a Tool from the fixed list is not in the catalog; `placeholders` fill its text."""

    key: ToolIssue
    placeholders: Mapping[str, str]


def _issue_id(key: str, entry: ConfigEntry[Any], tool: str | None = None) -> str:
    """Issue ids are per domain, so each names its entry, and its Tool if it has one."""
    return f"{key}_{entry.entry_id}" + (f"_{tool}" if tool else "")


def _raise(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    issue_id: str,
    key: str,
    severity: ir.IssueSeverity = ir.IssueSeverity.ERROR,
    placeholders: Mapping[str, str] | None = None,
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        config_entry_id=entry.entry_id,
        is_fixable=False,
        severity=severity,
        translation_key=key,
        translation_placeholders={"name": entry.title, **(placeholders or {})},
    )


def _clear(hass: HomeAssistant, issue_id: str) -> None:
    ir.async_delete_issue(hass, DOMAIN, issue_id)


@callback
def async_set_clock_skew(hass: HomeAssistant, entry: ConfigEntry[Any], skewed: bool) -> None:
    """Raise or clear the issue that the clocks of Home Assistant and the Codie differ."""
    issue_id = _issue_id(ISSUE_CLOCK_SKEW, entry)
    if skewed:
        _raise(hass, entry, issue_id, ISSUE_CLOCK_SKEW)
    else:
        _clear(hass, issue_id)


@callback
def async_set_catalog_rejected(
    hass: HomeAssistant, entry: ConfigEntry[Any], error: str | None
) -> None:
    """Raise the issue that the Codie refused the catalog with `error`, or clear it on None."""
    issue_id = _issue_id(ISSUE_CATALOG_REJECTED, entry)
    if error:
        _raise(hass, entry, issue_id, ISSUE_CATALOG_REJECTED, placeholders={"error": error})
    else:
        _clear(hass, issue_id)


@callback
def async_set_tools_left_out(
    hass: HomeAssistant, entry: ConfigEntry[Any], left_out: Mapping[str, LeftOut]
) -> None:
    """Raise one issue for each Tool in `left_out`, and clear the entry's other Tool issues."""
    wanted = {
        _issue_id(reason.key, entry, tool): (tool, reason) for tool, reason in left_out.items()
    }
    # By id, not translation key: issues restored from storage after a restart have none.
    prefixes = tuple(f"{_issue_id(key, entry)}_" for key in _TOOL_ISSUES)
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(prefixes) and issue_id not in wanted:
            _clear(hass, issue_id)
    for issue_id, (tool, reason) in wanted.items():
        _raise(
            hass,
            entry,
            issue_id,
            reason.key,
            ir.IssueSeverity.WARNING,
            {"tool": tool, **reason.placeholders},
        )
