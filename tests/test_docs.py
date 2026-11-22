"""The README stays in step with what the integration offers."""

from __future__ import annotations

import re
from pathlib import Path

from custom_components.codie.tools import TOOLS

README = Path(__file__).parent.parent / "README.md"


def test_readme_lists_every_tool() -> None:
    """The README's Tool table names exactly the Tools in the catalog."""
    listed = set(re.findall(r"^\| `([a-z_]+)` \|", README.read_text("utf-8"), re.MULTILINE))
    assert listed == {tool.name for tool in TOOLS}
