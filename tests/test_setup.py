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
from custom_components.vzug.const import CONF_BASE_URL
from custom_components.vzug.shared import UNREACHABLE_RETRY_INTERVAL, Shared

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
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc) -> None:
        for target in self.background:
            target.close()
        for p in self._patches:
            p.stop()

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
            # state
            "getZHMode",
            "getDeviceStatus",
            "getLastPUSHNotifications",
            "getEcoInfo",
            # which coordinator the zone or program entities listen to
            "getProgram",
        ]
        assert set(_coordinators(setup)) == {"state"}
        # from the firmware 'aggregate_meta' fetched anyway
        assert setup.shared.device_info["sw_version"]


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
            if coord is not None
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
        assert config.update_interval == UNREACHABLE_RETRY_INTERVAL
        assert config._unsub_refresh is not None

        setup.appliance.busy.clear()
        await config._handle_refresh_interval()
        assert "config" in _coordinators(setup)


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
