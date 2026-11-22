"""Immediate Announcements: text the Character speaks as given."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from functools import partial

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_extract_config_entry_ids

from .api import CodieApiError, CodieAuthError, CodieClient, CodieConnectionError, CodieError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_ANNOUNCE = "announce"
ATTR_MESSAGE = "message"
ATTR_QUEUE = "queue"

MAX_IMMEDIATE_CHARS = 16000
MAX_QUEUED_CHARS = 200
# The Codie answers 409 with these codes while it is asleep, locked, or otherwise unable to speak.
BLOCKED_CODES = frozenset({"DEVICE_BLOCKED", "AMBIENT_MODE_BLOCKED"})
SLOT_MISMATCH = "SLOT_MISMATCH"
# The speech service is not ready, so the Codie cannot take an announcement yet.
SERVICE_UNAVAILABLE = 503

ANNOUNCE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_QUEUE, default=False): cv.boolean,
        **cv.TARGET_SERVICE_FIELDS,
    }
)


def _failed(key: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders
    )


def _announce_error(err: CodieError) -> HomeAssistantError:
    """Turn a failure of either announcement endpoint into a Home Assistant error."""
    if isinstance(err, CodieConnectionError):
        return _failed("announce_cannot_connect")
    if isinstance(err, CodieAuthError):
        return _failed("announce_auth_failed")
    if isinstance(err, CodieApiError):
        if err.code in BLOCKED_CODES:
            return _failed("announce_blocked", reason=err.code or "")
        if err.status == SERVICE_UNAVAILABLE:
            return _failed("announce_not_ready")
    return _failed("announce_failed", error=str(err))


def _checked_text(message: str, max_chars: int) -> str:
    text = message.strip()
    if not 1 <= len(text) <= max_chars:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="announcement_length",
            translation_placeholders={"max": str(max_chars)},
        )
    return text


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


async def async_announce(
    client: CodieClient, message: str, on_auth_failed: Callable[[], None]
) -> None:
    """Make the Character speak `message` now, cutting off whatever it is doing.

    Raises `ServiceValidationError` for text the Codie would refuse, and
    `HomeAssistantError` if it could not be spoken. A Codie that rejects the credentials
    also calls `on_auth_failed`, which starts re-authentication.
    """
    text = _checked_text(message, MAX_IMMEDIATE_CHARS)
    try:
        # A switch of Slots between the read and the POST is answered with 409; read again once.
        for attempt in (1, 2):
            slot = await client.async_get_slot()
            try:
                await client.async_post_utterance(str(uuid.uuid4()), slot, text)
            except CodieApiError as err:
                if err.code == SLOT_MISMATCH and attempt == 1:
                    _LOGGER.debug("The Slot changed while announcing; reading it again")
                    continue
                raise
            return
    except CodieAuthError as err:
        on_auth_failed()
        raise _announce_error(err) from err
    except CodieError as err:
        raise _announce_error(err) from err


def async_register_services(hass: HomeAssistant) -> None:
    """Register `codie.announce`."""

    async def handle_announce(call: ServiceCall) -> None:
        send = async_announce_queued if call.data[ATTR_QUEUE] else async_announce
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
            await send(
                entry.runtime_data.client,
                call.data[ATTR_MESSAGE],
                partial(entry.async_start_reauth, hass),
            )

    hass.services.async_register(DOMAIN, SERVICE_ANNOUNCE, handle_announce, ANNOUNCE_SCHEMA)
