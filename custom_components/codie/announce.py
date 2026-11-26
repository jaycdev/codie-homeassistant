"""Announcements: text the Character speaks as given, or says in its own words."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING, TypedDict

import probatio
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service import async_extract_config_entry_ids

from .api import (
    CodieApiError,
    CodieAuthError,
    CodieChatTurn,
    CodieClient,
    CodieConnectionError,
    CodieError,
)
from .const import DOMAIN
from .errors import failed, unreachable_error

if TYPE_CHECKING:
    from . import CodieConfigEntry

_LOGGER = logging.getLogger(__name__)

SERVICE_ANNOUNCE = "announce"
ATTR_MESSAGE = "message"
ATTR_QUEUE = "queue"
ATTR_IN_CHARACTER = "in_character"
ATTR_INSTRUCTIONS = "instructions"
ATTR_REMEMBER = "remember"

# Fired when an Interpreted announcement is over, however it went.
EVENT_ANNOUNCEMENT_FINISHED = f"{DOMAIN}_announcement_finished"

MAX_IMMEDIATE_CHARS = 16000
MAX_QUEUED_CHARS = 200
# A chat turn takes 16000 characters, and the fixed wording around the message and the
# instructions takes well under 500 of them.
MAX_INTERPRETED_CHARS = 15000
MAX_INSTRUCTIONS_CHARS = 500
# What the Character is asked to do with the message. The instructions, if any, go between the two.
FRAMING_REQUEST = "Tell me this in your own words, as a short announcement."
FRAMING_MESSAGE = "The message:"
# How often the turn is read, and how long it may take, speaking included, before the original
# text is spoken instead.
FOLLOW_INTERVAL_S = 1
FOLLOW_TIMEOUT_S = 120
# The Codie refuses a new chat turn with this code while it is out of tokens.
TOKEN_QUOTA_EXCEEDED = "TOKEN_QUOTA_EXCEEDED"
# The Codie answers 409 with these codes while it is asleep, locked, or otherwise unable to speak.
BLOCKED_CODES = frozenset({"DEVICE_BLOCKED", "AMBIENT_MODE_BLOCKED"})
SLOT_MISMATCH = "SLOT_MISMATCH"
# The speech service is not ready, so the Codie cannot take an announcement yet.
SERVICE_UNAVAILABLE = 503

ANNOUNCE_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_MESSAGE): cv.string,
        probatio.Optional(ATTR_QUEUE, default=False): cv.boolean,
        probatio.Optional(ATTR_IN_CHARACTER, default=False): cv.boolean,
        probatio.Optional(ATTR_INSTRUCTIONS, default=""): cv.string,
        probatio.Optional(ATTR_REMEMBER, default=False): cv.boolean,
        **cv.TARGET_SERVICE_FIELDS,
    }
)


# The outcomes of an Interpreted announcement, as `codie_announcement_finished` reports them.
# The Character said it in its own words.
OUTCOME_IN_CHARACTER = "in_character"
# The Character could not, so the original text was spoken as given.
OUTCOME_FALLBACK = "fallback"
# The Character could not, and the original text could not be spoken either.
OUTCOME_NOT_SPOKEN = "not_spoken"
# Something else took over the Codie, such as another Announcement or the user, so nothing more
# was spoken.
OUTCOME_INTERRUPTED = "interrupted"
# How many characters of a message a warning shows; the whole message is logged at debug level.
LOGGED_CHARS = 80
# The codes of a failed turn that mean something else took over the Codie. Any other failure
# happened in the model.
INTERRUPTED_CODES = frozenset({"DEVICE_INTERRUPTED", "DEVICE_BUSY"})
# What ends a sentence in the languages the Codie offers.
SENTENCE_ENDS = ".!?\u2026\u3002\uff01\uff1f\uff61"


class AnnouncementFinishedData(TypedDict):
    """The data of `codie_announcement_finished`, documented in the README."""

    device_id: str | None
    message: str
    # What the Character said; None if it said nothing.
    reply: str | None
    # How it went: IN_CHARACTER, FALLBACK, NOT_SPOKEN or INTERRUPTED.
    outcome: str


def _announce_error(err: CodieError) -> HomeAssistantError:
    """Turn a failure of any announcement endpoint into a Home Assistant error."""
    if (unreachable := unreachable_error(err, "announce")) is not None:
        return unreachable
    if isinstance(err, CodieApiError):
        if err.code in BLOCKED_CODES:
            return failed("announce_blocked", reason=err.code or "")
        if err.status == SERVICE_UNAVAILABLE:
            return failed("announce_not_ready")
    return failed("announce_failed", error=str(err))


def _checked_text(message: str, max_chars: int) -> str:
    text = message.strip()
    if not 1 <= len(text) <= max_chars:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="announcement_length",
            translation_placeholders={"max": str(max_chars)},
        )
    return text


def _invalid(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders
    )


async def async_announce_queued(
    client: CodieClient, message: str, on_auth_failed: Callable[[], None]
) -> None:
    """Have the Character speak `message` once idle, and remember it in its chat history.

    Raises `ServiceValidationError` for text over the Codie's 200 character limit (never
    truncated), and `HomeAssistantError` if it could not be queued. A Codie that rejects the
    credentials also calls `on_auth_failed`, which starts re-authentication.
    """
    text = _checked_text(message, MAX_QUEUED_CHARS)
    try:
        await client.async_post_agent_result(text)
    except CodieAuthError as err:
        on_auth_failed()
        raise _announce_error(err) from err
    except CodieError as err:
        raise _announce_error(err) from err


async def _post_in_slot[T](client: CodieClient, post: Callable[[str, int], Awaitable[T]]) -> T:
    """Call `post(request_id, slot)` for the active Slot, reading it again once if it moved."""
    # A switch of Slots between the read and the POST is answered with 409; read again once.
    for attempt in (1, 2):
        slot = await client.async_get_slot()
        request_id = str(uuid.uuid4())
        _LOGGER.debug("Announcing in Slot %s, request_id=%s", slot, request_id)
        try:
            return await post(request_id, slot)
        except CodieApiError as err:
            if err.code == SLOT_MISMATCH and attempt == 1:
                _LOGGER.debug("The Slot changed while announcing; reading it again")
                continue
            raise
    raise AssertionError("unreachable")  # the second attempt returns or raises


async def async_announce(
    client: CodieClient, message: str, on_auth_failed: Callable[[], None]
) -> None:
    """Make the Character speak `message` now, cutting off whatever it is doing.

    Raises `ServiceValidationError` for text the Codie would refuse, and
    `HomeAssistantError` if it could not be spoken. A Codie that rejects the credentials
    also calls `on_auth_failed`, which starts re-authentication.
    """
    text = _checked_text(message, MAX_IMMEDIATE_CHARS)

    async def post(request_id: str, slot: int) -> None:
        await client.async_post_utterance(request_id, slot, text)

    try:
        await _post_in_slot(client, post)
    except CodieAuthError as err:
        on_auth_failed()
        raise _announce_error(err) from err
    except CodieError as err:
        raise _announce_error(err) from err


def _framed(message: str, instructions: str) -> str:
    """The text of the turn: the message and the instructions in the fixed wording."""
    if not instructions:
        return f"{FRAMING_REQUEST} {FRAMING_MESSAGE} {message}"
    # The instructions are a sentence of their own: end it once, not twice.
    sentence = instructions if instructions[-1] in SENTENCE_ENDS else f"{instructions}."
    return f"{FRAMING_REQUEST} {sentence} {FRAMING_MESSAGE} {message}"


async def async_announce_in_character(
    hass: HomeAssistant,
    entry: CodieConfigEntry,
    message: str,
    instructions: str = "",
    remember: bool = False,
) -> None:
    """Have the Character say `message` in its own words now, cutting off whatever it is doing.

    Returns once the Codie accepted the turn. The turn is followed in the background until the
    Character has said it; if it cannot, `message` is spoken as given instead. The turn is
    saved to the Character's chat history only with `remember`, and that fallback never is.

    Raises `ServiceValidationError` for text the Codie would refuse, and
    `HomeAssistantError` if the turn could not be started. A Codie that rejects the credentials
    also starts re-authentication.
    """
    text = _checked_text(message, MAX_INTERPRETED_CHARS)
    instructions = instructions.strip()
    if len(instructions) > MAX_INSTRUCTIONS_CHARS:
        raise _invalid("instructions_length", max=str(MAX_INSTRUCTIONS_CHARS))
    client = entry.runtime_data.client
    on_auth_failed = partial(entry.async_start_reauth, hass)
    framed = _framed(text, instructions)

    async def post(request_id: str, slot: int) -> CodieChatTurn:
        return await client.async_post_chat_turn(request_id, slot, framed, remember=remember)

    try:
        turn = await _post_in_slot(client, post)
    except CodieAuthError as err:
        on_auth_failed()
        raise _announce_error(err) from err
    except CodieApiError as err:
        if err.code != TOKEN_QUOTA_EXCEEDED:
            raise _announce_error(err) from err
        # Out of tokens: the Character cannot say it, so there is no turn to follow.
        _LOGGER.warning("The Codie is out of tokens, so it speaks %r as given", _shortened(text))
        await async_announce(client, text, on_auth_failed)
        _fire_finished(hass, entry, text, None, OUTCOME_FALLBACK)
        return
    except CodieError as err:
        raise _announce_error(err) from err
    entry.async_create_background_task(
        hass, _follow_turn(hass, entry, turn.turn_id, text), "codie interpreted announcement"
    )


def _shortened(message: str) -> str:
    """The start of `message`, for a warning."""
    return message if len(message) <= LOGGED_CHARS else f"{message[:LOGGED_CHARS]}..."


async def _read_reply(
    client: CodieClient, turn_id: str, on_auth_failed: Callable[[], None]
) -> tuple[str | None, str, str]:
    """Wait for the turn to end.

    Returns the Character's reply, or None, why there is none, and the outcome to report then:
    `OUTCOME_FALLBACK` if the original text should be spoken, `OUTCOME_INTERRUPTED` if something
    else took over the Codie, or `OUTCOME_NOT_SPOKEN` if nothing more can be sent.
    """
    try:
        async with asyncio.timeout(FOLLOW_TIMEOUT_S):
            while True:
                try:
                    turn = await client.async_get_chat_turn(turn_id)
                except CodieConnectionError:
                    # The Codie may only be busy; keep asking until the time is up.
                    pass
                else:
                    if turn.finished:
                        break
                await asyncio.sleep(FOLLOW_INTERVAL_S)
    except TimeoutError:
        # It may still be speaking, and the fallback must not talk over it.
        try:
            await client.async_cancel_chat_turn(turn_id)
        except CodieError as err:
            _LOGGER.debug("Could not cancel the turn %s: %s", turn_id, err)
        return None, "it did not end in time", OUTCOME_FALLBACK
    except CodieAuthError as err:
        # The same credentials would be rejected again, so there is no use trying the fallback.
        on_auth_failed()
        return None, str(err), OUTCOME_NOT_SPOKEN
    except CodieError as err:
        return None, str(err), OUTCOME_FALLBACK
    if turn.state == "cancelled" or turn.error_code in INTERRUPTED_CODES:
        return None, f"the turn was taken over ({turn.state})", OUTCOME_INTERRUPTED
    if turn.state != "completed":
        return None, f"the turn failed: {turn.error_code or 'no error code'}", OUTCOME_FALLBACK
    if not turn.assistant_text:
        return None, "the Character said nothing", OUTCOME_FALLBACK
    return turn.assistant_text, "", OUTCOME_IN_CHARACTER


async def _follow_turn(
    hass: HomeAssistant, entry: CodieConfigEntry, turn_id: str, message: str
) -> None:
    """Follow the turn to its end, speaking `message` as given if the Character failed."""
    client = entry.runtime_data.client
    on_auth_failed = partial(entry.async_start_reauth, hass)
    reply, why_not, outcome = await _read_reply(client, turn_id, on_auth_failed)
    if outcome == OUTCOME_INTERRUPTED:
        _LOGGER.info(
            "Something else took over the Codie (%s), so %r is not spoken",
            why_not,
            _shortened(message),
        )
    elif reply is None:
        _LOGGER.warning(
            "The Character could not say %r in its own words (%s)%s",
            _shortened(message),
            why_not,
            ", so it speaks it as given" if outcome == OUTCOME_FALLBACK else "",
        )
        _LOGGER.debug("The whole message was %r", message)
    if outcome == OUTCOME_FALLBACK:
        try:
            await async_announce(client, message, on_auth_failed)
        except HomeAssistantError as err:
            _LOGGER.warning("Could not speak %r as given either: %s", _shortened(message), err)
            outcome = OUTCOME_NOT_SPOKEN
    _fire_finished(hass, entry, message, reply, outcome)


def _fire_finished(
    hass: HomeAssistant, entry: CodieConfigEntry, message: str, reply: str | None, outcome: str
) -> None:
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, str(entry.unique_id)), entry.entry_id
    )
    hass.bus.async_fire(
        EVENT_ANNOUNCEMENT_FINISHED,
        AnnouncementFinishedData(
            device_id=device.id if device else None,
            message=message,
            reply=reply,
            outcome=outcome,
        ),
    )


def async_register_services(hass: HomeAssistant) -> None:
    """Register `codie.announce`."""

    async def handle_announce(call: ServiceCall) -> None:
        in_character = call.data[ATTR_IN_CHARACTER]
        instructions = call.data[ATTR_INSTRUCTIONS]
        remember = call.data[ATTR_REMEMBER]
        if in_character and call.data[ATTR_QUEUE]:
            raise _invalid("in_character_not_queued")
        if (instructions.strip() or remember) and not in_character:
            raise _invalid("in_character_required")
        # The target picker also offers areas, floors, labels and entities; resolve them all
        # to config entries and keep the Codies among them.
        entries = [
            entry
            for entry_id in await async_extract_config_entry_ids(call)
            if (entry := hass.config_entries.async_get_entry(entry_id)) and entry.domain == DOMAIN
        ]
        if not entries:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="codie_target_missing"
            )
        for entry in entries:
            if entry.state is not ConfigEntryState.LOADED:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="codie_not_loaded"
                )
            if in_character:
                await async_announce_in_character(
                    hass, entry, call.data[ATTR_MESSAGE], instructions, remember
                )
                continue
            send = async_announce_queued if call.data[ATTR_QUEUE] else async_announce
            await send(
                entry.runtime_data.client,
                call.data[ATTR_MESSAGE],
                partial(entry.async_start_reauth, hass),
            )

    hass.services.async_register(DOMAIN, SERVICE_ANNOUNCE, handle_announce, ANNOUNCE_SCHEMA)
