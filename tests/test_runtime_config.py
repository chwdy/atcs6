"""Phase 2 (Advanced settings): runtime_config applies updates, coerces types,
and propagates to the live consumer module globals so changes take effect
without a restart. Backs `GET/PUT /api/settings`.
"""

import pytest

import runtime_config as rc


@pytest.fixture(autouse=True)
def _reset_runtime_config():
    # Never let a mutated tunable leak into other tests.
    rc.reset_to_defaults()
    yield
    rc.reset_to_defaults()


def test_apply_updates_coerces_and_propagates_to_module_globals():
    import constants
    import train_controller
    import train_controller.ato
    import train_physics

    rc.apply_updates(
        {
            "overspeed_eb_threshold_kmh": "12",  # coerced from str -> float
            "stopped_speed_threshold_kmh": 0.2,
            "brake_deadband_kmh": 0.5,
            "caution_speed_limit_kmh": 33.0,
        }
    )

    cfg = rc.get_runtime_config()
    assert cfg.overspeed_eb_threshold_kmh == 12.0
    # propagated onto every module global that holds the value
    assert train_controller.OVERSPEED_EB_THRESHOLD_KMH == 12.0
    assert constants.STOPPED_SPEED_THRESHOLD_KMH == 0.2
    assert train_physics.STOPPED_SPEED_THRESHOLD_KMH == 0.2
    assert train_controller.ato.BRAKE_DEADBAND_KMH == 0.5
    assert train_controller.atc.CAUTION_SPEED_LIMIT_KMH == 33.0


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




def test_reset_restores_defaults_and_repropagates():
    import train_controller

    rc.apply_updates({"overspeed_eb_threshold_kmh": 99})
    assert train_controller.OVERSPEED_EB_THRESHOLD_KMH == 99
    rc.reset_to_defaults()
    assert rc.get_runtime_config().overspeed_eb_threshold_kmh == 10.0
    assert train_controller.OVERSPEED_EB_THRESHOLD_KMH == 10.0


def test_as_dict_round_trips_and_unknown_keys_ignored():
    d = rc.as_dict()
    assert "overspeed_eb_threshold_kmh" in d
    assert "stopped_speed_threshold_kmh" in d
    # unknown keys and None values are silently ignored (no crash)
    rc.apply_updates({"nonexistent_field": 5, "overspeed_eb_threshold_kmh": None})
    assert "nonexistent_field" not in rc.as_dict()
    assert rc.get_runtime_config().overspeed_eb_threshold_kmh == 10.0


def test_notification_retention_is_tunable_and_bounds_the_archive():
    """`NotificationCenter` keeps the most recent `notification_retention_rows`
    events and drops the oldest (design_new.md "Runtime notifications",
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
# Validation (design_new.md "Runtime settings", Validation)
# ---------------------------------------------------------------------------


def test_non_finite_values_are_ignored():
    cfg = rc.get_runtime_config()
    defaults = rc.RuntimeConfig()
    rc.apply_updates({"caution_speed_limit_kmh": float("inf")})
    assert cfg.caution_speed_limit_kmh == defaults.caution_speed_limit_kmh
    rc.apply_updates({"ato_speed_margin_kmh": float("nan")})
    assert cfg.ato_speed_margin_kmh == defaults.ato_speed_margin_kmh


def test_single_field_bound_violations_are_ignored():
    cfg = rc.get_runtime_config()
    defaults = rc.RuntimeConfig()
    rc.apply_updates({"overspeed_warn_threshold_kmh": -1.0})  # must be >= 0
    assert cfg.overspeed_warn_threshold_kmh == defaults.overspeed_warn_threshold_kmh
    rc.apply_updates({"atc_signal_lookahead_blocks": 0})  # must be >= 1
    assert cfg.atc_signal_lookahead_blocks == defaults.atc_signal_lookahead_blocks
    rc.apply_updates({"stopped_speed_threshold_kmh": 0.0})  # must be > 0
    assert cfg.stopped_speed_threshold_kmh == defaults.stopped_speed_threshold_kmh


def test_other_fields_still_apply_when_one_is_dropped():
    cfg = rc.get_runtime_config()
    defaults = rc.RuntimeConfig()
    rc.apply_updates(
        {
            "atc_signal_lookahead_blocks": 0,  # dropped (must be >= 1)
            "brake_deadband_kmh": 0.7,  # valid, applies
        }
    )
    assert cfg.atc_signal_lookahead_blocks == defaults.atc_signal_lookahead_blocks
    assert cfg.brake_deadband_kmh == 0.7


def test_overspeed_relation_drops_both_writes_against_merged_pair():
    """EB must stay above warn. A request that would reverse the pair —
    evaluated against the merged post-request values — drops both threshold
    writes but leaves unrelated fields applied."""
    cfg = rc.get_runtime_config()
    defaults = rc.RuntimeConfig()  # eb 10, warn 5

    # eb below the current warn: violates against the merged pair -> both dropped.
    rc.apply_updates({"overspeed_eb_threshold_kmh": 1.0, "lookahead_headroom": 0.9})
    assert cfg.overspeed_eb_threshold_kmh == defaults.overspeed_eb_threshold_kmh
    assert cfg.lookahead_headroom == 0.9  # unrelated field applied

    # Raising warn above the current eb (merged 10 vs 20) -> dropped.
    rc.apply_updates({"overspeed_warn_threshold_kmh": 20.0})
    assert cfg.overspeed_warn_threshold_kmh == defaults.overspeed_warn_threshold_kmh

    # A consistent pair applies together.
    rc.apply_updates(
        {"overspeed_warn_threshold_kmh": 6.0, "overspeed_eb_threshold_kmh": 12.0}
    )
    assert cfg.overspeed_warn_threshold_kmh == 6.0
    assert cfg.overspeed_eb_threshold_kmh == 12.0


def test_ato_tolerance_relation_drops_both_when_reversed():
    cfg = rc.get_runtime_config()
    defaults = rc.RuntimeConfig()  # min 0.05, max 2.0
    # max below current min (merged pair) -> both dropped.
    rc.apply_updates({"ato_speed_tolerance_max_kmh": 0.01})
    assert cfg.ato_speed_tolerance_max_kmh == defaults.ato_speed_tolerance_max_kmh
    # A valid widening applies together.
    rc.apply_updates(
        {"ato_speed_tolerance_min_kmh": 0.1, "ato_speed_tolerance_max_kmh": 3.0}
    )
    assert cfg.ato_speed_tolerance_min_kmh == 0.1
    assert cfg.ato_speed_tolerance_max_kmh == 3.0
