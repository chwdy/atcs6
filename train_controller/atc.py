"""
ATC Computer sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: the ATC
Computer owns the live ATC speed-limit / clearance computation plus
the cached fields the rest of the controller and the API surface read.

This module absorbs the previous standalone ``atc_calculator.py``
``ATCCalculator`` helper. Under the v1 architecture, the ATC fields
(``speed_limit``, ``clearance_ahead_cm``, ``current_block_speed_limit``,
``safety_margin_cm``, ``signal_lookahead_blocks``,
``_last_limit_reason_code``) live on this class rather than on the
façade; cross-sub-module reads go through the parent controller
back-reference ``self._c``.

External callers access ATC state and behavior as:

  - ``controller.atc.speed_limit`` (cached cap; refreshed each tick)
  - ``controller.atc.clearance_ahead_cm`` (cached clearance)
  - ``controller.atc.current_block_speed_limit`` (cached block cap)
  - ``controller.atc.safety_margin_cm`` (tunable; default
    ``TRACK_END_SAFETY_MARGIN_CM``)
  - ``controller.atc.signal_lookahead_blocks`` (tunable scan depth)
  - ``controller.atc.clearance_ahead()`` (recompute clearance now)
  - ``controller.atc.speed_limit_now()`` (recompute speed cap now,
    returns ``(speed, reason_string)``)
  - ``controller.atc.speed_limit_with_code()`` (recompute, returns
    ``(speed, reason, reason_code)``)
  - ``controller.atc.is_caution_ahead()`` /
    ``controller.atc.is_approaching_yellow_signal()`` /
    ``controller.atc.is_signal_red_for_block(block_id)`` /
    ``controller.atc.effective_safety_margin()``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Tuple

from constants import (
    CAUTION_SPEED_LIMIT_KMH,
    PLATFORM_SAFETY_MARGIN_CM,
    STOPPED_SPEED_THRESHOLD_KMH,
    TRACK_END_SAFETY_MARGIN_CM,
)
from enums import AtcLimitReasonCode, BlockState, SignalState

if TYPE_CHECKING:
    from train_controller import TrainController


# ATC tuning constants local to this module (not authored on
# `constants.py` because they have no consumer outside ATCComputer).
ATC_NO_BLOCK_FALLBACK_CM = 100000.0
YELLOW_SIGNAL_SPEED_KMH = 40.0
ATC_SIGNAL_LOOKAHEAD_BLOCKS = 3


class ATCComputer:
    """Owns the ATC clearance + speed-limit computation and cached caps."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller

        # Cached caps — refreshed by `produce_physics_command()` (and on
        # demand by the recompute helpers below).
        self.clearance_ahead_cm: float = 0.0
        self.speed_limit: float = 0.0
        self.current_block_speed_limit: float = float("inf")
        self._last_limit_reason_code: Optional[AtcLimitReasonCode] = None

        # Tunables. ``safety_margin_cm`` is the track-end / authority-end
        # buffer; the smaller ``PLATFORM_SAFETY_MARGIN_CM`` is layered in
        # by ``effective_safety_margin()`` when the destination is a
        # station.
        self.safety_margin_cm: float = TRACK_END_SAFETY_MARGIN_CM
        self.signal_lookahead_blocks: int = ATC_SIGNAL_LOOKAHEAD_BLOCKS

    # ------------------------------------------------------------------
    # Clearance ahead
    # ------------------------------------------------------------------

    def clearance_ahead(self) -> float:
        """Available clearance in cm ahead of the train head.

        Walks the train's current path forward, summing block lengths
        until hitting an obstruction (other train, foreign reservation,
        red signal, end of path). When the path has been completed but
        the train is still approaching its destination station, derives
        clearance from ``_calculate_clearance_to_destination`` instead.
        """
        c = self._c
        train = c._get_train()
        if not train or not c.network:
            return ATC_NO_BLOCK_FALLBACK_CM

        clearance = 0.0

        current_block_id = c._get_current_block_id()
        if not current_block_id:
            return ATC_NO_BLOCK_FALLBACK_CM

        clearance += c._get_remaining_distance_in_current_block()

        blocks_ahead = c._get_blocks_ahead()

        for block_id in blocks_ahead:
            block = c.network.blocks.get(block_id)
            if not block:
                break

            if c._is_block_obstructed(block, block_id):
                break

            if self.is_signal_red_for_block(block_id):
                break

            clearance += block.length_cm

        if not blocks_ahead and c.path_projection.ato_destination_block_id:
            clearance = c._calculate_clearance_to_destination(clearance)

        return clearance

    # ------------------------------------------------------------------
    # ATC speed limit
    # ------------------------------------------------------------------

    def speed_limit_now(self) -> Tuple[float, Optional[str]]:
        """Return (speed_limit_kmh, reason_string).

        Uses physics ``v^2 = 2*a*d`` with the effective safety margin to
        derive a safe speed for the current clearance. Layers in CAUTION,
        yellow-signal, and block-speed caps as separate concerns.

        Per design_new.md step ``22a9`` (canonical lookahead), without a
        received path plan the controller publishes no forward authority:
        ATC limit is ``0`` and the reason is ``no_authority``. There is
        no block-bounded fallback — movement authority is binary on
        ``received_path_plan``.
        """
        from train_controller import calculate_max_safe_speed

        c = self._c
        if c.path_projection.received_path_plan is None:
            return 0.0, "no_authority"

        profile = c._get_speed_profile()
        decel_rate = profile.deceleration_rate

        clearance = self.clearance_ahead_cm
        safety_margin = self.effective_safety_margin()

        safe_speed = calculate_max_safe_speed(clearance, decel_rate, safety_margin)

        limiting_speed = safe_speed
        limiting_reason: Optional[str] = None

        if clearance < float("inf"):
            if clearance < 1000:
                limiting_reason = f"Braking: {clearance / 100:.0f}m ahead"
            else:
                limiting_reason = f"Clearance: {clearance / 100:.0f}m"

        if self.is_caution_ahead():
            if CAUTION_SPEED_LIMIT_KMH < limiting_speed:
                limiting_speed = CAUTION_SPEED_LIMIT_KMH
                limiting_reason = "CAUTION block ahead"
            elif limiting_reason:
                limiting_reason = f"{limiting_reason}; CAUTION block"

        if self.is_approaching_yellow_signal():
            if YELLOW_SIGNAL_SPEED_KMH < limiting_speed:
                limiting_speed = YELLOW_SIGNAL_SPEED_KMH
                limiting_reason = "Yellow signal ahead"
            elif limiting_reason:
                limiting_reason = f"{limiting_reason}; Yellow signal"

        block_limit = c._get_current_block_speed_limit()
        if block_limit < float("inf"):
            train = c._get_train()
            if block_limit < limiting_speed:
                limiting_speed = block_limit
                limiting_reason = f"Block limit: {block_limit:.0f} km/h"
            elif limiting_reason and train and block_limit < train.max_speed:
                limiting_reason = f"{limiting_reason}; Block: {block_limit:.0f}"

        return limiting_speed, limiting_reason

    def speed_limit_with_code(
        self,
    ) -> Tuple[float, Optional[str], Optional[AtcLimitReasonCode]]:
        """``speed_limit_now()`` plus the structured ``AtcLimitReasonCode``."""
        c = self._c
        limiting_speed, limiting_reason = self.speed_limit_now()
        clearance = self.clearance_ahead_cm
        limiting_code: Optional[AtcLimitReasonCode] = None

        if clearance < float("inf"):
            if limiting_speed <= STOPPED_SPEED_THRESHOLD_KMH:
                limiting_code = AtcLimitReasonCode.AUTHORITY_STOP
            else:
                limiting_code = AtcLimitReasonCode.OBSTRUCTION

        if self.is_caution_ahead():
            limiting_code = AtcLimitReasonCode.CAUTION

        if c.path_projection.received_path_plan is None and limiting_code in (
            None,
            AtcLimitReasonCode.AUTHORITY_STOP,
            AtcLimitReasonCode.OBSTRUCTION,
        ):
            limiting_code = AtcLimitReasonCode.NO_AUTHORITY

        return limiting_speed, limiting_reason, limiting_code

    # ------------------------------------------------------------------
    # Predicates and helpers
    # ------------------------------------------------------------------

    def is_caution_ahead(self) -> bool:
        """True when the next forward block is in CAUTION state."""
        c = self._c
        if not c.network:
            return False
        for block_id in c._get_blocks_ahead():
            block = c.network.blocks.get(block_id)
            if block is None:
                continue
            if block.state == BlockState.CAUTION:
                return True
            return False
        return False

    def effective_safety_margin(self) -> float:
        """5cm when destination is a station, otherwise the controller's
        configured ``safety_margin_cm`` (typically 25cm)."""
        c = self._c
        train = c._get_train()
        if not train or not c.network:
            return self.safety_margin_cm

        if c.path_projection.ato_destination_block_id:
            station_id = c._get_station_in_block(c.path_projection.ato_destination_block_id)
            if station_id:
                return PLATFORM_SAFETY_MARGIN_CM

        return self.safety_margin_cm

    def is_approaching_yellow_signal(self) -> bool:
        """True when a yellow / double-yellow signal lies within
        ``signal_lookahead_blocks`` blocks ahead in the train's travel
        direction."""
        c = self._c
        if not c.network:
            return False

        train = c._get_train()
        travel_edge = None
        if train and train.facing_direction:
            travel_edge = c._direction_to_edge(train.facing_direction)

        blocks_checked = 0
        blocks_ahead = c._get_blocks_ahead()

        for block_id in blocks_ahead:
            if blocks_checked >= self.signal_lookahead_blocks:
                break

            for _signal_id, signal in c.network.signals.items():
                if (
                    hasattr(signal, "monitored_block_id")
                    and signal.monitored_block_id == block_id
                ):
                    if travel_edge and hasattr(signal, "detection_direction"):
                        if signal.detection_direction != travel_edge:
                            continue
                    if signal.state in (
                        SignalState.YELLOW,
                        SignalState.DOUBLE_YELLOW,
                    ):
                        return True

            blocks_checked += 1

        return False

    def is_signal_red_for_block(self, block_id: str) -> bool:
        """True when a red signal protects ``block_id`` against the
        train's facing direction.

        Only considers signals whose ``detection_direction`` matches the
        train's facing direction. A signal with ``detection_direction =
        "E"`` monitors eastbound traffic and should not block a
        westbound train.
        """
        c = self._c
        if not c.network:
            return False

        train = c._get_train()
        travel_edge = None
        if train and train.facing_direction:
            travel_edge = c._direction_to_edge(train.facing_direction)

        for _signal_id, signal in c.network.signals.items():
            if (
                hasattr(signal, "monitored_block_id")
                and signal.monitored_block_id == block_id
            ):
                if signal.state == SignalState.RED:
                    if travel_edge and hasattr(signal, "detection_direction"):
                        if signal.detection_direction != travel_edge:
                            continue
                    return True

        return False
