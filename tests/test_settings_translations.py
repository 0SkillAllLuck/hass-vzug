"""Names of the config-tree settings follow Home Assistant's language."""

import json
from pathlib import Path

import pytest
from homeassistant.const import UnitOfTime

from custom_components.vzug import button, number, select, sensor, switch
from custom_components.vzug.helpers import (
    _SETTING_TRANSLATION_KEYS,
    setting_translation_key,
)

from .test_action_errors import _config_shared

COMPONENT = Path(__file__).parent.parent / "custom_components" / "vzug"
FIXTURES = Path(__file__).parent / "fixtures"

PLATFORMS = {
    "boolean": ("switch", switch.UserConfig),
    "range": ("number", number.UserConfig),
    "selection": ("select", select.UserConfig),
    "status": ("sensor", sensor.UserConfigSensor),
    "action": ("button", button.UserConfig),
}

# what the user's appliances (dishwasher, dryer, washer) sent on 2026-10-25
LIVE = [
    *(
        ("boolean", key)
        for key in (
            "PIN_DRY_PLUS",
            "PIN_ECO",
            "PIN_PARTIALLOAD",
            "PIN_RINSE_PLUS",
            "ALL_IN_ONE",
            "CHILD_PROOF_LOCK",
            "EcoManagement",
            "autoXdoorXopener",
            "backgroundXimage",
            "buttonXtone",
            "childproofXlock",
            "hygieneXinformation",
            "trommellicht",
            "BackgroundXimage",
            "ButtonXtone",
            "ChildproofXlock",
            "DrumXillumination",
            "ReversePlus",
        )
    ),
    *(
        ("range", key)
        for key in ("PIN_STARTTIME", "DSP_ILLUMINATION", "brightness", "Brightness")
    ),
    ("range", "LevelXofXdryness"),
    *(
        ("selection", key)
        for key in (
            "BRIGHTENER",
            "DSP_BUTTON_SOUND",
            "DSP_SIGNAL_SOUND",
            "TEMPERATURE_UNIT",
            "WATER_HARDNESS_UNIT",
            "WATER_HARDNESS",
            "aquaplus",
            "soiling",
            "spinningXat",
            "startXdelay",
            "ViewingXangle",
        )
    ),
    ("status", "AverageXperXcycleXdrumDry"),
    ("status", "TotalXconsumptionXdrumDry"),
    ("action", "AverageXperXcycleDEL"),
    ("action", "TotalXconsumptionDEL"),
]


def _entity(type_, key, **command):
    shared = _config_shared(type=type_, **command)
    commands = shared.config_coord.data["cat"].commands
    commands[key] = {**commands.pop("cmd"), "command": key}
    return PLATFORMS[type_][1](shared, category_key="cat", command_key=key)


def _fixture_commands():
    for path in sorted(FIXTURES.glob("*/responses/hh_get_command_details.json")):
        for entry in json.loads(path.read_text()):
            yield entry["details"]["type"], entry["details"]["command"]


@pytest.mark.parametrize(("type_", "key"), [*LIVE, *_fixture_commands()])
def test_known_settings_have_a_translation_key(type_, key):
    assert setting_translation_key(type_, key) is not None


@pytest.mark.parametrize(
    ("type_", "keys"),
    [
        ("boolean", ["CHILD_PROOF_LOCK", "ChildproofXlock", "childproofXlock"]),
        ("range", ["DSP_ILLUMINATION", "Brightness", "brightness"]),
        ("boolean", ["ButtonXtone", "buttonXtone"]),
        ("boolean", ["BackgroundXimage", "backgroundXimage", "Backgrounimage"]),
        ("boolean", ["DrumXillumination", "trommellicht"]),
        (
            "status",
            ["TotalXconsumption", "TotalXconsumptionXdrumDry", "ecomXstatXtotal"],
        ),
        ("action", ["ECO_MGMT_RESET_AVG", "AverageXperXcycleDEL"]),
    ],
)
def test_families_share_the_translation_key(type_, keys):
    assert len({setting_translation_key(type_, key) for key in keys}) == 1


def test_translation_key_depends_on_the_type():
    # its platform has no translation for it, the appliance's text is better than none
    assert setting_translation_key("selection", "childproofXlock") is None


@pytest.mark.parametrize("lang", ["en", "de", "fr"])
def test_translation_keys_exist(lang):
    strings = json.loads((COMPONENT / "translations" / f"{lang}.json").read_text())
    for type_, keys in _SETTING_TRANSLATION_KEYS.items():
        platform = PLATFORMS[type_][0]
        for key in keys.values():
            assert strings["entity"][platform][key]["name"], (lang, platform, key)


@pytest.mark.parametrize("type_", list(PLATFORMS))
def test_known_setting_is_named_by_its_translation(type_):
    key = next(iter(_SETTING_TRANSLATION_KEYS[type_]))
    entity = _entity(type_, key, description="Tastenton")

    assert entity.translation_key == _SETTING_TRANSLATION_KEYS[type_][key]
    # no '_attr_name', so Home Assistant takes the name from the translations
    assert not hasattr(entity, "_attr_name")
    assert entity.unique_id == f"fc:1b:ff:00:00:00-userconfig-cat-{key}"


@pytest.mark.parametrize("type_", list(PLATFORMS))
def test_unknown_setting_keeps_the_appliance_name(type_):
    entity = _entity(type_, "cmd", description="Neue Funktion")
    assert entity.translation_key is None
    assert entity.name == "Neue Funktion"
    assert entity.unique_id == "fc:1b:ff:00:00:00-userconfig-cat-cmd"

    for description in ({}, {"description": ""}):
        assert _entity(type_, "cmd", **description).name == "cmd"


def test_delayed_start_has_its_unit():
    entity = _entity("range", "PIN_STARTTIME", value="3", minMax=["0", "24"])
    assert entity.native_unit_of_measurement == UnitOfTime.HOURS
    assert _entity("range", "cmd").native_unit_of_measurement is None
