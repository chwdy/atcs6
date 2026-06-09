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
