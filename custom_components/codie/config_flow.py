"""Config flow: connect, then pair with the code the Codie displays."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

import probatio
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

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

STEP_USER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_HOST): str,
        probatio.Required(CONF_PORT, default=DEFAULT_PORT): probatio.All(
            probatio.Coerce(int), probatio.Range(min=1, max=65535)
        ),
    }
)
STEP_PAIR_SCHEMA = probatio.Schema({probatio.Required(CONF_CODE): str})


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
            result, error = await self._async_start_pairing()
            if result is not None:
                return result
            assert error is not None
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """A Codie announced itself on the network; offer to add it."""
        # The Codie's local API is reached over IPv4; an IPv6 address must not replace a
        # working host below.
        if discovery_info.ip_address.version != 4:
            return self.async_abort(reason="not_ipv4_address")
        self._host = discovery_info.host
        self._port = discovery_info.port or DEFAULT_PORT
        # The announced `device_id` is the Codie's `machine_id`, the entry's unique ID, so a
        # moved Codie gets its new address here.
        device_id = discovery_info.properties.get("device_id")
        if isinstance(device_id, str) and device_id:
            await self.async_set_unique_id(device_id)
            self._abort_if_unique_id_configured(
                updates={CONF_HOST: self._host, CONF_PORT: self._port}
            )
        # Also matched by address, for an announcement without a `device_id`.
        self._async_abort_entries_match({CONF_HOST: self._host})
        self.context["title_placeholders"] = {"host": self._host}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask before making the discovered Codie display a code."""
        return await self._async_confirm_then_pair("zeroconf_confirm", user_input)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The stored credentials stopped working; pair again with the same Codie."""
        self._host = entry_data[CONF_HOST]
        self._port = entry_data[CONF_PORT]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain, then make the Codie display a code."""
        return await self._async_confirm_then_pair("reauth_confirm", user_input)

    async def _async_confirm_then_pair(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Show `step_id`, which names the host; once submitted, make the Codie display a code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            result, error = await self._async_start_pairing()
            if result is not None:
                return result
            assert error is not None
            errors["base"] = error

        return self.async_show_form(
            step_id=step_id, description_placeholders={"host": self._host}, errors=errors
        )

    async def _async_start_pairing(self) -> tuple[ConfigFlowResult | None, str | None]:
        """Make the Codie display a code. Returns the next step or abort, else an error key."""
        self._client = CodieClient(async_get_clientsession(self.hass), self._host, self._port)
        try:
            await self._client.async_ping()
            bootstrap = await self._client.async_bootstrap()
            if not (bootstrap.device_logged_in and bootstrap.user_id):
                return self.async_abort(reason="not_signed_in"), None
            challenge = await self._client.async_pair_request()
        except CodieConnectionError:
            return None, "cannot_connect"
        except CodieApiError as err:
            if err.code == "DEVICE_NOT_LOGGED_IN":
                return self.async_abort(reason="not_signed_in"), None
            _LOGGER.warning("Codie refused the pairing request: %s", err)
            return None, "unknown"
        except CodieError as err:
            _LOGGER.warning("Unexpected reply while starting pairing: %s", err)
            return None, "unknown"
        self._user_id = bootstrap.user_id
        self._challenge_id = challenge.challenge_id
        return await self.async_step_pair(), None

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
        data = {
            CONF_HOST: self._host,
            CONF_PORT: self._port,
            CONF_LOCAL_SECRET: local_secret,
            CONF_USER_ID: self._user_id,
        }
        if local_session_id:
            data[CONF_LOCAL_SESSION_ID] = local_session_id

        if self.source == SOURCE_REAUTH:
            entry: ConfigEntry[Any] = self._get_reauth_entry()
            self._abort_if_unique_id_mismatch(reason="wrong_device")
            # Replace the credentials but keep the integration_id, so the Codie's
            # registration is reused.
            data[CONF_INTEGRATION_ID] = entry.data[CONF_INTEGRATION_ID]
            return self.async_update_reload_and_abort(entry, data=data)

        # Pairing the same Codie again gave it new credentials, maybe at a new address; keep
        # them, or the existing entry could be left with ones the Codie no longer accepts.
        self._abort_if_unique_id_configured(updates=data)
        data[CONF_INTEGRATION_ID] = str(uuid.uuid4())
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
