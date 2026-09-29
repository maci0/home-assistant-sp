"""The shipped catalog names every sensor and every state the mapper emits."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from custom_components.sp_group import const

PACKAGE = Path(__file__).resolve().parent.parent / "custom_components" / "sp_group"
STRINGS = json.loads((PACKAGE / "strings.json").read_text(encoding="utf-8"))
SENSORS = STRINGS["entity"]["sensor"]
# hassfest rejects anything else as a translation key.
TRANSLATION_KEY = re.compile(r"^[a-z0-9_]+$")


def _sensor_keys() -> set[str]:
    return {
        value for name, value in vars(const).items() if name.startswith("SENSOR_KEY_")
    }


def _sensor_states() -> set[str]:
    return {
        value for name, value in vars(const).items() if name.startswith("SENSOR_STATE_")
    }


def _sensor_units() -> set[str]:
    return {value for name, value in vars(const).items() if name.startswith("UNIT_")}


# The mapper builds a spec either directly or through its _spec helper, which
# takes the value positionally and names the unit argument ``unit``.
_SPEC_FACTORIES = {"SensorSpec": "native_value", "_spec": "unit_of_measurement"}


def _spec_calls(tree: ast.AST) -> list[ast.Call]:
    """Every spec construction in the mapper, read from the AST.

    Walking the AST keeps the check off comments and string content, and
    covers every call site, including ones a grep for a single literal misses.
    """
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _SPEC_FACTORIES
    ]


def _field_literals(call: ast.Call, field: str) -> list[ast.expr]:
    """The arguments a spec call passes for one ``SensorSpec`` field."""
    assert isinstance(call.func, ast.Name)
    if call.func.id == "_spec":
        if field == "native_value" and len(call.args) > 1:
            return [call.args[1]]
        if field == "unit_of_measurement":
            return [kw.value for kw in call.keywords if kw.arg == "unit"]
        return []
    return [kw.value for kw in call.keywords if kw.arg == field]


def _spec_string_literals(calls: list[ast.Call], field: str) -> list[str]:
    values: list[str] = []
    for call in calls:
        values.extend(
            value.value
            for value in _field_literals(call, field)
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        )
    return values


MAPPER = ast.parse((PACKAGE / "mapper.py").read_text(encoding="utf-8"))
SPEC_CALLS = _spec_calls(MAPPER)


def test_the_mapper_walk_covers_every_spec_construction() -> None:
    """Guard the guard: an empty walk would pass the literal checks vacuously."""
    assert len(SPEC_CALLS) >= 10


def test_every_spec_state_literal_is_a_declared_constant() -> None:
    """A state written as a literal ships untranslated, so it needs a constant."""
    states = _spec_string_literals(SPEC_CALLS, "native_value")
    assert set(states) <= _sensor_states()


def test_every_spec_unit_literal_is_a_declared_constant() -> None:
    units = _spec_string_literals(SPEC_CALLS, "unit_of_measurement")
    assert set(units) <= _sensor_units()


def test_every_sensor_key_has_a_translated_name() -> None:
    assert _sensor_keys() <= SENSORS.keys()
    assert all(entry["name"] for entry in SENSORS.values())


def test_every_generated_state_has_a_translation() -> None:
    translated = {
        state for entry in SENSORS.values() for state in entry.get("state", {})
    }
    assert _sensor_states() <= translated


@pytest.mark.parametrize("key", sorted(SENSORS))
def test_keys_are_valid_translation_keys(key: str) -> None:
    assert TRANSLATION_KEY.match(key)
    assert all(TRANSLATION_KEY.match(state) for state in SENSORS[key].get("state", {}))


def test_shipped_english_catalog_matches_source_strings() -> None:
    shipped = (PACKAGE / "translations" / "en.json").read_text(encoding="utf-8")
    assert json.loads(shipped) == STRINGS


def _raised_exception_keys() -> set[str]:
    """Keys the modules hand to translated_error, read without importing them."""
    keys: set[str] = set()
    for name in ("coordinator.py", "__init__.py"):
        tree = ast.parse((PACKAGE / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "translated_error"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                keys.add(node.args[0].value)
    return keys


def test_every_raised_exception_has_a_translated_message() -> None:
    keys = _raised_exception_keys()
    assert keys
    assert keys <= STRINGS["exceptions"].keys()


def test_exception_messages_only_use_the_error_placeholder() -> None:
    for entry in STRINGS["exceptions"].values():
        assert set(re.findall(r"{(\w+)}", entry["message"])) <= {"error"}


def _statistic_name_arguments() -> list[ast.expr]:
    """Every expression the coordinator hands the recorder as a statistic name.

    A series names itself in the tuple it appends, the one-shot series name
    theirs where they are passed to ``metadata()``, and the looped series name
    is the loop variable, so all three shapes are collected.
    """
    tree = ast.parse((PACKAGE / "coordinator.py").read_text(encoding="utf-8"))
    names: list[ast.expr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        called = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        if called == "metadata" and len(node.args) > 1:
            names.append(node.args[1])
        elif called == "append":
            series = node.args[0]
            if isinstance(series, ast.Tuple) and len(series.elts) == 5:
                names.append(series.elts[4])
    return names


def test_a_statistic_name_is_never_interpolated() -> None:
    """A sensor key is not a display name: ``f"SP Group {key}"`` would ship one."""
    names = _statistic_name_arguments()
    assert names
    assert all(isinstance(name, ast.Name) for name in names)


def test_every_statistic_name_is_spelled_out_and_used() -> None:
    series_names = [
        name
        for name in _statistic_name_arguments()
        if isinstance(name, ast.Name) and name.id.startswith("STATISTIC_NAME_")
    ]
    assert series_names
    used = {name.id for name in series_names}
    declared = {name for name in vars(const) if name.startswith("STATISTIC_NAME_")}
    assert used == declared
    for name in sorted(used):
        label = getattr(const, name)
        assert label.startswith("SP Group ")
        # A sensor key pasted into a name keeps its underscores:
        # "SP Group electricity" is a name, "SP Group electricity_last_period"
        # is a key that leaked into one.
        assert "_" not in label
