from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.discovery_flow import DiscoveryKey
from homeassistant.helpers.json import json_bytes

from custom_components.vzug.api import AggCategory, AggMeta, AggState, AggUpdateStatus
from custom_components.vzug.const import DOMAIN
from custom_components.vzug.diagnostics import async_get_config_entry_diagnostics
from custom_components.vzug.shared import Shared

_MAC = "02:43:a0:d1:80:f9"
_SERIAL = "46126 182263"
_UUID = "7738150909"
_HOST = "10.0.3.32"
_SECRETS = (_MAC, _SERIAL, _UUID, _HOST, "hunter2", "the-user", "Kitchen")


def _coord(data, interval=timedelta(seconds=30)):
    return SimpleNamespace(
        data=data, last_update_success=True, update_interval=interval
    )


@pytest.fixture
def setup():
    entry = ConfigEntry(
        data={
            "base_url": f"http://{_HOST}",
            "username": "the-user",
            "password": "hunter2",
        },
        discovery_keys={"dhcp": (DiscoveryKey(domain="dhcp", key=_MAC, version=1),)},
        domain=DOMAIN,
        minor_version=1,
        options={},
        source="dhcp",
        subentries_data=None,
        title=f"Kitchen Dishwasher ({_SERIAL})",
        unique_id=_MAC,
        version=2,
    )
    shared = Shared.__new__(Shared)
    shared.client = MagicMock()
    shared.meta = AggMeta(
        mac_address=_MAC,
        model_id="AS6TDI",
        model_name="AdoraDish V6000",
        device_name="Kitchen Dishwasher",
        serial_number=_SERIAL,
        api_version=(1, 8, 0),
        ai_fw_version={"fn": _SERIAL, "SW": "1052633-R20", "deviceUuid": _UUID},
    )
    shared.state_coord = _coord(
        AggState(
            zh_mode=2,
            device={
                "DeviceName": "Kitchen Dishwasher",
                "Serial": _SERIAL,
                "Inactive": "true",
                "deviceUuid": _UUID,
            },
            device_fetched_at=datetime(2026, 8, 11, 12, 0, tzinfo=UTC),
            notifications=[],
            eco_info={"water": {"total": 10783}},
        )
    )
    shared.update_coord = _coord(
        AggUpdateStatus(
            update={"status": "idle"},
            ai_fw_version={"fn": _SERIAL, "deviceUuid": _UUID},
            hh_fw_version={"fn": _SERIAL, "an": _UUID, "v": "1056701-R28"},
        ),
        timedelta(hours=6),
    )
    shared.config_coord = _coord(
        {"CAT": AggCategory(key="CAT", description="Settings", commands={})}
    )
    # zones still waiting for their first refresh, no program coordinator at all
    shared.zone_coord = _coord(None, None)
    shared.zone_coord.last_update_success = False
    shared.program_coord = None

    entry.runtime_data = shared
    return MagicMock(), entry, shared


@pytest.mark.asyncio
async def test_diagnostics_come_from_memory(setup):
    hass, entry, shared = setup
    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert shared.client.mock_calls == []
    assert diag["state"]["data"]["eco_info"] == {"water": {"total": 10783}}
    assert diag["update"]["update_interval"] == 6 * 3600
    assert diag["update"]["data"]["hh_fw_version"]["v"] == "1056701-R28"
    assert diag["config"]["data"]["CAT"]["description"] == "Settings"
    assert diag["meta"]["model_id"] == "AS6TDI"
    assert diag["zones"] == {
        "last_update_success": False,
        "update_interval": None,
        "data": None,
    }
    assert diag["programs"] is None


@pytest.mark.asyncio
async def test_identifying_values_are_redacted(setup):
    hass, entry, _ = setup
    # serialized the way the download is, so nothing hides in a nested object
    dumped = json_bytes(await async_get_config_entry_diagnostics(hass, entry)).decode()

    for secret in _SECRETS:
        assert secret not in dumped
