import contextlib
import functools
import logging
from datetime import UTC, datetime, timedelta

import yarl
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import api, zones
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

StateCoordinator = DataUpdateCoordinator[api.AggState]
# 'getDeviceStatus' wakes the appliance and it stays awake for ~20s afterwards, so
# polling it every 30s keeps the appliance awake around the clock. While a program
# runs it is awake anyway and polling is free; in standby we only ask the AI module
# for notifications, which doesn't disturb the appliance at all, and pay for a real
# status probe every few minutes to catch a program someone started by hand.
STATE_COORD_ACTIVE_INTERVAL = timedelta(seconds=30)
STATE_COORD_IDLE_INTERVAL = timedelta(seconds=60)
STATE_COORD_IDLE_PROBE_INTERVAL = timedelta(minutes=5)

UpdateCoordinator = DataUpdateCoordinator[api.AggUpdateStatus]
UPDATE_COORD_IDLE_INTERVAL = timedelta(hours=6)
UPDATE_COORD_ACTIVE_INTERVAL = timedelta(seconds=5)
# an update run doesn't always end in one go: the status drops back to 'idle' when
# an installation fails, the update is still available, and the appliance retries
# on its own a few minutes later. Falling back to the idle interval right there
# leaves the entity showing the old version for up to 6 hours, so keep an eye on it
# for a while - but only as long as the update is still pending. This is kept in
# memory only, after a restart we are back on the idle interval.
UPDATE_COORD_SETTLE_INTERVAL = timedelta(minutes=1)
UPDATE_COORD_SETTLE_PERIOD = timedelta(minutes=30)

ZoneCoordinator = DataUpdateCoordinator[zones.ZoneState]
# refrigerators are always awake, and the remaining time of a running SuperCool /
# timer is only interesting if it moves along
ZONE_COORD_INTERVAL = timedelta(seconds=30)
ZONE_MAX_STALE_UPDATES = 3

ProgramCoordinator = DataUpdateCoordinator[zones.ProgramState]
# 'getProgram' only tells which program is selected or running. It talks to the
# appliance itself, so it is polled no more often than the standby status probe: a
# program picked on the panel shows up within this interval, one picked through
# Home Assistant right away.
PROGRAM_COORD_INTERVAL = STATE_COORD_IDLE_PROBE_INTERVAL

ConfigCoordinator = DataUpdateCoordinator[api.AggConfig]
# the config tree is a snapshot of the appliance's settings, so it barely ever
# changes on its own. Polling it every few minutes only widens the window for the
# transient failures this API is prone to, so it is refreshed on demand instead:
# whenever the appliance starts or finishes a program, and after every write.
CONFIG_COORD_INTERVAL = timedelta(hours=1)

# how many consecutive bad updates we cover by keeping the previous data around.
# the appliance answers '200 []' or fails outright when it's busy and that must not
# reach the entities, but the grace period has to be bounded so a genuine change
# (ex. the eco counters being reset) still gets through.
CONFIG_MAX_STALE_UPDATES = 3
STATE_MAX_STALE_UPDATES = 3

# config and update are polled hours apart. Once the appliance stops answering,
# they look again a minute later instead, so their entities come back along with it.
# Only the next refresh is moved, after that their own interval applies again.
UNREACHABLE_RETRY_AFTER = 60  # seconds

type VZugConfigEntry = ConfigEntry[Shared]


class Shared:
    hass: HomeAssistant
    entry: VZugConfigEntry
    client: api.VZugApi

    state_coord: StateCoordinator
    update_coord: UpdateCoordinator
    config_coord: ConfigCoordinator
    zone_coord: ZoneCoordinator | None
    program_coord: ProgramCoordinator | None

    unique_id_prefix: str
    meta: api.AggMeta
    device_info: DeviceInfo

    def __init__(
        self,
        hass: HomeAssistant,
        entry: VZugConfigEntry,
        base_url: yarl.URL,
        credentials: api.Credentials | None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = api.VZugApi(
            base_url,
            credentials=credentials,
        )

        # 'always_update=False': the entities only show the data, so an unchanged
        # answer has nothing to tell them
        self.state_coord = DataUpdateCoordinator(
            hass,
            _LOGGER,
            config_entry=entry,
            always_update=False,
            name="state",
            update_interval=STATE_COORD_ACTIVE_INTERVAL,
            update_method=self._fetch_state,
        )
        self.update_coord = DataUpdateCoordinator(
            hass,
            _LOGGER,
            config_entry=entry,
            always_update=False,
            name="update",
            update_interval=UPDATE_COORD_IDLE_INTERVAL,
            update_method=self._fetch_update,
        )
        self.config_coord = DataUpdateCoordinator(
            hass,
            _LOGGER,
            config_entry=entry,
            always_update=False,
            name="config",
            update_interval=CONFIG_COORD_INTERVAL,
            update_method=self._fetch_config,
        )

        # only created for appliances that report zones, see '_setup_zones'
        self.zone_coord = None
        self.program_coord = None
        self._program_names: dict[int, str] = {}
        self._zone_modes: tuple[str, ...] = ()
        self._zone_stale_count = 0
        # durations (hours) the SuperCool / SuperFreeze / PartyCooling switches use,
        # set through their number entities
        self.boost_hours: dict[str, float] = {}

        # the rest will be set on first refresh
        self.unique_id_prefix = ""
        self._first_refresh_done = False
        self._config_stale_count = 0
        self._eco_stale_count = 0
        self._device_active: bool | None = None
        self._last_device_probe: datetime | None = None
        self._last_notification: str | None = None
        self._last_update_activity: datetime | None = None

    async def async_config_entry_first_refresh(self) -> None:
        # the appliance may simply be switched off (some turn off completely after a
        # program). Fail fast and let HA retry the setup with its own backoff instead
        # of blocking startup with our retries, or ending up in 'setup failed' for good.
        try:
            async with detect_auth_failed():
                self.meta = await self.client.aggregate_meta(attempts=1)
        except ConfigEntryAuthFailed:
            raise
        except Exception as exc:
            # HA only logs the message, not the chained exception
            raise ConfigEntryNotReady(
                f"unable to reach the appliance at {self.client.base_url}: {exc!r}"
            ) from exc

        await self.state_coord.async_config_entry_first_refresh()
        await self._setup_zones()

        try:
            await self._post_first_refresh()
        except Exception as exc:
            _LOGGER.exception("init failed")
            raise ConfigEntryNotReady() from exc

    async def async_refresh_the_rest(self) -> None:
        """The first refresh of everything the setup doesn't wait for.

        Over the appliance's single connection this takes several seconds, and Home
        Assistant only reports itself as started once every setup is done. The
        entities are added as their data arrives. A refresh that fails here is
        retried by the coordinator's polling, which their listeners keep going.
        """
        await self.update_coord.async_refresh()
        if self.zone_coord is not None:
            await self._refresh_zones()
        await self.config_coord.async_refresh()
        if self.program_coord is not None:
            await self._setup_programs()

    async def async_shutdown(self) -> None:
        await self.state_coord.async_shutdown()
        await self.update_coord.async_shutdown()
        await self.config_coord.async_shutdown()
        if self.zone_coord is not None:
            await self.zone_coord.async_shutdown()
        if self.program_coord is not None:
            await self.program_coord.async_shutdown()
        await self.client.aclose()

    async def _setup_zones(self) -> None:
        """Find out whether this appliance has zones.

        The platforms need to know which coordinator to listen to, everything else
        is left to 'async_refresh_the_rest'. Dishwashers and washing machines answer
        'getProgram' with a single entry without a zone, they get a program
        coordinator instead. It stays idle until '_setup_programs' finds names.
        """
        try:
            raw_programs = await self.client.get_raw_programs(attempts=2)
        except Exception as exc:
            _LOGGER.debug("'getProgram' failed, assuming no zones: %r", exc)
            return
        if not zones.has_zones(raw_programs):
            self.program_coord = DataUpdateCoordinator(
                self.hass,
                _LOGGER,
                config_entry=self.entry,
                always_update=False,
                name="programs",
                update_method=self._fetch_programs,
            )
            return

        self.zone_coord = DataUpdateCoordinator(
            self.hass,
            _LOGGER,
            config_entry=self.entry,
            always_update=False,
            name="zones",
            update_interval=ZONE_COORD_INTERVAL,
            update_method=self._fetch_zones,
        )

    async def _refresh_zones(self) -> None:
        """Learn which modes the appliance knows, then fetch the zones."""
        assert self.zone_coord is not None
        supported: list[str] = []
        for mode in zones.ALL_MODES:
            try:
                await self.client.get_cmd_value(mode, attempts=1)
            except Exception as exc:
                _LOGGER.debug("mode %s not supported: %r", mode, exc)
            else:
                supported.append(mode)
        self._zone_modes = tuple(supported)
        await self.zone_coord.async_refresh()

    async def _setup_programs(self) -> None:
        """Learn the names of the programs this appliance offers (dishwashers)."""
        assert self.program_coord is not None
        try:
            program_ids = await self.client.get_all_program_ids()
        except Exception as exc:
            _LOGGER.debug("'getAllProgramIds' failed, no program select: %r", exc)
            return

        names: dict[int, str] = {}
        for program_id in program_ids:
            try:
                details = await self.client.get_program_details(program_id)
            except Exception as exc:
                _LOGGER.debug("no details for program %s: %r", program_id, exc)
                continue
            if name := details.get("name"):
                names[program_id] = name
        if not names:
            return

        self._program_names = names
        self.program_coord.update_interval = PROGRAM_COORD_INTERVAL
        await self.program_coord.async_refresh()

    async def _fetch_programs(self) -> zones.ProgramState:
        previous = self.program_coord.data if self.program_coord else None
        try:
            async with detect_auth_failed():
                raw_programs = await self.client.get_raw_programs(attempts=2)
        except ConfigEntryAuthFailed:
            raise
        except api.NO_ANSWER as exc:
            raise UpdateFailed(f"no answer from the appliance: {exc!r}") from exc
        except Exception as exc:
            if previous is None:
                raise UpdateFailed(f"program update failed: {exc!r}") from exc
            _LOGGER.debug("program update failed, keeping previous data: %r", exc)
            return previous
        return zones.ProgramState(
            names=self._program_names,
            current=zones.current_program(raw_programs),
        )

    async def _fetch_zones(self) -> zones.ZoneState:
        previous = self.zone_coord.data if self.zone_coord else None
        try:
            async with detect_auth_failed():
                raw_programs = await self.client.get_raw_programs(attempts=2)
                modes = {
                    mode: await self.client.get_cmd_value(mode)
                    for mode in self._zone_modes
                }
            state = zones.ZoneState(zones=zones.index_zones(raw_programs), modes=modes)
            if not state.zones:
                raise UpdateFailed("'getProgram' returned no zones")
        except ConfigEntryAuthFailed:
            raise
        except api.NO_ANSWER as exc:
            raise UpdateFailed(f"no answer from the appliance: {exc!r}") from exc
        except Exception as exc:
            if previous is None or self._zone_stale_count >= ZONE_MAX_STALE_UPDATES:
                raise UpdateFailed(f"zone update failed: {exc!r}") from exc
            self._zone_stale_count += 1
            _LOGGER.debug(
                "zone update failed (%s/%s), keeping previous data: %r",
                self._zone_stale_count,
                ZONE_MAX_STALE_UPDATES,
                exc,
            )
            return previous

        self._zone_stale_count = 0
        return state

    async def _post_first_refresh(self) -> None:
        mac_addr = dr.format_mac(self.meta.mac_address)
        self.unique_id_prefix = mac_addr
        if not self.unique_id_prefix:
            _LOGGER.warning(
                "unable to determine unique id from device data: %s", self.meta
            )

        # left out rather than "" when the appliance doesn't tell (no 'getDeviceInfo'),
        # the registry then keeps what it already has
        optional = {
            "model_id": self.meta.model_id,
            "serial_number": self.meta.serial_number,
        }
        # built in one go, the entities are only created after this. Since HA 2026.11
        # it is a dataclass, '.update()' only works through a compatibility layer.
        self.device_info = DeviceInfo(
            configuration_url=str(self.client.base_url),
            identifiers={(DOMAIN, self.meta.serial_number)},
            name=self.meta.create_name(),
            hw_version=self.meta.ai_fw_version.get("HW"),
            sw_version=self.meta.ai_fw_version.get("SW"),
            connections={(dr.CONNECTION_NETWORK_MAC, mac_addr)},
            manufacturer="V-ZUG",
            model=self.meta.model_name,
            **{key: value for key, value in optional.items() if value},
        )

        self._first_refresh_done = True

    @functools.cached_property
    def device_id(self) -> str:
        """The appliance's id in the device registry, the parent of its zones."""
        # a child device needs its parent registered first, the entities on the
        # appliance itself may not have been added yet
        device = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id, **self.device_info
        )
        return device.id

    async def _fetch_state(self) -> api.AggState:
        previous = self.state_coord.data
        probe = self._device_probe_due()

        try:
            async with detect_auth_failed():
                state = await self.client.aggregate_state(
                    default_on_error=self._first_refresh_done,
                    include_device=probe,
                    include_eco=probe,
                )

                if self._note_notifications(state.notifications) and not probe:
                    # something happened at the appliance: a program finished, or a
                    # timed one started. It is awake now anyway, so this costs nothing.
                    _LOGGER.debug("new notification, fetching the full state")
                    probe = True
                    state = await self.client.aggregate_state(
                        default_on_error=self._first_refresh_done
                    )
                    self._refresh_config_soon()
        except api.NO_ANSWER as exc:
            # some appliances switch off completely after a program. The coordinator
            # marks the entities unavailable and logs it once.
            raise UpdateFailed(f"no answer from the appliance: {exc!r}") from exc

        if not probe:
            return self._carry_over(state, previous)

        self._last_device_probe = datetime.now(UTC)

        # an all-zero 'getEcoInfo' response is mapped to an empty EcoInfo, which would
        # put every eco sensor to 'unknown'. Keep the previous values for a while
        # instead - but not forever, the counters can legitimately be reset to 0.
        if not state.eco_info and previous is not None and previous.eco_info:
            if self._eco_stale_count < STATE_MAX_STALE_UPDATES:
                self._eco_stale_count += 1
                _LOGGER.debug(
                    "eco info is empty (%s/%s), keeping previous values",
                    self._eco_stale_count,
                    STATE_MAX_STALE_UPDATES,
                )
                state.eco_info = previous.eco_info
        else:
            self._eco_stale_count = 0

        self._track_activity(state.device)
        self.state_coord.update_interval = (
            STATE_COORD_IDLE_INTERVAL
            if self._device_active is False
            else STATE_COORD_ACTIVE_INTERVAL
        )
        return state

    async def async_probe_device(self) -> None:
        """Bring everything up to date, waking the appliance if necessary.

        In standby the state updates deliberately leave the appliance alone and the
        config tree is only refreshed hourly, so neither a plain refresh nor waiting
        gets fresh data. This is the way to ask for it anyway. Both coordinators are
        included because the appliance is being woken either way, and a settings
        change made on its panel is exactly the kind of thing someone would press
        this for.
        """
        self._last_device_probe = None
        await self.state_coord.async_request_refresh()
        await self.config_coord.async_request_refresh()

    def _device_probe_due(self) -> bool:
        """Whether this poll may wake the appliance up."""
        if self._device_active is not False:
            # running, or we don't know yet - either way it isn't asleep
            return True
        if self._last_device_probe is None:
            return True
        age = datetime.now(UTC) - self._last_device_probe
        return age >= STATE_COORD_IDLE_PROBE_INTERVAL

    def _note_notifications(self, notifications: list[api.PushNotification]) -> bool:
        """Remember the most recent notification, reporting whether it is new."""
        try:
            latest = notifications[0]["date"]
        except LookupError:
            return False

        previous, self._last_notification = self._last_notification, latest
        return previous is not None and previous != latest

    def _carry_over(
        self, state: api.AggState, previous: api.AggState | None
    ) -> api.AggState:
        """Fill in the values we deliberately didn't ask for."""
        if previous is None:
            return state

        # 'device_fetched_at' has to travel with the device data: ProgramEnd computes
        # the finishing time as 'device_fetched_at + remaining', so a fresh timestamp
        # on stale data would push the countdown further out on every poll
        state.device = previous.device
        state.device_fetched_at = previous.device_fetched_at
        state.eco_info = previous.eco_info
        state.zh_mode = previous.zh_mode
        return state

    def _track_activity(self, device: api.DeviceStatus) -> None:
        """Refresh the config tree whenever the appliance starts or finishes."""
        inactive = device.get("Inactive")
        if inactive not in ("true", "false"):
            # unknown state, don't guess
            return

        active = inactive == "false"
        if self._device_active is not None and active != self._device_active:
            # the eco statistics in the config tree ('ecomXstatXtotal' and friends)
            # are updated when a program completes, and someone standing at the
            # appliance may have changed settings on its panel
            _LOGGER.debug("device activity changed to %s, refreshing config", active)
            self._refresh_config_soon()
        self._device_active = active

    def _refresh_config_soon(self) -> None:
        # tied to the entry, so an unload cancels it before the client is closed
        self.entry.async_create_background_task(
            self.hass,
            self.config_coord.async_request_refresh(),
            f"{DOMAIN} config refresh",
        )

    async def _fetch_update(self) -> api.AggUpdateStatus:
        previous = self.update_coord.data
        try:
            async with detect_auth_failed():
                data = await self.client.aggregate_update_status(
                    supports_update_status=self.meta.supports_update_status(),
                    default_on_error=True,  # we allow the update to fail because it's not essential
                )
        except api.NO_ANSWER as exc:
            if previous is not None and not self._update_window_open():
                raise UpdateFailed(
                    f"no answer from the appliance: {exc!r}",
                    retry_after=UNREACHABLE_RETRY_AFTER,
                ) from exc
            # restarting into new firmware, which is what the carry-over below is
            # for, or this is the setup, which the update status mustn't fail: go on
            # as if every request had failed on its own
            data = api.AggUpdateStatus(
                update=api.UpdateStatus(),
                ai_fw_version=api.AiFwVersion(),
                hh_fw_version=api.HhFwVersion(),
            )

        status = data.update.get("status")
        if status not in ("idle", None):
            self.note_update_activity()
            self.update_coord.update_interval = UPDATE_COORD_ACTIVE_INTERVAL
        elif not self._update_window_open():
            self.update_coord.update_interval = UPDATE_COORD_IDLE_INTERVAL
        elif status is None:
            # the request failed - the appliance always sends a status. That is to
            # be expected while it restarts into new firmware, so keep watching and
            # don't let the entity flip to 'no update' in the meantime
            self.update_coord.update_interval = UPDATE_COORD_SETTLE_INTERVAL
            return self._carry_over_update(data, previous)
        elif self._update_pending(data.update):
            self.update_coord.update_interval = UPDATE_COORD_SETTLE_INTERVAL
        else:
            # installed, or gone: nothing left to wait for
            self.update_coord.update_interval = UPDATE_COORD_IDLE_INTERVAL

        if self._update_window_open():
            # 'getFWVersion' can fail on its own just as well
            return self._carry_over_update(data, previous)
        return data

    def note_update_activity(self) -> None:
        """Remember that an update is running, or has just been requested."""
        self._last_update_activity = datetime.now(UTC)

    def _update_window_open(self) -> bool:
        """Whether an update was running or requested within the settle period."""
        if self._last_update_activity is None:
            return False
        age = datetime.now(UTC) - self._last_update_activity
        return age < UPDATE_COORD_SETTLE_PERIOD

    @staticmethod
    def _update_pending(update: api.UpdateStatus) -> bool:
        return bool(
            update.get("isAIUpdateAvailable") or update.get("isHHGUpdateAvailable")
        )

    @staticmethod
    def _carry_over_update(
        data: api.AggUpdateStatus, previous: api.AggUpdateStatus | None
    ) -> api.AggUpdateStatus:
        """Fill in the parts that came back empty with the previous ones."""
        if previous is None:
            return data
        update = data.update
        installed = data.ai_fw_version.get("SW")
        if not update and installed in (None, previous.ai_fw_version.get("SW")):
            # the previous status is only worth keeping while it still describes the
            # installed firmware. Right after the new one is up, it would still offer
            # the update that was just installed.
            update = previous.update
        return api.AggUpdateStatus(
            update=update,
            ai_fw_version=data.ai_fw_version or previous.ai_fw_version,
            hh_fw_version=data.hh_fw_version or previous.hh_fw_version,
        )

    async def _fetch_config(self) -> api.AggConfig:
        previous = self.config_coord.data
        try:
            async with detect_auth_failed():
                config_tree = await self.client.aggregate_config()
            if not config_tree and previous:
                # the appliance answers '200 []' to 'getCategories' when it's busy.
                # An empty result is only believable if the previous one was empty too.
                raise UpdateFailed("getCategories returned empty")
        except ConfigEntryAuthFailed:
            raise
        except api.NO_ANSWER as exc:
            # the grace period is for a busy appliance, not for one that is gone
            raise UpdateFailed(
                f"no answer from the appliance: {exc!r}",
                retry_after=UNREACHABLE_RETRY_AFTER,
            ) from exc
        except Exception as exc:
            # 'previous is None' means we never had a successful update, so there is
            # nothing to keep and the caller should see the failure
            if previous is None:
                # its entities only appear with the first tree, don't wait an hour
                raise UpdateFailed(
                    f"config update failed: {exc!r}",
                    retry_after=UNREACHABLE_RETRY_AFTER,
                ) from exc
            if self._config_stale_count >= CONFIG_MAX_STALE_UPDATES:
                raise UpdateFailed(f"config update failed: {exc!r}") from exc
            self._config_stale_count += 1
            _LOGGER.debug(
                "config update failed (%s/%s), keeping previous data: %r",
                self._config_stale_count,
                CONFIG_MAX_STALE_UPDATES,
                exc,
            )
            return previous

        self._config_stale_count = 0
        return config_tree


@contextlib.asynccontextmanager
async def detect_auth_failed():
    try:
        yield
    except api.AuthenticationFailed:
        raise ConfigEntryAuthFailed
