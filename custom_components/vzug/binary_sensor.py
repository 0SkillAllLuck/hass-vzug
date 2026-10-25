from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import zones
from .helpers import ZoneEntity, add_entities_when_ready, zone_translation
from .shared import Shared, VZugConfigEntry

# read-only, the coordinators do all the talking to the appliance
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: VZugConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    shared = config_entry.runtime_data
    zone_coord = shared.zone_coord
    if zone_coord is None:
        return

    add_entities_when_ready(
        config_entry,
        zone_coord,
        async_add_entities,
        lambda: [
            Door(shared, zone=name)
            for name, zone in zone_coord.data.zones.items()
            if isinstance(zone.get(zones.DOOR_KEY), bool)
        ],
    )


class Door(ZoneEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.DOOR

    def __init__(self, shared: Shared, *, zone: str) -> None:
        super().__init__(shared, zone=zone, key=zones.DOOR_KEY)
        self._attr_translation_key, name = zone_translation("door", zone)
        if name:
            self._attr_name = name

    @property
    def is_on(self) -> bool | None:
        closed = (self.zone_data or {}).get(zones.DOOR_KEY)
        return None if closed is None else not closed
