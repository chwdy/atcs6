"""Speed-curve math pulled out of `TrainController`.

This module owns the two pure braking-curve kinematics helpers
(`calculate_max_safe_speed`, `calculate_speed_for_target`) and the
`SpeedCurveCalculator`, which builds the ATC/ATO speed-limit envelope that
`TrainController` used to inline as `calculate_speed_curves`.

`SpeedCurveCalculator` holds a back-reference to `TrainController` (the same
"helper class holding a controller backref" shape as `LookaheadBuilder`)
because the curve build reads controller-side state (the train, its speed
profile, and the ATC sub-module's effective safety margin). It is constructed
once per controller and lazy-initialised on first use; the build is read-only
with respect to controller state.

`ATO_SPEED_MARGIN_KMH` and `CURVE_SAMPLE_RESOLUTION_CM` keep their canonical
home in `train_controller/__init__.py`: the former is a live runtime tunable
that `runtime_config` rewrites by name on the `train_controller` module, and
both are imported there by other readers. The build reads them through a
function-local `import train_controller` at call time (never at module load,
which would be a circular import) so a runtime change to the margin still
flows into the curve, exactly as it did before this code moved.
"""

import math
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from constants import CAUTION_SPEED_LIMIT_KMH, TRACK_END_SAFETY_MARGIN_CM
from enums import AtcLimitReasonCode, BlockState
from model_scale import get_speed_conversion_factor

if TYPE_CHECKING:
    from train_controller import TrainController


def calculate_max_safe_speed(
    clearance_cm: float,
    deceleration_rate: float,
    safety_margin_cm: float = TRACK_END_SAFETY_MARGIN_CM,
) -> float:
    """
    Calculate maximum speed that allows stopping within clearance.

    Uses physics formula: v^2 = 2 * a * d
    Where:
        v = velocity (converted between km/h and cm/s)
        a = deceleration (km/h/s -> cm/s^2)
        d = distance (cm)

    Args:
        clearance_cm: Available distance before obstruction
        deceleration_rate: Braking rate in km/h per second
        safety_margin_cm: Buffer distance (default 25cm)

    Returns:
        Maximum safe speed in km/h
    """
    effective_distance = max(0, clearance_cm - safety_margin_cm)

    if effective_distance <= 0:
        return 0.0

    if deceleration_rate <= 0:
        return 0.0

    km_h_to_cm_s = get_speed_conversion_factor()
    decel_cm_s2 = deceleration_rate * km_h_to_cm_s

    # v^2 = 2 * a * d
    v_cm_s_squared = 2 * decel_cm_s2 * effective_distance
    v_cm_s = math.sqrt(v_cm_s_squared)

    # Convert back to km/h
    v_kmh = v_cm_s / km_h_to_cm_s

    return v_kmh


def calculate_braking_distance(
    speed_kmh: float,
    deceleration_rate: float,
    safety_margin_cm: float = TRACK_END_SAFETY_MARGIN_CM,
) -> float:
    """Forward distance needed to brake from ``speed_kmh`` to a stand.

    The algebraic inverse of :func:`calculate_max_safe_speed`: it returns the
    clearance a train needs to stop at ``deceleration_rate`` from ``speed_kmh``
    with ``safety_margin_cm`` still intact — the stopping distance plus the
    margin. Used by the braking-distance authority lock (design_new.md
    "Braking-distance lock") to bound how far a powered run locks track.

    Uses physics formula: d = v^2 / (2 * a), with v in cm/s and a in cm/s^2.

    Args:
        speed_kmh: Current speed in km/h.
        deceleration_rate: Service braking rate in km/h per second.
        safety_margin_cm: Buffer distance kept clear beyond the stop (default 25cm).

    Returns:
        Forward distance in cm — the stopping distance plus the safety margin.
        At a stand (``speed_kmh <= 0``) the stopping distance is ``0`` and this
        is just ``safety_margin_cm``, so a stopped train's floor still clears the
        margin ahead (design_new.md "The floor keeps a stopped train able to
        start").
    """
    if deceleration_rate <= 0:
        # A train that cannot brake needs unbounded room; the caller floors it.
        return float("inf")
    if speed_kmh <= 0:
        return safety_margin_cm

    km_h_to_cm_s = get_speed_conversion_factor()
    v_cm_s = speed_kmh * km_h_to_cm_s
    decel_cm_s2 = deceleration_rate * km_h_to_cm_s

    stopping_distance_cm = (v_cm_s * v_cm_s) / (2 * decel_cm_s2)
    return stopping_distance_cm + safety_margin_cm


def calculate_speed_for_target(
    distance_cm: float,
    target_speed_kmh: float,
    deceleration_rate: float,
) -> float:
    """
    Calculate maximum speed that allows decelerating to target speed within distance.

    Uses physics formula: v_final^2 = v_initial^2 - 2 * a * d
    Rearranged: v_initial = sqrt(v_final^2 + 2 * a * d)

    Args:
        distance_cm: Available distance before reaching target
        target_speed_kmh: Required speed at end of distance
        deceleration_rate: Braking rate in km/h per second

    Returns:
        Maximum initial speed in km/h that allows reaching target_speed_kmh
    """
    if distance_cm <= 0:
        return target_speed_kmh

    if deceleration_rate <= 0:
        return target_speed_kmh

    km_h_to_cm_s = get_speed_conversion_factor()
    decel_cm_s2 = deceleration_rate * km_h_to_cm_s
    target_cm_s = target_speed_kmh * km_h_to_cm_s

    # v_initial^2 = v_final^2 + 2 * a * d
    v_initial_squared = target_cm_s * target_cm_s + 2 * decel_cm_s2 * distance_cm
    v_initial_cm_s = math.sqrt(v_initial_squared)

    # Convert back to km/h
    v_kmh = v_initial_cm_s / km_h_to_cm_s

    return v_kmh


class SpeedCurveCalculator:
    """Builds the ATC/ATO speed-limit envelope for a `TrainController`.

    Constructed once per controller and lazy-initialised on first use. The
    build is read-only with respect to controller state.
    """

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller

    def calculate_speed_curves(
        self,
        max_distance_cm: float = 500.0,
        lookahead_blocks: Optional[List[Dict]] = None,
        lookahead_signals: Optional[List[Dict]] = None,
        lookahead_stations: Optional[List[Dict]] = None,
        target_stop_cm: Optional[float] = None,
        override_reason_code: Optional[AtcLimitReasonCode] = None,
        override_reason_detail: Optional[str] = None,
        path_end_reason_code: Optional[AtcLimitReasonCode] = None,
    ) -> List[Dict]:
        """
        Calculate ATC and ATO speed limit curves at 3cm resolution.

        For each sample point at distance d from train head, calculate:
        - Braking curve to path end (stop at max_distance_cm)
        - Braking curves to each speed restriction ahead
        - Take the minimum of all curves

        This creates the characteristic "speed envelope" where the curve
        shows what speed you can be at each point while still being able
        to stop or slow down before any restriction ahead.

        ATC Curve: Uses 100% of service braking (B8), NOT emergency brake.
                   This ensures EB can always stop the train if ATC is exceeded.

        ATO Curve: Uses 70% braking for comfort + stays 5 km/h below ATC.

        Args:
            max_distance_cm: Total clearance/path end distance (cm)
            lookahead_blocks: Block info with speed limits
            lookahead_signals: Signal info
            lookahead_stations: Station info (for tighter safety margin)
            target_stop_cm: Target stopping distance (clearance - safety_margin) for explicit stop
                point

        Returns:
            List of dicts with distance_cm, atc_limit, operational_limit
        """
        # `CURVE_SAMPLE_RESOLUTION_CM` and `ATO_SPEED_MARGIN_KMH` keep their
        # canonical home (and runtime-tunable binding site) on the
        # `train_controller` module. Read them through a call-time import so a
        # runtime change to `ATO_SPEED_MARGIN_KMH` still takes effect, and so
        # this module never imports `train_controller` at load time.
        import train_controller as _tc

        CURVE_SAMPLE_RESOLUTION_CM = _tc.CURVE_SAMPLE_RESOLUTION_CM
        ATO_SPEED_MARGIN_KMH = _tc.ATO_SPEED_MARGIN_KMH

        train = self._c._get_train()
        if not train:
            return []

        if override_reason_code is not None:
            # No cab / EB / wrong direction / no authority: ATC limit is zero
            # regardless of geometry. Build a flat zero curve with the reason
            # metadata stamped on every sample.
            override_curves: List[Dict[str, Any]] = []
            step = CURVE_SAMPLE_RESOLUTION_CM
            sample_distance = 0.0
            limit = max(0.0, max_distance_cm)

            def _override_point(distance: float) -> Dict[str, Any]:
                point: Dict[str, Any] = {
                    "distance_cm": distance,
                    "atc_limit": 0.0,
                    "operational_limit": 0.0,
                    "atc_limit_reason_code": override_reason_code.value,
                }
                if override_reason_detail:
                    point["atc_limit_reason_detail"] = override_reason_detail
                return point

            while sample_distance <= limit:
                override_curves.append(_override_point(sample_distance))
                sample_distance += step
            if not override_curves or override_curves[-1]["distance_cm"] < limit:
                override_curves.append(_override_point(limit))
            return override_curves

        profile = self._c._get_speed_profile()
        max_speed = train.max_speed
        decel_rate = profile.deceleration_rate
        operational_decel = decel_rate * profile.operational_decel_ratio

        # Build list of speed restrictions ahead. Each restriction tracks
        # distance, speed limit, safety margin, and a canonical reason code +
        # optional plain-text detail so the dominant constraint at each
        # sample stamps reason metadata into the canonical lookahead payload.
        restrictions: List[
            Tuple[float, float, float, AtcLimitReasonCode, Optional[str]]
        ] = []

        # Add path end as a stop (speed 0)
        # Use effective safety margin based on destination type:
        # - 5cm if ato_destination_id is a station
        # - 25cm otherwise
        # This ensures the curve matches what _get_effective_safety_margin() returns
        path_end_safety_margin = self._c.atc.effective_safety_margin()
        # The path end stop is the safety-margin-adjusted authority boundary.
        # `path_end_reason_code` lets callers override the reason — the
        # fallback (no active path) builder uses `NO_AUTHORITY` here, while
        # the path-based builder uses `AUTHORITY_STOP`.
        path_end_code = path_end_reason_code or AtcLimitReasonCode.AUTHORITY_STOP
        restrictions.append(
            (max_distance_cm, 0.0, path_end_safety_margin, path_end_code, None)
        )

        # Add block speed limits as restrictions (for braking curve calculation)
        # This creates braking curves to slow down BEFORE entering speed-limited blocks.
        # A CAUTION block always contributes the controller-owned global cap
        # CAUTION_SPEED_LIMIT_KMH (design_new.md:1849), taking the lower of that
        # cap and any nominal block limit; this applies even when the block has
        # no nominal speed_limit_kmh of its own.
        if lookahead_blocks:
            for block in lookahead_blocks:
                block_start = block.get("start_cm", 0)
                block_speed = block.get("speed_limit_kmh")  # None if no limit
                block_state = block.get("state")
                is_caution = block_state == BlockState.CAUTION.value
                if is_caution:
                    block_code = AtcLimitReasonCode.CAUTION
                    restriction_speed = (
                        CAUTION_SPEED_LIMIT_KMH
                        if block_speed is None
                        else min(block_speed, CAUTION_SPEED_LIMIT_KMH)
                    )
                else:
                    # A posted block speed limit is a permitted-speed
                    # restriction, not an in-path conflict (design_new.md
                    # reason-code enumeration): use SPEED_LIMIT, not OBSTRUCTION.
                    block_code = AtcLimitReasonCode.SPEED_LIMIT
                    restriction_speed = block_speed
                if restriction_speed is not None and restriction_speed < max_speed:
                    block_id = block.get("id")
                    detail = (
                        f"Block speed limit at {block_id}" if block_id else None
                    )
                    restrictions.append(
                        (block_start, restriction_speed, 0.0, block_code, detail)
                    )

        # Signal restrictions are intentionally NOT used for ATC/ATO speed calculation.
        # This is a CBTC block-based system where:
        # - Speed control comes from BLOCKS (clearance distance, speed_limit_kmh)
        # - Signals are visual indicators of block state only (for display in UI)
        # - Train stopping is enforced by block reservation, not signal aspects
        # See docs/ARCHITECTURE.md "CBTC Block-Based Signaling System" for details.
        #
        # if lookahead_signals:
        #     for signal in lookahead_signals:
        #         signal_dist = signal.get("distance_cm", float("inf"))
        #         signal_state = signal.get("state", "green")
        #         if signal_state == "red":
        #             restrictions.append((signal_dist, 0.0, path_end_safety_margin))
        #         elif signal_state in ("yellow", "double_yellow"):
        #             restrictions.append((signal_dist, YELLOW_SIGNAL_SPEED_KMH, 0.0))

        # NOTE: Station speed limits are NOT added as restrictions here.
        # ATC/ATO only cares about BLOCKS, not platforms. Station speed limits
        # are applied through the block's speed_limit_kmh property, not the station's
        # top_speed_kmh which is for visualization/informational purposes only.

        curves = []

        # Use 5cm resolution for smooth curves
        step = CURVE_SAMPLE_RESOLUTION_CM
        sample_distance = 0.0
        while sample_distance <= max_distance_cm:
            # For this sample point, find the minimum speed limit considering
            # all restrictions ahead, and remember which restriction's reason
            # code dominates so the curve point can carry it forward.

            # Each entry: (limit_kmh, reason_code, reason_detail)
            atc_candidates: List[Tuple[float, Optional[AtcLimitReasonCode], Optional[str]]] = [
                (max_speed, None, None)
            ]
            operational_limits: List[float] = [max_speed]

            for (
                restriction_dist,
                restriction_speed,
                restriction_safety,
                restriction_code,
                restriction_detail,
            ) in restrictions:
                # Distance from sample point to this restriction
                dist_to_restriction = restriction_dist - sample_distance

                # Handle being AT or PAST a stop restriction (speed=0)
                # At or past the path end, speed must be 0
                if dist_to_restriction <= 0 and restriction_speed <= 0:
                    atc_candidates.append((0.0, restriction_code, restriction_detail))
                    operational_limits.append(0.0)
                    continue

                # Apply braking curve when BEFORE the restriction point
                # Once past non-stop restrictions, the speed limit comes from block/station
                # position checks below
                if dist_to_restriction > 0:
                    # Sample point is before restriction
                    # Calculate braking curve back from restriction

                    if restriction_speed <= 0:
                        # Must stop: use stopping distance formula
                        effective_dist = dist_to_restriction - restriction_safety
                        if effective_dist <= 0:
                            atc_candidates.append(
                                (0.0, restriction_code, restriction_detail)
                            )
                            operational_limits.append(0.0)
                        else:
                            atc_limit = calculate_max_safe_speed(
                                effective_dist + restriction_safety,
                                decel_rate,
                                restriction_safety,
                            )
                            operational_limit = calculate_max_safe_speed(
                                effective_dist + restriction_safety,
                                operational_decel,
                                restriction_safety,
                            )
                            atc_candidates.append(
                                (atc_limit, restriction_code, restriction_detail)
                            )
                            operational_limits.append(operational_limit)
                    else:
                        # Must slow to target speed: use decel to target formula
                        atc_limit = calculate_speed_for_target(
                            dist_to_restriction, restriction_speed, decel_rate
                        )
                        operational_limit = calculate_speed_for_target(
                            dist_to_restriction, restriction_speed, operational_decel
                        )
                        atc_candidates.append(
                            (atc_limit, restriction_code, restriction_detail)
                        )
                        operational_limits.append(operational_limit)

            # Also apply block speed limit at current position
            if lookahead_blocks:
                for block in lookahead_blocks:
                    block_start = block.get("start_cm", 0)
                    block_end = block.get("end_cm", 0)
                    if block_start <= sample_distance < block_end:
                        block_speed = block.get("speed_limit_kmh")  # None if no limit
                        block_state = block.get("state")
                        is_caution = block_state == BlockState.CAUTION.value
                        if is_caution:
                            inside_code = AtcLimitReasonCode.CAUTION
                            # CAUTION always caps at the global limit, lower of
                            # it and any nominal block limit (design_new.md:1849).
                            inside_speed = (
                                CAUTION_SPEED_LIMIT_KMH
                                if block_speed is None
                                else min(block_speed, CAUTION_SPEED_LIMIT_KMH)
                            )
                        else:
                            # Posted block speed limit, not an in-path conflict:
                            # SPEED_LIMIT, not OBSTRUCTION (see above).
                            inside_code = AtcLimitReasonCode.SPEED_LIMIT
                            inside_speed = block_speed
                        if inside_speed is not None:
                            block_id = block.get("id")
                            inside_detail = (
                                f"Block speed limit at {block_id}"
                                if block_id
                                else None
                            )
                            atc_candidates.append(
                                (inside_speed, inside_code, inside_detail)
                            )
                            operational_limits.append(inside_speed)
                        break

            # NOTE: Station speed limits are NOT applied here.
            # ATC/ATO only cares about BLOCKS. Station's top_speed_kmh is for
            # visualization only. Block speed limits (if any) are enforced above.

            # Take minimum of all ATC candidates and remember its reason
            atc_limit, dominant_code, dominant_detail = min(
                atc_candidates, key=lambda c: c[0]
            )
            operational_limit = min(operational_limits)

            # Cap at train max speed
            atc_limit = min(atc_limit, max_speed)
            operational_limit = min(operational_limit, max_speed)

            # Ensure ATO limit stays ATO_SPEED_MARGIN_KMH below ATC
            operational_limit = min(
                operational_limit, max(0.0, atc_limit - ATO_SPEED_MARGIN_KMH)
            )

            point: Dict[str, Any] = {
                "distance_cm": sample_distance,
                "atc_limit": atc_limit,
                "operational_limit": operational_limit,
            }
            if dominant_code is not None:
                point["atc_limit_reason_code"] = dominant_code.value
                if dominant_detail:
                    point["atc_limit_reason_detail"] = dominant_detail
            curves.append(point)

            sample_distance += step

        # Add explicit curve points for cleaner visualization. Both anchor
        # points carry the path-end reason code so consumers can key on it
        # (`AUTHORITY_STOP` with an active path, `NO_AUTHORITY` without).
        anchor_code = path_end_reason_code or AtcLimitReasonCode.AUTHORITY_STOP

        # 1. Target stop point (where train actually stops)
        if target_stop_cm is not None and target_stop_cm > 0:
            has_target_point = any(
                abs(point["distance_cm"] - target_stop_cm) < step / 2
                for point in curves
            )
            if not has_target_point:
                curves.append(
                    {
                        "distance_cm": target_stop_cm,
                        "atc_limit": 0.0,
                        "operational_limit": 0.0,
                        "atc_limit_reason_code": anchor_code.value,
                    }
                )

        # 2. Clearance end point (end of available distance) — always emit
        # this to show the clearance boundary.
        has_clearance_point = any(
            abs(point["distance_cm"] - max_distance_cm) < step / 2 for point in curves
        )
        if not has_clearance_point:
            curves.append(
                {
                    "distance_cm": max_distance_cm,
                    "atc_limit": 0.0,
                    "operational_limit": 0.0,
                    "atc_limit_reason_code": anchor_code.value,
                }
            )

        # Sort by distance to ensure proper curve drawing
        curves.sort(key=lambda p: p["distance_cm"])

        return curves
