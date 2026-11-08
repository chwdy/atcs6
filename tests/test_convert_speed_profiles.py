"""tools/convert_speed_profiles.py — old two-curve profiles to `voltage_speed_curve`."""

import json

from speed_profile import SpeedProfileRegistry
from tools.convert_speed_profiles import convert_file, convert_profiles


def _old_pair():
    """The rig profile pair as it was saved on 2026-11-03: the model measured
    with the 1.5 V anchor, the instance later given a 1.9 V lighting voltage."""
    return {
        "M": {
            "profile_id": "M",
            "name": "Model",
            "derived_from": None,
            "lighting_voltage": 1.5,
            "max_voltage": 12.0,
            "power_speed_curve": [[0, 0], [20, 87.0], [40, 190.0], [60, 228.0], [80, 245.0], [100, 262.0]],
            "voltage_power_curve": [[1.5, 0.0], [12.0, 100.0]],
        },
        "I": {
            "profile_id": "I",
            "name": "Instance",
            "derived_from": "M",
            "lighting_voltage": 1.9,
            "power_speed_curve": None,
            "voltage_power_curve": [],
        },
    }


def test_each_point_is_stored_at_the_voltage_it_was_measured_at():
    profiles = _old_pair()
    convert_profiles(profiles)
    assert profiles["M"]["voltage_speed_curve"] == [
        [3.6, 87.0], [5.7, 190.0], [7.8, 228.0], [9.9, 245.0], [12.0, 262.0]
    ]
    assert "power_speed_curve" not in profiles["M"]
    assert "voltage_power_curve" not in profiles["M"]
    # The instance had no curve of its own and keeps reading its model's.
    assert profiles["I"]["voltage_speed_curve"] is None
    assert profiles["I"]["lighting_voltage"] == 1.9

    registry = SpeedProfileRegistry.from_dict(profiles)
    resolved = registry.resolve("I")
    assert resolved.first_point_voltage == 3.6
    assert resolved.top_speed_kmh == 262.0
    assert resolved.lighting_voltage == 1.9


def test_an_instance_curve_is_read_through_its_own_lighting_anchor():
    profiles = _old_pair()
    profiles["I"]["power_speed_curve"] = [[0, 0], [50, 100.0], [100, 200.0]]
    convert_profiles(profiles)
    # 50% on the model's line re-anchored at 1.9 V is 1.9 + 0.5 * 10.1 = 6.95 V.
    assert profiles["I"]["voltage_speed_curve"] == [[6.95, 100.0], [12.0, 200.0]]


def test_a_curve_that_cannot_be_stored_is_dropped():
    profiles = _old_pair()
    profiles["M"]["power_speed_curve"] = [[0, 0], [100, 0.0]]
    report = convert_profiles(profiles)
    assert "voltage_speed_curve" not in profiles["M"]
    assert any("dropped its curve" in line for line in report)


def test_a_file_is_backed_up_and_rewritten(tmp_path):
    path = tmp_path / "layout.json"
    path.write_text(json.dumps({"snapshot": {"speed_profiles": _old_pair()}}))
    lines = convert_file(path)
    assert "backup" in lines[0]
    assert (tmp_path / "layout.before-voltage-curve.json").exists()
    data = json.loads(path.read_text())
    assert data["snapshot"]["speed_profiles"]["M"]["voltage_speed_curve"][0] == [3.6, 87.0]


def test_a_file_with_nothing_to_convert_is_left_alone(tmp_path):
    path = tmp_path / "layout.json"
    path.write_text(json.dumps({"snapshot": {"speed_profiles": {}}}))
    assert convert_file(path) == [f"{path}: nothing to convert"]
    assert not (tmp_path / "layout.before-voltage-curve.json").exists()
