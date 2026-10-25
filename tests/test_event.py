"""The program started / finished event, without a running Home Assistant."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.vzug.api import AggState
from custom_components.vzug.event import FINISHED, STARTED, ProgramEvent
from custom_components.vzug.shared import Shared

_COMPONENT = Path(__file__).parents[1] / "custom_components" / "vzug"


def _state(device) -> AggState:
    return AggState(
        zh_mode=2,
        device=device,
        device_fetched_at=datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
        notifications=[],
        eco_info={},
    )


def _event(inactive: str = "true", program: str = "") -> ProgramEvent:
    coord = MagicMock()
    coord.data = _state({"Inactive": inactive, "Program": program})
    coord.last_update_success = True
    shared = SimpleNamespace(
        state_coord=coord,
        unique_id_prefix="fc:1b:ff:06:6e:8e",
        device_info={"name": "Kitchen Dishwasher"},
    )
    entity = ProgramEvent(shared)
    entity.async_write_ha_state = MagicMock()
    return entity


def _poll(entity: ProgramEvent, device=None, *, success: bool = True) -> None:
    """One coordinator update, the way the entity's listener sees it."""
    if device is not None:
        entity.coordinator.data = _state(device)
    entity.coordinator.last_update_success = success
    entity._handle_coordinator_update()


def _fired(entity: ProgramEvent) -> tuple[str | None, dict]:
    attributes = entity.state_attributes
    return attributes["event_type"], {
        key: value for key, value in attributes.items() if key != "event_type"
    }


def test_unique_id():
    assert _event().unique_id == "fc:1b:ff:06:6e:8e-event-program"


def test_dishwasher_program_starts_and_finishes():
    """The AdoraDish on 2026-10-24: 'Automatic' from 12:35 to 15:03."""
    entity = _event()

    _poll(entity, {"Inactive": "false", "Program": "Automatic"})
    assert _fired(entity) == (STARTED, {"program": "Automatic"})
    started_at = entity.state

    # final drying and the door opener, still running
    _poll(entity, {"Inactive": "false", "Program": "Automatic"})
    assert entity.state == started_at

    _poll(entity, {"Inactive": "true", "Program": ""})
    assert _fired(entity) == (FINISHED, {"program": "Automatic"})


def test_finished_names_the_program_the_oven_ran():
    """When idle the Combair reports "Keine Betriebsart", not its last program."""
    entity = _event(program="Keine Betriebsart")

    _poll(entity, {"Inactive": "false", "Program": "PizzaPlus"})
    _poll(entity, {"Inactive": "true", "Program": "Keine Betriebsart"})

    assert _fired(entity) == (FINISHED, {"program": "PizzaPlus"})


def test_started_without_a_program_name():
    entity = _event()

    _poll(entity, {"Inactive": "false", "Program": ""})
    assert _fired(entity) == (STARTED, {"program": None})

    # the name came a poll later
    _poll(entity, {"Inactive": "false", "Program": "Eco"})
    _poll(entity, {"Inactive": "true", "Program": ""})
    assert _fired(entity) == (FINISHED, {"program": "Eco"})


@pytest.mark.parametrize("inactive", ["true", "false"])
def test_nothing_on_startup(inactive):
    entity = _event(inactive=inactive, program="Eco")

    _poll(entity)

    assert entity.state is None


def test_finish_of_a_program_running_at_startup():
    entity = _event(inactive="false", program="Eco")

    _poll(entity, {"Inactive": "true", "Program": ""})

    assert _fired(entity) == (FINISHED, {"program": "Eco"})


@pytest.mark.parametrize(
    ("before", "after"), [("true", "false"), ("false", "true"), ("false", "false")]
)
def test_nothing_when_the_appliance_comes_back(before, after):
    """Some appliances switch off completely after a program."""
    entity = _event(inactive=before)

    _poll(entity, success=False)
    _poll(entity, {"Inactive": after, "Program": ""})

    assert entity.state is None


def test_back_from_unavailable_it_fires_again():
    entity = _event()
    _poll(entity, success=False)
    _poll(entity, {"Inactive": "true", "Program": ""})

    _poll(entity, {"Inactive": "false", "Program": "Eco"})

    assert _fired(entity) == (STARTED, {"program": "Eco"})


def test_nothing_on_carried_over_data():
    """In standby the device status is mostly carried over from the last probe."""
    entity = _event()
    previous = entity.coordinator.data
    skipped = _state({})

    carried = Shared._carry_over(Shared.__new__(Shared), skipped, previous)
    _poll(entity, carried.device)

    assert entity.state is None


@pytest.mark.parametrize("device", [{}, {"Inactive": "maybe"}])
def test_unknown_activity_is_no_news(device):
    """A failed status request must not look like a start or a finish."""
    entity = _event(inactive="false")

    _poll(entity, device)
    assert entity.state is None

    _poll(entity, {"Inactive": "false", "Program": ""})
    assert entity.state is None


@pytest.mark.parametrize("lang", ["en", "de", "fr"])
def test_event_types_are_translated(lang):
    strings = json.loads((_COMPONENT / "translations" / f"{lang}.json").read_text())
    program = strings["entity"]["event"]["program"]

    assert program["name"]
    states = program["state_attributes"]["event_type"]["state"]
    assert set(states) == {STARTED, FINISHED}
    assert all(states.values())
