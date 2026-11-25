"""Timers: a Command the Character sets to run after a delay, checked when it is set."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from hassil.models import UnmatchedTextEntity
from hassil.recognize import MISSING_ENTITY
from homeassistant.components.conversation import ConversationInput
from homeassistant.components.conversation.agent_manager import async_converse, get_agent_manager
from homeassistant.components.conversation.const import HOME_ASSISTANT_AGENT
from homeassistant.core import CALLBACK_TYPE, Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.intent import IntentResponse, IntentResponseType
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util import language as language_util

from .errors import (
    ERROR_AMBIGUOUS_TARGET,
    ERROR_INVALID_ARGUMENTS,
    ERROR_TARGET_NOT_FOUND,
    ERROR_TOOL_UNAVAILABLE,
    ToolError,
    ToolNeedsClarification,
)

_LOGGER = logging.getLogger(__name__)

MAX_COMMAND_CHARS = 200
MAX_NAME_CHARS = 100
MAX_RUNNING = 10
STORE_VERSION = 1
MAX_DURATION = timedelta(hours=24)
TIMERS_UNAVAILABLE = "Timers need Home Assistant's built-in agent to support the Codie's language"

# What the Character says when a Timer's Command fails and the agent gave no speech of its own,
# by the Codie's AI language. These are the AI languages the Codie offers, keyed in lower case.
# English is the last resort. Each holds `{command}` once and no other placeholder.
FAILURE_MESSAGES = {
    "en": "I couldn't {command}.",
    "de": "Ich konnte „{command}“ nicht ausführen.",
    "es": "No pude ejecutar «{command}».",
    "fr": "Je n'ai pas pu exécuter « {command} ».",
    "it": "Non ho potuto eseguire «{command}».",
    "ja": "「{command}」を実行できませんでした。",
    "ko": '"{command}" 실행에 실패했어요.',
    "pt": "Não consegui executar “{command}”.",
    "zh": "我没能执行“{command}”。",
    "zh-tw": "我沒能執行「{command}」。",
}
# Other tags for Chinese in traditional characters.
_TRADITIONAL_CHINESE = ("zh-hant", "zh-hk", "zh-mo")
# Ends a sentence. A Command's own is dropped, since the message ends the sentence itself.
# The fullwidth "!" and "?" are escaped, as they look like the ASCII ones.
_SENTENCE_END = ".!?。\uff01\uff1f"


def timers_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Where the Timers of one Codie are kept, apart from every other Codie's."""
    return Store(hass, STORE_VERSION, f"codie.timers.{entry_id}")


async def async_remove_timers(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the Timers of a Codie that was removed."""
    await timers_store(hass, entry_id).async_remove()


def _built_in_agent(hass: HomeAssistant, language: str | None) -> Any | None:
    """Home Assistant's built-in agent if it can check and run Commands in `language`.

    The recognizer is not a public helper of Home Assistant. If it is missing, the Timer Tools
    are unavailable: a Timer is never started unchecked.
    """
    agent = get_agent_manager(hass).default_agent
    if agent is None or not language:
        return None
    if not all(
        callable(getattr(agent, method, None))
        for method in ("async_recognize_intent", "async_recognize_sentence_trigger")
    ):
        return None
    if not language_util.matches(language, agent.supported_languages):
        return None
    return agent


def timers_supported(hass: HomeAssistant, language: str | None) -> bool:
    """Whether the Timer Tools can be offered for a Codie whose AI language is `language`."""
    return _built_in_agent(hass, language) is not None


def _text(arguments: dict[str, Any], key: str, max_chars: int) -> str:
    """The text argument `key`, stripped, or "" if there is none."""
    value = arguments.get(key)
    if value is None:
        return ""
    if not isinstance(value, str) or len(value.strip()) > max_chars:
        raise ToolError(
            ERROR_INVALID_ARGUMENTS, f"{key} must be text of at most {max_chars} characters"
        )
    return value.strip()


def _duration(arguments: dict[str, Any]) -> timedelta:
    """The delay `hours`, `minutes` and `seconds` add up to, which must be within the limits."""
    parts: dict[str, int] = {}
    for unit in ("hours", "minutes", "seconds"):
        value = arguments.get(unit, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ToolError(ERROR_INVALID_ARGUMENTS, f"{unit} must be a whole number, 0 or more")
        parts[unit] = value
    delay = timedelta(**parts)
    if delay <= timedelta(0):
        raise ToolError(ERROR_INVALID_ARGUMENTS, "The Timer must last longer than zero")
    if delay > MAX_DURATION:
        raise ToolError(ERROR_INVALID_ARGUMENTS, "A Timer can last at most 24 hours")
    return delay


def failure_message(language: str | None, command: str) -> str:
    """What the Character says when `command` failed, in the AI language `language`.

    A language without its own message falls back to its base language, then to English. The
    Command loses its own sentence-ending punctuation; braces in it are not read as
    placeholders. In the English message, a capital on a first word such as "Turn" is dropped,
    as the Command continues the sentence. A word like "TV" keeps its capitals.
    """
    tag = (language or "").replace("_", "-").casefold()
    if tag.startswith(_TRADITIONAL_CHINESE):
        tag = "zh-tw"
    if tag not in FAILURE_MESSAGES:
        tag = tag.split("-", 1)[0]
    if tag not in FAILURE_MESSAGES:
        tag = "en"
    command = command.strip().rstrip(_SENTENCE_END).rstrip() or command
    if tag == "en" and (words := command.split(maxsplit=1)):
        first = words[0]
        if len(first) > 1 and first[0].isupper() and first[1:].islower():
            command = command[0].lower() + command[1:]
    return FAILURE_MESSAGES[tag].format(command=command)


def _failed(response: IntentResponse) -> bool:
    """Whether the agent answered with an error, or did the Command for only some targets."""
    return response.response_type is IntentResponseType.ERROR or bool(response.failed_results)


@dataclass
class _Timer:
    name: str
    command: str
    # When it ends, as a UTC timestamp.
    ends_at: float
    cancel: CALLBACK_TYPE | None = None

    def cancel_callback(self) -> None:
        """Stop the scheduled end of the Timer, if it is still scheduled."""
        if self.cancel is not None:
            self.cancel()


class CodieTimers:
    """The Timers of one Codie."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: Store[dict[str, Any]],
        language: str | None,
        announce: Callable[[str], Awaitable[None]],
    ) -> None:
        self._hass = hass
        self._store = store
        self._language = language
        # Has the Character speak a message at once.
        self._announce = announce
        self._timers: dict[str, _Timer] = {}
        # Makes checking the limits and adding a Timer one step: the Command check awaits.
        self._start_lock = asyncio.Lock()
        self._unloaded = False

    async def async_load(self) -> None:
        """Resume the stored Timers with the time they have left.

        One that ended while Home Assistant was down is dropped: a Command is never run late.
        """
        stored = await self._store.async_load() or {}
        now = dt_util.utcnow().timestamp()
        for item in stored.get("timers", []):
            try:
                timer = _Timer(item["name"], item["command"], float(item["ends_at"]))
            except KeyError, TypeError, ValueError:
                _LOGGER.warning("Dropping a stored Timer that cannot be read: %s", item)
                continue
            if timer.ends_at <= now:
                _LOGGER.warning(
                    "Dropping the Timer '%s': it ended while Home Assistant was not running, "
                    "so its Command '%s' was not run",
                    timer.name,
                    timer.command,
                )
                continue
            self._schedule(timer, timedelta(seconds=timer.ends_at - now))
        await self._async_save()

    @property
    def running(self) -> list[tuple[str, str, datetime]]:
        """The name, Command and end of each running Timer, soonest first."""
        return [
            (timer.name, timer.command, dt_util.utc_from_timestamp(timer.ends_at))
            for timer in sorted(self._timers.values(), key=lambda timer: timer.ends_at)
        ]

    async def async_start(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """The `timer_start` Tool."""
        command = _text(arguments, "command", MAX_COMMAND_CHARS)
        if not command:
            raise ToolError(ERROR_INVALID_ARGUMENTS, "The Command must not be empty")
        name = _text(arguments, "name", MAX_NAME_CHARS) or command
        delay = _duration(arguments)
        async with self._start_lock:
            self._check_room(name)
            await self._async_check(command)
            if self._unloaded:
                raise ToolError(ERROR_TOOL_UNAVAILABLE, TIMERS_UNAVAILABLE)
            timer = _Timer(name, command, dt_util.utcnow().timestamp() + delay.total_seconds())
            self._schedule(timer, delay)
            await self._async_save()
        return {"success": True, "result": f"Timer '{name}' will run: {command}"}

    def _check_room(self, name: str) -> None:
        """Fail if another Timer cannot be started under `name`."""
        if len(self._timers) >= MAX_RUNNING:
            raise ToolError(
                ERROR_INVALID_ARGUMENTS,
                f"{MAX_RUNNING} Timers are already running, the most there can be. Cancel one "
                "first.",
            )
        if name.casefold() in self._timers:
            raise ToolError(
                ERROR_INVALID_ARGUMENTS,
                f"A Timer named '{name}' is already running. Give this one another name.",
            )

    async def async_control(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """The `timer_control` Tool."""
        action = arguments.get("action")
        name = _text(arguments, "name", MAX_NAME_CHARS)
        if action == "status":
            timers = [self._find(name)] if name else list(self._timers.values())
            if not timers:
                return {"success": True, "result": "No Timers are running"}
            return {"success": True, "result": "\n".join(self._describe(t) for t in timers)}
        if action == "cancel":
            timer = self._find(name) if name else self._only()
            timer.cancel_callback()
            del self._timers[timer.name.casefold()]
            await self._async_save()
            return {"success": True, "result": f"Cancelled the Timer '{timer.name}'"}
        raise ToolError(ERROR_INVALID_ARGUMENTS, "action must be one of cancel, status")

    def _only(self) -> _Timer:
        """The one running Timer; asks which when there are several."""
        if len(self._timers) > 1:
            raise ToolNeedsClarification(
                ERROR_AMBIGUOUS_TARGET,
                f"More than one Timer is running: {self._names()}. Ask which one to cancel.",
            )
        if not self._timers:
            raise ToolError(ERROR_TARGET_NOT_FOUND, "No Timers are running")
        return next(iter(self._timers.values()))

    def _find(self, name: str) -> _Timer:
        """The Timer called `name` or, failing that, the only one whose name contains it."""
        key = name.casefold()
        if (timer := self._timers.get(key)) is not None:
            return timer
        fits = [timer for known, timer in self._timers.items() if key in known]
        if len(fits) == 1:
            return fits[0]
        if fits:
            raise ToolNeedsClarification(
                ERROR_AMBIGUOUS_TARGET,
                f"More than one Timer fits '{name}': {self._names(fits)}. Ask which one.",
            )
        running = self._names() if self._timers else "none"
        raise ToolError(ERROR_TARGET_NOT_FOUND, f"No Timer is named '{name}'. Running: {running}")

    def _names(self, timers: list[_Timer] | None = None) -> str:
        """The names of `timers`, or of every running Timer, for a message."""
        return ", ".join(t.name for t in timers or self._timers.values())

    def _describe(self, timer: _Timer) -> str:
        """One line for `timer`: its name, the time it has left and its Command."""
        left = max(0, math.ceil(timer.ends_at - dt_util.utcnow().timestamp()))
        hours, rest = divmod(left, 3600)
        minutes, seconds = divmod(rest, 60)
        parts = [f"{n} {unit}" for n, unit in ((hours, "h"), (minutes, "min"), (seconds, "s")) if n]
        return f"{timer.name}: {' '.join(parts) or '0 s'} left, runs: {timer.command}"

    def _schedule(self, timer: _Timer, delay: timedelta) -> None:
        timer.cancel = async_call_later(self._hass, delay, partial(self._async_end, timer))
        self._timers[timer.name.casefold()] = timer

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "timers": [
                    {"name": t.name, "command": t.command, "ends_at": t.ends_at}
                    for t in self._timers.values()
                ]
            }
        )

    @callback
    def async_unload(self) -> None:
        """Stop every callback. The Timers stay stored, so a reload resumes them."""
        self._unloaded = True
        for timer in self._timers.values():
            timer.cancel_callback()
        self._timers.clear()

    async def _async_end(self, timer: _Timer, _now: datetime) -> None:
        """Run the Command of a Timer that ended."""
        self._timers.pop(timer.name.casefold(), None)
        await self._async_save()
        try:
            result = await async_converse(
                self._hass,
                timer.command,
                conversation_id=None,
                context=Context(),
                language=self._language,
                agent_id=HOME_ASSISTANT_AGENT,
            )
        except Exception as err:
            await self._async_report_failure(timer, str(err) or type(err).__name__)
            return
        if _failed(result.response):
            # The agent's own speech is already in the AI language, which is often not English.
            speech = result.response.speech.get("plain", {}).get("speech", "")
            await self._async_report_failure(
                timer, speech or "Home Assistant could not do it", speech
            )

    async def _async_report_failure(self, timer: _Timer, reason: str, speech: str = "") -> None:
        """Log that the Command failed and have the Character say so in the AI language.

        The agent's own speech is said as it is. Without it, the Character says a set message in
        its language. The `reason`, often an English exception text, only goes to the log.
        """
        _LOGGER.warning(
            "Timer '%s' could not run its Command '%s': %s", timer.name, timer.command, reason
        )
        message = speech or failure_message(self._language, timer.command)
        try:
            await self._announce(message)
        except HomeAssistantError as err:
            _LOGGER.warning("Timer '%s' failed and the Codie could not say so: %s", timer.name, err)

    async def _async_check(self, command: str) -> None:
        """Fail unless the built-in agent would run `command` as an intent or sentence trigger."""
        agent = _built_in_agent(self._hass, self._language)
        if agent is None:
            raise ToolError(ERROR_TOOL_UNAVAILABLE, TIMERS_UNAVAILABLE)
        user_input = ConversationInput(
            text=command,
            context=Context(),
            conversation_id=None,
            device_id=None,
            satellite_id=None,
            language=self._language or "",
            agent_id=HOME_ASSISTANT_AGENT,
        )
        # Sentence triggers come first, as when the agent runs the Command.
        if await agent.async_recognize_sentence_trigger(user_input) is not None:
            return
        result = await agent.async_recognize_intent(user_input)
        if result is None:
            raise ToolError(
                ERROR_INVALID_ARGUMENTS,
                f"Home Assistant does not understand the Command '{command}'. Rephrase it as a "
                "short home-control sentence, such as 'turn off the kitchen lamp'.",
            )
        if result.unmatched_entities:
            # A sentence the agent knows, naming no entity or area it knows, such as a misheard
            # name. It would fail when the Timer ends, so it fails now instead.
            names = ", ".join(
                sorted(
                    repr(entity.text)
                    for entity in result.unmatched_entities_list
                    if isinstance(entity, UnmatchedTextEntity) and entity.text != MISSING_ENTITY
                )
            )
            raise ToolError(
                ERROR_TARGET_NOT_FOUND,
                f"No entity or area matches {names or 'a name'} in the Command "
                f"'{command}'. Call get_home_state for the exposed names and rephrase it.",
            )
