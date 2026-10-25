from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .shared import Shared, StateCoordinator, VZugConfigEntry

# read-only, the coordinators do all the talking to the appliance
PARALLEL_UPDATES = 0

STARTED = "program_started"
FINISHED = "program_finished"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: VZugConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    # refrigerators get one too: whether the appliance has zones is only known
    # later, if ever, and ovens have zones as well as programs
    async_add_entities([ProgramEvent(config_entry.runtime_data)])


class ProgramEvent(CoordinatorEntity[StateCoordinator], EventEntity):
    """Fires when the appliance starts or finishes a program.

    Follows 'Inactive' rather than 'Program': ovens report a text like "Keine
    Betriebsart" as their program when idle. A dishwasher only goes inactive once
    the final drying and the automatic door opening are done, when it also
    reports the program as finished.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "program"
    _attr_event_types = [STARTED, FINISHED]

    def __init__(self, shared: Shared) -> None:
        super().__init__(shared.state_coord)
        self._attr_unique_id = f"{shared.unique_id_prefix}-event-program"
        self._attr_device_info = shared.device_info
        self._active: bool | None = None
        self._program: str | None = None
        # what the appliance is doing right now is no news
        self._observe()

    @callback
    def _handle_coordinator_update(self) -> None:
        if not self.coordinator.last_update_success:
            # some appliances switch off completely after a program, coming back
            # isn't a start. Whatever it does then is taken as it is.
            self._active = None
        elif event := self._observe():
            self._trigger_event(event, {"program": self._program})
        self.async_write_ha_state()

    def _observe(self) -> str | None:
        """Take in the latest device status, the event type if it is one."""
        device = self.coordinator.data.device
        inactive = device.get("Inactive")
        if inactive not in ("true", "false"):
            # the status request failed, or the appliance doesn't tell
            return None

        active = inactive == "false"
        previous, self._active = self._active, active
        program = device.get("Program") or None
        if active and (program or not previous):
            # kept for 'finished': by then the appliance reports no program, or
            # the oven its idle text
            self._program = program
        if previous is None or previous == active:
            return None
        return STARTED if active else FINISHED
