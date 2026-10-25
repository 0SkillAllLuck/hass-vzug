import contextlib
from collections.abc import AsyncIterator, Callable, Hashable, Iterable, Iterator

import httpx
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import ChildDeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import UndefinedType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from . import api, zones
from .const import DOMAIN
from .shared import ConfigCoordinator, Shared, ZoneCoordinator


@contextlib.asynccontextmanager
async def action_errors(shared: Shared) -> AsyncIterator[None]:
    """Turn a failed request of an entity action into an error the user can read.

    Without this the UI only shows "Unknown error".
    """
    try:
        yield
    except api.AuthenticationFailed as err:
        # the coordinators would only notice on their next poll, and the user
        # wants to know how to fix this now. HA ignores a second reauth request.
        shared.entry.async_start_reauth(shared.hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="auth_failed"
        ) from err
    except api.NO_ANSWER as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="no_answer"
        ) from err
    except (httpx.HTTPError, AssertionError, ValueError) as err:
        # it answered, but refused the request or sent something unusable
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="request_failed"
        ) from err


def add_entities_as_they_appear[T, K: Hashable, V](
    config_entry: ConfigEntry,
    coordinator: DataUpdateCoordinator[T],
    async_add_entities: AddConfigEntryEntitiesCallback,
    candidates: Callable[[T], Iterable[tuple[K, V]]],
    factory: Callable[[K, V], Entity | None],
) -> None:
    """Add an entity for every candidate, now and whenever an update brings new ones.

    A busy appliance answers with an empty config tree or all-zero eco info, so what
    exists at setup isn't everything there is. 'factory' returns None to skip a
    candidate, it is asked again on the next update.

    Entities whose candidate disappears are kept: a busy appliance's answer can't be
    told apart from a real removal.
    """
    added: set[K] = set()

    @callback
    def _add_new() -> None:
        if coordinator.data is None:
            return
        entities: list[Entity] = []
        for key, value in candidates(coordinator.data):
            if key in added or (entity := factory(key, value)) is None:
                continue
            added.add(key)
            entities.append(entity)
        if entities:
            async_add_entities(entities)

    _add_new()
    config_entry.async_on_unload(coordinator.async_add_listener(_add_new))


def add_entities_when_ready(
    config_entry: ConfigEntry,
    coordinator: DataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[], Iterable[Entity]],
) -> None:
    """Add the entities 'build' returns once the coordinator has data.

    For the coordinators whose first refresh 'Shared.async_refresh_the_rest' does,
    after the setup: their entities read the data as soon as they exist. 'build' is
    asked again on every update, new entities in its answer are added too.
    """
    add_entities_as_they_appear(
        config_entry,
        coordinator,
        async_add_entities,
        lambda _: ((entity.unique_id, entity) for entity in build()),
        lambda _, entity: entity,
    )


def user_config_commands(
    config: api.AggConfig,
) -> Iterator[tuple[tuple[str, str], api.Command]]:
    """((category key, command key), command) for every command in the config tree."""
    for category in config.values():
        for command in category.commands.values():
            yield (category.key, command.get("command", "")), command


class UserConfigEntity(CoordinatorEntity[ConfigCoordinator]):
    _attr_has_entity_name = True
    # fixed, not taken from 'alterable': HA only reads the category when it adds
    # the entity, so a setting added mid-program would register as diagnostic
    _attr_entity_category = EntityCategory.CONFIG
    # fixed per entity, no point storing them with every state change
    _unrecorded_attributes = frozenset({"category_key", "command_key"})

    def __init__(self, shared: Shared, *, category_key: str, command_key: str) -> None:
        super().__init__(shared.config_coord)
        self.shared = shared
        self.vzug_category_key = category_key
        self.vzug_command_key = command_key

        self._attr_unique_id = (
            f"{shared.unique_id_prefix}-userconfig-{category_key}-{command_key}"
        )
        self._attr_device_info = shared.device_info
        self._attr_extra_state_attributes = {
            "category_key": category_key,
            "command_key": command_key,
        }

    @property
    def vzug_command(self) -> api.Command:
        try:
            return self.coordinator.data[self.vzug_category_key].commands[
                self.vzug_command_key
            ]
        except LookupError:
            return api.Command()

    @property
    def name(self) -> str | UndefinedType | None:
        name = self.vzug_command.get("description")
        if not name:
            name = self.vzug_command_key
        return name

    def ensure_alterable(self) -> None:
        """Refuse a change the appliance doesn't allow right now.

        It locks its settings while a program runs. Older firmware doesn't send
        'alterable' at all, those settings can always be changed.
        """
        if not self.vzug_command.get("alterable", True):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="setting_locked"
            )


class ZoneEntity(CoordinatorEntity[ZoneCoordinator]):
    """An entity reading one key of one zone of a refrigerator.

    'zone' is the appliance's zone name ('fridge1', 'freezer1', 'countdown1'), or ""
    for entities about the whole appliance (PartyCooling, the 'cmd' modes).
    """

    _attr_has_entity_name = True

    def __init__(self, shared: Shared, *, zone: str, key: str) -> None:
        super().__init__(shared.zone_coord)
        self.shared = shared
        self.vzug_zone = zone
        self.vzug_key = key

        self._attr_unique_id = f"{shared.unique_id_prefix}-zone-{zone or 'all'}-{key}"
        label = zone_label(zone)
        if label in KNOWN_ZONE_LABELS and key != zones.PARTY_KEY:
            # the fridge and the freezer are parts of the appliance with entities of
            # their own. PartyCooling covers the whole appliance, it stays with it.
            self._attr_device_info = ChildDeviceInfo(
                identifiers={(DOMAIN, f"{shared.meta.serial_number}-{zone}")},
                parent_device_id=shared.device_id,
                translation_key=label,
            )
        else:
            self._attr_device_info = shared.device_info

    @property
    def zone_data(self) -> zones.Zone | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.zones.get(self.vzug_zone)

    @property
    def available(self) -> bool:
        return super().available and (not self.vzug_zone or self.zone_data is not None)

    async def _write_zone(self, value: str) -> None:
        async with action_errors(self.shared):
            await self.shared.client.set_zone_value(
                self.vzug_zone, self.vzug_key, value
            )
        await self.coordinator.async_request_refresh()


def zone_label(zone: str) -> str:
    """'fridge1' -> 'fridge', used as translation placeholder"""
    return zone.rstrip("0123456789")


# translation keys must be snake_case
_SNAKE = {
    "superCool": "super_cool",
    "superFreeze": "super_freeze",
    "partyCooling": "party_cooling",
    "HolidayMode": "holiday_mode",
    "SilentPlusMode": "silent_plus_mode",
    "SabbatMode": "sabbath_mode",
}
# zones we have translations for, anything else gets a generated name
KNOWN_ZONE_LABELS = ("fridge", "freezer")


def zone_translation(prefix: str, zone: str) -> tuple[str | None, str | None]:
    """(translation_key, fallback name) for an entity about one zone."""
    if zone_label(zone) in KNOWN_ZONE_LABELS:
        # on the zone's own device, whose name already says which zone it is
        return prefix, None
    return None, f"{prefix.replace('_', ' ').capitalize()} {zone}"


def snake(key: str) -> str:
    return _SNAKE.get(key, key.lower())
