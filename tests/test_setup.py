"""The whole setup of an entry, against the recorded answers in tests/fixtures."""

import asyncio
import importlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.vzug import async_setup_entry, async_unload_entry
from custom_components.vzug.api import VZugApi
from custom_components.vzug.const import CONF_BASE_URL
from custom_components.vzug.shared import (
    DETECTION_RETRY_INTERVAL,
    UNREACHABLE_RETRY_AFTER,
    Shared,
)

FIXTURES = Path(__file__).parent / "fixtures"


class FakeAppliance:
    """Answers like the emulator in tests/fixtures/start_single_emulator.py.

    Except for 'getProgram' with an id, which the emulator answers like the plain
    one: this one gives every program a name, the way a real dishwasher does.
    """

    def __init__(self, device: str) -> None:
        self.responses = FIXTURES / device / "responses"
        self.categories = {c["id"]: c for c in self._json("hh_get_categories.json")}
        self.commands = {
            c["id"]: c["details"] for c in self._json("hh_get_command_details.json")
        }
        self.requests: list[str] = []
        # commands answered the way a busy appliance does
        self.busy: set[str] = set()

    def _json(self, name: str):
        return json.loads((self.responses / name).read_text())

    async def get(self, url: str) -> httpx.Response:
        split = urlsplit(url)
        query = parse_qs(split.query)
        command, value = query["command"][0], query.get("value", [None])[0]
        self.requests.append(command)
        request = httpx.Request("GET", url)

        if command in self.busy:
            return httpx.Response(400, json={"error": "400.03"}, request=request)
        if command == "getCategories":
            return httpx.Response(200, json=list(self.categories), request=request)
        if command in ("getCategory", "getCommands", "getCommand"):
            if command == "getCommand":
                found = self.commands.get(value)
            else:
                category = self.categories.get(value, {})
                found = category.get(
                    "category" if command == "getCategory" else "commands"
                )
            if found is None:
                return httpx.Response(400, json={"error": "400.03"}, request=request)
            return httpx.Response(200, json=found, request=request)
        if command == "getProgram" and value:
            program_id = json.loads(value)["id"]
            body = [{"id": program_id, "name": f"Program {program_id}"}]
            return httpx.Response(200, json=body, request=request)

        name = "getprogram" if command == "getProgram" else command[3:].lower()
        component = split.path.rsplit("/", 1)[-1]
        files = list(self.responses.glob(f"{component}_get_{name}.*"))
        if not files:
            # what the emulator answers to a command it doesn't know
            return httpx.Response(404, json={"error": {"code": 404}}, request=request)
        content = files[0].read_text().strip()
        status = 404 if '"code": 404' in content else 200
        return httpx.Response(status, content=content, request=request)


class Setup:
    """An entry set up the way Home Assistant does it, entities collected by id."""

    def __init__(self, device: str) -> None:
        self.appliance = FakeAppliance(device)
        self.entities: dict[str, object] = {}
        self.background: list = []
        self.hass = MagicMock(data={}, is_stopping=False)
        self.hass.config_entries.async_forward_entry_setups = self._forward
        self.entry = MagicMock(
            entry_id="entry",
            data={CONF_BASE_URL: "http://appliance"},
            state=ConfigEntryState.SETUP_IN_PROGRESS,
            pref_disable_polling=False,
        )
        self.entry.async_create_background_task = (
            lambda hass, target, name, eager_start=True: self.background.append(target)
        )
        self.on_unload: list = []
        self.entry.async_on_unload = self.on_unload.append
        # what 'async_call_later' was asked to run, with its delay
        self.later: list = []

    def _add(self, entities, update_before_add=False) -> None:
        for entity in entities:
            assert entity.unique_id not in self.entities
            self.entities[entity.unique_id] = entity

    async def _forward(self, entry, platforms) -> None:
        for platform in platforms:
            module = importlib.import_module(f"custom_components.vzug.{platform}")
            await module.async_setup_entry(self.hass, entry, self._add)

    @property
    def shared(self) -> Shared:
        return self.entry.runtime_data

    def __enter__(self) -> Setup:
        self.hass.loop = asyncio.get_running_loop()
        self._patches = [
            patch.object(httpx.AsyncClient, "get", self.appliance.get),
            patch("custom_components.vzug.api.asyncio.sleep", new_callable=AsyncMock),
            patch("custom_components.vzug.shared.async_call_later", self._call_later),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc) -> None:
        for target in self.background:
            target.close()
        for p in self._patches:
            p.stop()

    def _call_later(self, hass, delay, action):
        self.later.append((delay, action))
        return MagicMock()

    async def run_later(self) -> None:
        """Let the time pass for what 'async_call_later' was asked to run."""
        while self.later:
            self.later.pop(0)[1](None)
            await self.run_background()

    async def setup(self) -> int:
        """Set the entry up, returning the number of requests it took."""
        assert await async_setup_entry(self.hass, self.entry)
        return len(self.appliance.requests)

    async def run_on_unload(self) -> None:
        """What HA does after 'async_unload_entry', or after a failed setup."""
        while self.on_unload:
            if job := self.on_unload.pop()():
                await job

    async def run_background(self) -> int:
        """Run what the setup left for later, returning the number of requests."""
        before = len(self.appliance.requests)
        while self.background:
            await self.background.pop(0)
        return len(self.appliance.requests) - before


DISHWASHER, WASHER, OVEN = (
    "adora_dish_v6000",
    "adora_wash_v6000",
    "combair_steamer_v6000_76c",
)


def _coordinators(setup: Setup) -> dict[str, list[str]]:
    """The entity ids by the coordinator they read, left out: those without one."""
    by_coordinator: dict[str, list[str]] = {}
    for uid, entity in setup.entities.items():
        if coordinator := getattr(entity, "coordinator", None):
            by_coordinator.setdefault(coordinator.name, []).append(uid)
    return by_coordinator


@pytest.mark.asyncio
@pytest.mark.parametrize("device", [DISHWASHER, WASHER, OVEN])
async def test_setup_only_waits_for_meta_and_state(device):
    with Setup(device) as setup:
        await setup.setup()

        assert setup.appliance.requests == [
            # meta
            "getMacAddress",
            "getDeviceStatus",
            "getModelDescription",
            "getFWVersion",
            "getDeviceInfo",
            # state, its 'getDeviceStatus' is the one meta fetched
            "getZHMode",
            "getLastPUSHNotifications",
            "getEcoInfo",
        ]
        assert set(_coordinators(setup)) == {"state"}
        # from the firmware 'aggregate_meta' fetched anyway
        assert setup.shared.device_info["sw_version"]


@pytest.mark.asyncio
@pytest.mark.parametrize("device", [DISHWASHER, WASHER, OVEN])
async def test_first_state_reuses_the_device_status_meta_fetched(device):
    passed = []
    aggregate_state = VZugApi.aggregate_state

    async def recording_state(self, **kwargs):
        passed.append(kwargs.get("device_status"))
        return await aggregate_state(self, **kwargs)

    with (
        Setup(device) as setup,
        patch.object(VZugApi, "aggregate_state", recording_state),
    ):
        await setup.setup()
        shared = setup.shared
        device_status, fetched_at = passed[0]
        state = shared.state_coord.data

        # ProgramEnd counts from the time the status was really fetched
        assert state.device_fetched_at == fetched_at
        assert state.device is device_status
        # it would only go stale in the meta, and the diagnostics would show it
        assert shared.meta.device_status is None

        # the same as asking for everything again
        fresh = await aggregate_state(shared.client, default_on_error=False)
        fresh.device_fetched_at = fetched_at
        assert state == fresh


@pytest.mark.asyncio
async def test_update_reuses_the_firmware_meta_fetched():
    with Setup(DISHWASHER) as setup:
        await setup.setup()
        await setup.run_background()

        # the AI module's in meta, the hh module's in the update
        assert setup.appliance.requests.count("getFWVersion") == 2
        assert setup.shared.update_coord.data.ai_fw_version == (
            setup.shared.meta.ai_fw_version
        )

        await setup.shared.update_coord.async_refresh()
        # from then on it is asked for again
        assert setup.appliance.requests.count("getFWVersion") == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("device", "model_id", "serial_number"),
    [
        (DISHWASHER, "AS6TDI", "46126 182263"),
        (WASHER, "AW6T", "33147 536171"),
        (OVEN, "CSB", "40695 200742"),
        # no 'getDeviceInfo', the AI module has no model id
        ("adora_slq", None, "33493 832315"),
        ("adora_tslq_wp", None, "61358 902645"),
    ],
)
async def test_device_info(device, model_id, serial_number):
    with Setup(device) as setup:
        await setup.setup()
        info = setup.shared.device_info

        assert info["manufacturer"] == "V-ZUG"
        assert info.get("model_id") == model_id
        assert info.get("serial_number") == serial_number


@pytest.mark.asyncio
async def test_device_info_leaves_out_an_empty_serial_number():
    with Setup(DISHWASHER) as setup:
        await setup.setup()
        setup.shared.meta.serial_number = ""
        await setup.shared._post_first_refresh()

        assert setup.shared.device_info.get("serial_number") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("device", "coordinators"),
    [
        (DISHWASHER, {"state", "update", "config", "programs"}),
        (WASHER, {"state", "update", "config", "programs"}),
        # its config tree is empty
        (OVEN, {"state", "update", "zones"}),
    ],
)
async def test_the_rest_arrives_after_the_setup(device, coordinators):
    with Setup(device) as setup:
        await setup.setup()
        await setup.run_background()

        assert set(_coordinators(setup)) == coordinators
        # nothing to ask again
        assert setup.later == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("device", "busy", "coordinator"),
    [
        (OVEN, "getProgram", "zones"),
        (DISHWASHER, "getProgram", "programs"),
        (DISHWASHER, "getAllProgramIds", "programs"),
    ],
)
async def test_failing_detection_is_retried(device, busy, coordinator):
    """A busy appliance must not keep its zones or programs away until a reload."""
    with Setup(device) as setup:
        setup.appliance.busy.add(busy)
        await setup.setup()
        await setup.run_background()

        assert coordinator not in _coordinators(setup)
        assert [delay for delay, _ in setup.later] == [DETECTION_RETRY_INTERVAL]

        # still busy: asked again, no more often
        setup.later.pop()[1](None)
        await setup.run_background()
        assert coordinator not in _coordinators(setup)
        assert len(setup.later) == 1

        setup.appliance.busy.clear()
        await setup.run_later()
        assert coordinator in _coordinators(setup)


@pytest.mark.asyncio
@pytest.mark.parametrize("device", ["adora_slq", "adora_tslq_wp"])
async def test_unsupported_detection_is_not_retried(device):
    """Adora SLQ / TSLQ don't know 'getProgram', asking again won't change that."""
    with Setup(device) as setup:
        await setup.setup()
        await setup.run_background()

        assert set(_coordinators(setup)) == {"state", "update", "config"}
        assert setup.appliance.requests.count("getProgram") == 1
        assert setup.later == []


@pytest.mark.asyncio
@pytest.mark.parametrize("device", [DISHWASHER, OVEN])
async def test_unchanged_answers_leave_the_entities_alone(device):
    """'always_update=False' relies on the data comparing by value."""
    with Setup(device) as setup:
        await setup.setup()
        await setup.run_background()
        shared = setup.shared

        coordinators = [
            coord
            for coord in (
                shared.state_coord,
                shared.update_coord,
                shared.config_coord,
                shared.zone_coord,
                shared.program_coord,
            )
            # the other one of zones and programs is never used
            if coord.data is not None
        ]
        assert len(coordinators) == 4
        for coord in coordinators:
            assert coord.config_entry is setup.entry
            calls = []
            unsub = coord.async_add_listener(lambda: calls.append(1))
            # a fresh object every time, but the same answers
            before = coord.data
            await coord.async_refresh()
            unsub()

            assert coord.last_update_success
            assert coord.data is not before
            assert calls == [], coord.name


@pytest.mark.asyncio
async def test_failing_background_refresh_is_retried():
    """A busy appliance must not fail the setup, nor keep its settings away."""
    with Setup(DISHWASHER) as setup:
        setup.appliance.busy.add("getCategories")
        await setup.setup()
        await setup.run_background()

        config = setup.shared.config_coord
        assert config.data is None
        assert "config" not in _coordinators(setup)
        # the listener waiting for its entities keeps the polling going, and it
        # doesn't wait the usual hour
        assert _seconds_to_next_refresh(config) <= UNREACHABLE_RETRY_AFTER + 1

        setup.appliance.busy.clear()
        await config._handle_refresh_interval()
        assert "config" in _coordinators(setup)
        # back to the usual hour on its own
        assert _seconds_to_next_refresh(config) > UNREACHABLE_RETRY_AFTER + 1


def _seconds_to_next_refresh(coord) -> float:
    assert coord._unsub_refresh is not None
    return coord._unsub_refresh.__self__.when() - coord.hass.loop.time()


@pytest.mark.asyncio
async def test_unload_closes_the_client():
    with Setup(DISHWASHER) as setup:
        await setup.setup()
        await setup.run_background()
        shared = setup.shared
        setup.hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

        assert await async_unload_entry(setup.hass, setup.entry)
        assert not shared.client._client.is_closed
        await setup.run_on_unload()

        assert shared.client._client.is_closed
        assert shared.state_coord._shutdown_requested


@pytest.mark.asyncio
async def test_failed_setup_closes_the_client():
    """HA retries it with a new client every time, for as long as it is off."""
    with Setup(DISHWASHER) as setup:
        setup.appliance.busy.add("getMacAddress")
        with pytest.raises(ConfigEntryNotReady):
            await setup.setup()
        shared = next(
            job.__self__ for job in setup.on_unload if isinstance(job.__self__, Shared)
        )

        await setup.run_on_unload()
        assert shared.client._client.is_closed
