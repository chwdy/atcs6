"""
ATONavigator — owns the ATO speed-curve / handle-selection / destination-info
computation pulled out of `TrainController`.

Per design_new.md "ATO Computer": ATO produces the next handle command in
ATO mode by tracking the operational speed curve published in
`controller.get_lookahead()`. The computation is mostly stateless once the
curve and current speed are known; the few pieces of state it does touch
on the controller (`handle_reason`, `_previous_speed`) are diagnostic.

Algorithm summary (kinematic, single pass):

1. Determine the *ceiling at distance 0* — the most binding of the curve
   sample at 0, train max speed, ATC limit, and current block limit. The
   train must not exceed this.
2. If the ceiling at 0 is below the stopped-threshold, brake (B8) until
   stopped.
3. Otherwise, sample the operational curve at evenly-spaced points within a
   *planning horizon* (proportional to current speed × tick dt, capped at
   `ATO_RESTRICTION_LOOKAHEAD_CM`). For each sample, compute the
   deceleration we would need to be at-or-below the curve when we reach
   that point. Take the maximum required.
4. Map `required_decel / op_decel` to a brake notch via `BRAKE_MULTIPLIERS`
   (the table is now defined as multipliers of op_decel — B6 = 1.00×).
5. If no brake is needed, power up while below the ceiling, else coast.

`TrainController` keeps the public entry points (`_calculate_ato_info`,
`_ato_calculate_handle`, `_calculate_braking_notch_for_speed`,
`_calculate_power_notch`) as thin forwarders that delegate here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from constants import STOPPED_SPEED_THRESHOLD_KMH
from enums import (
    BRAKE_MULTIPLIERS,
    HANDLE_POSITION_INDEX,
    INDEX_TO_HANDLE_POSITION,
    POWER_MULTIPLIERS,
    HandlePosition,
)
from model_scale import get_speed_conversion_factor
from train_controller_types import ATOInfo

if TYPE_CHECKING:
    from train_controller import TrainController


# Tuning constants. ATO_RESTRICTION_LOOKAHEAD_CM caps how far ahead the
# planner samples the curve; PLANNING_TICKS_AHEAD scales the actual horizon
# with current speed and tick dt.
#
# Speed-tracking deadband is scaled to the operating band rather than fixed
# at one value. A 1-2 km/h gap is natural at 50 km/h cruise but huge at
# 1 km/h creep — the same fixed deadband would prevent the train from
# holding a low-speed ceiling. `ato_speed_tolerance(reference)` returns
# `clamp(reference * RATIO, MIN, MAX)`, so the band stays a similar
# fraction of the speed it is bounding. MAX preserves the prior high-speed
# behavior; MIN keeps creep precise enough to honor sub-km/h ceilings near
# the authority-end safety margin.
ATO_SPEED_TOLERANCE_MAX = 2.0
ATO_SPEED_TOLERANCE_MIN = 0.05
ATO_SPEED_TOLERANCE_RATIO = 0.1
ATO_RESTRICTION_LOOKAHEAD_CM = 150.0
PLANNING_TICKS_AHEAD = 100
MIN_PLANNING_HORIZON_CM = 30.0
PLANNING_SAMPLE_COUNT = 6


def ato_speed_tolerance(reference_kmh: float) -> float:
    """Speed-tracking tolerance for ATO at the given reference speed.

    `reference_kmh` is typically `max(current_speed_kmh, ceiling_at_zero)` —
    the value the band is sized against. The result is clamped to
    [ATO_SPEED_TOLERANCE_MIN, ATO_SPEED_TOLERANCE_MAX].
    """
    return max(
        ATO_SPEED_TOLERANCE_MIN,
        min(ATO_SPEED_TOLERANCE_MAX, max(0.0, reference_kmh) * ATO_SPEED_TOLERANCE_RATIO),
    )


# =============================================================================
# Module-level helper: rate-limit handle changes
# =============================================================================


def rate_limit_handle(
    current: HandlePosition,
    target: HandlePosition,
    allow_fast: bool = False,
) -> HandlePosition:
    """Rate-limit handle changes to prevent jitter.

    Limits handle movement to ±1 notch per call (±2 if `allow_fast=True`).
    Emergency brake (`HandlePosition.EB`) always bypasses rate limiting.
    """
    if target == HandlePosition.EB:
        return HandlePosition.EB

    current_idx = HANDLE_POSITION_INDEX.get(current, 5)
    target_idx = HANDLE_POSITION_INDEX.get(target, 5)

    max_step = 2 if allow_fast else 1

    if target_idx > current_idx:
        new_idx = min(current_idx + max_step, target_idx)
    elif target_idx < current_idx:
        new_idx = max(current_idx - max_step, target_idx)
    else:
        new_idx = current_idx

    return INDEX_TO_HANDLE_POSITION.get(new_idx, HandlePosition.N)


# =============================================================================
# ATONavigator
# =============================================================================


_BRAKE_HANDLES_ASCENDING = (
    HandlePosition.B1,
    HandlePosition.B2,
    HandlePosition.B3,
    HandlePosition.B4,
    HandlePosition.B5,
    HandlePosition.B6,
    HandlePosition.B7,
    HandlePosition.B8,
)


class ATONavigator:
    """Owns the ATO computations for one controller."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller

    # ------------------------------------------------------------------
    # Destination info (used by tick() for display)
    # ------------------------------------------------------------------

    def compute_ato_info(self) -> Optional[ATOInfo]:
        """Build the `ATOInfo` payload for the controller's current path
        + destination, or return None when neither is known."""
        c = self._c
        train = c._get_train()
        if not train or not c.network:
            return None

        # `path_tiles` is read for parity with the original implementation
        # (which fetched it before deciding whether to compute).
        c.get_path_tiles()

        path_id = c.assigned_path_id or c.ato_path_id
        destination_block_id = c.ato_destination_block_id

        if not path_id and not destination_block_id:
            return None

        path = None
        if hasattr(c.network, "paths"):
            path = c.network.paths.get(path_id)

        if path_id and not path and not destination_block_id:
            return ATOInfo(path_id=path_id)

        path_heading = "a"
        required_cab = c.cab or "A"

        connectivity_cab = c._required_cab_for_path()
        if connectivity_cab is not None:
            required_cab = connectivity_cab

        destination_station_name = None

        if not destination_block_id:
            destination_block_id = c._get_current_block_id()

        if destination_block_id:
            station_id = c._get_station_in_block(destination_block_id)
            if station_id:
                station = c.network.platforms.get(station_id)
                if station:
                    destination_station_name = station.name

        return ATOInfo(
            path_id=path_id,
            destination_block_id=destination_block_id,
            destination_station_name=destination_station_name,
            path_heading=path_heading,
            required_cab=required_cab,
        )

    # ------------------------------------------------------------------
    # Speed-curve sampling and forward simulation
    # ------------------------------------------------------------------

    @staticmethod
    def sample_curve_at(
        curve: List[Dict], distance_cm: float, key: str = "operational_limit"
    ) -> float:
        """Linearly interpolate a speed-curve sample at `distance_cm`.

        Pure: depends only on `curve` (a list of `{distance_cm, atc_limit,
        operational_limit, ...}` entries) and the requested key.
        """
        if not curve:
            return 0.0

        if distance_cm <= 0:
            return curve[0].get(key, 0.0)

        if distance_cm >= curve[-1].get("distance_cm", 0):
            return curve[-1].get(key, 0.0)

        for i in range(len(curve) - 1):
            d1 = curve[i].get("distance_cm", 0)
            d2 = curve[i + 1].get("distance_cm", 0)
            if d1 <= distance_cm <= d2:
                v1 = curve[i].get(key, 0.0)
                v2 = curve[i + 1].get(key, 0.0)
                t = (distance_cm - d1) / (d2 - d1) if d2 != d1 else 0
                return v1 + t * (v2 - v1)

        return curve[-1].get(key, 0.0)

    def simulate_forward(
        self,
        current_speed_kmh: float,
        handle: HandlePosition,
        distance_cm: float,
        dt: float = 0.02,
    ) -> float:
        """Predict the train's speed at `distance_cm` ahead under `handle`.

        Brake notches apply `op_decel × BRAKE_MULTIPLIERS[handle]`, matching
        `train_physics`. Air resistance is intentionally omitted because
        callers compare handles relative to each other.
        """
        if distance_cm <= 0:
            return current_speed_kmh

        profile = self._c._get_speed_profile()
        op_decel = profile.deceleration_rate * profile.operational_decel_ratio

        if handle in POWER_MULTIPLIERS:
            accel_kmh_s = profile.acceleration_rate * POWER_MULTIPLIERS[handle]
        elif handle in BRAKE_MULTIPLIERS:
            accel_kmh_s = -op_decel * BRAKE_MULTIPLIERS[handle]
        elif handle == HandlePosition.EB:
            accel_kmh_s = -profile.emergency_brake_rate
        else:
            accel_kmh_s = 0.0

        km_h_to_cm_s = get_speed_conversion_factor()
        speed_cm_s = current_speed_kmh * km_h_to_cm_s
        distance_remaining = distance_cm

        while distance_remaining > 0 and speed_cm_s >= 0:
            speed_cm_s += accel_kmh_s * km_h_to_cm_s * dt

            if handle == HandlePosition.N and speed_cm_s > 0:
                speed_cm_s -= profile.friction_rate * km_h_to_cm_s * dt

            speed_kmh = speed_cm_s / km_h_to_cm_s
            if speed_kmh <= STOPPED_SPEED_THRESHOLD_KMH and accel_kmh_s <= 0:
                speed_cm_s = 0.0
                break

            if speed_cm_s <= 0:
                speed_cm_s = 0.0
                break

            step_dist = speed_cm_s * dt
            distance_remaining -= step_dist

        return speed_cm_s / km_h_to_cm_s

    # ------------------------------------------------------------------
    # Notch helpers
    # ------------------------------------------------------------------

    @staticmethod
    def pick_brake_notch_for_multiplier(multiplier: float) -> HandlePosition:
        """Smallest brake notch whose `BRAKE_MULTIPLIERS` value ≥ `multiplier`.

        Multipliers are relative to op_decel. Caps at B8 — if more decel is
        required than B8 provides, ATC EB will catch the resulting overspeed.
        """
        if multiplier <= 0:
            return HandlePosition.N
        for handle in _BRAKE_HANDLES_ASCENDING:
            if BRAKE_MULTIPLIERS[handle] >= multiplier:
                return handle
        return HandlePosition.B8

    @staticmethod
    def calculate_power_notch(
        current_speed: float, target_speed: float
    ) -> HandlePosition:
        """Pick a power notch to accelerate toward `target_speed`."""
        diff = target_speed - current_speed
        if diff > 8:
            return HandlePosition.P5
        if diff > 5:
            return HandlePosition.P4
        if diff > 3:
            return HandlePosition.P3
        if diff > 1.5:
            return HandlePosition.P2
        return HandlePosition.P1

    @staticmethod
    def calculate_braking_notch_for_speed(
        current_speed: float, target_speed: float
    ) -> HandlePosition:
        """Pick a brake notch from a raw speed gap (current → target).

        Used by callers that need a notch independent of distance — e.g. the
        ATO-not-running "brake to stop" branch in `TrainController.tick()`.
        Picks more aggressively as the gap grows. Each notch's multiplier
        is interpreted against op_decel (see `BRAKE_MULTIPLIERS`).
        """
        diff = current_speed - target_speed
        if diff <= 0:
            return HandlePosition.N
        if diff > 25:
            return HandlePosition.B8
        if diff > 18:
            return HandlePosition.B7
        if diff > 12:
            return HandlePosition.B6
        if diff > 8:
            return HandlePosition.B5
        if diff > 5:
            return HandlePosition.B4
        if diff > 3:
            return HandlePosition.B3
        if diff > 1:
            return HandlePosition.B2
        return HandlePosition.B1

    # ------------------------------------------------------------------
    # Top-level handle calculator
    # ------------------------------------------------------------------

    def compute_handle(self, delta_seconds: float = 0.05) -> HandlePosition:
        """Pick the next ATO handle by tracking the operational speed curve.

        Mutates three diagnostic fields on the controller (`handle_reason`,
        `_previous_speed`, `ato_debug`); does not touch any other controller
        state. `ato_debug` is consumed by `routes/inspector.py` "ATO Debug"
        group.

        Args:
            delta_seconds: tick duration in seconds. Used to scale the
                planning horizon with the current motion rate.
        """
        c = self._c
        debug: Dict[str, Any] = {
            "delta_seconds": float(delta_seconds),
            "current_speed_kmh": float(c.current_speed_kmh),
        }

        train = c._get_train()
        if not train:
            c.handle_reason = "No train"
            debug["mode"] = "no_train"
            debug["final_handle"] = HandlePosition.N.value
            c.ato_debug = debug
            return HandlePosition.N

        speed_kmh = c.current_speed_kmh
        c._previous_speed = speed_kmh

        lookahead = c.get_lookahead()
        curves = lookahead.get("lookahead", {}).get("speed_curves", [])

        if not curves:
            c.handle_reason = "No curves"
            debug["mode"] = "no_curves"
            debug["final_handle"] = HandlePosition.N.value
            c.ato_debug = debug
            return HandlePosition.N

        profile = c._get_speed_profile()
        op_decel = max(
            0.1, profile.deceleration_rate * profile.operational_decel_ratio
        )
        km_h_to_cm_s = get_speed_conversion_factor()
        speed_cm_s = speed_kmh * km_h_to_cm_s

        # Effective ceiling at the train head: never exceed any of these.
        curve_at_zero = self.sample_curve_at(curves, 0.0, "operational_limit")
        ceiling_at_zero = min(
            curve_at_zero,
            train.max_speed,
            c.atc_speed_limit,
            c.current_block_speed_limit,
        )

        debug.update(
            {
                "op_decel_kmh_s": float(op_decel),
                "decel_rate_kmh_s": float(profile.deceleration_rate),
                "operational_decel_ratio": float(profile.operational_decel_ratio),
                "curve_at_zero_kmh": float(curve_at_zero),
                "max_speed_kmh": float(train.max_speed),
                "atc_speed_limit_kmh": float(c.atc_speed_limit),
                "block_speed_limit_kmh": (
                    float(c.current_block_speed_limit)
                    if c.current_block_speed_limit != float("inf")
                    else None
                ),
                "ceiling_at_zero_kmh": float(ceiling_at_zero),
            }
        )

        # Stopping zone: curve says stop right here.
        if ceiling_at_zero <= STOPPED_SPEED_THRESHOLD_KMH:
            if speed_kmh > STOPPED_SPEED_THRESHOLD_KMH:
                c.handle_reason = (
                    f"Stopping: ceiling@0={ceiling_at_zero:.2f} "
                    f"speed={speed_kmh:.1f}"
                )
                debug["mode"] = "stopping"
                debug["final_handle"] = HandlePosition.B8.value
                c.ato_debug = debug
                return HandlePosition.B8
            c.handle_reason = "Stopped"
            debug["mode"] = "stopped"
            debug["final_handle"] = HandlePosition.N.value
            c.ato_debug = debug
            return HandlePosition.N

        # Planning horizon: scale with speed and tick dt, capped at
        # ATO_RESTRICTION_LOOKAHEAD_CM and floored at MIN_PLANNING_HORIZON_CM.
        planning_distance_cm = speed_cm_s * delta_seconds * PLANNING_TICKS_AHEAD
        max_curve_dist = curves[-1].get("distance_cm", ATO_RESTRICTION_LOOKAHEAD_CM)
        horizon_cm = min(
            ATO_RESTRICTION_LOOKAHEAD_CM,
            max(MIN_PLANNING_HORIZON_CM, planning_distance_cm),
            max_curve_dist,
        )

        debug.update(
            {
                "planning_distance_cm": float(planning_distance_cm),
                "planning_horizon_cm": float(horizon_cm),
                "planning_horizon_cap_cm": float(ATO_RESTRICTION_LOOKAHEAD_CM),
                "max_curve_distance_cm": float(max_curve_dist),
            }
        )

        # Sample the curve and find the largest required deceleration to be
        # at-or-below the curve at each sample point.
        required_decel_kmh_s = 0.0
        binding_dist_cm = 0.0
        binding_target_kmh = ceiling_at_zero
        samples: List[Dict[str, Any]] = []

        for i in range(PLANNING_SAMPLE_COUNT + 1):
            d_cm = horizon_cm * i / PLANNING_SAMPLE_COUNT
            target_at_d = self.sample_curve_at(curves, d_cm, "operational_limit")
            target_at_d = min(target_at_d, train.max_speed, c.atc_speed_limit)

            sample: Dict[str, Any] = {
                "distance_cm": round(d_cm, 2),
                "target_kmh": round(target_at_d, 2),
                "skipped": False,
                "required_decel_kmh_s": 0.0,
                "multiplier": 0.0,
            }

            # Deadband scales with the reference speed: 2 km/h at cruise is
            # natural; the same gap at 1 km/h creep is huge in relative terms
            # and would let the train coast past low-speed ceilings.
            skip_tolerance = ato_speed_tolerance(max(speed_kmh, target_at_d))
            if target_at_d >= speed_kmh - skip_tolerance:
                sample["skipped"] = True
                sample["skip_reason"] = "target ≥ speed - tolerance"
                sample["tolerance_kmh"] = skip_tolerance
                samples.append(sample)
                continue

            if d_cm <= 0:
                # Already over the curve at the train head — close the gap
                # in roughly one second of catch-up over op_decel.
                gap_kmh = speed_kmh - target_at_d
                req = op_decel + gap_kmh
                sample["formula"] = "op_decel + (speed - target)"
            else:
                u_cm_s = speed_cm_s
                v_cm_s = target_at_d * km_h_to_cm_s
                a_cm_s2 = (u_cm_s * u_cm_s - v_cm_s * v_cm_s) / (2.0 * d_cm)
                req = a_cm_s2 / km_h_to_cm_s
                sample["formula"] = "(u² - v²) / (2 d)"

            sample["required_decel_kmh_s"] = round(req, 3)
            sample["multiplier"] = round(req / op_decel, 3)
            samples.append(sample)

            if req > required_decel_kmh_s:
                required_decel_kmh_s = req
                binding_dist_cm = d_cm
                binding_target_kmh = target_at_d

        debug["samples"] = samples
        debug["binding_distance_cm"] = round(binding_dist_cm, 2)
        debug["binding_target_kmh"] = round(binding_target_kmh, 2)
        debug["required_decel_kmh_s"] = round(required_decel_kmh_s, 3)

        # No braking required — power up while below the ceiling, else coast.
        # Power-up deadband scales with the reference speed via
        # `ato_speed_tolerance(...)`. At cruise this is ~2 km/h so the train
        # holds near the ceiling instead of bleeding off after coasting in;
        # at very low ceilings (e.g. a 0.8 km/h authority-end creep band) it
        # tightens automatically so the deadband does not eat the ceiling.
        if required_decel_kmh_s <= 0.0:
            debug["multiplier_needed"] = 0.0
            power_up_tolerance = ato_speed_tolerance(
                max(speed_kmh, ceiling_at_zero)
            )
            debug["power_up_tolerance_kmh"] = power_up_tolerance
            if speed_kmh < ceiling_at_zero - power_up_tolerance:
                target_handle = self.calculate_power_notch(
                    speed_kmh, ceiling_at_zero
                )
                c.handle_reason = (
                    f"Power up: speed={speed_kmh:.1f} "
                    f"< ceiling={ceiling_at_zero:.1f}"
                )
                debug["mode"] = "power_up"
            else:
                target_handle = HandlePosition.N
                c.handle_reason = (
                    f"Coast: speed={speed_kmh:.1f} ceiling={ceiling_at_zero:.1f}"
                )
                debug["mode"] = "coast"
            debug["target_handle"] = target_handle.value
            # Power-up uses the same fast rate (±2 notches/tick) as braking,
            # so re-accelerating after a slow zone matches the snap of a
            # fresh start instead of crawling up one notch per tick.
            final = rate_limit_handle(
                current=c.handle_position,
                target=target_handle,
                allow_fast=True,
            )
            debug["rate_limited"] = final != target_handle
            debug["final_handle"] = final.value
            c.ato_debug = debug
            return final

        # Brake notch from required deceleration as a multiple of op_decel.
        multiplier_needed = required_decel_kmh_s / op_decel
        target_handle = self.pick_brake_notch_for_multiplier(multiplier_needed)
        overspeed_at_zero = speed_kmh > ceiling_at_zero + ato_speed_tolerance(
            max(speed_kmh, ceiling_at_zero)
        )

        debug.update(
            {
                "mode": "brake",
                "multiplier_needed": round(multiplier_needed, 3),
                "target_handle": target_handle.value,
                "overspeed_at_zero": overspeed_at_zero,
            }
        )

        c.handle_reason = (
            f"Brake {target_handle.value}: "
            f"need={required_decel_kmh_s:.2f}km/h/s "
            f"({multiplier_needed:.2f}× op_decel) "
            f"target={binding_target_kmh:.1f}@{binding_dist_cm:.0f}cm"
        )

        if overspeed_at_zero:
            # Already over the ceiling — bypass rate-limit so the chosen
            # notch takes effect immediately.
            debug["rate_limited"] = False
            debug["final_handle"] = target_handle.value
            c.ato_debug = debug
            return target_handle

        final = rate_limit_handle(
            current=c.handle_position,
            target=target_handle,
            allow_fast=True,
        )
        debug["rate_limited"] = final != target_handle
        debug["final_handle"] = final.value
        c.ato_debug = debug
        return final
