from custom_components.vzug.sensor import ProgramEnd, ProgramEndRaw


def test_only_the_raw_program_end_is_disabled_by_default():
    assert ProgramEndRaw.__new__(ProgramEndRaw).entity_registry_enabled_default is False
    assert ProgramEnd.__new__(ProgramEnd).entity_registry_enabled_default is True
