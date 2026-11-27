"""The README stays in step with what the integration offers."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

from custom_components.codie.tools import TOOLS

README = Path(__file__).parent.parent / "README.md"


def test_readme_lists_every_tool() -> None:
    """The README's Tool table names exactly the Tools in the catalog."""
    listed = set(re.findall(r"^\| `([a-z_]+)` \|", README.read_text("utf-8"), re.MULTILINE))
    assert listed == {tool.name for tool in TOOLS}


def test_readme_offers_every_blueprint_for_import() -> None:
    """Each blueprint has an "Import blueprint" button pointing at its file."""
    blueprints = README.parent / "custom_components" / "codie" / "blueprints" / "automation"
    urls = {
        unquote(url)
        for url in re.findall(
            r"https://my\.home-assistant\.io/redirect/blueprint_import/\?blueprint_url=([^)\s]+)",
            README.read_text("utf-8"),
        )
    }
    assert urls == {
        f"https://github.com/jaycdev/codie-homeassistant/blob/main/custom_components/codie/blueprints/automation/{path.name}"
        for path in blueprints.glob("*.yaml")
    }
