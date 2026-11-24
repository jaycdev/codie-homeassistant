"""Errors shared between the tool runner and the executor."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError

from .api import CodieAuthError, CodieConnectionError, CodieError
from .const import DOMAIN


class ToolError(Exception):
    """A Tool call failed; `code` goes back to the Codie in the `failed` result."""

    status = "failed"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ToolNeedsClarification(ToolError):
    """The request matched more than one target, so the Character should ask which one."""

    status = "needs_clarification"


def failed(key: str, **placeholders: str) -> HomeAssistantError:
    """A Home Assistant error showing the translation `key` with `placeholders`."""
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders
    )


def unreachable_error(err: CodieError, prefix: str) -> HomeAssistantError | None:
    """The error for a Codie that cannot be reached or rejects us, or None for any other failure.

    The messages are `{prefix}_cannot_connect` and `{prefix}_auth_failed`.
    """
    if isinstance(err, CodieConnectionError):
        return failed(f"{prefix}_cannot_connect")
    if isinstance(err, CodieAuthError):
        return failed(f"{prefix}_auth_failed")
    return None
