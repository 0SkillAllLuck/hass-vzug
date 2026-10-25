import ast
import json
from pathlib import Path

import pytest

_INTEGRATION = Path(__file__).parents[1] / "custom_components" / "vzug"


@pytest.mark.parametrize("path", sorted(_INTEGRATION.glob("*.py")), ids=str)
def test_no_icons_in_python(path):
    """Icons belong in 'icons.json', keyed by the entity's translation key."""
    for node in ast.walk(ast.parse(path.read_text())):
        assert not (isinstance(node, ast.Name) and node.id == "_attr_icon"), path
        assert not (isinstance(node, ast.Attribute) and node.attr == "_attr_icon"), path
        assert not (isinstance(node, ast.keyword) and node.arg == "icon"), path


def test_icons_match_translation_keys():
    icons = json.loads((_INTEGRATION / "icons.json").read_text())["entity"]
    strings = json.loads((_INTEGRATION / "translations" / "en.json").read_text())

    for platform, keys in icons.items():
        assert set(keys) <= set(strings["entity"][platform]), platform
