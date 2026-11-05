"""Rewrite saved speed profiles from the two-curve format to `voltage_speed_curve`.

One-off tool for layouts saved before the curve redesign (design_new.md
"SpeedProfile"). An old profile stored `power_speed_curve` ([power %, km/h])
and, on a model profile, `voltage_power_curve` ([V, power %]); an instance
profile stored no voltage curve and read its model's, re-anchored at its own
`lighting_voltage`. This tool turns each measured point back into the voltage
it was driven at and writes `voltage_speed_curve` ([V, km/h]):

- a point's voltage is its power percent read through the voltage/power curve
  the profile resolved when it was measured (for an instance: its model's
  curve with the bottom moved to the instance's lighting voltage);
- points that recorded 0 km/h are dropped (they were the stall band, below the
  starting voltage);
- the old curves are removed; `lighting_voltage` is kept as it is.

A curve that does not meet the new conditions after converting (fewer than two
moving points, or a last point short of `max_voltage`) is dropped instead, so
the profile falls back to the seeded default and reports it.

Usage:
    python tools/convert_speed_profiles.py layouts/autosave.json [more.json ...]

Each file is backed up next to itself as `<name>.before-voltage-curve.json`
before it is rewritten. Files with nothing to convert are left untouched.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from speed_profile import curve_problems  # noqa: E402

Point = Tuple[float, float]


def _interpolate(x1: float, y1: float, x2: float, y2: float, x: float) -> float:
    if x2 == x1:
        return y1
    return y1 + (x - x1) / (x2 - x1) * (y2 - y1)


def _power_to_voltage(vp_curve: List[Point], percent: float) -> float:
    """The old `SpeedProfile.power_to_voltage`: percent read backward through a
    [V, power %] curve, clamped at its ends."""
    if not vp_curve:
        return 0.0
    if percent <= vp_curve[0][1]:
        return vp_curve[0][0]
    if percent >= vp_curve[-1][1]:
        return vp_curve[-1][0]
    for (v1, p1), (v2, p2) in zip(vp_curve, vp_curve[1:]):
        if p1 <= percent <= p2:
            return _interpolate(p1, v1, p2, v2, percent)
    return vp_curve[-1][0]


def _reanchor(vp_curve: List[Point], anchor: float, max_voltage: float) -> List[Point]:
    """The old instance overlay: the model's curve with its 0% point moved to
    the instance's lighting voltage."""
    kept = sorted((v, p) for v, p in vp_curve if v > anchor and p > 0.0)
    if not kept:
        kept = [(max_voltage, 100.0)]
    return [(anchor, 0.0)] + kept


def _points(raw: Any) -> List[Point]:
    return [(float(a), float(b)) for a, b in (raw or [])]


def convert_profiles(profiles: Dict[str, Dict[str, Any]]) -> List[str]:
    """Convert every old-format profile in a `speed_profiles` map in place.
    Returns one line per profile describing what happened."""
    report: List[str] = []
    old = {
        pid: p
        for pid, p in profiles.items()
        if isinstance(p, dict) and ("power_speed_curve" in p or "voltage_power_curve" in p)
    }
    # Read every model's stored voltage/power curve before anything is rewritten.
    model_vp = {pid: _points(p.get("voltage_power_curve")) for pid, p in old.items()}
    for pid, profile in old.items():
        model_id = profile.get("derived_from")
        model = profiles.get(model_id) if model_id else None
        max_voltage = float(
            (model or profile).get("max_voltage", profile.get("max_voltage", 12.0)) or 12.0
        )
        if model_id:
            lighting = profile.get("lighting_voltage")
            if lighting is None and model is not None:
                lighting = model.get("lighting_voltage")
            vp = _reanchor(model_vp.get(model_id, []), float(lighting or 0.0), max_voltage)
        else:
            vp = model_vp.get(pid, [])
            if not vp:
                anchor = float(profile.get("lighting_voltage") or 0.0)
                vp = [(anchor, 0.0), (max_voltage, 100.0)]

        ps = profile.get("power_speed_curve")
        curve: Optional[List[List[float]]] = None
        if ps is not None:
            converted = sorted(
                (round(_power_to_voltage(vp, pct), 4), speed)
                for pct, speed in _points(ps)
                if speed > 0.0
            )
            problems = curve_problems(converted, max_voltage)
            if problems:
                report.append(f"{pid}: dropped its curve ({'; '.join(problems)})")
            else:
                curve = [[v, s] for v, s in converted]
                report.append(f"{pid}: {len(curve)} points, starting at {curve[0][0]:g} V")
        else:
            report.append(f"{pid}: no curve of its own")

        profile.pop("power_speed_curve", None)
        profile.pop("voltage_power_curve", None)
        if curve is not None:
            profile["voltage_speed_curve"] = curve
        elif model_id:
            profile["voltage_speed_curve"] = None
        else:
            profile.pop("voltage_speed_curve", None)
    return report


def _profile_maps(node: Any):
    """Every `speed_profiles` map anywhere in a saved layout."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "speed_profiles" and isinstance(value, dict):
                yield value
            else:
                yield from _profile_maps(value)
    elif isinstance(node, list):
        for value in node:
            yield from _profile_maps(value)


def convert_file(path: Path) -> List[str]:
    data = json.loads(path.read_text())
    report: List[str] = []
    for profiles in _profile_maps(data):
        report.extend(convert_profiles(profiles))
    if not report:
        return [f"{path}: nothing to convert"]
    backup = path.with_name(path.stem + ".before-voltage-curve.json")
    shutil.copyfile(path, backup)
    path.write_text(json.dumps(data, indent=2))
    return [f"{path} (backup: {backup.name})"] + ["  " + line for line in report]


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for name in argv:
        for line in convert_file(Path(name)):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
