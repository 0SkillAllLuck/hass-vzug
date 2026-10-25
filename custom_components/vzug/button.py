from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import api, zones
from .const import DOMAIN
from .helpers import (
    UserConfigEntity,
    action_errors,
    add_entities_as_they_appear,
    add_entities_when_ready,
    user_config_commands,
)
from .shared import ProgramCoordinator, Shared, StateCoordinator, VZugConfigEntry

# the appliance handles one request at a time, see 'api.VZugApi.__init__'
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: VZugConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    shared = config_entry.runtime_data

    entities: list[ButtonEntity] = [RefreshNow(shared)]
    if shared.meta.supports_update_status():
        entities.append(CheckUpdate(shared))

    async_add_entities(entities)

    if shared.program_coord is not None:
        add_entities_when_ready(
            config_entry,
            shared.program_coord,
            async_add_entities,
            lambda: [SendProgram(shared)],
        )

    def user_config(key: tuple[str, str], command: api.Command) -> UserConfig | None:
        if command.get("type") == "action":
            return UserConfig(shared, category_key=key[0], command_key=key[1])
        return None

    add_entities_as_they_appear(
        config_entry,
        shared.config_coord,
        async_add_entities,
        user_config_commands,
        user_config,
    )


class SendProgram(CoordinatorEntity[ProgramCoordinator], ButtonEntity):
    """'Send to appliance' in the V-ZUG app: transfer the selected program.

    The appliance starts it once its door is closed. There is no way to start a
    program remotely without that, the appliance insists on it.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "send_program"

    def __init__(self, shared: Shared) -> None:
        assert shared.program_coord is not None
        super().__init__(shared.program_coord)
        self.shared = shared
        self._attr_unique_id = f"{shared.unique_id_prefix}-send-program"
        self._attr_device_info = shared.device_info

    async def async_press(self) -> None:
        # the selection might have been changed on the panel since the last poll
        await self.coordinator.async_refresh()
        current = self.coordinator.data.current
        if current is None or not isinstance(current.get("id"), int):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_program_selected"
            )
        async with action_errors(self.shared):
            await self.shared.client.set_program(
                current["id"], zones.program_options(current)
            )
        await self.coordinator.async_request_refresh()


class RefreshNow(ButtonEntity):
    """Bring every value up to date right now, waking the appliance if necessary.

    While the appliance is in standby the state updates only talk to the AI module
    so it can stay asleep, and the config tree is only read hourly. Both are a
    deliberate trade-off, and this is the way out of it: for someone who just
    changed a setting on the appliance's panel, or for an automation that knows
    better than the integration does - a smart plug reporting power draw, a door
    contact, a motion sensor in the laundry room.

    It stays available while the appliance doesn't answer: right after switching
    it back on, this is the quickest way to bring its entities back.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_translation_key = "refresh_now"

    def __init__(self, shared: Shared) -> None:
        self.shared = shared

        self._attr_unique_id = f"{shared.unique_id_prefix}-refresh-now"
        self._attr_device_info = shared.device_info

    async def async_press(self) -> None:
        await self.shared.async_probe_device()


class CheckUpdate(CoordinatorEntity[StateCoordinator], ButtonEntity):
    """Ask the appliance to look for new firmware.

    It is only available while the appliance answers, which the state coordinator
    knows best: it is polled every minute, the update coordinator only every few
    hours.
    """

    _attr_device_class = ButtonDeviceClass.UPDATE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_translation_key = "check_update"

    def __init__(self, shared: Shared) -> None:
        super().__init__(shared.state_coord)
        self.shared = shared

        self._attr_unique_id = f"{shared.unique_id_prefix}-check-update"
        self._attr_device_info = shared.device_info

    async def async_press(self) -> None:
        async with action_errors(self.shared):
            await self.shared.client.check_for_updates()
        await self.shared.update_coord.async_request_refresh()


class UserConfig(ButtonEntity, UserConfigEntity):
    async def async_press(self) -> None:
        async with action_errors(self.shared):
            await self.shared.client.do_command_action(self.vzug_command_key)
