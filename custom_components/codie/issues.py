"""Repair issues for problems the user can fix, one set per config entry."""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .api import CodieApiError
from .const import DOMAIN
from .tools import TOOLS, LeftOut, LeftOutKind

ISSUE_CLOCK_SKEW = "clock_skew"
ISSUE_CATALOG_REJECTED = "catalog_rejected"
# The Tools left out for a reason worth repairing, with the translation key of each. A missing
# device intent and the Timer Tools for an unsupported language are left out by design. One
# issue per Tool, so each clears on its own.
_TOOL_ISSUES = {
    LeftOutKind.SCHEMA_DOES_NOT_FIT: "tool_unsupported",
    LeftOutKind.CORE_INTENT_MISSING: "core_tool_missing",
}

# Statuses that say nothing about the catalog: credentials or clock, a stale revision (which
# is retried), and a Codie that is busy or failing rather than refusing.
_NOT_A_REFUSAL = frozenset(
    {
        HTTPStatus.UNAUTHORIZED,
        HTTPStatus.REQUEST_TIMEOUT,
        HTTPStatus.CONFLICT,
        HTTPStatus.TOO_MANY_REQUESTS,
    }
)


def is_refusal(err: CodieApiError) -> bool:
    """Whether the Codie refused the catalog itself, so sending it again would not help."""
    return err.status < HTTPStatus.INTERNAL_SERVER_ERROR and err.status not in _NOT_A_REFUSAL


class CodieIssues:
    """Creates and clears the repair issues of one Codie entry.

    Issue ids start with the entry id, so two Codies never share an issue.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
        self._hass = hass
        self._entry = entry

    @callback
    def clock_skew(self, skewed: bool) -> None:
        """The Codie rejected a request for its time, or a signed request worked again."""
        if skewed:
            self._create(ISSUE_CLOCK_SKEW, ISSUE_CLOCK_SKEW)
        else:
            self._delete(ISSUE_CLOCK_SKEW)

    @callback
    def catalog_refused(self, err: CodieApiError | None) -> None:
        """The Codie answered the catalog's PUT with `err`, or None if it accepted it."""
        if err is None:
            self._delete(ISSUE_CATALOG_REJECTED)
        elif is_refusal(err):
            self._create(ISSUE_CATALOG_REJECTED, ISSUE_CATALOG_REJECTED, error=_describe(err))

    @callback
    def tools_left_out(self, left_out: Mapping[str, LeftOut]) -> None:
        """Raise an issue for each Tool in `left_out` worth repairing; clear it for the rest."""
        for tool in TOOLS:
            issue_id = f"tool_left_out_{tool.name}"
            left = left_out.get(tool.name)
            if left is None or (key := _TOOL_ISSUES.get(left.kind)) is None:
                self._delete(issue_id)
                continue
            self._create(
                issue_id,
                key,
                tool=tool.name,
                assist_tool=left.assist_name or "",
                error=left.reason,
            )

    def _create(self, issue_id: str, translation_key: str, **placeholders: str) -> None:
        if self._hass.config_entries.async_get_entry(self._entry.entry_id) is None:
            # A request still in flight when the entry was removed; Home Assistant refuses to
            # link an issue to an entry it no longer has.
            return
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            f"{self._entry.entry_id}_{issue_id}",
            # Linked, so Home Assistant removes it with the entry.
            config_entry_id=self._entry.entry_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=translation_key,
            translation_placeholders={"name": self._entry.title, **placeholders},
        )

    def _delete(self, issue_id: str) -> None:
        ir.async_delete_issue(self._hass, DOMAIN, f"{self._entry.entry_id}_{issue_id}")


def _describe(err: CodieApiError) -> str:
    """The status, code and message of `err`, leaving out the parts the Codie did not send."""
    head = " ".join(str(part) for part in (err.status, err.code) if part)
    return f"{head}: {err.message}" if err.message else head


@callback
def async_clear_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Remove every issue whose id belongs to the entry `entry_id`."""
    issues = ir.async_get(hass).issues
    for domain, issue_id in list(issues):
        if domain == DOMAIN and issue_id.startswith(f"{entry_id}_"):
            ir.async_delete_issue(hass, DOMAIN, issue_id)
