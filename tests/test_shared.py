from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.vzug.api import (
    AggState,
    AggUpdateStatus,
    AuthenticationFailed,
    VZugApi,
)
from custom_components.vzug.button import CheckUpdate
from custom_components.vzug.sensor import Program
from custom_components.vzug.shared import (
    STATE_COORD_ACTIVE_INTERVAL,
    STATE_COORD_IDLE_INTERVAL,
    STATE_COORD_IDLE_PROBE_INTERVAL,
    STATE_MAX_STALE_UPDATES,
    UPDATE_COORD_ACTIVE_INTERVAL,
    UPDATE_COORD_IDLE_INTERVAL,
    UPDATE_COORD_SETTLE_INTERVAL,
    UPDATE_COORD_SETTLE_PERIOD,
    UNREACHABLE_RETRY_AFTER,
    Shared,
)

from .test_api import _json_response, _status_response

_ECO_INFO = {"water": {"total": 80085.5}, "energy": {"total": 615.7}}


@pytest.fixture
def shared():
    """A Shared instance without the HomeAssistant machinery around it."""
    instance = Shared.__new__(Shared)
    instance.hass = MagicMock()
    instance.entry = MagicMock()
    instance.client = MagicMock()
    instance.config_coord = MagicMock()
    instance.state_coord = MagicMock()
    instance.state_coord.data = None
    instance.update_coord = MagicMock()
    instance.update_coord.data = None
    instance._first_refresh_done = True
    instance._eco_stale_count = 0
    instance._device_active = None
    instance._last_device_probe = None
    instance._last_notification = None
    instance._last_update_activity = None
    return instance


def _config_refreshes(shared: Shared) -> int:
    # bound to the entry, so an unload cancels them
    assert not shared.hass.async_create_task.called
    return shared.entry.async_create_background_task.call_count


def _state(eco_info):
    return AggState(
        zh_mode=2,
        device={"Inactive": "true"},
        device_fetched_at=datetime(2026, 8, 11, 12, 0, tzinfo=UTC),
        notifications=[],
        eco_info=eco_info,
    )


async def _poll(shared: Shared, eco_info) -> AggState:
    """Run one update and store the result the way the coordinator would."""
    shared.client.aggregate_state = AsyncMock(return_value=_state(eco_info))
    # eco info is only fetched on a poll that is allowed to wake the appliance, so
    # force one - otherwise these tests would pass without reaching the code at all
    shared._last_device_probe = None
    state = await shared._fetch_state()
    shared.state_coord.data = state
    return state


@pytest.mark.asyncio
async def test_zeroed_eco_info_keeps_the_previous_values(shared):
    """An all-zero response would otherwise put every eco sensor to 'unknown'."""
    await _poll(shared, _ECO_INFO)

    state = await _poll(shared, {})

    assert state.eco_info == _ECO_INFO


@pytest.mark.asyncio
async def test_a_real_response_resets_the_grace_period(shared):
    await _poll(shared, _ECO_INFO)
    await _poll(shared, {})
    assert shared._eco_stale_count == 1

    await _poll(shared, _ECO_INFO)

    assert shared._eco_stale_count == 0


@pytest.mark.asyncio
async def test_the_grace_period_is_bounded(shared):
    """'ecomXstatXtotalXclear' resets the counters, that has to reach the sensors."""
    await _poll(shared, _ECO_INFO)

    for _ in range(STATE_MAX_STALE_UPDATES):
        assert (await _poll(shared, {})).eco_info == _ECO_INFO

    assert (await _poll(shared, {})).eco_info == {}


@pytest.mark.asyncio
async def test_without_previous_values_there_is_nothing_to_keep(shared):
    """The very first update has no history to fall back on."""
    state = await _poll(shared, {})

    assert state.eco_info == {}
    assert shared._eco_stale_count == 0


def test_first_state_does_not_refresh_config(shared):
    """Nothing to compare against yet, so there is nothing to react to."""
    shared._track_activity({"Inactive": "true"})

    assert shared._device_active is False
    assert _config_refreshes(shared) == 0


def test_program_start_refreshes_config(shared):
    shared._track_activity({"Inactive": "true"})
    shared._track_activity({"Inactive": "false"})

    assert shared._device_active is True
    assert _config_refreshes(shared) == 1


def test_program_end_refreshes_config(shared):
    """The eco statistics in the config tree are updated on completion."""
    shared._track_activity({"Inactive": "false"})
    shared._track_activity({"Inactive": "true"})

    assert _config_refreshes(shared) == 1


def test_unchanged_activity_does_not_refresh_config(shared):
    for _ in range(5):
        shared._track_activity({"Inactive": "true"})

    assert _config_refreshes(shared) == 0


def test_unknown_activity_is_ignored(shared):
    """An appliance which doesn't report 'Inactive' must not trigger anything."""
    shared._track_activity({"Inactive": "false"})
    shared._track_activity({})
    shared._track_activity({"Inactive": "maybe"})

    # the last known state is kept, so a later 'true' still counts as a change
    assert shared._device_active is True
    assert _config_refreshes(shared) == 0

    shared._track_activity({"Inactive": "true"})
    assert _config_refreshes(shared) == 1


_NOTIFICATION = "2026-08-09T15:14:46Z"


def _full_state(*, inactive: str = "true", notification: str = _NOTIFICATION):
    """What aggregate_state returns for a poll that asked for everything."""
    return AggState(
        zh_mode=2,
        device={"Inactive": inactive, "Program": ""},
        device_fetched_at=datetime(2026, 8, 9, 15, 0, tzinfo=UTC),
        notifications=[{"date": notification, "message": "Programm beendet"}],
        eco_info=_ECO_INFO,
    )


def _light_state(notification: str = _NOTIFICATION):
    """What it returns when the device fetch was skipped."""
    return AggState(
        zh_mode=-1,
        device={},
        device_fetched_at=datetime.now(UTC),
        notifications=[{"date": notification, "message": "Programm beendet"}],
        eco_info={},
    )


def _in_standby(shared: Shared) -> None:
    shared.state_coord.data = _full_state()
    shared._device_active = False
    shared._last_device_probe = datetime.now(UTC)
    shared._last_notification = _NOTIFICATION


@pytest.mark.asyncio
async def test_first_poll_asks_for_everything(shared):
    """We don't know the appliance's state yet, so we must not skip anything."""
    shared.client.aggregate_state = AsyncMock(return_value=_full_state())

    await shared._fetch_state()

    kwargs = shared.client.aggregate_state.call_args.kwargs
    assert kwargs["include_device"] is True
    assert kwargs["include_eco"] is True
    assert shared.state_coord.update_interval == STATE_COORD_IDLE_INTERVAL


@pytest.mark.asyncio
async def test_standby_poll_leaves_the_appliance_alone(shared):
    """In standby only the AI module is asked, which doesn't wake the appliance."""
    _in_standby(shared)
    previous = shared.state_coord.data
    shared.client.aggregate_state = AsyncMock(return_value=_light_state())

    state = await shared._fetch_state()

    kwargs = shared.client.aggregate_state.call_args.kwargs
    assert kwargs["include_device"] is False
    assert kwargs["include_eco"] is False

    # the skipped values are carried over, timestamp included - a fresh one would
    # push the ProgramEnd countdown further out on every poll
    assert state.device == previous.device
    assert state.device_fetched_at == previous.device_fetched_at
    assert state.eco_info == previous.eco_info
    # a deliberately skipped fetch is not a failure and must not burn the grace period
    assert shared._eco_stale_count == 0


@pytest.mark.asyncio
async def test_standby_probes_the_device_after_the_interval(shared):
    _in_standby(shared)
    shared._last_device_probe = datetime.now(UTC) - STATE_COORD_IDLE_PROBE_INTERVAL
    shared.client.aggregate_state = AsyncMock(return_value=_full_state())

    await shared._fetch_state()

    assert shared.client.aggregate_state.call_args.kwargs["include_device"] is True


@pytest.mark.asyncio
async def test_running_program_is_polled_in_full(shared):
    """While a program runs the appliance is awake anyway, so polling is free."""
    _in_standby(shared)
    shared._device_active = True
    shared.client.aggregate_state = AsyncMock(
        return_value=_full_state(inactive="false")
    )

    await shared._fetch_state()

    assert shared.client.aggregate_state.call_args.kwargs["include_device"] is True
    assert shared.state_coord.update_interval == STATE_COORD_ACTIVE_INTERVAL


@pytest.mark.asyncio
async def test_new_notification_triggers_a_full_poll(shared):
    """A program finished or a timed one started - fetch everything right away."""
    _in_standby(shared)
    shared._last_notification = "2026-08-09T09:06:42Z"
    shared.client.aggregate_state = AsyncMock(
        side_effect=[_light_state(), _full_state(inactive="false")]
    )

    state = await shared._fetch_state()

    assert shared.client.aggregate_state.call_count == 2
    assert "include_device" not in shared.client.aggregate_state.call_args.kwargs
    assert state.device == {"Inactive": "false", "Program": ""}
    assert _config_refreshes(shared) >= 1


@pytest.mark.asyncio
async def test_probe_request_reaches_the_appliance(shared):
    """A refresh asked for by hand must not be answered from standby shortcuts."""
    _in_standby(shared)
    shared.state_coord.async_request_refresh = AsyncMock()
    shared.config_coord.async_request_refresh = AsyncMock()

    await shared.async_probe_device()

    # the settings are hourly otherwise, and changing one on the appliance's panel
    # is exactly what someone would press this for
    shared.config_coord.async_request_refresh.assert_awaited_once()

    shared.state_coord.async_request_refresh.assert_awaited_once()
    assert shared._device_probe_due() is True

    # and the update it triggers really does ask for everything
    shared.client.aggregate_state = AsyncMock(return_value=_full_state())
    await shared._fetch_state()
    assert shared.client.aggregate_state.call_args.kwargs["include_device"] is True


@pytest.mark.asyncio
async def test_unchanged_notification_does_not_trigger_a_poll(shared):
    _in_standby(shared)
    shared.client.aggregate_state = AsyncMock(return_value=_light_state())

    await shared._fetch_state()

    assert shared.client.aggregate_state.call_count == 1


@pytest.mark.asyncio
async def test_unreachable_appliance_retries_the_setup(shared):
    """Some appliances switch off completely, that must not fail the setup for good."""
    shared.client.aggregate_meta = AsyncMock(side_effect=httpx.ConnectTimeout(""))
    shared.state_coord.async_config_entry_first_refresh = AsyncMock()

    with pytest.raises(ConfigEntryNotReady):
        await shared.async_config_entry_first_refresh()

    # HA retries with its own backoff, our retries would only block the startup
    shared.client.aggregate_meta.assert_awaited_once_with(attempts=1)
    shared.state_coord.async_config_entry_first_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_credentials_are_not_retried(shared):
    """Retrying won't fix the credentials, HA has to ask for new ones."""
    shared.client.aggregate_meta = AsyncMock(side_effect=AuthenticationFailed())

    with pytest.raises(ConfigEntryAuthFailed):
        await shared.async_config_entry_first_refresh()


def _update_status(
    status: str | None,
    *,
    available: bool = False,
    hhg_available: bool = False,
    sw: str | None = "1052633-R19",
) -> AggUpdateStatus:
    update = {}
    if status is not None:
        update = {
            "status": status,
            "isAIUpdateAvailable": available,
            "isHHGUpdateAvailable": hhg_available,
        }
    return AggUpdateStatus(
        update=update,
        ai_fw_version={"SW": sw} if sw else {},
        hh_fw_version={},
    )


async def _poll_update(shared: Shared, status: str | None, **kwargs):
    """Run one update and store the result the way the coordinator would."""
    shared.meta = MagicMock()
    shared.client.aggregate_update_status = AsyncMock(
        return_value=_update_status(status, **kwargs)
    )
    shared.update_coord.data = await shared._fetch_update()
    return shared.update_coord.update_interval


@pytest.mark.asyncio
async def test_pending_update_alone_does_not_speed_up_polling(shared):
    """An update nobody has started yet is no reason to poll more often."""
    assert await _poll_update(shared, "idle", available=True) == (
        UPDATE_COORD_IDLE_INTERVAL
    )


@pytest.mark.asyncio
async def test_failed_update_keeps_polling_until_it_went_through(shared):
    """The appliance retries a failed installation on its own a few minutes later."""
    assert await _poll_update(shared, "installing", available=True) == (
        UPDATE_COORD_ACTIVE_INTERVAL
    )
    # failed: back to 'idle', but the update is still there
    assert await _poll_update(shared, "idle", available=True) == (
        UPDATE_COORD_SETTLE_INTERVAL
    )
    # the retry went through
    assert await _poll_update(shared, "idle", sw="1052633-R20") == (
        UPDATE_COORD_IDLE_INTERVAL
    )
    assert shared.update_coord.data.ai_fw_version["SW"] == "1052633-R20"


@pytest.mark.asyncio
async def test_pending_hhg_update_settles_too(shared):
    shared.note_update_activity()

    assert await _poll_update(shared, "idle", hhg_available=True) == (
        UPDATE_COORD_SETTLE_INTERVAL
    )


@pytest.mark.asyncio
async def test_settling_is_bounded(shared):
    """If the appliance never retries, we don't keep waking it every minute."""
    shared._last_update_activity = (
        datetime.now(UTC) - UPDATE_COORD_SETTLE_PERIOD - timedelta(seconds=1)
    )

    assert await _poll_update(shared, "idle", available=True) == (
        UPDATE_COORD_IDLE_INTERVAL
    )


@pytest.mark.asyncio
async def test_requested_update_settles_even_if_it_has_not_started_yet(shared):
    """Right after 'doAIUpdate' the status can still read 'idle'."""
    shared.note_update_activity()

    assert await _poll_update(shared, "idle", available=True) == (
        UPDATE_COORD_SETTLE_INTERVAL
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before",
    [
        pytest.param("installing", id="mid-run"),
        pytest.param("idle", id="while-settling"),
        pytest.param(None, id="right-after-the-request"),
    ],
)
async def test_failed_request_keeps_watching_the_update(shared, before):
    """The AI module restarts into new firmware, so failures are to be expected."""
    shared.note_update_activity()
    if before is not None:
        await _poll_update(shared, before, available=True)

    assert await _poll_update(shared, None, sw=None) == UPDATE_COORD_SETTLE_INTERVAL
    if before is not None:
        # and the entity doesn't flip to 'no update' meanwhile
        data = shared.update_coord.data
        assert data.update["isAIUpdateAvailable"] is True
        assert data.ai_fw_version["SW"] == "1052633-R19"


@pytest.mark.asyncio
async def test_failed_firmware_read_keeps_the_installed_version(shared):
    await _poll_update(shared, "installing", available=True)

    await _poll_update(shared, "idle", available=True, sw=None)

    assert shared.update_coord.data.ai_fw_version["SW"] == "1052633-R19"


@pytest.mark.asyncio
async def test_failed_request_outside_an_update_is_left_alone(shared):
    """Away from an update run nothing changes: no extra polls, no stale data."""
    await _poll_update(shared, "idle")

    assert await _poll_update(shared, None, sw=None) == UPDATE_COORD_IDLE_INTERVAL
    assert shared.update_coord.data.update == {}


@pytest.mark.asyncio
async def test_status_is_not_carried_over_past_a_new_firmware(shared):
    """Otherwise the update that was just installed would be offered once more."""
    await _poll_update(shared, "installing", available=True)

    await _poll_update(shared, None, sw="1052633-R20")

    data = shared.update_coord.data
    assert data.ai_fw_version["SW"] == "1052633-R20"
    assert data.update == {}


@pytest.mark.asyncio
async def test_failed_requests_do_not_extend_the_window(shared):
    """Only a running update or an install request opens it."""
    shared._last_update_activity = (
        datetime.now(UTC) - UPDATE_COORD_SETTLE_PERIOD - timedelta(seconds=1)
    )

    assert await _poll_update(shared, None, sw=None) == UPDATE_COORD_IDLE_INTERVAL


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ConnectError(""), httpx.ConnectTimeout("")])
async def test_unreachable_appliance_makes_the_state_unavailable(shared, error):
    """No answer must not become empty data: 'Program' would read 'active'."""
    shared.client = VZugApi(base_url="http://example.com")
    with (
        patch.object(shared.client._client, "get", side_effect=error) as get,
        pytest.raises(UpdateFailed),
    ):
        await shared._fetch_state()

    # the warm-up is the first request, nothing else waits for the appliance
    assert get.call_count == 1


@pytest.mark.asyncio
async def test_a_failing_hh_command_still_degrades_gracefully(shared):
    """The appliance answers, just not to 'getEcoInfo': keep the rest."""
    answers = {
        "getZHMode": {"value": 2},
        "getDeviceStatus": {"Inactive": "true", "Program": ""},
        "getLastPUSHNotifications": [],
    }

    async def get(url):
        command = url.split("command=")[1].split("&")[0]
        if command in answers:
            return _json_response(answers[command])
        return _status_response(503)

    shared.client = VZugApi(base_url="http://example.com")
    with (
        patch.object(shared.client._client, "get", side_effect=get),
        patch("custom_components.vzug.api.asyncio.sleep", new_callable=AsyncMock),
    ):
        state = await shared._fetch_state()

    assert state.device == {"Inactive": "true", "Program": ""}
    assert state.eco_info == {}


_AWAKE = {
    "getZHMode": {"value": 2},
    "getDeviceStatus": {"Inactive": "true", "Program": ""},
    "getLastPUSHNotifications": [],
    "getEcoInfo": _ECO_INFO,
}


async def _fetch_state_timing_out(shared: Shared, timeouts: dict[str, int]):
    """A state poll where each command times out the given number of times first."""

    async def get(url):
        command = url.split("command=")[1].split("&")[0]
        if timeouts.get(command, 0) > 0:
            timeouts[command] -= 1
            raise httpx.ReadTimeout("")
        return _json_response(_AWAKE[command])

    shared.client = VZugApi(base_url="http://example.com")
    with (
        patch.object(shared.client._client, "get", side_effect=get),
        patch("custom_components.vzug.api.asyncio.sleep", new_callable=AsyncMock),
    ):
        return await shared._fetch_state()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["getZHMode", "getDeviceStatus"])
async def test_waking_appliance_gets_a_second_chance(shared, command):
    """Seen on an AdoraWash V2000: asleep, it misses the read timeout once."""
    state = await _fetch_state_timing_out(shared, {command: 1})

    assert state.zh_mode == 2
    assert state.device == _AWAKE["getDeviceStatus"]
    assert state.eco_info == _ECO_INFO


@pytest.mark.asyncio
async def test_slow_appliance_degrades_like_a_bad_answer(shared):
    """It took the connection, so it is there: no reason to make it unavailable."""
    state = await _fetch_state_timing_out(shared, dict.fromkeys(_AWAKE, 2))

    assert state == AggState(
        zh_mode=-1,
        device={},
        device_fetched_at=state.device_fetched_at,
        notifications=[],
        eco_info={},
    )
    # and it says nothing about whether the appliance knows 'getZHMode'
    assert shared.client._zh_mode_warmup_failures == 0


def _state_entity_shared(device) -> SimpleNamespace:
    coord = MagicMock(last_update_success=True)
    coord.data = AggState(
        zh_mode=-1,
        device=device,
        device_fetched_at=datetime.now(UTC),
        notifications=[],
        eco_info={},
    )
    return SimpleNamespace(
        state_coord=coord, unique_id_prefix="fc:1b:ff:06:6e:8e", device_info={}
    )


@pytest.mark.parametrize(
    ("device", "expected"),
    [
        ({}, None),
        ({"Inactive": "true", "Program": ""}, "standby"),
        ({"Inactive": "false", "Program": ""}, "active"),
        ({"Inactive": "false", "Program": "Eco"}, "Eco"),
    ],
)
def test_program_does_not_guess_without_device_data(device, expected):
    assert Program(_state_entity_shared(device)).native_value == expected


def test_check_update_is_unavailable_with_the_appliance():
    shared = _state_entity_shared({})
    button = CheckUpdate(shared)
    assert button.available

    shared.state_coord.last_update_success = False
    assert not button.available


async def _unreachable_update(shared: Shared):
    shared.meta = MagicMock()
    shared.client.aggregate_update_status = AsyncMock(
        side_effect=httpx.ConnectTimeout("")
    )
    shared.update_coord.data = await shared._fetch_update()


@pytest.mark.asyncio
async def test_unreachable_appliance_makes_the_update_unavailable(shared):
    await _poll_update(shared, "idle")

    with pytest.raises(UpdateFailed) as failed:
        await _unreachable_update(shared)

    # polled every 6 hours otherwise, it should come back along with the appliance
    assert failed.value.retry_after == UNREACHABLE_RETRY_AFTER
    assert shared.update_coord.update_interval == UPDATE_COORD_IDLE_INTERVAL


@pytest.mark.asyncio
async def test_restart_into_new_firmware_keeps_watching_the_update(shared):
    """It doesn't answer while it restarts, the carry-over is there for that."""
    await _poll_update(shared, "installing", available=True)

    await _unreachable_update(shared)

    assert shared.update_coord.update_interval == UPDATE_COORD_SETTLE_INTERVAL
    data = shared.update_coord.data
    assert data.update["isAIUpdateAvailable"] is True
    assert data.ai_fw_version["SW"] == "1052633-R19"


@pytest.mark.asyncio
async def test_unreachable_update_does_not_fail_the_setup(shared):
    """The update status isn't essential, the setup goes on without it."""
    await _unreachable_update(shared)

    assert shared.update_coord.data.update == {}
    assert shared.update_coord.update_interval == UPDATE_COORD_IDLE_INTERVAL


@pytest.mark.asyncio
async def test_unreachable_appliance_makes_the_config_unavailable(shared):
    """The grace period is for a busy appliance, not for one that is gone."""
    previous = {"settings": MagicMock()}
    shared.config_coord.data = previous
    shared._config_stale_count = 0
    shared.client.aggregate_config = AsyncMock(side_effect=httpx.ConnectError(""))

    with pytest.raises(UpdateFailed) as failed:
        await shared._fetch_config()
    assert failed.value.retry_after == UNREACHABLE_RETRY_AFTER

    # a bad answer still keeps the previous tree
    shared.client.aggregate_config = AsyncMock(side_effect=ValueError("busy"))
    assert await shared._fetch_config() is previous
