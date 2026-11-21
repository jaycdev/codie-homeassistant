"""Errors shared between the tool runner and the executor."""

from __future__ import annotations


class ToolError(Exception):
    """A Tool call failed; `code` goes back to the Codie in the `failed` result."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
