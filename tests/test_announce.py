"""Immediate Announcements: the `notify` entity and `codie.announce`, against the fake Codie."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import TargetSelector
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant.common import MockConfigEntry

from custom_components.codie.const import DOMAIN

from .fake_codie import FakeCodie
from .test_reauth import reauth_flows
from .test_tools import make_entry, setup

NOTIFY_ENTITY = "notify.codie_announce"


@pytest.fixture(autouse=True)
async def core_loaded(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "homeassistant", {})


async def notify(hass: HomeAssistant, message: str) -> None:
    await hass.services.async_call(
        "notify", "send_message", {"entity_id": NOTIFY_ENTITY, "message": message}, blocking=True
    )


async def announce(hass: HomeAssistant, entry: MockConfigEntry, **data: Any) -> None:
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    await hass.services.async_call(
        DOMAIN, "announce", {"device_id": devices[0].id, **data}, blocking=True
    )


async def test_notify_sends_the_text_for_the_current_slot(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    fake_codie.requests.clear()  # the settings poll reads the Slot too

    await notify(hass, "The washing is done")

    (utterance,) = fake_codie.utterances
    assert utterance["text"] == "The washing is done"
    assert utterance["slot"] == fake_codie.slot
    assert str(uuid.UUID(utterance["request_id"])) == utterance["request_id"]
    (read,) = fake_codie.requests_to("/data/user/slot")
    assert read.method == "GET"


async def test_the_action_sends_the_same_announcement(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)

    await announce(hass, entry, message="  Dinner is ready  ", queue=False)

    assert [u["text"] for u in fake_codie.utterances] == ["Dinner is ready"]


async def test_each_announcement_gets_a_new_request_id(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)

    await notify(hass, "One")
    await notify(hass, "One")

    first, second = fake_codie.utterances
    assert first["request_id"] != second["request_id"]


async def test_the_request_id_is_logged(
    hass: HomeAssistant, fake_codie: FakeCodie, caplog: pytest.LogCaptureFixture
) -> None:
    """A failure report needs the request_id to find the Announcement on the Codie's side."""
    await setup(hass, fake_codie)

    with caplog.at_level(logging.DEBUG, logger="custom_components.codie"):
        await notify(hass, "One")

    (utterance,) = fake_codie.utterances
    assert f"request_id={utterance['request_id']}" in caplog.text


async def test_a_slot_switch_between_read_and_send_is_retried_once(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    fake_codie.requests.clear()  # the settings poll reads the Slot too
    fake_codie.slot = 3
    fake_codie.switch_slot_to = 4

    await notify(hass, "Hello")

    (utterance,) = fake_codie.utterances
    assert utterance["slot"] == 4
    assert len(fake_codie.requests_to("/data/user/slot")) == 2
    assert len(fake_codie.requests_to("/data/user/tts/utterances")) == 2


async def test_a_second_slot_mismatch_is_an_error(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    fake_codie.utterance_error = (409, "SLOT_MISMATCH")

    with pytest.raises(HomeAssistantError):
        await notify(hass, "Hello")

    assert len(fake_codie.requests_to("/data/user/tts/utterances")) == 2


@pytest.mark.parametrize(
    ("status", "code", "reason"),
    [
        (409, "DEVICE_BLOCKED", "DEVICE_BLOCKED"),
        (409, "AMBIENT_MODE_BLOCKED", "AMBIENT_MODE_BLOCKED"),
    ],
)
async def test_a_blocked_codie_raises_an_error_naming_the_reason(
    hass: HomeAssistant, fake_codie: FakeCodie, status: int, code: str, reason: str
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.utterance_error = (status, code)

    for call in (notify(hass, "Hello"), announce(hass, entry, message="Hello")):
        with pytest.raises(HomeAssistantError) as raised:
            await call
        assert reason in str(raised.value)
    # Never retried, and the screen is never woken.
    assert len(fake_codie.requests_to("/data/user/tts/utterances")) == 2


@pytest.mark.parametrize("text", ["", "   \n", "x" * 16001])
async def test_empty_or_oversized_text_is_rejected_before_sending(
    hass: HomeAssistant, fake_codie: FakeCodie, text: str
) -> None:
    entry = await setup(hass, fake_codie)

    with pytest.raises(ServiceValidationError):
        await notify(hass, text)
    with pytest.raises(ServiceValidationError):
        await announce(hass, entry, message=text)

    assert fake_codie.requests_to("/data/user/tts/utterances") == []


async def test_the_longest_allowed_text_is_sent(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    await setup(hass, fake_codie)

    await notify(hass, "x" * 16000)

    assert len(fake_codie.utterances) == 1


async def test_a_queued_announcement_is_sent_as_an_agent_result(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.requests.clear()  # the settings poll reads the Slot too

    await announce(hass, entry, message="  Dinner is ready  ", queue=True)

    assert fake_codie.agent_results == [{"text": "Dinner is ready"}]
    assert fake_codie.utterances == []
    assert fake_codie.requests_to("/data/user/slot") == []


async def test_the_notify_entity_stays_immediate(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)

    await notify(hass, "Hello")

    assert len(fake_codie.utterances) == 1
    assert fake_codie.agent_results == []


async def test_the_longest_queued_text_is_sent(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    entry = await setup(hass, fake_codie)

    await announce(hass, entry, message=" " + "x" * 200 + " ", queue=True)

    assert len(fake_codie.agent_results) == 1


@pytest.mark.parametrize("text", ["", "   \n", "x" * 201])
async def test_empty_or_over_200_character_queued_text_is_rejected_not_truncated(
    hass: HomeAssistant, fake_codie: FakeCodie, text: str
) -> None:
    entry = await setup(hass, fake_codie)

    with pytest.raises(ServiceValidationError):
        await announce(hass, entry, message=text, queue=True)

    assert fake_codie.agent_results == []


async def test_a_speech_service_that_is_not_ready_is_an_error(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.agent_result_error = (503, "SPEECH_NOT_READY")

    with pytest.raises(HomeAssistantError) as raised:
        await announce(hass, entry, message="Hello", queue=True)

    assert not isinstance(raised.value, ServiceValidationError)
    assert "not ready" in str(raised.value)
    assert len(fake_codie.requests_to("/data/user/agent/results")) == 1


async def test_an_unreachable_codie_is_an_error(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    await setup(hass, fake_codie)
    await fake_codie.stop()

    with pytest.raises(HomeAssistantError):
        await notify(hass, "Hello")


async def test_the_action_targets_only_the_chosen_codie(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    other = FakeCodie(machine_id="b" * 64)
    await other.start()
    try:
        second = make_entry(other)
        second.add_to_hass(hass)
        assert await hass.config_entries.async_setup(second.entry_id)
        await hass.async_block_till_done()

        await announce(hass, entry, message="Hello")

        assert len(fake_codie.utterances) == 1
        assert other.utterances == []
    finally:
        await other.stop()


async def test_the_action_accepts_an_area_and_an_entity_as_the_target(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    area = ar.async_get(hass).async_create("Study")
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)

    await hass.services.async_call(
        DOMAIN, "announce", {"area_id": area.id, "message": "By area"}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN,
        "announce",
        {"entity_id": NOTIFY_ENTITY, "message": "By entity"},
        blocking=True,
    )

    assert [u["text"] for u in fake_codie.utterances] == ["By area", "By entity"]


async def test_a_target_with_no_codie_is_an_error(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    area = ar.async_get(hass).async_create("Empty room")

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, "announce", {"area_id": area.id, "message": "Hello"}, blocking=True
        )

    assert raised.value.translation_key == "codie_target_missing"


async def test_a_codie_that_rejects_the_credentials_starts_reauth_for_the_action(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.auth_error = "LOCAL_SECRET_MISMATCH"

    for queue in (False, True):
        with pytest.raises(HomeAssistantError) as raised:
            await announce(hass, entry, message="Hello", queue=queue)
        assert raised.value.translation_key == "announce_auth_failed"
        await hass.async_block_till_done()
        assert len(reauth_flows(hass, entry)) == 1

    assert fake_codie.utterances == []
    assert fake_codie.agent_results == []


async def test_a_codie_that_rejects_the_credentials_starts_reauth_for_the_notify_entity(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.auth_error = "LOCAL_SECRET_MISMATCH"

    with pytest.raises(HomeAssistantError) as raised:
        await notify(hass, "Hello")

    assert raised.value.translation_key == "announce_auth_failed"
    await hass.async_block_till_done()
    assert len(reauth_flows(hass, entry)) == 1
    assert fake_codie.utterances == []


async def test_a_call_without_a_target_asks_for_a_codie(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(DOMAIN, "announce", {"message": "Hello"}, blocking=True)

    assert raised.value.translation_key == "codie_target_missing"


async def test_a_codie_that_is_not_loaded_is_an_error_of_its_own(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert await hass.config_entries.async_unload(entry.entry_id)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            "announce",
            {"device_id": devices[0].id, "message": "Hello"},
            blocking=True,
        )

    assert raised.value.translation_key == "codie_not_loaded"
    assert fake_codie.utterances == []


def test_the_target_picker_offers_the_action_only_for_codie_entities() -> None:
    # Without an entity filter the frontend offers the action for every entity in the home.
    services_yaml = Path(__file__).parent.parent / "custom_components/codie/services.yaml"
    services = load_yaml_dict(str(services_yaml))

    target = TargetSelector(services["announce"]["target"]).config

    assert target["entity"] == [{"integration": DOMAIN, "domain": ["notify"]}]
