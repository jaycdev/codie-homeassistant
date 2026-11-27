"""The integration puts its blueprints where Home Assistant's Blueprints page finds them."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.automation.helpers import async_get_blueprints
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.codie.const import DOMAIN

SHIPPED = sorted(
    path.name
    for path in (
        Path(__file__).parent.parent / "custom_components" / "codie" / "blueprints" / "automation"
    ).glob("*.yaml")
)


async def test_blueprints_are_listed_after_setup(hass: HomeAssistant, tmp_path: Path) -> None:
    """A fresh install, as HACS leaves it, offers every blueprint."""
    hass.config.config_dir = str(tmp_path)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    listed = await async_get_blueprints(hass).async_get_blueprints()

    assert SHIPPED == ["appliance_finished.yaml", "doorbell.yaml", "morning_briefing.yaml"]
    ours = sorted(Path(path).name for path in listed if Path(path).parts[0] == DOMAIN)
    assert ours == SHIPPED
    assert all(not isinstance(blueprint, Exception) for blueprint in listed.values())


async def test_a_copy_already_there_is_kept(hass: HomeAssistant, tmp_path: Path) -> None:
    """A blueprint the user edited is not overwritten."""
    hass.config.config_dir = str(tmp_path)
    edited = tmp_path / "blueprints" / "automation" / DOMAIN / "doorbell.yaml"
    edited.parent.mkdir(parents=True)
    edited.write_text("edited", "utf-8")

    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    assert edited.read_text("utf-8") == "edited"
    assert sorted(path.name for path in edited.parent.glob("*.yaml")) == SHIPPED
