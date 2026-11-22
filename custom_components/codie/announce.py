"""Immediate Announcements: text the Character speaks as given."""

from __future__ import annotations

import logging
import uuid

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
# The Codie answers 409 with these codes while it is asleep, locked, or otherwise unable to speak.
BLOCKED_CODES = frozenset({"DEVICE_BLOCKED", "AMBIENT_MODE_BLOCKED"})
SLOT_MISMATCH = "SLOT_MISMATCH"

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


async def async_announce(client: CodieClient, message: str) -> None:
    """Make the Character speak `message` now, cutting off whatever it is doing.

    Raises `ServiceValidationError` for text the Codie would refuse, and
    `HomeAssistantError` if it could not be spoken.
    """
    text = message.strip()
    if not 1 <= len(text) <= MAX_IMMEDIATE_CHARS:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="announcement_length",
            translation_placeholders={"max": str(MAX_IMMEDIATE_CHARS)},
        )
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
    except CodieConnectionError as err:
        raise _failed("announce_cannot_connect") from err
    except CodieAuthError as err:
        raise _failed("announce_auth_failed") from err
    except CodieApiError as err:
        if err.code in BLOCKED_CODES:
            raise _failed("announce_blocked", reason=err.code or "") from err
        raise _failed("announce_failed", error=str(err)) from err
    except CodieError as err:
        raise _failed("announce_failed", error=str(err)) from err


def async_register_services(hass: HomeAssistant) -> None:
    """Register `codie.announce`."""

    async def handle_announce(call: ServiceCall) -> None:
        if call.data[ATTR_QUEUE]:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="queue_not_supported"
            )
        # The target picker also offers areas, floors, labels and entities; resolve them all
        # to config entries and keep the Codies among them.
        entries = [
            entry
            for entry_id in await async_extract_config_entry_ids(call)
            if (entry := hass.config_entries.async_get_entry(entry_id)) and entry.domain == DOMAIN
        ]
        if not entries:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="codie_not_loaded"
            )
        for entry in entries:
            if entry.state is not ConfigEntryState.LOADED:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="codie_not_loaded"
                )
            await async_announce(entry.runtime_data.client, call.data[ATTR_MESSAGE])

    hass.services.async_register(DOMAIN, SERVICE_ANNOUNCE, handle_announce, ANNOUNCE_SCHEMA)
