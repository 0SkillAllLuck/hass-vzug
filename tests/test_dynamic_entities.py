"""Entities for config-tree commands and eco categories that show up after setup."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.vzug import sensor, switch
from custom_components.vzug.api import AggCategory, AggState
from custom_components.vzug.const import DOMAIN
from custom_components.vzug.helpers import add_entities_as_they_appear

PREFIX = "fc:1b:ff:06:6e:8e"


class FakeCoordinator:
    def __init__(self, data):
        self.data = data
        self.last_update_success = True
        self.listeners = []

    def async_add_listener(self, update_callback, context=None):
        self.listeners.append(update_callback)
        return lambda: self.listeners.remove(update_callback)

    def update(self, data):
        self.data = data
        for listener in list(self.listeners):
            listener()


def _config(**commands):
    """{category: [command, ...]} -> the config tree"""
    return {
        key: AggCategory(
            key=key, description=key, commands={c["command"]: c for c in cmds}
        )
        for key, cmds in commands.items()
    }


def _state(eco_info):
    return AggState(
        zh_mode=-1,
        device={},
        device_fetched_at=datetime.now(UTC),
        notifications=[],
        eco_info=eco_info,
    )


def _setup(config, eco_info=None):
    shared = SimpleNamespace(
        config_coord=FakeCoordinator(config),
        state_coord=FakeCoordinator(_state(eco_info or {})),
        zone_coord=None,
        unique_id_prefix=PREFIX,
        device_info={},
    )
    hass = SimpleNamespace(data={DOMAIN: {"entry": shared}})
    entry = MagicMock(entry_id="entry")
    added = []

    def add(entities, update_before_add=False):
        added.append([e.unique_id for e in entities])

    return shared, hass, entry, add, added


_SOUND = {"command": "sound", "type": "boolean", "value": "true"}
_LIGHT = {"command": "light", "type": "boolean", "value": "false"}
_LOCKED = {"command": "locked", "type": "boolean", "alterable": False}
_HARDNESS = {"command": "hardness", "type": "status", "value": "7"}


@pytest.mark.asyncio
async def test_settings_that_appear_later_get_entities():
    shared, hass, entry, add, added = _setup(_config(settings=[_SOUND]))
    await switch.async_setup_entry(hass, entry, add)
    assert added == [[], [f"{PREFIX}-userconfig-settings-sound"]]

    added.clear()
    shared.config_coord.update(_config(settings=[_SOUND, _LIGHT, _LOCKED]))
    # 'locked' isn't alterable, so it stays without a switch as before
    assert added == [[f"{PREFIX}-userconfig-settings-light"]]


@pytest.mark.asyncio
async def test_busy_appliance_at_startup():
    # a busy appliance answers getCategories with an empty list
    shared, hass, entry, add, added = _setup({})
    await switch.async_setup_entry(hass, entry, add)
    assert added == [[]]

    shared.config_coord.update(_config(settings=[_SOUND]))
    shared.config_coord.update({})
    shared.config_coord.update(_config(settings=[_SOUND]))
    assert added == [[], [f"{PREFIX}-userconfig-settings-sound"]]
    entry.async_on_unload.assert_called_once()


@pytest.mark.asyncio
async def test_eco_sensors_that_appear_later():
    # all-zero eco info at startup is an empty EcoInfo
    config = _config(settings=[_SOUND, _HARDNESS])
    shared, hass, entry, add, added = _setup(config)
    await sensor.async_setup_entry(hass, entry, add)
    assert added[1:] == [[f"{PREFIX}-userconfig-settings-hardness"]]

    added.clear()
    shared.state_coord.update(_state({"water": {"total": 80085.5}}))
    shared.state_coord.update(_state({}))
    shared.state_coord.update(_state({"water": {"total": 80085.5}}))
    assert added == [
        [
            f"{PREFIX}-sensor-water_total",
            f"{PREFIX}-sensor-water_program",
            f"{PREFIX}-sensor-water_average",
        ]
    ]

    added.clear()
    shared.state_coord.update(
        _state({"water": {"total": 80085.5}, "energy": {"total": 615.7}})
    )
    assert added == [
        [
            f"{PREFIX}-sensor-energy_total",
            f"{PREFIX}-sensor-energy_program",
            f"{PREFIX}-sensor-energy_average",
        ]
    ]


def test_skipped_candidates_are_asked_again():
    coordinator = FakeCoordinator({"a": False})
    entry = MagicMock()
    added = []
    add_entities_as_they_appear(
        entry,
        coordinator,
        added.append,
        lambda data: data.items(),
        lambda key, ready: SimpleNamespace(key=key) if ready else None,
    )
    assert added == []

    coordinator.update({"a": True})
    coordinator.update({"a": True})
    assert [[e.key for e in batch] for batch in added] == [["a"]]


def test_no_data_yet():
    coordinator = FakeCoordinator(None)
    added = []
    add_entities_as_they_appear(
        MagicMock(),
        coordinator,
        added.append,
        lambda data: data.items(),
        lambda key, value: SimpleNamespace(key=key),
    )
    assert added == []

    coordinator.update({"a": 1})
    assert [[e.key for e in batch] for batch in added] == [["a"]]


def test_listener_removed_on_unload():
    coordinator = FakeCoordinator({})
    entry = MagicMock()
    add_entities_as_they_appear(
        entry, coordinator, MagicMock(), lambda data: (), MagicMock()
    )
    (remove,) = entry.async_on_unload.call_args.args
    remove()
    assert coordinator.listeners == []
