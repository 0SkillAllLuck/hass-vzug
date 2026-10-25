import dataclasses
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONF_BASE_URL, DOMAIN
from .shared import Shared

# the same values 'tests/fixtures/collect_responses.py' de-identifies, plus the
# ones that carry them along: the unique id and the DHCP discovery key are the MAC,
# the title and the device name usually contain the serial number
TO_REDACT: set[str] = {
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_BASE_URL,
    "unique_id",
    "discovery_keys",
    "title",
    "mac_address",
    "serial_number",
    "device_name",
    "DeviceName",
    "Serial",
    "serialNumber",
    "deviceUuid",
    "fn",
    "an",
}


def _coordinator(coord: DataUpdateCoordinator[Any] | None) -> dict[str, Any] | None:
    if coord is None:
        return None
    data = coord.data
    if isinstance(data, dict):
        # the config tree
        data = {key: dataclasses.asdict(category) for key, category in data.items()}
    elif data is not None:
        data = dataclasses.asdict(data)
    interval = coord.update_interval
    return {
        "last_update_success": coord.last_update_success,
        "update_interval": interval.total_seconds() if interval else None,
        "data": data,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    shared: Shared = hass.data[DOMAIN][entry.entry_id]

    # only what is already in memory: asking the appliance would compete with the
    # coordinators for its single connection
    return async_redact_data(
        {
            "entry": entry.as_dict(),
            "meta": dataclasses.asdict(shared.meta),
            "state": _coordinator(shared.state_coord),
            "update": _coordinator(shared.update_coord),
            "config": _coordinator(shared.config_coord),
            "zones": _coordinator(shared.zone_coord),
            "programs": _coordinator(shared.program_coord),
        },
        TO_REDACT,
    )
