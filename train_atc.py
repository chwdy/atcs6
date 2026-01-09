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
from typing import Optional, List, TYPE_CHECKING

from train_config import (
    ATC_SAFETY_MARGIN_CM,
    ATC_SIGNAL_LOOKAHEAD_BLOCKS,
    ATC_MIN_CLEARANCE_CM,
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

    # Convert deceleration from km/h/s to cm/s²
    # 1 km/h = 100000/3600 cm/s ≈ 27.78 cm/s
    # So km/h/s to cm/s² = multiply by 27.78
    km_h_to_cm_s = 100000.0 / 3600.0
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
            return ATC_MIN_CLEARANCE_CM  # Minimal clearance if no block

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
        """
        current_block_id = self._get_current_block_id()
        if not current_block_id:
            return 0.0

        block = self.network.blocks.get(current_block_id)
        if not block:
            return 0.0

        # Estimate: assume train is at the start of the block
        # In a more detailed implementation, this would consider
        # actual train position within the block
        return block.length_cm

    def _get_blocks_ahead(self) -> List[str]:
        """
        Get list of block IDs ahead of the train.

        Uses the train's assigned path or current path to determine
        which blocks are ahead.
        """
        blocks = []

        # If train has an assigned path from dispatcher, use that
        if self.train.assigned_path_id:
            path = (
                self.network.paths.get(self.train.assigned_path_id)
                if hasattr(self.network, "paths")
                else None
            )
            if path:
                # Get blocks from path that are ahead of current position
                # This is a simplified version - would need more detail
                # for actual path segment tracking
                pass

        # Fallback: use reserved blocks if available
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
            if hasattr(signal, "block_id") and signal.block_id == block_id:
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
                if hasattr(signal, "block_id") and signal.block_id == block_id:
                    if signal.state in (SignalState.YELLOW, SignalState.DOUBLE_YELLOW):
                        return True

            blocks_checked += 1

        return False

    def _get_current_block_speed_limit(self) -> float:
        """
        Get speed limit for current block.

        Returns the block's max_speed_kmh if set, otherwise returns
        a high value (effectively no limit).
        """
        current_block_id = self._get_current_block_id()
        if not current_block_id:
            return float("inf")

        block = self.network.blocks.get(current_block_id)
        if block and block.max_speed_kmh:
            return block.max_speed_kmh

        return float("inf")

    def get_speed_limit(self, speed_profile: Optional["SpeedProfile"] = None) -> float:
        """
        Get the current ATC speed limit.

        Considers:
        - Clearance ahead (braking curve)
        - Block speed limits
        - Signal aspects (YELLOW = caution speed)

        Args:
            speed_profile: Optional speed profile for deceleration rate

        Returns:
            Maximum allowed speed in km/h
        """
        # Get deceleration rate
        decel_rate = DEFAULT_DECELERATION_RATE
        if speed_profile:
            decel_rate = speed_profile.deceleration_rate

        # Calculate clearance
        clearance = self.get_clearance_ahead()

        # Calculate max speed for braking curve
        safe_speed = calculate_max_safe_speed(
            clearance, decel_rate, self.safety_margin_cm
        )

        # Check for yellow signals (caution speed)
        if self._is_approaching_yellow_signal():
            safe_speed = min(safe_speed, YELLOW_SIGNAL_SPEED_KMH)

        # Check block speed limits
        block_limit = self._get_current_block_speed_limit()

        # Cache and return
        self._cached_speed_limit = min(safe_speed, block_limit)
        return self._cached_speed_limit

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
        atc_limit = self.get_speed_limit(speed_profile)
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
