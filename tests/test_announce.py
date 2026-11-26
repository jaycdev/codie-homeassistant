"""Announcements: the `notify` entities and `codie.announce`, against the fake Codie."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import TargetSelector
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant.common import MockConfigEntry

from custom_components.codie import announce as announce_module
from custom_components.codie.announce import EVENT_ANNOUNCEMENT_FINISHED
from custom_components.codie.api import CodieConnectionError
from custom_components.codie.const import DOMAIN
from custom_components.codie.logbook import async_describe_events

from .fake_codie import FakeCodie
from .test_reauth import reauth_flows
from .test_tools import make_entry, setup

NOTIFY_ENTITY = "notify.codie_announce"
IN_CHARACTER_ENTITY = "notify.codie_announce_in_character"


@pytest.fixture(autouse=True)
async def core_loaded(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "homeassistant", {})


async def notify(hass: HomeAssistant, message: str) -> None:
    await hass.services.async_call(
        "notify", "send_message", {"entity_id": NOTIFY_ENTITY, "message": message}, blocking=True
    )


async def notify_in_character(hass: HomeAssistant, message: str, **data: Any) -> None:
    await hass.services.async_call(
        "notify",
        "send_message",
        {"entity_id": IN_CHARACTER_ENTITY, "message": message, **data},
        blocking=True,
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


# Interpreted announcements


class Finished:
    """The `codie_announcement_finished` events of a test."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._events: list[Event[Any]] = []
        self._arrived = asyncio.Event()

        @callback
        def collect(event: Event[Any]) -> None:
            self._events.append(event)
            self._arrived.set()

        hass.bus.async_listen(EVENT_ANNOUNCEMENT_FINISHED, collect)

    def now(self) -> list[dict[str, Any]]:
        """The events fired so far."""
        return [dict(event.data) for event in self._events]

    async def __call__(self) -> list[dict[str, Any]]:
        """The events fired once the background follow-up is done and has fired its event."""
        # The executor is a background task too, so the follow-up cannot be waited for as one.
        async with asyncio.timeout(5):
            await self._arrived.wait()
        await self._hass.async_block_till_done()
        return self.now()


@pytest.fixture
def finished(hass: HomeAssistant) -> Finished:
    return Finished(hass)


@pytest.fixture
def fast_follow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poll the turn without waiting, and give up on one that never ends after a moment."""
    monkeypatch.setattr(announce_module, "FOLLOW_INTERVAL_S", 0.01)
    monkeypatch.setattr(announce_module, "FOLLOW_TIMEOUT_S", 0.2)


async def test_an_interpreted_announcement_is_sent_as_a_chat_turn(
    hass: HomeAssistant, fake_codie: FakeCodie, fast_follow: None, finished: Finished
) -> None:
    entry = await setup(hass, fake_codie)

    await announce(hass, entry, message="  Washing machine is finished  ", in_character=True)

    (turn,) = fake_codie.chat_turns
    assert turn["slot"] == fake_codie.slot
    assert str(uuid.UUID(turn["request_id"])) == turn["request_id"]
    assert turn["input"] == {
        "type": "text",
        "text": (
            "Tell me this in your own words, as a short announcement. "
            "The message: Washing machine is finished"
        ),
    }
    assert turn["output"] == {"speech": "device"}
    assert turn["tools"] == "none"
    assert turn["interrupt"] is True
    assert turn["retention"] == "none"
    assert fake_codie.utterances == []
    assert fake_codie.agent_results == []
    await finished()


async def test_instructions_are_part_of_the_framing(
    hass: HomeAssistant, fake_codie: FakeCodie, fast_follow: None, finished: Finished
) -> None:
    entry = await setup(hass, fake_codie)

    await announce(
        hass, entry, message="Dinner", in_character=True, instructions="  Sound urgent  "
    )
    await announce(hass, entry, message="Dinner", in_character=True, instructions="Keep it short!")
    await announce(
        hass, entry, message="Dinner", in_character=True, instructions="\u6025\u3044\u3067\uff01"
    )

    assert [turn["input"]["text"] for turn in fake_codie.chat_turns] == [
        "Tell me this in your own words, as a short announcement. Sound urgent. "
        "The message: Dinner",
        "Tell me this in your own words, as a short announcement. Keep it short! "
        "The message: Dinner",
        "Tell me this in your own words, as a short announcement. "
        "\u6025\u3044\u3067\uff01 The message: Dinner",
    ]
    await finished()


async def test_a_blank_instruction_is_left_out(
    hass: HomeAssistant, fake_codie: FakeCodie, fast_follow: None, finished: Finished
) -> None:
    entry = await setup(hass, fake_codie)

    await announce(hass, entry, message="Dinner", in_character=True, instructions="   ")

    assert fake_codie.chat_turns[0]["input"]["text"] == (
        "Tell me this in your own words, as a short announcement. The message: Dinner"
    )
    await finished()


async def test_remember_saves_the_turn_to_chat_history(
    hass: HomeAssistant, fake_codie: FakeCodie, fast_follow: None, finished: Finished
) -> None:
    entry = await setup(hass, fake_codie)

    await announce(hass, entry, message="Dinner", in_character=True, remember=True)

    assert fake_codie.chat_turns[0]["retention"] == "chat"
    await finished()


async def test_the_longest_interpreted_texts_are_sent_whole(
    hass: HomeAssistant, fake_codie: FakeCodie, fast_follow: None, finished: Finished
) -> None:
    entry = await setup(hass, fake_codie)

    await announce(hass, entry, message="x" * 15000, in_character=True, instructions="y" * 500)

    text = fake_codie.chat_turns[0]["input"]["text"]
    assert "y" * 500 in text
    assert text.endswith("x" * 15000)
    await finished()


@pytest.mark.parametrize(
    ("data", "key"),
    [
        ({"message": "x" * 15001, "in_character": True}, "announcement_length"),
        ({"message": "", "in_character": True}, "announcement_length"),
        (
            {"message": "Hi", "in_character": True, "instructions": "y" * 501},
            "instructions_length",
        ),
        ({"message": "Hi", "instructions": "Sound urgent"}, "in_character_required"),
        ({"message": "Hi", "remember": True}, "in_character_required"),
        ({"message": "Hi", "in_character": True, "queue": True}, "in_character_not_queued"),
    ],
)
async def test_invalid_interpreted_announcements_are_rejected_before_sending(
    hass: HomeAssistant, fake_codie: FakeCodie, data: dict[str, Any], key: str
) -> None:
    entry = await setup(hass, fake_codie)

    with pytest.raises(ServiceValidationError) as raised:
        await announce(hass, entry, **data)

    assert raised.value.translation_key == key
    assert fake_codie.chat_turns == []
    assert fake_codie.utterances == []
    assert fake_codie.agent_results == []


async def test_empty_instructions_and_remember_off_need_no_in_character(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)

    await announce(hass, entry, message="Hi", instructions="", remember=False)

    assert [u["text"] for u in fake_codie.utterances] == ["Hi"]
    assert fake_codie.chat_turns == []


async def test_a_slot_switch_is_retried_once_for_a_chat_turn(
    hass: HomeAssistant, fake_codie: FakeCodie, fast_follow: None, finished: Finished
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.requests.clear()  # the settings poll reads the Slot too
    fake_codie.slot = 3
    fake_codie.switch_slot_to = 4

    await announce(hass, entry, message="Hello", in_character=True)

    (turn,) = fake_codie.chat_turns
    assert turn["slot"] == 4
    assert len(fake_codie.requests_to("/data/user/slot")) == 2
    assert len(fake_codie.requests_to("/data/user/chat/turns")) == 2
    await finished()


async def test_a_second_slot_mismatch_is_an_error_for_a_chat_turn(
    hass: HomeAssistant, fake_codie: FakeCodie, finished: Finished
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.chat_turn_error = (409, "SLOT_MISMATCH")

    with pytest.raises(HomeAssistantError):
        await announce(hass, entry, message="Hello", in_character=True)

    assert len(fake_codie.requests_to("/data/user/chat/turns")) == 2
    assert finished.now() == []


@pytest.mark.parametrize("code", ["DEVICE_BLOCKED", "AMBIENT_MODE_BLOCKED"])
async def test_a_blocked_codie_raises_an_error_for_a_chat_turn(
    hass: HomeAssistant, fake_codie: FakeCodie, finished: Finished, code: str
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.chat_turn_error = (409, code)

    with pytest.raises(HomeAssistantError) as raised:
        await announce(hass, entry, message="Hello", in_character=True)

    assert code in str(raised.value)
    assert len(fake_codie.requests_to("/data/user/chat/turns")) == 1
    # Nothing was said and the Codie cannot speak, so there is no fallback either.
    assert fake_codie.utterances == []
    assert finished.now() == []


async def test_a_speech_service_that_is_not_ready_is_an_error_for_a_chat_turn(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.chat_turn_error = (503, "SERVICE_NOT_READY")

    with pytest.raises(HomeAssistantError) as raised:
        await announce(hass, entry, message="Hello", in_character=True)

    assert raised.value.translation_key == "announce_not_ready"
    assert fake_codie.utterances == []


async def test_an_unreachable_codie_is_an_error_for_a_chat_turn(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    await fake_codie.stop()

    with pytest.raises(HomeAssistantError) as raised:
        await announce(hass, entry, message="Hello", in_character=True)

    assert raised.value.translation_key == "announce_cannot_connect"


async def test_a_codie_that_rejects_the_credentials_starts_reauth_for_a_chat_turn(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.auth_error = "LOCAL_SECRET_MISMATCH"

    with pytest.raises(HomeAssistantError) as raised:
        await announce(hass, entry, message="Hello", in_character=True)

    assert raised.value.translation_key == "announce_auth_failed"
    await hass.async_block_till_done()
    assert len(reauth_flows(hass, entry)) == 1
    assert fake_codie.chat_turns == []


async def test_the_action_returns_before_the_turn_ends(
    hass: HomeAssistant, fake_codie: FakeCodie, fast_follow: None, finished: Finished
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.chat_turn_polls = None

    await announce(hass, entry, message="Hello", in_character=True)

    assert finished.now() == []
    await finished()


async def test_a_finished_turn_fires_an_event_with_the_reply(
    hass: HomeAssistant, fake_codie: FakeCodie, fast_follow: None, finished: Finished
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.chat_turn_polls = 3
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)

    await announce(hass, entry, message="Washing machine is finished", in_character=True)

    assert await finished() == [
        {
            "device_id": device.id,
            "message": "Washing machine is finished",
            "reply": "The laundry is done!",
            "outcome": "in_character",
        }
    ]
    assert len(fake_codie.requests_to("/data/user/chat/turns/turn-1")) == 4
    assert fake_codie.utterances == []


@pytest.mark.parametrize(
    "outcome",
    [
        {"state": "failed", "error": {"code": "TOKEN_QUOTA_EXCEEDED"}},
        {"state": "failed", "error": {"code": "TURN_TIMEOUT"}},
        {"state": "failed", "error": {"code": "TTS_EMPTY_OUTPUT"}},
        {"state": "failed", "error": {"code": "CONVERSATION_FAILED"}},
        {"state": "failed"},
        {"state": "completed", "assistant_text": ""},
        {"state": "completed", "assistant_text": "   "},
        {"state": "completed"},
    ],
)
async def test_a_turn_that_fails_in_the_model_falls_back_to_the_original_text(
    hass: HomeAssistant,
    fake_codie: FakeCodie,
    fast_follow: None,
    finished: Finished,
    caplog: pytest.LogCaptureFixture,
    outcome: dict[str, Any],
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.chat_turn_outcome = outcome

    await announce(
        hass, entry, message="Washing machine is finished", in_character=True, remember=True
    )
    (event,) = await finished()

    # A plain immediate Announcement, which is never remembered.
    (utterance,) = fake_codie.utterances
    assert utterance["text"] == "Washing machine is finished"
    assert fake_codie.agent_results == []
    assert len(fake_codie.chat_turns) == 1
    assert event["outcome"] == "fallback"
    assert event["message"] == "Washing machine is finished"
    assert event["reply"] is None
    assert any(
        record.levelno == logging.WARNING and "Washing machine is finished" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize(
    "outcome",
    [
        {"state": "cancelled"},
        {"state": "failed", "error": {"code": "DEVICE_INTERRUPTED"}},
        {"state": "failed", "error": {"code": "DEVICE_BUSY"}},
    ],
)
async def test_a_turn_something_else_took_over_is_not_spoken_again(
    hass: HomeAssistant,
    fake_codie: FakeCodie,
    fast_follow: None,
    finished: Finished,
    outcome: dict[str, Any],
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.chat_turn_outcome = outcome

    await announce(hass, entry, message="Hello", in_character=True)
    (event,) = await finished()

    assert fake_codie.utterances == []
    assert event["outcome"] == "interrupted"
    assert event["reply"] is None


async def test_a_long_message_is_cut_short_in_the_warning_only(
    hass: HomeAssistant,
    fake_codie: FakeCodie,
    fast_follow: None,
    finished: Finished,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.chat_turn_outcome = {"state": "failed", "error": {"code": "TURN_TIMEOUT"}}

    await announce(hass, entry, message="x" * 500, in_character=True)
    await finished()

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("x" * 80 in message for message in warnings)
    assert not any("x" * 200 in message for message in warnings)
    assert fake_codie.utterances[0]["text"] == "x" * 500


async def test_a_follow_up_that_fails_once_at_connection_level_still_finds_the_reply(
    hass: HomeAssistant,
    fake_codie: FakeCodie,
    fast_follow: None,
    finished: Finished,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await setup(hass, fake_codie)
    real = entry.runtime_data.client.async_get_chat_turn
    calls: list[str] = []

    async def flaky(turn_id: str) -> Any:
        calls.append(turn_id)
        if len(calls) == 1:
            raise CodieConnectionError("hiccup")
        return await real(turn_id)

    monkeypatch.setattr(entry.runtime_data.client, "async_get_chat_turn", flaky)

    await announce(hass, entry, message="Hello", in_character=True)
    (event,) = await finished()

    assert event["outcome"] == "in_character"
    assert event["reply"] == "The laundry is done!"
    assert fake_codie.utterances == []


async def test_a_turn_that_never_ends_falls_back_once_the_follow_up_times_out(
    hass: HomeAssistant, fake_codie: FakeCodie, fast_follow: None, finished: Finished
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.chat_turn_polls = None

    await announce(hass, entry, message="Hello", in_character=True)
    (event,) = await finished()

    assert [u["text"] for u in fake_codie.utterances] == ["Hello"]
    assert event["outcome"] == "fallback"
    # The turn may still be speaking, so it is stopped before the fallback talks over it.
    assert fake_codie.cancelled_turns == ["turn-1"]


async def test_a_turn_that_cannot_be_cancelled_still_falls_back(
    hass: HomeAssistant, fake_codie: FakeCodie, fast_follow: None, finished: Finished
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.chat_turn_polls = None
    fake_codie.overrides["/data/user/chat/turns/turn-1/cancel"] = (
        409,
        b'{"success":false,"code":"TURN_NOT_CANCELLABLE","message":"x"}',
    )

    await announce(hass, entry, message="Hello", in_character=True)
    (event,) = await finished()

    assert [u["text"] for u in fake_codie.utterances] == ["Hello"]
    assert event["outcome"] == "fallback"


async def test_a_quota_refusal_when_starting_falls_back_to_the_original_text(
    hass: HomeAssistant, fake_codie: FakeCodie, finished: Finished
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.chat_turn_error = (429, "TOKEN_QUOTA_EXCEEDED")

    await announce(hass, entry, message="Hello", in_character=True)

    assert [u["text"] for u in fake_codie.utterances] == ["Hello"]
    (event,) = await finished()
    assert event["outcome"] == "fallback"
    assert event["reply"] is None


async def test_a_fallback_that_cannot_be_spoken_is_only_logged(
    hass: HomeAssistant,
    fake_codie: FakeCodie,
    fast_follow: None,
    finished: Finished,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.chat_turn_outcome = {"state": "failed", "error": {"code": "TURN_TIMEOUT"}}
    fake_codie.utterance_error = (409, "DEVICE_BLOCKED")

    await announce(hass, entry, message="Hello", in_character=True)
    (event,) = await finished()

    assert fake_codie.utterances == []
    assert event["outcome"] == "not_spoken"
    assert event["reply"] is None
    assert "DEVICE_BLOCKED" in caplog.text


async def test_a_turn_that_is_no_longer_known_falls_back(
    hass: HomeAssistant, fake_codie: FakeCodie, fast_follow: None, finished: Finished
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.overrides["/data/user/chat/turns/turn-1"] = (
        404,
        b'{"success":false,"code":"NOT_FOUND","message":"NOT_FOUND"}',
    )

    await announce(hass, entry, message="Hello", in_character=True)
    (event,) = await finished()

    assert [u["text"] for u in fake_codie.utterances] == ["Hello"]
    assert event["outcome"] == "fallback"


async def test_unloading_the_entry_stops_following_the_turn(
    hass: HomeAssistant,
    fake_codie: FakeCodie,
    monkeypatch: pytest.MonkeyPatch,
    finished: Finished,
) -> None:
    monkeypatch.setattr(announce_module, "FOLLOW_INTERVAL_S", 0.01)
    monkeypatch.setattr(announce_module, "FOLLOW_TIMEOUT_S", 60)
    entry = await setup(hass, fake_codie)
    fake_codie.chat_turn_polls = None
    await announce(hass, entry, message="Hello", in_character=True)

    assert await hass.config_entries.async_unload(entry.entry_id)
    reads = len(fake_codie.requests_to("/data/user/chat/turns/turn-1"))
    await asyncio.sleep(0.1)

    assert len(fake_codie.requests_to("/data/user/chat/turns/turn-1")) == reads
    assert finished.now() == []
    assert fake_codie.utterances == []


async def test_a_codie_that_rejects_the_credentials_while_following_starts_reauth(
    hass: HomeAssistant, fake_codie: FakeCodie, fast_follow: None, finished: Finished
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.chat_turn_polls = None
    await announce(hass, entry, message="Hello", in_character=True)
    fake_codie.auth_error = "LOCAL_SECRET_MISMATCH"

    (event,) = await finished()
    await hass.async_block_till_done()

    # The same credentials would be rejected again, so nothing more is sent.
    assert event["outcome"] == "not_spoken"
    assert fake_codie.utterances == []
    assert fake_codie.cancelled_turns == []
    assert len(reauth_flows(hass, entry)) == 1


async def test_the_notify_entity_says_it_in_character_with_the_default_wording(
    hass: HomeAssistant, fake_codie: FakeCodie, fast_follow: None, finished: Finished
) -> None:
    await setup(hass, fake_codie)

    await notify_in_character(hass, "The washing is done", title="Laundry")

    (turn,) = fake_codie.chat_turns
    assert turn["input"]["text"] == (
        "Tell me this in your own words, as a short announcement. The message: The washing is done"
    )
    assert turn["retention"] == "none"
    assert fake_codie.utterances == []
    (event,) = await finished()
    assert event["reply"] == "The laundry is done!"


async def test_the_notify_entity_rejects_over_long_text(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)

    with pytest.raises(ServiceValidationError):
        await notify_in_character(hass, "x" * 15001)

    assert fake_codie.chat_turns == []


async def test_a_plain_announcement_fires_no_event(
    hass: HomeAssistant, fake_codie: FakeCodie, finished: Finished
) -> None:
    await setup(hass, fake_codie)

    await notify(hass, "Hello")

    assert finished.now() == []


def describe_finished(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """What the logbook shows for a `codie_announcement_finished` event."""
    registered: dict[str, Any] = {}

    def register(domain: str, event_name: str, describe: Any) -> None:
        registered[event_name] = (domain, describe)

    async_describe_events(hass, register)
    domain, describe = registered[EVENT_ANNOUNCEMENT_FINISHED]
    assert domain == DOMAIN
    return dict(describe(Event(EVENT_ANNOUNCEMENT_FINISHED, data)))


async def test_the_logbook_shows_what_the_character_said(
    hass: HomeAssistant, fake_codie: FakeCodie, fast_follow: None, finished: Finished
) -> None:
    entry = await setup(hass, fake_codie)
    await announce(hass, entry, message="Washing machine is finished", in_character=True)
    (event,) = await finished()

    described = describe_finished(hass, event)

    assert described["name"] == "Codie"
    assert described["message"] == (
        'said "Washing machine is finished" in its own words: "The laundry is done!"'
    )


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        ("fallback", 'could not say "Hello" in its own words and said it as given'),
        ("not_spoken", 'could not say "Hello" in its own words or as given'),
        ("interrupted", 'was interrupted before saying "Hello" in its own words'),
    ],
)
async def test_the_logbook_shows_what_went_wrong(
    hass: HomeAssistant, fake_codie: FakeCodie, outcome: str, message: str
) -> None:
    entry = await setup(hass, fake_codie)
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    dr.async_get(hass).async_update_device(device.id, name_by_user="Desk Codie")
    event = {"device_id": device.id, "message": "Hello", "reply": None, "outcome": outcome}

    described = describe_finished(hass, event)

    assert described["name"] == "Desk Codie"
    assert described["message"] == message


async def test_the_logbook_copes_with_a_codie_that_is_gone(hass: HomeAssistant) -> None:
    event = {"device_id": None, "message": "Hello", "reply": "Hi!", "outcome": "in_character"}

    described = describe_finished(hass, event)

    assert described["name"] == "Codie"
