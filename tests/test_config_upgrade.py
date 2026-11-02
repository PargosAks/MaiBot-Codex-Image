"""Upgrade synthetic 0.9.0 data without importing Host or reading live config."""

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Mapping
import ast
import os

import pytest

from image_plugin_under_test import Config


# Fixed Host 1.3.15 type contract; runs even without an installed public Host.
CONTRACT = '''
def _deep_copy_plugin_config_value(value):
    if isinstance(value, Mapping):
        return _deep_copy_plugin_config_mapping(value)
    if isinstance(value, list):
        return [_deep_copy_plugin_config_value(item) for item in value]
    return value

def _deep_copy_plugin_config_mapping(value):
    return {str(key): _deep_copy_plugin_config_value(item) for key, item in value.items()}

def _is_plugin_config_value_type_compatible(target_value, source_value):
    if target_value is None:
        return True
    if isinstance(target_value, bool):
        return isinstance(source_value, bool)
    if isinstance(target_value, str):
        return isinstance(source_value, str)
    if isinstance(target_value, list):
        return isinstance(source_value, list)
    if isinstance(target_value, int):
        return isinstance(source_value, int) and not isinstance(source_value, bool)
    if isinstance(target_value, float):
        return isinstance(source_value, (int, float)) and not isinstance(source_value, bool)
    return isinstance(source_value, type(target_value))

def _overlay_plugin_config_fields(target, source):
    for key, source_value in source.items():
        if key not in target or key == "config_version":
            continue
        target_value = target[key]
        if isinstance(target_value, dict) and isinstance(source_value, Mapping):
            _overlay_plugin_config_fields(target_value, source_value)
            continue
        if _is_plugin_config_value_type_compatible(target_value, source_value):
            target[key] = _deep_copy_plugin_config_value(source_value)
'''
NAMES = {
    "_deep_copy_plugin_config_value", "_deep_copy_plugin_config_mapping",
    "_is_plugin_config_value_type_compatible", "_overlay_plugin_config_fields",
}
FIELDS = (("connection", "poll_seconds", 2.0),
          ("connection", "watch_seconds", 360.0),
          ("autonomy", "tick_seconds", 5.0),
          ("autonomy", "timeout_seconds", 30.0))


@pytest.fixture(params=["contract", "public-host"])
def overlay(request):
    text, filename = CONTRACT, "<host-1.3.15-contract>"
    if request.param == "public-host":
        explicit = os.environ.get("CODEX_IMAGE_TEST_HOST_SOURCE")
        source = (Path(explicit) if explicit else Path(__file__).resolve().parents[4]
                  / "local-maibot/tools/maibot/Lib/site-packages/src")
        path = source / "plugin_runtime/runner/runner_main.py"
        if not explicit and not path.is_file():
            pytest.skip("Optional public Host source unavailable; contract tests still run")
        text, filename = path.read_text(encoding="utf-8"), str(path)
    nodes = [node for node in ast.parse(text).body
             if isinstance(node, ast.FunctionDef) and node.name in NAMES]
    assert {node.name for node in nodes} == NAMES
    namespace = {"Any": Any, "Dict": Dict, "Mapping": Mapping}
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.Module(body=[future, *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), filename, "exec"), namespace)
    return namespace["_overlay_plugin_config_fields"]


def test_studio_catalog_is_importable_but_not_an_operator_form():
    from image_plugin_under_test import CodexImagePlugin
    from test_profiles_flow import PROFILES, SERIES

    plugin = CodexImagePlugin()
    plugin.set_plugin_config({"plugin": {"config_version": "0.9.2"},
                              "autonomy": {"spaces": [{"space_id": "legacy-space",
                                                        "profiles": PROFILES, "series": SERIES}]}})
    legacy = plugin.config.autonomy.spaces[0]
    assert legacy.profiles == PROFILES and legacy.series == SERIES
    autonomy_fields = plugin.get_webui_config_schema()["sections"]["autonomy"]["fields"]
    assert autonomy_fields["persona"]["hidden"] is True
    fields = autonomy_fields["spaces"]["item_fields"]
    assert {"profiles", "series"}.isdisjoint(fields)
    assert {"stream_id", "user_id", "continuous", "consent_ref", "limits"} <= fields.keys()


def old_config(integer=False):
    config = Config().model_dump()
    config["plugin"]["config_version"] = "0.9.0"
    config["connection"].update(secret="synthetic-secret-" + "x" * 32,
                                 allowed_users=["qq:synthetic-user"])
    values = (3.0, 1200.0, 2.0, 300.0)
    for (section, key, _), value in zip(FIELDS, values):
        config[section][key] = int(value) if integer else value
    budget = dict(think=16, render=2, share=1, max_inflight=2, max_revisions=1)
    config["autonomy"].update(enabled=False, global_limits=budget,
                              spaces=[dict(space_id="synthetic", limits=deepcopy(budget))])
    return config


@pytest.mark.parametrize("integer", [False, True], ids=["old-float", "old-integer"])
def test_upgrade_preserves_existing_values_and_does_not_alias(overlay, integer):
    old = old_config(integer)
    before = deepcopy(old)
    target = Config().model_dump()
    new_version = target["plugin"]["config_version"]
    overlay(target, old)
    expected = deepcopy(old)
    expected["plugin"]["config_version"] = new_version
    assert target == expected
    assert target["autonomy"]["enabled"] is False
    for section, key, _ in FIELDS:
        assert type(target[section][key]) is (int if integer else float)
    target["connection"]["allowed_users"].append("qq:another")
    target["autonomy"]["spaces"][0]["limits"]["think"] = 0
    target["autonomy"]["global_limits"]["render"] = 0
    assert old == before


def test_regression_integer_defaults_discard_old_float_values(overlay):
    target = Config().model_dump()
    for section, key, value in FIELDS:
        target[section][key] = int(value)
    overlay(target, old_config())
    for section, key, value in FIELDS:
        assert type(target[section][key]) is int
        assert target[section][key] == int(value)


def test_bool_is_not_accepted_as_a_float_setting(overlay):
    target = Config().model_dump()
    old = old_config()
    for section, key, _ in FIELDS:
        old[section][key] = True
    overlay(target, old)
    for section, key, value in FIELDS:
        assert type(target[section][key]) is float
        assert target[section][key] == value


def test_declared_float_defaults_keep_the_same_numbers():
    defaults = Config().model_dump()
    for section, key, value in FIELDS:
        assert type(defaults[section][key]) is float
        assert defaults[section][key] == value


@pytest.mark.parametrize("configured", [None, 217, 217.5])
def test_send_timeout_upgrade_preserves_old_config_and_budgets(overlay, configured):
    old = old_config()
    old["connection"].pop("send_timeout_seconds")
    if configured is not None:
        old["connection"]["send_timeout_seconds"] = configured
    before = deepcopy(old)
    target = Config().model_dump()
    overlay(target, old)
    expected = deepcopy(old)
    expected["plugin"]["config_version"] = target["plugin"]["config_version"]
    expected["connection"]["send_timeout_seconds"] = 240.0 if configured is None else configured
    assert target == expected and old == before
