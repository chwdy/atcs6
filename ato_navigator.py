"""
ATONavigator — owns the ATO speed-curve / handle-selection / destination-info
computation pulled out of `TrainController`.

Per design_new.md "ATO Computer": ATO produces the next handle command in
ATO mode by tracking the operational speed curve published in
`controller.get_lookahead()`. The computation is mostly stateless once the
curve and current speed are known; the few pieces of state it does touch
on the controller (`handle_reason`, `_previous_speed`) are diagnostic.

`TrainController` keeps the public entry points (`_calculate_ato_info`,
`_ato_calculate_handle`, `_calculate_braking_notch`, etc.) as thin
forwarders that delegate here. Existing call sites and `monkeypatch`
fixtures continue to work without change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional

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


# Tuning constants — sourced from `train_controller` module-level constants.
# Re-declared here so this module is self-contained and the values can drift
# independently if needed.
ATO_SPEED_TOLERANCE = 2.0
ATO_RESTRICTION_LOOKAHEAD_CM = 150.0
ATO_OVERSHOOT_WEIGHT = 10.0
NEAR_STOP_DETECTION_CM = 50.0


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
        required_cab = c.active_cab or "A"

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

        Uses a simplified physics model based on `TrainPhysics`: handle →
        acceleration via `POWER_MULTIPLIERS` / `BRAKE_MULTIPLIERS`, then
        time-stepped integration. Air resistance is intentionally omitted
        because the evaluator compares handles relative to each other.
        """
        if distance_cm <= 0:
            return current_speed_kmh

        profile = self._c._get_speed_profile()

        if handle in POWER_MULTIPLIERS:
            accel_kmh_s = profile.acceleration_rate * POWER_MULTIPLIERS[handle]
        elif handle in BRAKE_MULTIPLIERS:
            accel_kmh_s = -profile.deceleration_rate * BRAKE_MULTIPLIERS[handle]
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

    def evaluate_handles_against_curve(
        self,
        current_speed_kmh: float,
        curve: List[Dict],
        horizon_cm: float = 30.0,
        sample_count: int = 5,
    ) -> HandlePosition:
        """Pick the best handle by simulating each candidate forward and
        scoring against the operational speed curve."""
        if not curve:
            return HandlePosition.N

        max_curve_dist = curve[-1].get("distance_cm", 0)
        horizon_cm = min(horizon_cm, max_curve_dist)
        if horizon_cm <= 0:
            return self.calculate_braking_notch(current_speed_kmh)

        current_idx = HANDLE_POSITION_INDEX.get(self._c.handle_position, 5)
        candidate_indices = set()

        candidate_indices.add(HANDLE_POSITION_INDEX[HandlePosition.P5])  # 0
        candidate_indices.add(HANDLE_POSITION_INDEX[HandlePosition.N])   # 5
        candidate_indices.add(HANDLE_POSITION_INDEX[HandlePosition.B8])  # 13

        for offset in range(-3, 4):
            idx = current_idx + offset
            if 0 <= idx <= 13:
                candidate_indices.add(idx)

        candidates = [
            INDEX_TO_HANDLE_POSITION[i]
            for i in sorted(candidate_indices)
            if i in INDEX_TO_HANDLE_POSITION
        ]

        sample_distances = [0.0] + [
            horizon_cm * (i + 1) / sample_count for i in range(sample_count)
        ]

        curve_limits = [
            self.sample_curve_at(curve, dist, "operational_limit")
            for dist in sample_distances
        ]

        best_handle = HandlePosition.N
        best_score = float("inf")

        for handle in candidates:
            score = 0.0
            for j, dist in enumerate(sample_distances):
                predicted_speed = self.simulate_forward(
                    current_speed_kmh, handle, dist
                )

                if predicted_speed > curve_limits[j]:
                    overshoot = predicted_speed - curve_limits[j]
                    score += overshoot * ATO_OVERSHOOT_WEIGHT
                else:
                    gap = curve_limits[j] - predicted_speed
                    score += gap

            if score < best_score:
                best_score = score
                best_handle = handle

        return best_handle

    # ------------------------------------------------------------------
    # Braking-notch helpers
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_braking_notch(current_speed: float) -> HandlePosition:
        """Pick a braking notch to stop from `current_speed`."""
        if current_speed > 30:
            return HandlePosition.B8
        elif current_speed > 25:
            return HandlePosition.B7
        elif current_speed > 20:
            return HandlePosition.B6
        elif current_speed > 14:
            return HandlePosition.B5
        elif current_speed > 10:
            return HandlePosition.B4
        elif current_speed > 5:
            return HandlePosition.B3
        elif current_speed > 2:
            return HandlePosition.B2
        else:
            return HandlePosition.B1

    @staticmethod
    def calculate_braking_notch_for_speed(
        current_speed: float, target_speed: float
    ) -> HandlePosition:
        """Pick a braking notch to reduce from `current_speed` toward `target_speed`."""
        speed_diff = current_speed - target_speed

        if speed_diff > 30:
            return HandlePosition.B8
        elif speed_diff > 25:
            return HandlePosition.B7
        elif speed_diff > 20:
            return HandlePosition.B6
        elif speed_diff > 14:
            return HandlePosition.B5
        elif speed_diff > 10:
            return HandlePosition.B4
        elif speed_diff > 5:
            return HandlePosition.B3
        elif speed_diff > 2:
            return HandlePosition.B2
        else:
            return HandlePosition.B1

    @staticmethod
    def calculate_power_notch(
        current_speed: float, target_speed: float
    ) -> HandlePosition:
        """Pick a power notch to accelerate toward `target_speed`."""
        speed_diff = target_speed - current_speed
        if speed_diff > 5:
            return HandlePosition.P5
        elif speed_diff > 3:
            return HandlePosition.P2
        else:
            return HandlePosition.P1

    # ------------------------------------------------------------------
    # Top-level handle calculator
    # ------------------------------------------------------------------

    def compute_handle(self) -> HandlePosition:
        """Pick the next handle in ATO mode using future-aware curve tracking.

        Mutates two diagnostic fields on the controller (`handle_reason` and
        `_previous_speed`); does not touch any other controller state.
        """
        c = self._c
        train = c._get_train()
        if not train:
            c.handle_reason = "No train"
            return HandlePosition.N

        current_speed = c.current_speed_kmh

        lookahead = c.get_lookahead()
        curves = lookahead.get("lookahead", {}).get("speed_curves", [])

        if not curves:
            c.handle_reason = "No curves"
            c._previous_speed = current_speed
            return HandlePosition.N

        # ---------- Zone 1: stopping (curve at 0 says stop) ---------------
        curve_at_zero = self.sample_curve_at(curves, 0.0, "operational_limit")

        effective_limit_at_zero = min(
            curve_at_zero,
            train.max_speed,
            c.atc_speed_limit,
            c.current_block_speed_limit,
        )

        if effective_limit_at_zero <= STOPPED_SPEED_THRESHOLD_KMH:
            if current_speed > 0:
                result_handle = self.calculate_braking_notch(current_speed)
                reason = (
                    f"Stopping: curve@0={curve_at_zero:.1f}, "
                    f"braking at {current_speed:.1f}"
                )
            else:
                result_handle = HandlePosition.N
                reason = "Stopped"
            c._previous_speed = current_speed
            c.handle_reason = reason
            return result_handle

        if current_speed > effective_limit_at_zero + ATO_SPEED_TOLERANCE:
            result_handle = self.calculate_braking_notch_for_speed(
                current_speed, effective_limit_at_zero
            )
            result_handle = rate_limit_handle(
                current=c.handle_position,
                target=result_handle,
                allow_fast=True,
            )
            c._previous_speed = current_speed
            c.handle_reason = (
                f"Curve overspeed: speed={current_speed:.1f} "
                f"> curve@0={effective_limit_at_zero:.1f}"
            )
            return result_handle

        # ---------- Future-aware control: evaluate handles vs curve --------
        km_h_to_cm_s = get_speed_conversion_factor()
        speed_cm_s = current_speed * km_h_to_cm_s

        profile = c._get_speed_profile()
        operational_decel = max(
            0.1, profile.deceleration_rate * profile.operational_decel_ratio
        )
        restriction_window_cm = min(
            ATO_RESTRICTION_LOOKAHEAD_CM,
            curves[-1].get("distance_cm", ATO_RESTRICTION_LOOKAHEAD_CM),
        )
        lower_curve_limits = [
            point.get("operational_limit", effective_limit_at_zero)
            for point in curves
            if 0.0 < point.get("distance_cm", 0.0) <= restriction_window_cm
            and (
                point.get("operational_limit", effective_limit_at_zero)
                > STOPPED_SPEED_THRESHOLD_KMH
                or point.get("distance_cm", 0.0) <= NEAR_STOP_DETECTION_CM
            )
            and point.get("operational_limit", effective_limit_at_zero)
            < current_speed - ATO_SPEED_TOLERANCE
        ]
        braking_horizon_cm = 0.0
        if lower_curve_limits:
            future_target_speed = min(lower_curve_limits)
            decel_cm_s2 = operational_decel * km_h_to_cm_s
            current_cm_s = current_speed * km_h_to_cm_s
            target_cm_s = future_target_speed * km_h_to_cm_s
            braking_horizon_cm = max(
                0.0,
                (current_cm_s * current_cm_s - target_cm_s * target_cm_s)
                / (2 * decel_cm_s2),
            )

        horizon_cm = max(5.0, min(restriction_window_cm, speed_cm_s * 3.0))
        if braking_horizon_cm > 0.0:
            horizon_cm = max(horizon_cm, min(restriction_window_cm, braking_horizon_cm))

        result_handle = self.evaluate_handles_against_curve(
            current_speed_kmh=current_speed,
            curve=curves,
            horizon_cm=horizon_cm,
        )

        # Safety cap: if result would exceed ATC limits, override
        predicted = self.simulate_forward(current_speed, result_handle, 5.0)
        if predicted > c.atc_speed_limit + ATO_SPEED_TOLERANCE:
            result_handle = self.calculate_braking_notch(current_speed)
            reason = (
                f"ATC cap: predicted {predicted:.1f} > limit {c.atc_speed_limit:.1f}"
            )
        else:
            curve_target = self.sample_curve_at(
                curves, horizon_cm / 2, "operational_limit"
            )
            reason = (
                f"FutureAware: handle={result_handle.value} "
                f"speed={current_speed:.1f} curve@{horizon_cm / 2:.0f}cm={curve_target:.1f} "
                f"horizon={horizon_cm:.0f}cm"
            )

        is_near_stop = False
        for point in curves:
            if (
                point.get("operational_limit", 999) <= STOPPED_SPEED_THRESHOLD_KMH
                and point.get("distance_cm", 999) < NEAR_STOP_DETECTION_CM
            ):
                is_near_stop = True
                break

        result_handle = rate_limit_handle(
            current=c.handle_position,
            target=result_handle,
            allow_fast=is_near_stop,
        )

        c._previous_speed = current_speed
        c.handle_reason = reason
        return result_handle
