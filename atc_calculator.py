"""
ATCCalculator — owns the ATC speed-limit / clearance computation pulled out
of `TrainController`.

Per design_new.md "ATC Computer": ATC computes the maximum safe speed for
the train given clearance ahead, block speed limits, signal state, and
controller mode. The computation is structurally a pure derivation of
controller + network + path state, but in v1 the helpers it relies on
(`_get_current_block_id`, `_is_block_obstructed`, etc.) still live on
`TrainController`. This module relocates the ATC bodies into a single
class while leaving those helpers in place; full pure-function
purification is deferred to a follow-up PR.

`TrainController` keeps the public method names (`_calculate_atc_speed_limit`,
`_calculate_clearance_ahead`, etc.) as thin forwarders that delegate
here, so existing callers and `monkeypatch.setattr(controller, ...)`
test fixtures continue to work without change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Tuple

from constants import STOPPED_SPEED_THRESHOLD_KMH
from enums import AtcLimitReasonCode, BlockState

if TYPE_CHECKING:
    from train_controller import TrainController


# ATC tuning constants — sourced from `train_controller` module-level
# constants. Re-declared here to keep the module self-contained; they
# stay in lockstep because the values are short and stable.
TRACK_END_SAFETY_MARGIN_CM = 25.0
PLATFORM_SAFETY_MARGIN_CM = 5.0
ATC_NO_BLOCK_FALLBACK_CM = 100000.0
YELLOW_SIGNAL_SPEED_KMH = 40.0
CAUTION_SPEED_LIMIT_KMH = 40.0


class ATCCalculator:
    """Owns the ATC clearance + speed-limit computation for one controller."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller

    # ------------------------------------------------------------------
    # Clearance ahead
    # ------------------------------------------------------------------

    def clearance_ahead(self) -> float:
        """Available clearance in cm ahead of the train head.

        Walks the train's current path forward, summing block lengths until
        hitting an obstruction (other train, foreign reservation, red signal,
        end of path). When the path has been completed but the train is still
        approaching its destination station, derives clearance from
        `_calculate_clearance_to_destination` instead.
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

            if c._is_signal_red_for_block(block_id):
                break

            clearance += block.length_cm

        if not blocks_ahead and c.ato_destination_block_id:
            clearance = c._calculate_clearance_to_destination(clearance)

        return clearance

    # ------------------------------------------------------------------
    # ATC speed limit
    # ------------------------------------------------------------------

    def speed_limit(self) -> Tuple[float, Optional[str]]:
        """Return (speed_limit_kmh, reason_string).

        Uses physics `v^2 = 2*a*d` with the effective safety margin to
        derive a safe speed for the current clearance. Layers in CAUTION,
        yellow-signal, and block-speed caps as separate concerns.
        """
        from train_controller import calculate_max_safe_speed

        c = self._c
        profile = c._get_speed_profile()
        decel_rate = profile.deceleration_rate

        clearance = c.clearance_ahead_cm
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
        """`speed_limit()` plus the structured `AtcLimitReasonCode`.

        Calls back through `controller._calculate_atc_speed_limit()` so that
        tests monkey-patching the controller-side method still take effect.
        """
        c = self._c
        limiting_speed, limiting_reason = c._calculate_atc_speed_limit()
        clearance = c.clearance_ahead_cm
        limiting_code: Optional[AtcLimitReasonCode] = None

        if clearance < float("inf"):
            if limiting_speed <= STOPPED_SPEED_THRESHOLD_KMH:
                limiting_code = AtcLimitReasonCode.AUTHORITY_STOP
            else:
                limiting_code = AtcLimitReasonCode.OBSTRUCTION

        if self.is_caution_ahead():
            limiting_code = AtcLimitReasonCode.CAUTION

        if c.received_path_plan is None and limiting_code in (
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
        configured `safety_margin_cm` (typically 25cm)."""
        c = self._c
        train = c._get_train()
        if not train or not c.network:
            return c.safety_margin_cm

        if c.ato_destination_block_id:
            station_id = c._get_station_in_block(c.ato_destination_block_id)
            if station_id:
                return PLATFORM_SAFETY_MARGIN_CM

        return c.safety_margin_cm

    def is_approaching_yellow_signal(self) -> bool:
        """True when a yellow / double-yellow signal lies within
        `signal_lookahead_blocks` blocks ahead in the train's travel
        direction."""
        from enums import SignalState

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
            if blocks_checked >= c.signal_lookahead_blocks:
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
