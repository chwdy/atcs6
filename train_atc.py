"""
Automatic Train Control (ATC)

Definition:
This module provides the TrainATC class which limits train speed based on
available clearance ahead. ATC prevents collisions by calculating safe braking
curves and enforcing speed limits.

The module includes:
- TrainATC: Main class for automatic speed control
- calculate_max_safe_speed: Braking curve calculation utility
"""

import math
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, TYPE_CHECKING

from train_config import (
    ATC_SAFETY_MARGIN_CM,
    ATC_SIGNAL_LOOKAHEAD_BLOCKS,
    ATC_MIN_CLEARANCE_CM,
    ATC_NO_BLOCK_FALLBACK_CM,
    YELLOW_SIGNAL_SPEED_KMH,
    DEFAULT_DECELERATION_RATE,
    kmh_to_cm_per_sec,
)
from enums import BlockState, SignalState

if TYPE_CHECKING:
    from network import RailwayNetwork
    from train import Train
    from speed_profile import SpeedProfile


def calculate_max_safe_speed(
    clearance_cm: float,
    deceleration_rate: float,
    safety_margin_cm: float = ATC_SAFETY_MARGIN_CM,
) -> float:
    """
    Calculate maximum speed that allows stopping within clearance.

    Uses physics formula: v² = 2 * a * d
    Where:
        v = velocity (converted between km/h and cm/s)
        a = deceleration (km/h/s → cm/s²)
        d = distance (cm)

    Args:
        clearance_cm: Available distance before obstruction
        deceleration_rate: Braking rate in km/h per second
        safety_margin_cm: Buffer distance (default 50cm)

    Returns:
        Maximum safe speed in km/h
    """
    effective_distance = max(0, clearance_cm - safety_margin_cm)

    if effective_distance <= 0:
        return 0.0

    if deceleration_rate <= 0:
        return 0.0

    from train_config import get_speed_conversion_factor

    km_h_to_cm_s = get_speed_conversion_factor()
    decel_cm_s2 = deceleration_rate * km_h_to_cm_s

    # v² = 2 * a * d
    v_cm_s_squared = 2 * decel_cm_s2 * effective_distance
    v_cm_s = math.sqrt(v_cm_s_squared)

    # Convert back to km/h
    v_kmh = v_cm_s / km_h_to_cm_s

    return v_kmh


@dataclass
class TrainATC:
    """
    Automatic Train Control - limits speed based on clearance.

    ATC calculates the available clearance ahead of a train and determines
    the maximum safe speed based on braking curves. It also considers
    signal aspects and block speed limits.

    Attributes:
        train: The train being controlled
        network: Reference to the railway network
        safety_margin_cm: Buffer distance before obstruction
        signal_lookahead_blocks: How many blocks to check signals
    """

    train: "Train"
    network: "RailwayNetwork"

    # Configuration
    safety_margin_cm: float = ATC_SAFETY_MARGIN_CM
    signal_lookahead_blocks: int = ATC_SIGNAL_LOOKAHEAD_BLOCKS

    # Cached values (updated per tick)
    _cached_clearance: float = field(default=0.0, init=False)
    _cached_speed_limit: float = field(default=0.0, init=False)
    _cached_speed_limit_reason: Optional[str] = field(default=None, init=False)

    def get_clearance_ahead(self) -> float:
        """
        Calculate available clearance in cm.

        Follows the train's current path forward, summing block lengths
        until hitting an obstruction:
        - A block occupied by another train
        - A block not reserved for this train's path
        - A red signal
        - End of path/track

        Returns:
            Distance in cm until next obstruction
        """
        clearance = 0.0

        # Get train's current block
        current_block_id = self._get_current_block_id()
        if not current_block_id:
            return ATC_NO_BLOCK_FALLBACK_CM

        # Add remaining distance in current block
        clearance += self._get_remaining_distance_in_current_block()

        # Get blocks ahead in path
        blocks_ahead = self._get_blocks_ahead()

        for block_id in blocks_ahead:
            block = self.network.blocks.get(block_id)
            if not block:
                break

            # Check if block is obstructed
            if self._is_block_obstructed(block, block_id):
                break

            # Check signals for this block
            if self._is_signal_red_for_block(block_id):
                break

            # Add block length to clearance
            clearance += block.length_cm

        self._cached_clearance = clearance
        return clearance

    def _get_current_block_id(self) -> Optional[str]:
        """Get the block ID where the train's head is located."""
        if not self.train.current_pos:
            return None

        track = self.network.tracks.get(self.train.current_pos)
        if track:
            return track.block_section_id

        return None

    def _get_remaining_distance_in_current_block(self) -> float:
        """
        Get remaining distance in the current block.

        This is the distance from train's head to the block boundary.
        Calculates based on actual train position within the block.
        """
        current_block_id = self._get_current_block_id()
        if not current_block_id:
            return 0.0

        block = self.network.blocks.get(current_block_id)
        if not block or not block.length_cm:
            return 0.0

        if not self.train.current_pos or not self.train.current_path:
            return block.length_cm

        block_positions = self.network.get_block_positions(current_block_id)
        if not block_positions:
            return block.length_cm

        tile_count = len(block_positions)
        if tile_count <= 0:
            return block.length_cm

        per_tile_length = block.length_cm / tile_count

        current_tile_index = -1
        for i, (bx, by) in enumerate(block_positions):
            if (bx, by) == self.train.current_pos:
                current_tile_index = i
                break

        if current_tile_index < 0:
            return block.length_cm

        remaining_tiles_after_current = tile_count - current_tile_index - 1

        current_track = self.network.get_track(*self.train.current_pos)
        fraction_through_tile = 0.0
        if current_track:
            track_length_cm = self.network._get_tile_length_cm(*self.train.current_pos)
            if track_length_cm > 0:
                fraction_through_tile = self.train.distance_on_track / track_length_cm
                fraction_through_tile = max(0.0, min(1.0, fraction_through_tile))

        remaining_in_current_tile = (1.0 - fraction_through_tile) * per_tile_length
        remaining_in_later_tiles = remaining_tiles_after_current * per_tile_length

        return remaining_in_current_tile + remaining_in_later_tiles

    def _get_blocks_ahead(self) -> List[str]:
        """
        Get list of block IDs ahead of the train.

        Uses the train's reserved blocks to determine which blocks are ahead.
        """
        blocks = []

        if self.train.reserved_blocks:
            current_block_id = self._get_current_block_id()
            if current_block_id:
                try:
                    idx = self.train.reserved_blocks.index(current_block_id)
                    blocks = self.train.reserved_blocks[idx + 1 :]
                except ValueError:
                    blocks = self.train.reserved_blocks

        return blocks

    def _is_block_obstructed(self, block: "Block", block_id: str) -> bool:
        """
        Check if a block is obstructed (cannot be entered).

        A block is obstructed if:
        - It's occupied by another train
        - It's reserved for a different path
        - It's disabled
        """
        # Disabled blocks are obstructed
        if block.disabled:
            return True

        # Occupied by another train
        if block.state == BlockState.OCCUPIED:
            if block.occupied_by != self.train.train_id:
                return True

        # Reserved for a different path
        if block.state == BlockState.RESERVED:
            if block.reserved_by_path and self.train.assigned_path_id:
                if block.reserved_by_path != self.train.assigned_path_id:
                    return True
            elif block.reserved_by_path:
                # Block reserved but train has no path
                return True

        # CAUTION state means train just left and next block may be occupied
        if block.state == BlockState.CAUTION:
            return True

        return False

    def _is_signal_red_for_block(self, block_id: str) -> bool:
        """
        Check if there's a red signal protecting this block.

        Looks for signals at the entry to the block in the train's
        direction of travel.
        """
        # Find signals associated with this block
        for signal_id, signal in self.network.signals.items():
            # Check if signal protects entry to this block
            if (
                hasattr(signal, "monitored_block_id")
                and signal.monitored_block_id == block_id
            ):
                if signal.state == SignalState.RED:
                    return True

        return False

    def _is_approaching_yellow_signal(self) -> bool:
        """
        Check if train is approaching a yellow signal.

        Looks ahead for yellow signals within the lookahead distance.
        """
        blocks_checked = 0
        blocks_ahead = self._get_blocks_ahead()

        for block_id in blocks_ahead:
            if blocks_checked >= self.signal_lookahead_blocks:
                break

            # Check for yellow signal at this block
            for signal_id, signal in self.network.signals.items():
                if (
                    hasattr(signal, "monitored_block_id")
                    and signal.monitored_block_id == block_id
                ):
                    if signal.state in (SignalState.YELLOW, SignalState.DOUBLE_YELLOW):
                        return True

            blocks_checked += 1

        return False

    def _get_current_block_speed_limit(self) -> float:
        """
        Get speed limit for current block.

        Returns the block's max_speed_kmh if set, otherwise returns
        a high value (effectively no limit).

        NOTE: Switches (tracks without block_section_id) are treated as
        having no speed limit when the route is properly set.
        """
        if not self.train.current_pos:
            return float("inf")

        track = self.network.tracks.get(self.train.current_pos)
        if not track:
            return float("inf")

        if not track.block_section_id:
            return float("inf")

        block = self.network.blocks.get(track.block_section_id)
        if block and block.max_speed_kmh:
            return block.max_speed_kmh

        return float("inf")

    def get_speed_limit(
        self, speed_profile: Optional["SpeedProfile"] = None
    ) -> Tuple[float, Optional[str]]:
        """
        Get the current ATC speed limit with reason.

        Considers:
        - Clearance ahead (braking curve)
        - Block speed limits
        - Signal aspects (YELLOW = caution speed)

        Args:
            speed_profile: Optional speed profile for deceleration rate

        Returns:
            Tuple of (speed_limit_kmh, reason_string)
        """
        decel_rate = DEFAULT_DECELERATION_RATE
        if speed_profile:
            decel_rate = speed_profile.deceleration_rate

        clearance = self.get_clearance_ahead()

        safe_speed = calculate_max_safe_speed(
            clearance, decel_rate, self.safety_margin_cm
        )

        reasons = []
        limiting_speed = safe_speed
        limiting_reason = None

        if clearance < float("inf"):
            if clearance < 1000:
                limiting_reason = f"Braking: {clearance / 100:.0f}m ahead"
            else:
                limiting_reason = f"Clearance: {clearance / 100:.0f}m"

        if self._is_approaching_yellow_signal():
            if YELLOW_SIGNAL_SPEED_KMH < limiting_speed:
                limiting_speed = YELLOW_SIGNAL_SPEED_KMH
                limiting_reason = "Yellow signal ahead"
            elif limiting_reason:
                limiting_reason = f"{limiting_reason}; Yellow signal"

        block_limit = self._get_current_block_speed_limit()
        if block_limit < float("inf"):
            if block_limit < limiting_speed:
                limiting_speed = block_limit
                limiting_reason = f"Block limit: {block_limit:.0f} km/h"
            elif limiting_reason and block_limit < self.train.max_speed:
                limiting_reason = (
                    f"{limiting_reason}; Block limit: {block_limit:.0f} km/h"
                )

        self._cached_speed_limit = limiting_speed
        self._cached_speed_limit_reason = limiting_reason
        return limiting_speed, limiting_reason

    def get_target_speed(self, speed_profile: Optional["SpeedProfile"] = None) -> float:
        """
        Get the target speed for the train.

        Returns min of:
        - Train's max_speed
        - Current block's speed limit
        - ATC calculated limit

        Args:
            speed_profile: Optional speed profile

        Returns:
            Target speed in km/h
        """
        atc_limit, _ = self.get_speed_limit(speed_profile)
        return min(self.train.max_speed, atc_limit)

    def is_safe_to_move(self) -> bool:
        """
        Check if it's safe for the train to move.

        Returns True if there's sufficient clearance for minimal movement.
        """
        clearance = self.get_clearance_ahead()
        return clearance > ATC_MIN_CLEARANCE_CM

    def get_clearance_status(self) -> dict:
        """
        Get detailed clearance status for debugging/display.

        Returns:
            Dictionary with clearance info
        """
        return {
            "clearance_cm": self._cached_clearance,
            "speed_limit_kmh": self._cached_speed_limit,
            "current_block": self._get_current_block_id(),
            "blocks_ahead_count": len(self._get_blocks_ahead()),
            "yellow_signal_ahead": self._is_approaching_yellow_signal(),
            "block_speed_limit": self._get_current_block_speed_limit(),
        }


# Factory function to create ATC for a train
def create_atc_for_train(train: "Train", network: "RailwayNetwork") -> TrainATC:
    """
    Create a TrainATC instance for a train.

    Args:
        train: Train to control
        network: Railway network

    Returns:
        TrainATC instance
    """
    return TrainATC(train=train, network=network)
