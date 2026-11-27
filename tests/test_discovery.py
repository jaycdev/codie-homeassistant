"""Finding a Codie on the network through its `_codie-config._tcp` mDNS service."""

from __future__ import annotations

import json
from ipaddress import ip_address
from pathlib import Path

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from custom_components.codie.const import CONF_CODE, CONF_LOCAL_SECRET, DEFAULT_PORT, DOMAIN

from .fake_codie import FakeCodie

SERVICE_TYPE = "_codie-config._tcp.local."


def service_info(
    host: str, port: int | None, device_id: str | None, *, version: str = "261118175"
) -> ZeroconfServiceInfo:
    """What a Codie announces, shaped like a real one's TXT record."""
    properties = {"version": version, "ipv4": host}
    if device_id is not None:
        properties["device_id"] = device_id
        properties["name"] = f"Codie-{device_id}-{host}"
    return ZeroconfServiceInfo(
        ip_address=ip_address(host),
        ip_addresses=[ip_address(host)],
        port=port,
        hostname="codie.local.",
        type=SERVICE_TYPE,
        name=f"Codie-abc.{SERVICE_TYPE}",
        properties=properties,
    )


async def discover(hass: HomeAssistant, info: ZeroconfServiceInfo) -> ConfigFlowResult:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
    )


async def add_by_hand(hass: HomeAssistant, codie: FakeCodie) -> config_entries.ConfigEntry:
    """Set up `codie` through the user flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: codie.host, CONF_PORT: codie.port}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: codie.pairing_code}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result["result"]


def test_the_manifest_listens_for_the_codie_service() -> None:
    manifest = json.loads(
        (Path(__file__).parents[1] / "custom_components/codie/manifest.json").read_text()
    )

    assert manifest["zeroconf"] == [SERVICE_TYPE]


async def test_a_new_codie_asks_before_showing_a_code(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    result = await discover(hass, service_info(fake_codie.host, fake_codie.port, "b" * 64))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"] == {"host": fake_codie.host}
    # Nothing is shown on the Codie until the user confirms.
    assert fake_codie.requests_to("/auth/pair/request") == []


async def test_confirming_a_discovered_codie_pairs_it(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    result = await discover(
        hass, service_info(fake_codie.host, fake_codie.port, fake_codie.machine_id)
    )

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"
    assert len(fake_codie.requests_to("/auth/pair/request")) == 1

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: fake_codie.pairing_code}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == fake_codie.machine_id
    assert entry.data[CONF_HOST] == fake_codie.host
    assert entry.data[CONF_PORT] == fake_codie.port
    await hass.async_block_till_done()


async def test_a_discovered_codie_is_titled_by_its_address(hass: HomeAssistant) -> None:
    result = await discover(hass, service_info("10.0.20.108", None, "b" * 64))

    assert result["type"] is FlowResultType.FORM
    flow = hass.config_entries.flow.async_get(result["flow_id"])
    assert flow["context"]["title_placeholders"] == {"host": "10.0.20.108"}


async def test_a_configured_codie_is_not_offered_again(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await add_by_hand(hass, fake_codie)

    result = await discover(
        hass, service_info(fake_codie.host, fake_codie.port, fake_codie.machine_id)
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_a_configured_codie_that_moved_gets_its_new_address(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await add_by_hand(hass, fake_codie)

    result = await discover(hass, service_info("10.0.20.200", 8082, fake_codie.machine_id))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "10.0.20.200"
    assert entry.data[CONF_PORT] == 8082
    await hass.async_block_till_done()


async def test_an_announcement_without_a_port_means_the_default_port(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await add_by_hand(hass, fake_codie)

    await discover(hass, service_info("10.0.20.200", None, fake_codie.machine_id))

    assert entry.data[CONF_PORT] == DEFAULT_PORT
    await hass.async_block_till_done()


async def test_an_ipv6_announcement_leaves_a_configured_codie_alone(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await add_by_hand(hass, fake_codie)

    result = await discover(hass, service_info("fe80::1", 8081, fake_codie.machine_id))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_ipv4_address"
    assert entry.data[CONF_HOST] == fake_codie.host


async def test_pairing_a_configured_codie_again_keeps_its_new_credentials(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await add_by_hand(hass, fake_codie)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST: "localhost"})
    await hass.async_block_till_done()
    fake_codie.local_secret = "a-new-secret"

    # Announced with an ID that matches nothing, at an address the entry no longer has.
    result = await discover(hass, service_info(fake_codie.host, fake_codie.port, "c" * 64))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: fake_codie.pairing_code}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == fake_codie.host
    assert entry.data[CONF_LOCAL_SECRET] == "a-new-secret"
    await hass.async_block_till_done()


async def test_a_configured_codie_with_another_device_id_is_matched_by_its_address(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await add_by_hand(hass, fake_codie)

    result = await discover(hass, service_info(fake_codie.host, fake_codie.port, "c" * 64))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_an_announcement_without_a_device_id_is_still_offered(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    result = await discover(hass, service_info(fake_codie.host, fake_codie.port, None))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"


async def test_the_same_codie_announced_twice_is_offered_once(hass: HomeAssistant) -> None:
    info = service_info("10.0.20.108", 8081, "b" * 64)

    first = await discover(hass, info)
    second = await discover(hass, info)

    assert first["type"] is FlowResultType.FORM
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_in_progress"
