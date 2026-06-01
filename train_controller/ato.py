"""
ATO Computer sub-module of ``TrainController``.

Per design_new.md "ATO Computer": ATO produces the next handle command
in ATO mode by tracking the operational speed curve published in
``controller.get_lookahead()``. The computation is mostly stateless
once the curve and current speed are known; the few pieces of state
the sub-module retains (`running`, `info`, `_previous_speed`, `debug`)
live on this class rather than on the façade.

This module absorbs the previous standalone ``ato_navigator.py``
``ATONavigator`` helper. The compat shim
``train_controller._rate_limit_handle`` has been retired; callers
import ``rate_limit_handle`` from this module directly.

Algorithm summary (kinematic, single pass):

1. Determine the *ceiling at distance 0* — the most binding of the curve
   sample at 0, train max speed, ATC limit, and current block limit. The
   train must not exceed this.
2. If the ceiling at 0 is below the stopped-threshold, brake (B8) until
   stopped.
3. Otherwise, sample the operational curve at evenly-spaced points within a
   *planning horizon* (proportional to current speed × tick dt, capped at
   ``ATO_RESTRICTION_LOOKAHEAD_CM``). For each sample, compute the
   deceleration we would need to be at-or-below the curve when we reach
   that point. Take the maximum required.
4. Map ``required_decel / op_decel`` to a brake notch via
   ``BRAKE_MULTIPLIERS`` (the table is now defined as multipliers of
   op_decel — B6 = 1.00×).
5. If no brake is needed, power up while below the ceiling, else coast.

External callers access ATO state and behavior as:

  - ``controller.ato.running`` (bool — is an ATO run in progress?)
  - ``controller.ato.info`` (Optional[ATOInfo] — destination snapshot)
  - ``controller.ato.debug`` (Optional[dict] — per-tick debug snapshot
    consumed by the debug inspector panel)
  - ``controller.ato.go()`` (start a run)
  - ``controller.ato.stop()`` (end a run; operator/admin)
  - ``controller.ato.compute_handle(delta_seconds)`` (per-tick handle
    selection)
  - ``controller.ato.compute_info()`` (rebuild destination snapshot)
  - ``controller.ato.sample_curve_at(curve, distance_cm, key)``
  - ``controller.ato.simulate_forward(speed, handle, distance_cm, dt)``
  - ``controller.ato.calculate_power_notch(...)`` /
    ``controller.ato.calculate_braking_notch_for_speed(...)``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from constants import STOPPED_SPEED_THRESHOLD_KMH
from enums import (
    BRAKE_MULTIPLIERS,
    HANDLE_POSITION_INDEX,
    INDEX_TO_HANDLE_POSITION,
    POWER_MULTIPLIERS,
    CommandResult,
    HandlePosition,
    TrainState,
)
from model_scale import get_speed_conversion_factor
from train_controller_types import ATOInfo

if TYPE_CHECKING:
    from train_controller import TrainController


# =============================================================================
# Module-level tuning constants
# =============================================================================

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

# Braking lookahead: how far ahead the controller reads the operational_limit
# envelope when choosing a brake notch.
#   look = max(MIN_LOOKAHEAD_CM, travel_this_tick * (1 + LOOKAHEAD_HEADROOM))
# capped at the curve's extent. The travel term guarantees `look >= per-tick
# travel`, so the train never overshoots the point it planned for inside one
# tick (this is what makes the law dt-stable — a bigger dt looks further). The
# headroom adds lead time; the floor keeps a creep approach anticipating the
# next stop. Because operational_limit is the op_decel braking envelope, the
# chord (u²−v²)/(2·look) evaluates to op_decel at any look distance, so a train
# riding the curve commands B6 regardless of dt.
LOOKAHEAD_HEADROOM = 0.5
MIN_LOOKAHEAD_CM = 5.0
LOOKAHEAD_SAMPLE_COUNT = 4

# Braking deadband: ignore a sub-notch speed excess so the controller does not
# dither between a faint brake and coast while holding a flat ceiling. Far
# smaller than the cruise power-up tolerance: on a one-tick horizon the envelope
# only drops ~op_decel*dt (well under 1 km/h) per tick, so a 2 km/h band would
# swallow the entire braking signal.
BRAKE_DEADBAND_KMH = 0.3

# Tick duration the rate-limiter's per-tick step budget is sized against.
# At play-speed multipliers > 1 the simulation packs more sim-time into one
# tick; the rate limiter scales `max_step` by `delta / NOMINAL_TICK_DT` so the
# handle-change rate stays consistent in sim-time rather than per-tick.
NOMINAL_TICK_DT = 0.1

# ATO speed margin used by the cab-resume / depart-acceptance branch on the
# controller façade. The margin lets a previously commanded ATO run resume
# without re-issuing depart when the train is still slightly above stopped.
ATO_SPEED_MARGIN_KMH = 1.0


def ato_speed_tolerance(reference_kmh: float) -> float:
    """Speed-tracking tolerance for ATO at the given reference speed.

    ``reference_kmh`` is typically ``max(current_speed_kmh, ceiling_at_zero)``
    — the value the band is sized against. The result is clamped to
    ``[ATO_SPEED_TOLERANCE_MIN, ATO_SPEED_TOLERANCE_MAX]``.
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
    delta_seconds: float = NOMINAL_TICK_DT,
) -> HandlePosition:
    """Rate-limit handle changes to prevent jitter.

    At the nominal tick rate (``delta_seconds == NOMINAL_TICK_DT``),
    limits handle movement to ±1 notch per call (±2 if
    ``allow_fast=True``). The nominal step budget is scaled by
    ``delta_seconds / NOMINAL_TICK_DT`` so the handle-change rate stays
    consistent in sim-time when the play speed packs more sim-time into
    one tick. Emergency brake (``HandlePosition.EB``) always bypasses
    rate limiting.
    """
    if target == HandlePosition.EB:
        return HandlePosition.EB

    current_idx = HANDLE_POSITION_INDEX.get(current, 5)
    target_idx = HANDLE_POSITION_INDEX.get(target, 5)

    nominal_step = 2 if allow_fast else 1
    scale = max(1e-6, delta_seconds) / NOMINAL_TICK_DT
    max_step = max(1, int(round(nominal_step * scale)))

    if target_idx > current_idx:
        new_idx = min(current_idx + max_step, target_idx)
    elif target_idx < current_idx:
        new_idx = max(current_idx - max_step, target_idx)
    else:
        new_idx = current_idx

    return INDEX_TO_HANDLE_POSITION.get(new_idx, HandlePosition.N)


# =============================================================================
# ATOComputer
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


class ATOComputer:
    """Owns the ATO computations and run-lifecycle state for one controller."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller

        # Run lifecycle and per-tick diagnostics.
        self.running: bool = False
        self.info: Optional[ATOInfo] = None
        self.debug: Optional[Dict[str, Any]] = None
        # Diagnostic: previous tick's speed; used to compute trend in compute_handle.
        self._previous_speed: float = 0.0

    # ------------------------------------------------------------------
    # Run lifecycle: go / stop
    # ------------------------------------------------------------------

    def go(self) -> CommandResult:
        """Canonical helper that arms an ATO run by setting ``running = True``.

        Per design_new.md "ato_running meaning and lifecycle", both
        transitions into ``RUNNING_MOVING`` (the depart-acceptance path
        and the reactive speed-based evaluation path) converge on this
        writer so callers do not duplicate the precondition check.

        Accepted entry states:
        - ``READY``: full depart-acceptance path with cab auto-selection
          and state transition to ``RUNNING_MOVING``.
        - ``RUNNING_MOVING``: idempotent success (the reactive
          transition has already moved state forward); skip cab
          auto-selection because the train is already moving with an
          ATC-accepted cab.
        """
        c = self._c
        train = c._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if c.commands.control_mode != "ato":
            return CommandResult(False, "Not in ATO mode")

        if not c.power_on:
            return CommandResult(False, "Power is off")

        if c.ops.state not in (TrainState.READY, TrainState.RUNNING_MOVING):
            return CommandResult(
                False, f"Train not in READY (is {c.ops.state.value})"
            )

        # Reactive-transition entry: train is already moving under live
        # authority with an ATC-accepted cab. Just write `running`;
        # path_backed_run_active is derived.
        if c.ops.state == TrainState.RUNNING_MOVING:
            self.running = True
            return CommandResult(True, "ATO armed (already moving)")

        # READY entry: full depart-acceptance path.
        # Recalculate ATO info fresh to ensure cab direction matches
        # the current path (path may have been assigned since last tick).
        self.info = self.compute_info()

        # ATO Auto-selection/correction: determine the required cab from path
        # direction and select or switch if needed.
        if self.info and self.info.required_cab:
            required_cab = self.info.required_cab
            if c.commands.cab != required_cab:
                old_cab = c.commands.cab
                # Ensure train is physically stopped before switching cab.
                if c.physics_sync.current_speed_kmh > 0:
                    c.physics_sync.current_speed_kmh = 0.0
                result = c.commands.set_cab(required_cab)
                if not result.success:
                    return CommandResult(
                        False,
                        f"ATO cannot start: cab auto-{'selection' if old_cab is None else 'correction'} failed - {result.message}",
                    )
                c.notifications.emit(
                    f"ATO auto-{'selected' if old_cab is None else 'switched'} cab to {required_cab} for path direction"
                )
        elif c.commands.cab is None:
            return CommandResult(
                False,
                "No cab selected and no path available to auto-select direction",
            )

        # After auto-selection, verify we're still in READY (set_cab keeps
        # state == READY for non-dwelling controllers).
        if c.ops.state != TrainState.READY:
            return CommandResult(False, f"Train not in READY (is {c.ops.state.value})")
        if c.commands.cab not in ("A", "B"):
            return CommandResult(False, "ATO requires a cab to be selected")

        c.ops.state = TrainState.RUNNING_MOVING
        self.running = True
        return CommandResult(True, "ATO started")

    def stop(self) -> CommandResult:
        """Stop ATO movement (service stop, not emergency).

        Sets ``running = False``. The tick() will handle braking and
        transitioning to ``READY`` when stopped.
        """
        c = self._c
        train = c._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not self.running:
            return CommandResult(False, "ATO not running")

        self.running = False
        return CommandResult(True, "ATO stopping")

    # ------------------------------------------------------------------
    # Destination info (used by tick() for display)
    # ------------------------------------------------------------------

    def compute_info(self) -> Optional[ATOInfo]:
        """Build the ``ATOInfo`` payload for the controller's current
        path + destination, or return None when neither is known."""
        c = self._c
        train = c._get_train()
        if not train or not c.network:
            return None

        path_id = c.path_projection.assigned_path_id or c.path_projection.ato_path_id
        destination_block_id = c.path_projection.ato_destination_block_id

        if not path_id and not destination_block_id:
            return None

        path = None
        if hasattr(c.network, "paths"):
            path = c.network.paths.get(path_id)

        if path_id and not path and not destination_block_id:
            return ATOInfo(path_id=path_id)

        path_heading = "a"
        required_cab = c.commands.cab or "A"

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
        """Linearly interpolate a speed-curve sample at ``distance_cm``.

        Pure: depends only on ``curve`` (a list of ``{distance_cm,
        atc_limit, operational_limit, ...}`` entries) and the requested
        key.
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
        """Predict the train's speed at ``distance_cm`` ahead under
        ``handle``.

        Brake notches apply ``op_decel × BRAKE_MULTIPLIERS[handle]``,
        matching ``train_physics``. Air resistance is intentionally
        omitted because callers compare handles relative to each other.
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
        """Smallest brake notch whose ``BRAKE_MULTIPLIERS`` value ≥
        ``multiplier``.

        Multipliers are relative to op_decel. Caps at B8 — if more
        decel is required than B8 provides, ATC EB will catch the
        resulting overspeed.
        """
        if multiplier <= 0:
            return HandlePosition.N
        for handle in _BRAKE_HANDLES_ASCENDING:
            if BRAKE_MULTIPLIERS[handle] >= multiplier:
                return handle
        return HandlePosition.B8

    @staticmethod
    def calculate_power_notch(
        current_speed: float,
        target_speed: float,
        speed_trend_kmh_s: float = 0.0,
    ) -> HandlePosition:
        """Pick a power notch to accelerate toward ``target_speed``.

        ``speed_trend_kmh_s`` is the measured rate of speed change this
        tick (positive = accelerating). When the trend is a small
        negative value and the deficit is modest, the speed is declining
        due to friction only — P1 is enough to counteract it. A steeper
        decline (grade or active decel) passes through to the normal
        gap table so the controller escalates appropriately.
        """
        diff = target_speed - current_speed
        if speed_trend_kmh_s < 0.0 and abs(speed_trend_kmh_s) < 1.0 and diff < 3.0:
            return HandlePosition.P1
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

        Used by callers that need a notch independent of distance — e.g.
        the ATO-not-running "brake to stop" branch in
        ``TrainController.tick()``. Picks more aggressively as the gap
        grows. Each notch's multiplier is interpreted against op_decel
        (see ``BRAKE_MULTIPLIERS``).
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

        Mutates three diagnostic fields: ``self._previous_speed``,
        ``self.debug`` (consumed by ``routes/inspector.py`` "ATO Debug"
        group), and ``self._c.handle_reason`` (still on the façade until
        the ops sub-module lands).

        Args:
            delta_seconds: tick duration in seconds. Used to scale the
                planning horizon with the current motion rate.
        """
        c = self._c
        debug: Dict[str, Any] = {
            "delta_seconds": float(delta_seconds),
            "current_speed_kmh": float(c.physics_sync.current_speed_kmh),
        }

        train = c._get_train()
        if not train:
            c.ops.handle_reason = "No train"
            debug["mode"] = "no_train"
            debug["final_handle"] = HandlePosition.N.value
            self.debug = debug
            return HandlePosition.N

        speed_kmh = c.physics_sync.current_speed_kmh
        speed_trend_kmh_s = (
            (speed_kmh - self._previous_speed) / delta_seconds
            if delta_seconds > 0
            else 0.0
        )
        self._previous_speed = speed_kmh

        lookahead = c.get_lookahead()
        curves = lookahead.get("lookahead", {}).get("speed_curves", [])

        if not curves:
            c.ops.handle_reason = "No curves"
            debug["mode"] = "no_curves"
            debug["final_handle"] = HandlePosition.N.value
            self.debug = debug
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
            c.atc.speed_limit,
            c.atc.current_block_speed_limit,
        )

        debug.update(
            {
                "op_decel_kmh_s": float(op_decel),
                "decel_rate_kmh_s": float(profile.deceleration_rate),
                "operational_decel_ratio": float(profile.operational_decel_ratio),
                "curve_at_zero_kmh": float(curve_at_zero),
                "max_speed_kmh": float(train.max_speed),
                "atc_speed_limit_kmh": float(c.atc.speed_limit),
                "block_speed_limit_kmh": (
                    float(c.atc.current_block_speed_limit)
                    if c.atc.current_block_speed_limit != float("inf")
                    else None
                ),
                "ceiling_at_zero_kmh": float(ceiling_at_zero),
            }
        )

        # Stopping zone: curve says stop right here.
        if ceiling_at_zero <= STOPPED_SPEED_THRESHOLD_KMH:
            if speed_kmh > STOPPED_SPEED_THRESHOLD_KMH:
                c.ops.handle_reason = (
                    f"Stopping: ceiling@0={ceiling_at_zero:.2f} "
                    f"speed={speed_kmh:.1f}"
                )
                debug["mode"] = "stopping"
                debug["final_handle"] = HandlePosition.B8.value
                self.debug = debug
                return HandlePosition.B8
            c.ops.handle_reason = "Stopped"
            debug["mode"] = "stopped"
            debug["final_handle"] = HandlePosition.N.value
            self.debug = debug
            return HandlePosition.N

        # One-tick-ahead lookahead distance: at least the distance the train
        # travels this tick (so it never overshoots the planned point within
        # one tick), plus a dt-scaled headroom for lead time, floored for
        # creep approaches, and capped at how far the curve extends.
        travel_cm = speed_cm_s * delta_seconds
        max_curve_dist = curves[-1].get("distance_cm", MIN_LOOKAHEAD_CM)
        look_cm = max(MIN_LOOKAHEAD_CM, travel_cm * (1.0 + LOOKAHEAD_HEADROOM))
        look_cm = min(look_cm, max_curve_dist)

        debug.update(
            {
                "lookahead_travel_cm": float(travel_cm),
                "lookahead_distance_cm": float(look_cm),
                "max_curve_distance_cm": float(max_curve_dist),
            }
        )

        required_decel_kmh_s = 0.0
        binding_dist_cm = 0.0
        binding_target_kmh = ceiling_at_zero
        samples: List[Dict[str, Any]] = []
        deadband_approached = False
        for i in range(LOOKAHEAD_SAMPLE_COUNT + 1):
            d_cm = look_cm * i / LOOKAHEAD_SAMPLE_COUNT
            target_at_d = self.sample_curve_at(curves, d_cm, "operational_limit")
            target_at_d = min(target_at_d, train.max_speed, c.atc.speed_limit)
            samples.append(
                {"distance_cm": round(d_cm, 2), "target_kmh": round(target_at_d, 2)}
            )

            gap_kmh = speed_kmh - target_at_d
            if gap_kmh <= BRAKE_DEADBAND_KMH:
                if gap_kmh > 0:
                    deadband_approached = True
                continue

            if d_cm <= 0:
                req = op_decel + gap_kmh
            else:
                u_cm_s = speed_cm_s
                v_cm_s = target_at_d * km_h_to_cm_s
                a_cm_s2 = (u_cm_s * u_cm_s - v_cm_s * v_cm_s) / (2.0 * d_cm)
                req = a_cm_s2 / km_h_to_cm_s

            if req > required_decel_kmh_s:
                required_decel_kmh_s = req
                binding_dist_cm = d_cm
                binding_target_kmh = target_at_d

        debug["samples"] = samples
        debug["binding_distance_cm"] = round(binding_dist_cm, 2)
        debug["binding_target_kmh"] = round(binding_target_kmh, 2)
        debug["required_decel_kmh_s"] = round(required_decel_kmh_s, 3)

        if required_decel_kmh_s <= 0.0:
            debug["multiplier_needed"] = 0.0
            debug["deadband_approached"] = deadband_approached
            power_up_tolerance = ato_speed_tolerance(
                max(speed_kmh, ceiling_at_zero)
            )
            debug["power_up_tolerance_kmh"] = power_up_tolerance
            debug["speed_trend_kmh_s"] = round(speed_trend_kmh_s, 3)
            if deadband_approached:
                target_handle = HandlePosition.N
                c.ops.handle_reason = (
                    f"Coast (limit near): speed={speed_kmh:.1f} "
                    f"approaching restriction"
                )
                debug["mode"] = "coast_limit_near"
            elif speed_kmh < ceiling_at_zero - power_up_tolerance:
                target_handle = self.calculate_power_notch(
                    speed_kmh, ceiling_at_zero, speed_trend_kmh_s
                )
                c.ops.handle_reason = (
                    f"Power up: speed={speed_kmh:.1f} "
                    f"< ceiling={ceiling_at_zero:.1f}"
                )
                debug["mode"] = "power_up"
            else:
                target_handle = HandlePosition.N
                c.ops.handle_reason = (
                    f"Coast: speed={speed_kmh:.1f} ceiling={ceiling_at_zero:.1f}"
                )
                debug["mode"] = "coast"
            debug["target_handle"] = target_handle.value
            final = rate_limit_handle(
                current=c.ops.handle_position,
                target=target_handle,
                allow_fast=True,
                delta_seconds=delta_seconds,
            )
            debug["rate_limited"] = final != target_handle
            debug["final_handle"] = final.value
            self.debug = debug
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

        c.ops.handle_reason = (
            f"Brake {target_handle.value}: "
            f"need={required_decel_kmh_s:.2f}km/h/s "
            f"({multiplier_needed:.2f}× op_decel) "
            f"target={binding_target_kmh:.1f}@{binding_dist_cm:.0f}cm"
        )

        final = rate_limit_handle(
            current=c.ops.handle_position,
            target=target_handle,
            allow_fast=True,
            delta_seconds=delta_seconds,
        )
        debug["rate_limited"] = final != target_handle
        debug["final_handle"] = final.value
        self.debug = debug
        return final
