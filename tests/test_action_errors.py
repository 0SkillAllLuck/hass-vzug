"""Entity actions raise translated errors instead of "Unknown error"."""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.vzug import api, zones
from custom_components.vzug.button import SendProgram
from custom_components.vzug.helpers import action_errors
from custom_components.vzug.number import Timer
from custom_components.vzug.select import ProgramSelect
from custom_components.vzug.switch import Mode

from .test_update_install import _entity as _update_entity
from .test_zone_entities import _shared as _zone_shared

COMPONENT = Path(__file__).parent.parent / "custom_components" / "vzug"
_REQUEST = httpx.Request("GET", "http://appliance/hh")


def _status_error(code: int) -> httpx.HTTPStatusError:
    response = httpx.Response(code, request=_REQUEST)
    return httpx.HTTPStatusError("refused", request=_REQUEST, response=response)


def _program_shared(current=None):
    coord = MagicMock()
    coord.data = zones.ProgramState(names={51: "Automatic"}, current=current)
    coord.async_refresh = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    return SimpleNamespace(
        program_coord=coord,
        client=MagicMock(set_program=AsyncMock()),
        unique_id_prefix="fc:1b:ff:00:00:00",
        device_info={},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "key"),
    [
        (httpx.ConnectError("unreachable", request=_REQUEST), "no_answer"),
        (httpx.ReadTimeout("too slow", request=_REQUEST), "no_answer"),
        (_status_error(400), "request_failed"),
        (_status_error(503), "request_failed"),
        (httpx.RemoteProtocolError("dropped", request=_REQUEST), "request_failed"),
        (AssertionError("empty response rejected"), "request_failed"),
        (ValueError("device returned an error response"), "request_failed"),
    ],
)
async def test_failures_map_to_translated_errors(error, key):
    shared = MagicMock()
    with pytest.raises(HomeAssistantError) as raised:
        async with action_errors(shared):
            raise error
    assert type(raised.value) is HomeAssistantError
    assert (raised.value.translation_domain, raised.value.translation_key) == (
        "vzug",
        key,
    )
    assert raised.value.__cause__ is error
    shared.entry.async_start_reauth.assert_not_called()


@pytest.mark.asyncio
async def test_rejected_credentials_start_reauth():
    shared = MagicMock()
    with pytest.raises(HomeAssistantError) as raised:
        async with action_errors(shared):
            raise api.AuthenticationFailed
    assert raised.value.translation_key == "auth_failed"
    shared.entry.async_start_reauth.assert_called_once_with(shared.hass)


@pytest.mark.asyncio
async def test_switch_without_an_answer():
    shared = _zone_shared()
    shared.client.set_cmd_value.side_effect = httpx.ConnectTimeout("too slow")
    holiday = Mode(shared, mode="HolidayMode")
    with pytest.raises(HomeAssistantError) as raised:
        await holiday.async_turn_on()
    assert raised.value.translation_key == "no_answer"
    shared.zone_coord.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_number_refused():
    shared = _zone_shared()
    shared.client.set_zone_value.side_effect = _status_error(400)
    timer = Timer(shared, zone="countdown1")
    with pytest.raises(HomeAssistantError) as raised:
        await timer.async_set_native_value(5)
    assert raised.value.translation_key == "request_failed"


@pytest.mark.asyncio
async def test_select_unknown_program():
    select = ProgramSelect(_program_shared())
    with pytest.raises(ServiceValidationError) as raised:
        await select.async_select_option("nonexistent")
    assert raised.value.translation_key == "unknown_program"
    assert raised.value.translation_placeholders == {"option": "nonexistent"}


@pytest.mark.asyncio
async def test_send_program_without_a_selection():
    shared = _program_shared(current=None)
    with pytest.raises(ServiceValidationError) as raised:
        await SendProgram(shared).async_press()
    assert raised.value.translation_key == "no_program_selected"
    shared.client.set_program.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_of_an_unknown_component():
    entity = _update_entity("XYZ")
    with pytest.raises(HomeAssistantError) as raised:
        await entity.async_install(None, False)
    assert raised.value.translation_key == "unknown_update_component"
    assert raised.value.translation_placeholders == {"name": "XYZ"}


def test_every_exception_is_translated():
    used = {
        key
        for path in COMPONENT.glob("*.py")
        for key in re.findall(
            r'translation_domain=DOMAIN,\s*translation_key="(\w+)"', path.read_text()
        )
    }
    assert used >= {"auth_failed", "no_answer", "request_failed", "unknown_program"}
    for lang in ("en", "de", "fr"):
        strings = json.loads((COMPONENT / "translations" / f"{lang}.json").read_text())
        assert set(strings["exceptions"]) == used, lang
