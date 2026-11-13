"""The runtime settings (design_v2.md "Runtime settings"): `runtime_config`
applies updates, coerces and validates them, and writes each accepted value
onto the module attributes its consumers read, so a change takes effect with
no restart. Backs `GET/PUT /api/settings`.
"""

import math

import pytest

import runtime_config as rc


@pytest.fixture(autouse=True)
def _reset_runtime_config():
    # Never let a mutated setting leak into other tests.
    rc.reset_to_defaults()
    yield
    rc.reset_to_defaults()


def test_the_fields_are_the_two_the_document_names():
    assert rc.as_dict() == {"hold_time_s": 5.0, "notification_retention_rows": 10000}


def test_a_hold_time_write_reaches_a_running_train_runtime():
    """A `TrainRuntime` built without its own hold time reads the live
    setting, so a write reaches a session that is already running."""
    import train_runtime
    from network import RailwayNetwork

    runtime = train_runtime.TrainRuntime(RailwayNetwork(), set_block_voltage=lambda *a: None)
    rc.apply_updates({"hold_time_s": "7.5"})  # coerced from str
    assert rc.get_runtime_config().hold_time_s == 7.5
    assert train_runtime.HOLD_TIME_S == 7.5
    assert runtime.hold_time_s == 7.5


def test_reset_restores_defaults_and_repropagates():
    import train_runtime

    rc.apply_updates({"hold_time_s": 9})
    rc.reset_to_defaults()
    assert rc.get_runtime_config().hold_time_s == 5.0
    assert train_runtime.HOLD_TIME_S == 5.0


def test_every_mirror_target_resolves():
    """Every `_TARGETS` entry must name a module global that actually exists.

    `_propagate` guards each rebind with `hasattr(module, attr)`, so a target
    that was renamed — or that never existed — is skipped in silence: the
    settings API accepts the value, stores it, and serves it back while the
    consumer keeps its old constant. That failure is invisible at runtime, so
    it has to be caught here.

    A tunable whose consumer does not yet read a module global simply gets no
    mirror for it, rather than a mirror that silently does nothing.
    """
    import importlib

    dead = []
    for field_name, targets in rc._TARGETS.items():
        for module_path, attr in targets:
            try:
                module = importlib.import_module(module_path)
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                dead.append(f"{field_name} -> {module_path}.{attr} (import failed: {exc})")
                continue
            if not hasattr(module, attr):
                dead.append(f"{field_name} -> {module_path}.{attr} (attribute missing)")

    assert not dead, (
        "runtime_config._TARGETS names module globals that do not exist, so "
        "`_propagate` skips them and the tunable silently does nothing:\n  "
        + "\n  ".join(dead)
    )


def test_every_tunable_has_at_least_one_mirror():
    """A `RuntimeConfig` field with no `_TARGETS` entry is a settings-API knob
    that cannot reach any consumer. Kept separate from the resolve check so the
    two failures read differently."""
    from dataclasses import fields

    unmirrored = [
        f.name for f in fields(rc.RuntimeConfig) if not rc._TARGETS.get(f.name)
    ]
    assert not unmirrored, (
        f"RuntimeConfig fields with no propagation target: {unmirrored}"
    )


def test_notification_retention_is_tunable_and_bounds_the_archive():
    """`NotificationCenter` keeps the most recent `notification_retention_rows`
    events and drops the oldest (design_v2.md "Runtime notifications",
    Retention). `notification_id` keeps counting past a drop, so a gap in the
    ids means events aged out rather than that the stream lost order."""
    import notification_center
    from notification_center import Kind, NotificationCenter

    center = NotificationCenter()
    rc.apply_updates({"notification_retention_rows": 5})
    assert notification_center.NOTIFICATION_RETENTION_ROWS == 5

    for _ in range(12):
        center.emit(source="T", operation="op", kind=Kind.INFO, message="m")

    kept = center.peek_recent(100)
    assert len(kept) == 5
    # The last five emitted, and their ids still run 8..12.
    assert [n.notification_id for n in kept] == [8, 9, 10, 11, 12]

    rc.reset_to_defaults()
    assert notification_center.NOTIFICATION_RETENTION_ROWS == 10000


# ---------------------------------------------------------------------------
# Validation (design_v2.md "Runtime settings", Validation)
# ---------------------------------------------------------------------------


def test_unknown_fields_and_nulls_are_ignored():
    rc.apply_updates({"nonexistent_field": 5, "hold_time_s": None})
    assert "nonexistent_field" not in rc.as_dict()
    assert rc.get_runtime_config().hold_time_s == 5.0


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan, "not a number"])
def test_a_value_that_is_not_a_finite_number_is_ignored(value):
    rc.apply_updates({"hold_time_s": value})
    assert rc.get_runtime_config().hold_time_s == 5.0


@pytest.mark.parametrize(
    "field_name,value",
    [("hold_time_s", 0), ("hold_time_s", -1), ("notification_retention_rows", 0)],
)
def test_a_value_that_breaks_its_field_rule_is_ignored(field_name, value):
    before = getattr(rc.get_runtime_config(), field_name)
    rc.apply_updates({field_name: value})
    assert getattr(rc.get_runtime_config(), field_name) == before


def test_other_fields_still_apply_when_one_is_dropped():
    rc.apply_updates({"hold_time_s": -1, "notification_retention_rows": 50})
    assert rc.get_runtime_config().hold_time_s == 5.0
    assert rc.get_runtime_config().notification_retention_rows == 50
