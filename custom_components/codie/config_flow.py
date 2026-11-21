"""Config flow: connect, then pair with the code the Codie displays."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    CodieApiError,
    CodieClient,
    CodieConnectionError,
    CodieCredentials,
    CodieError,
)
from .const import (
    CONF_CODE,
    CONF_INTEGRATION_ID,
    CONF_LOCAL_SECRET,
    CONF_LOCAL_SESSION_ID,
    CONF_USER_ID,
    DEFAULT_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Codes `pair/confirm` answers with once the challenge can no longer succeed.
CHALLENGE_OVER_CODES = frozenset({"PAIRING_CODE_EXPIRED", "PAIRING_CODE_USED"})

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
    }
)
STEP_PAIR_SCHEMA = vol.Schema({vol.Required(CONF_CODE): str})


class CodieConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle adding a Codie."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._host = ""
        self._port = DEFAULT_PORT
        self._user_id = ""
        self._client: CodieClient | None = None
        # Set while a pairing code is displayed on the Codie and not yet used.
        self._challenge_id: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the host and port, then make the Codie display a code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = user_input[CONF_HOST].strip()
            self._port = user_input[CONF_PORT]
            self._client = CodieClient(async_get_clientsession(self.hass), self._host, self._port)
            try:
                await self._client.async_ping()
                bootstrap = await self._client.async_bootstrap()
                if not (bootstrap.device_logged_in and bootstrap.user_id):
                    return self.async_abort(reason="not_signed_in")
                challenge = await self._client.async_pair_request()
            except CodieConnectionError:
                errors["base"] = "cannot_connect"
            except CodieApiError as err:
                if err.code == "DEVICE_NOT_LOGGED_IN":
                    return self.async_abort(reason="not_signed_in")
                _LOGGER.warning("Codie refused the pairing request: %s", err)
                errors["base"] = "unknown"
            except CodieError as err:
                _LOGGER.warning("Unexpected reply while starting pairing: %s", err)
                errors["base"] = "unknown"
            else:
                self._user_id = bootstrap.user_id
                self._challenge_id = challenge.challenge_id
                return await self.async_step_pair()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Check the code the user read off the Codie."""
        assert self._client is not None
        assert self._challenge_id is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                pairing = await self._client.async_pair_confirm(
                    self._challenge_id, user_input[CONF_CODE].strip()
                )
            except CodieApiError as err:
                if err.code == "INVALID_CODE":
                    errors["base"] = "invalid_code"
                elif err.code in CHALLENGE_OVER_CODES:
                    await self._async_cancel_challenge()
                    return self.async_abort(reason="code_expired")
                else:
                    _LOGGER.warning("Pairing failed: %s", err)
                    errors["base"] = "unknown"
            except CodieConnectionError:
                return self.async_abort(reason="cannot_connect")
            except CodieError as err:
                _LOGGER.warning("Unexpected reply while pairing: %s", err)
                errors["base"] = "unknown"
            else:
                self._challenge_id = None
                return await self._async_finish(pairing.local_secret, pairing.local_session_id)

        return self.async_show_form(step_id="pair", data_schema=STEP_PAIR_SCHEMA, errors=errors)

    async def _async_finish(self, local_secret: str, local_session_id: str) -> ConfigFlowResult:
        """Identify the device with the new credentials and create the entry."""
        credentials = CodieCredentials(local_secret, self._user_id, local_session_id)
        client = CodieClient(
            async_get_clientsession(self.hass), self._host, self._port, credentials
        )
        try:
            system = await client.async_get_system()
        except CodieConnectionError:
            return self.async_abort(reason="cannot_connect")
        except CodieError as err:
            _LOGGER.warning("Could not read the Codie's identity: %s", err)
            return self.async_abort(reason="unknown")
        machine_id = system.get("machine_id")
        if not isinstance(machine_id, str) or not machine_id:
            _LOGGER.warning("The Codie did not report a machine_id")
            return self.async_abort(reason="unknown")

        await self.async_set_unique_id(machine_id)
        self._abort_if_unique_id_configured()

        data = {
            CONF_HOST: self._host,
            CONF_PORT: self._port,
            CONF_LOCAL_SECRET: local_secret,
            CONF_USER_ID: self._user_id,
            CONF_INTEGRATION_ID: str(uuid.uuid4()),
        }
        if local_session_id:
            data[CONF_LOCAL_SESSION_ID] = local_session_id
        return self.async_create_entry(title=f"Codie ({self._host})", data=data)

    async def _async_cancel_challenge(self) -> None:
        """Tell the Codie to stop displaying the code. Best effort."""
        challenge_id, self._challenge_id = self._challenge_id, None
        assert challenge_id is not None and self._client is not None
        try:
            await self._client.async_pair_cancel(challenge_id)
        except CodieError as err:
            _LOGGER.debug("Could not cancel the pairing request: %s", err)

    @callback
    def async_remove(self) -> None:
        """The flow ended; if the user walked away from a displayed code, cancel it."""
        if self._challenge_id is not None:
            self.hass.async_create_task(
                self._async_cancel_challenge(), "codie cancel pairing request"
            )
