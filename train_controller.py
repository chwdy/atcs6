"""
Train Controller - Unified ATC + ATO + Command Handling

Definition:
This module provides the TrainController class which is the single authority for
train operational state and control decisions. It merges the functionality of:

- OnboardComputer: Power, cab, mode, handle state, command validation
- TrainATC: Clearance calculation, speed limits, braking curves

TrainController is the "cab computer" that:
- Controls power on/off and emergency stop
- Stores the current control mode (ATO vs Manual)
- Knows which cab is active and the travel direction
- Manages the throttle/brake handle position
- Calculates safe speed limits based on clearance ahead (ATC)
- In ATO mode, automatically drives toward destination
- Validates whether commands should execute
- Reports current operational state to displays

Architecture:
    SimulationController (Orchestrator / Clock)
            |
            v
    +-----------------+     +---------------+
    | TrainController |---->| TrainPhysics  |
    | (Control Logic) |     | (Movement)    |
    +-----------------+     +---------------+
            |                      |
            v                      v
        +-------+           +-------------+
        | Train |           | BlockManager|
        | (Data)|           +-------------+
        +-------+

Design Principles:
1. Single Authority: TrainController owns all train control state
2. Combined ATC+ATO: One class handles both safety limits AND automatic driving
3. Command Validation: All commands go through TrainController
4. Read-Only UI: Control panels read from TrainController, never compute locally
5. Separation from Physics: TrainController decides WHAT speed; TrainPhysics executes
"""

import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from enums import (
    BRAKE_MULTIPLIERS,
    POWER_MULTIPLIERS,
    BlockState,
    CommandResult,
    Direction,
    HandlePosition,
    SignalState,
    TrainState,
)
from model_scale import get_speed_conversion_factor

if TYPE_CHECKING:
    from network import RailwayNetwork
    from speed_profile import SpeedProfile
    from train import Train


logger = logging.getLogger(__name__)


ATC_SAFETY_MARGIN_CM = 25.0
ATC_PLATFORM_SAFETY_MARGIN_CM = 5.0
ATC_SIGNAL_LOOKAHEAD_BLOCKS = 3
ATC_MIN_CLEARANCE_CM = 10.0
ATC_NO_BLOCK_FALLBACK_CM = 100000.0
YELLOW_SIGNAL_SPEED_KMH = 40.0
STOPPED_SPEED_THRESHOLD_KMH = 0.5


# =============================================================================
# Data Structures for Lookahead and ATO
# =============================================================================


@dataclass
class LookaheadPoint:
    """
    A point in the lookahead representing block information ahead of the train.

    Used by both ATC (for safety speed calculation) and ATO (for navigation).
    Signal state is included for display purposes but does not affect ATC speed
    calculation (this is a CBTC-like block-based system).
    """

    distance_cm: float  # Distance from train head to start of this block
    speed_limit_kmh: float  # Block speed limit (or 0 if path end)
    block_id: str  # Block ID
    signal_state: Optional[SignalState] = None  # Signal for display (not ATC calc)
    station_id: Optional[str] = None  # Station ID if block has station
    is_path_end: bool = False  # True if this is the last block in path


@dataclass
class ATOInfo:
    """
    ATO navigation information derived from the train's assigned path.

    Contains path/destination details and determines which cab should be active
    based on path direction.
    """

    path_id: Optional[str] = None
    destination_block_id: Optional[str] = None
    destination_station_name: Optional[str] = None
    path_heading: str = "a"  # "a" or "b" - which end the path heads toward
    required_cab: str = "A"  # "A" or "B" based on path_heading


# =============================================================================
# Speed Calculation Helper
# =============================================================================


def calculate_max_safe_speed(
    clearance_cm: float,
    deceleration_rate: float,
    safety_margin_cm: float = ATC_SAFETY_MARGIN_CM,
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


@dataclass
class TrainController:
    """
    Combined ATC + ATO + command handling for one train.

    TrainController is the single authority for:
    - Power state and emergency stop
    - Active cab selection
    - Throttle/brake handle position
    - Control mode (ATO/Manual)
    - Travel direction
    - ATC speed limit calculation
    - ATO automatic driving decisions
    - Command validation

    It does NOT own:
    - Train position (owned by Train, updated by TrainPhysics)
    - Train current_speed (owned by Train, updated by TrainPhysics)
    - Block state (owned by BlockManager, updated by TrainPhysics)
    - Simulation time (owned by SimulationController)
    """

    train_id: str
    network: Optional["RailwayNetwork"] = None

    # -------------------------------------------------------------------------
    # Power & Safety State
    # -------------------------------------------------------------------------
    power_on: bool = False
    eb_active: bool = (
        False  # Emergency brake active (renamed from emergency_stop_active)
    )
    atc_warning_active: bool = False  # True when overspeed by 5-10 km/h

    # -------------------------------------------------------------------------
    # Notifications (for frontend toast messages via API)
    # -------------------------------------------------------------------------
    pending_notifications: List[str] = field(default_factory=list)

    # -------------------------------------------------------------------------
    # Cab & Mode State
    # -------------------------------------------------------------------------
    active_cab: Optional[str] = None  # 'A', 'B', or None
    control_mode: str = "ato"  # 'ato' or 'manual'
    travel_direction: str = "forward"  # 'forward' or 'reverse'

    # -------------------------------------------------------------------------
    # Operational State
    # -------------------------------------------------------------------------
    state: TrainState = field(default=TrainState.OFF)
    target_speed: float = 0.0
    handle_position: HandlePosition = field(default=HandlePosition.N)

    # -------------------------------------------------------------------------
    # ATO State
    # -------------------------------------------------------------------------
    ato_running: bool = False
    ato_destination_id: Optional[str] = None
    ato_path_id: Optional[str] = None

    # -------------------------------------------------------------------------
    # ATC State (calculated each tick)
    # -------------------------------------------------------------------------
    clearance_ahead_cm: float = 0.0
    atc_speed_limit: float = 0.0
    current_block_speed_limit: float = float("inf")
    speed_limit_reason: Optional[str] = None

    # Configuration
    safety_margin_cm: float = ATC_SAFETY_MARGIN_CM
    signal_lookahead_blocks: int = ATC_SIGNAL_LOOKAHEAD_BLOCKS

    # -------------------------------------------------------------------------
    # Lookahead & ATO Info (calculated each tick)
    # -------------------------------------------------------------------------
    lookahead: List[LookaheadPoint] = field(default_factory=list)
    ato_info: Optional[ATOInfo] = None

    # -------------------------------------------------------------------------
    # Internal Helpers
    # -------------------------------------------------------------------------

    def _get_train(self) -> Optional["Train"]:
        """Get the Train entity this controller controls."""
        if self.network is None:
            return None
        return self.network.trains.get(self.train_id)

    def _get_speed_profile(self) -> "SpeedProfile":
        """Get the SpeedProfile for this train."""
        from speed_profile import DEFAULT_SPEED_PROFILE

        train = self._get_train()
        if not train or not self.network:
            return DEFAULT_SPEED_PROFILE

        profile_id = train.speed_profile_id
        if profile_id and hasattr(self.network, "speed_profiles"):
            profile = self.network.speed_profiles.get(profile_id)
            if profile:
                return profile

        return DEFAULT_SPEED_PROFILE

    # -------------------------------------------------------------------------
    # Tick Method (Called Each Simulation Tick)
    # -------------------------------------------------------------------------

    def tick(self, delta: float) -> None:
        """
        Called each simulation tick by SimulationController.

        Workflow:
        0. Power check
        1. Emergency brake (EB) check
        2. Lookahead calculation (shared)
        3. ATC info collection
        4. ATC action (warning/EB on overspeed)
        5. ATO info
        6. ATO action (cab correction, handle calculation)
        7. Effective limit & target speed
        8. State transitions

        Does NOT move the train - that's TrainPhysics.tick().

        Args:
            delta: Time elapsed since last tick (seconds)
        """
        train = self._get_train()
        if not train:
            self.target_speed = 0.0
            return

        logger.debug(
            "[TrainController %s] tick start: power=%s eb=%s mode=%s state=%s speed=%.2f",
            self.train_id,
            self.power_on,
            self.eb_active,
            self.control_mode,
            self.state.value,
            train.current_speed,
        )

        # ===== 0. POWER CHECK =====
        if not self.power_on:
            self.target_speed = 0.0
            self.state = TrainState.OFF
            train.state = TrainState.OFF
            logger.debug(
                "[TrainController %s] power off -> target=0 state=off",
                self.train_id,
            )
            return

        # ===== 1. EMERGENCY BRAKE CHECK =====
        if self.eb_active:
            self.handle_position = HandlePosition.EB
            self.target_speed = 0.0

            logger.debug(
                "[TrainController %s] EB active -> handle=EB target=0 speed=%.2f",
                self.train_id,
                train.current_speed,
            )

            # Check if train has stopped
            if train.current_speed <= STOPPED_SPEED_THRESHOLD_KMH:
                # EB complete - transition to STANDBY
                self.eb_active = False
                self.state = TrainState.STANDBY
                train.state = TrainState.STANDBY
                self.pending_notifications.append("Emergency brake released")
                logger.debug(
                    "[TrainController %s] EB release -> state=standby",
                    self.train_id,
                )
            return

        # ===== 2. LOOKAHEAD CALCULATION (shared by ATC and ATO) =====
        self.lookahead = self._calculate_lookahead()

        # ===== 3. ATC INFO COLLECTION =====
        if self.active_cab is None:
            self.atc_speed_limit = 0.0
            self.speed_limit_reason = "No cab selected"
            logger.debug(
                "[TrainController %s] ATC blocked: no cab selected",
                self.train_id,
            )
        else:
            # Get block speed limit for current block
            self.current_block_speed_limit = self._get_current_block_speed_limit()

            # Calculate clearance and ATC speed from existing methods
            # (These use lookahead data internally)
            self.clearance_ahead_cm = self._calculate_clearance_ahead()
            self.atc_speed_limit, self.speed_limit_reason = (
                self._calculate_atc_speed_limit()
            )
            logger.debug(
                "[TrainController %s] ATC: clearance=%.2f limit=%.2f reason=%s",
                self.train_id,
                self.clearance_ahead_cm,
                self.atc_speed_limit,
                self.speed_limit_reason,
            )

        # ===== 4. ATC ACTION =====
        overspeed = train.current_speed - self.atc_speed_limit

        if overspeed > 10:
            # Emergency brake - overspeed > 10 km/h
            self.eb_active = True
            self.speed_limit_reason = "Emergency brake: overspeed"
            self.handle_position = HandlePosition.EB
            self.pending_notifications.append(
                "ATC triggered emergency brake: overspeed"
            )
            logger.debug(
                "[TrainController %s] ATC EB: overspeed=%.2f",
                self.train_id,
                overspeed,
            )
            return
        elif overspeed > 5:
            # Warning only
            self.atc_warning_active = True
            logger.debug(
                "[TrainController %s] ATC warning: overspeed=%.2f",
                self.train_id,
                overspeed,
            )
        else:
            self.atc_warning_active = False

        # ===== 5. ATO INFO =====
        self.ato_info = self._calculate_ato_info()

        # ===== 6. ATO ACTION (skip if manual) =====
        if self.control_mode == "ato":
            # Check cab matches path direction
            if self.ato_info and self.active_cab != self.ato_info.required_cab:
                old_cab = self.active_cab
                self.active_cab = self.ato_info.required_cab
                self.pending_notifications.append(
                    f"Cab switched from {old_cab} to {self.active_cab}"
                )
                logger.debug(
                    "[TrainController %s] ATO cab switch: %s -> %s",
                    self.train_id,
                    old_cab,
                    self.active_cab,
                )

            # Calculate handle position
            if self.ato_running:
                self.handle_position = self._ato_calculate_handle()
                logger.debug(
                    "[TrainController %s] ATO handle=%s target=%.2f",
                    self.train_id,
                    self.handle_position.value,
                    self.target_speed,
                )
        else:
            # Manual mode - ATC still enforces braking if overspeed
            if train.current_speed > self.atc_speed_limit:
                self.handle_position = self._calculate_braking_notch_for_speed(
                    train.current_speed, self.atc_speed_limit
                )
                logger.debug(
                    "[TrainController %s] Manual ATC brake: handle=%s",
                    self.train_id,
                    self.handle_position.value,
                )

        # ===== 7. EFFECTIVE LIMIT & TARGET SPEED =====
        effective_limit = min(
            train.max_speed,
            self.atc_speed_limit,
            self.current_block_speed_limit,
        )
        self.target_speed = effective_limit
        train.target_speed = self.target_speed
        train.speed_limit_reason = self.speed_limit_reason
        logger.debug(
            "[TrainController %s] target_speed=%.2f effective_limit=%.2f",
            self.train_id,
            self.target_speed,
            effective_limit,
        )

        # ===== 8. STATE TRANSITIONS =====
        if self.state == TrainState.RUNNING_MOVING:
            # Check for station arrival
            if self._is_at_station():
                self.state = TrainState.RUNNING_AT_STATION
                train.state = TrainState.RUNNING_AT_STATION
                self.ato_running = False
                # RouteExecutor will handle dwell timing
                logger.debug(
                    "[TrainController %s] state -> RUNNING_AT_STATION",
                    self.train_id,
                )

            # Check for obstruction
            elif effective_limit <= STOPPED_SPEED_THRESHOLD_KMH:
                if train.current_speed <= STOPPED_SPEED_THRESHOLD_KMH:
                    self.state = TrainState.RUNNING_WAITING
                    train.state = TrainState.RUNNING_WAITING
                    train.wait_reason = self.speed_limit_reason or "Obstruction ahead"
                    logger.debug(
                        "[TrainController %s] state -> RUNNING_WAITING reason=%s",
                        self.train_id,
                        train.wait_reason,
                    )

        elif self.state == TrainState.RUNNING_WAITING:
            # Check if path cleared AND train is ready to move
            # Only auto-transition if: power is on, EB is not active, and obstruction cleared
            if (
                effective_limit > STOPPED_SPEED_THRESHOLD_KMH
                and self.power_on
                and not self.eb_active
            ):
                self.state = TrainState.RUNNING_MOVING
                train.state = TrainState.RUNNING_MOVING
                train.wait_reason = None
                if self.control_mode == "ato":
                    self.ato_running = True
                logger.debug(
                    "[TrainController %s] state -> RUNNING_MOVING",
                    self.train_id,
                )

        # Other states (IDLE, STANDBY, AT_STATION, OFF) handled by commands

    # -------------------------------------------------------------------------
    # ATC (Automatic Train Control) Logic
    # -------------------------------------------------------------------------

    def _calculate_clearance_ahead(self) -> float:
        """
        Calculate available clearance in cm.

        Follows the train's current path forward, summing block lengths
        until hitting an obstruction:
        - Block occupied by another train
        - Block not reserved for this train's path
        - Red signal
        - End of path

        Returns:
            Distance in cm until next obstruction
        """
        train = self._get_train()
        if not train or not self.network:
            return ATC_NO_BLOCK_FALLBACK_CM

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

        return clearance

    def _calculate_atc_speed_limit(self) -> Tuple[float, Optional[str]]:
        """
        Calculate maximum safe speed based on clearance ahead.

        Uses physics: v^2 = 2 * a * d

        Returns:
            Tuple of (speed_limit_kmh, reason_string)
        """
        profile = self._get_speed_profile()
        decel_rate = profile.deceleration_rate

        clearance = self.clearance_ahead_cm

        # Determine safety margin based on destination type
        safety_margin = self._get_effective_safety_margin()

        safe_speed = calculate_max_safe_speed(clearance, decel_rate, safety_margin)

        limiting_speed = safe_speed
        limiting_reason = None

        if clearance < float("inf"):
            if clearance < 1000:
                limiting_reason = f"Braking: {clearance / 100:.0f}m ahead"
            else:
                limiting_reason = f"Clearance: {clearance / 100:.0f}m"

        # Check for yellow signal
        if self._is_approaching_yellow_signal():
            if YELLOW_SIGNAL_SPEED_KMH < limiting_speed:
                limiting_speed = YELLOW_SIGNAL_SPEED_KMH
                limiting_reason = "Yellow signal ahead"
            elif limiting_reason:
                limiting_reason = f"{limiting_reason}; Yellow signal"

        # Check block speed limit
        block_limit = self._get_current_block_speed_limit()
        if block_limit < float("inf"):
            train = self._get_train()
            if block_limit < limiting_speed:
                limiting_speed = block_limit
                limiting_reason = f"Block limit: {block_limit:.0f} km/h"
            elif limiting_reason and train and block_limit < train.max_speed:
                limiting_reason = f"{limiting_reason}; Block: {block_limit:.0f}"

        return limiting_speed, limiting_reason

    def _get_effective_safety_margin(self) -> float:
        """
        Get the effective safety margin based on destination type.

        Returns:
            5cm if approaching a platform/station, 25cm otherwise
        """
        train = self._get_train()
        if not train or not self.network:
            return self.safety_margin_cm

        # Check if we're in the last block and it contains a station
        blocks_ahead = self._get_blocks_ahead()
        if not blocks_ahead:
            # No blocks ahead - we're in the last block
            if train.current_path and len(train.current_path) > 0:
                last_tile = train.current_path[-1]
                last_track = self.network.tracks.get((last_tile[0], last_tile[1]))
                if last_track and last_track.station_id:
                    return ATC_PLATFORM_SAFETY_MARGIN_CM

        return self.safety_margin_cm

    def _get_current_block_id(self) -> Optional[str]:
        """Get the block ID where the train's head is located."""
        head_tile = self._get_head_tile()
        if not head_tile or not self.network:
            return None

        track = self.network.tracks.get(head_tile)
        if track:
            return track.block_section_id

        return None

    def _get_head_tile(self) -> Optional[Tuple[int, int]]:
        """Get the head tile for the train (TrainPosition preferred)."""
        train = self._get_train()
        if not train:
            return None

        if train.position:
            return train.position.head_tile

        return train.current_pos

    def _get_head_distance_cm(self) -> float:
        """Get head distance within the current tile in cm."""
        train = self._get_train()
        if not train:
            return 0.0

        if train.position:
            return train.position.head_distance_cm

        return train.distance_on_track

    def _get_head_path_index(self, train: "Train", head_tile: Tuple[int, int]) -> int:
        """Get the path index for the train head tile."""
        if not train.current_path:
            return -1

        if 0 <= train.path_index < len(train.current_path):
            path_tile = train.current_path[train.path_index]
            if (path_tile[0], path_tile[1]) == head_tile:
                return train.path_index

        matches = [
            i for i, (x, y, _) in enumerate(train.current_path) if (x, y) == head_tile
        ]
        if not matches:
            return -1

        if train.path_index >= 0:
            return min(matches, key=lambda i: abs(i - train.path_index))

        return matches[0]

    def _get_remaining_distance_in_current_block(self) -> float:
        """
        Get remaining distance in the current block.

        This is the distance from train's head to the block boundary.
        """
        train = self._get_train()
        if not train or not self.network:
            return 0.0

        current_block_id = self._get_current_block_id()
        if not current_block_id:
            return 0.0

        block = self.network.blocks.get(current_block_id)
        if not block or not block.length_cm:
            return 0.0

        head_tile = self._get_head_tile()
        if not head_tile:
            return block.length_cm

        if train.position and train.current_path:
            head_index = self._get_head_path_index(train, head_tile)
            if head_index < 0:
                return block.length_cm

            head_tile_length = self.network._get_tile_length_cm(*head_tile)
            remaining = max(0.0, head_tile_length - train.position.head_distance_cm)

            for x, y, _ in train.current_path[head_index + 1 :]:
                track = self.network.get_track(x, y)
                if not track or track.block_section_id != current_block_id:
                    break
                remaining += self.network._get_tile_length_cm(x, y)

            return remaining

        block_positions = self.network.get_block_positions(current_block_id)
        if not block_positions:
            return block.length_cm

        tile_count = len(block_positions)
        if tile_count <= 0:
            return block.length_cm

        per_tile_length = block.length_cm / tile_count

        current_tile_index = -1
        for i, (bx, by) in enumerate(block_positions):
            if (bx, by) == head_tile:
                current_tile_index = i
                break

        if current_tile_index < 0:
            return block.length_cm

        remaining_tiles_after_current = tile_count - current_tile_index - 1

        current_track = self.network.get_track(*head_tile)
        fraction_through_tile = 0.0
        if current_track:
            track_length_cm = self.network._get_tile_length_cm(*head_tile)
            if track_length_cm > 0:
                head_distance_cm = self._get_head_distance_cm()
                fraction_through_tile = head_distance_cm / track_length_cm
                fraction_through_tile = max(0.0, min(1.0, fraction_through_tile))

        remaining_in_current_tile = (1.0 - fraction_through_tile) * per_tile_length
        remaining_in_later_tiles = remaining_tiles_after_current * per_tile_length

        return remaining_in_current_tile + remaining_in_later_tiles

    def _get_blocks_ahead(self) -> List[str]:
        """
        Get list of block IDs ahead of the train.

        Uses the train's reserved blocks to determine which blocks are ahead.
        """
        train = self._get_train()
        if not train:
            return []

        blocks = []

        if train.reserved_blocks:
            current_block_id = self._get_current_block_id()
            if current_block_id:
                try:
                    idx = train.reserved_blocks.index(current_block_id)
                    blocks = train.reserved_blocks[idx + 1 :]
                except ValueError:
                    blocks = train.reserved_blocks

        return blocks

    def _is_block_obstructed(self, block: Any, block_id: str) -> bool:
        """
        Check if a block is obstructed (cannot be entered).

        A block is obstructed if:
        - It's occupied by another train
        - It's reserved for a different path
        - It's disabled
        """
        train = self._get_train()
        if not train:
            return True

        # Disabled blocks are obstructed
        if hasattr(block, "disabled") and block.disabled:
            return True

        # Occupied by another train
        if block.state == BlockState.OCCUPIED:
            if block.occupied_by != train.train_id:
                return True

        # Reserved for a different path
        if block.state == BlockState.RESERVED:
            if hasattr(block, "reserved_by_path") and block.reserved_by_path:
                if train.assigned_path_id:
                    if block.reserved_by_path != train.assigned_path_id:
                        return True
                else:
                    # Block reserved but train has no path
                    return True

        # CAUTION state means train just left
        if block.state == BlockState.CAUTION:
            return True

        return False

    def _is_signal_red_for_block(self, block_id: str) -> bool:
        """
        Check if there's a red signal protecting this block.
        """
        if not self.network:
            return False

        for signal_id, signal in self.network.signals.items():
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
        """
        if not self.network:
            return False

        blocks_checked = 0
        blocks_ahead = self._get_blocks_ahead()

        for block_id in blocks_ahead:
            if blocks_checked >= self.signal_lookahead_blocks:
                break

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
        """
        head_tile = self._get_head_tile()
        if not head_tile or not self.network:
            return float("inf")

        track = self.network.tracks.get(head_tile)
        if not track:
            return float("inf")

        if not track.block_section_id:
            return float("inf")

        block = self.network.blocks.get(track.block_section_id)
        if block and hasattr(block, "max_speed_kmh") and block.max_speed_kmh:
            return block.max_speed_kmh

        return float("inf")

    def is_safe_to_move(self) -> bool:
        """
        Check if it's safe for the train to move.
        """
        return self.clearance_ahead_cm > ATC_MIN_CLEARANCE_CM

    # -------------------------------------------------------------------------
    # Lookahead Calculation (shared by ATC and ATO)
    # -------------------------------------------------------------------------

    def _calculate_lookahead(self) -> List[LookaheadPoint]:
        """
        Calculate lookahead points from the train's current path.

        Generates a list of LookaheadPoint objects representing blocks ahead,
        with distance, speed limit, signal state, and station info.

        This is the shared lookahead used by both ATC (for safety speed calculation)
        and ATO (for navigation).

        Returns:
            List of LookaheadPoint objects ordered by distance from train head
        """
        train = self._get_train()
        if not train or not self.network:
            return []

        lookahead_points: List[LookaheadPoint] = []
        current_distance = 0.0

        # Get current block and remaining distance
        current_block_id = self._get_current_block_id()
        if current_block_id:
            remaining_in_current = self._get_remaining_distance_in_current_block()
            current_block = self.network.blocks.get(current_block_id)
            if current_block:
                # Get signal for this block (for display only)
                signal_state = self._get_signal_state_for_block(current_block_id)
                # Get station if present
                station_id = self._get_station_in_block(current_block_id)

                lookahead_points.append(
                    LookaheadPoint(
                        distance_cm=0.0,  # Current position
                        speed_limit_kmh=current_block.max_speed_kmh or float("inf"),
                        block_id=current_block_id,
                        signal_state=signal_state,
                        station_id=station_id,
                        is_path_end=False,
                    )
                )
                current_distance = remaining_in_current

        # Get blocks ahead in path
        blocks_ahead = self._get_blocks_ahead()

        if not blocks_ahead:
            # No path ahead - add end point
            if current_block_id:
                lookahead_points.append(
                    LookaheadPoint(
                        distance_cm=current_distance,
                        speed_limit_kmh=0.0,  # Must stop at path end
                        block_id=current_block_id,
                        signal_state=None,
                        station_id=None,
                        is_path_end=True,
                    )
                )
            return lookahead_points

        for i, block_id in enumerate(blocks_ahead):
            block = self.network.blocks.get(block_id)
            if not block:
                continue

            # Get signal for this block (for display only)
            signal_state = self._get_signal_state_for_block(block_id)
            # Get station if present
            station_id = self._get_station_in_block(block_id)
            # Check if this is the last block
            is_last = i == len(blocks_ahead) - 1

            lookahead_points.append(
                LookaheadPoint(
                    distance_cm=current_distance,
                    speed_limit_kmh=block.max_speed_kmh or float("inf"),
                    block_id=block_id,
                    signal_state=signal_state,
                    station_id=station_id,
                    is_path_end=is_last,
                )
            )

            current_distance += block.length_cm

        return lookahead_points

    def _get_signal_state_for_block(self, block_id: str) -> Optional[SignalState]:
        """Get the signal state for a block (for display purposes only)."""
        if not self.network:
            return None

        for signal_id, signal in self.network.signals.items():
            if (
                hasattr(signal, "monitored_block_id")
                and signal.monitored_block_id == block_id
            ):
                return signal.state

        return None

    def _get_station_in_block(self, block_id: str) -> Optional[str]:
        """Check if a block contains a station and return station ID."""
        if not self.network:
            return None

        # Get block's tracks and check for stations
        for track in self.network.tracks.values():
            if track.block_section_id == block_id and track.station_id:
                return track.station_id

        return None

    # -------------------------------------------------------------------------
    # ATO Info Calculation
    # -------------------------------------------------------------------------

    def _calculate_ato_info(self) -> Optional[ATOInfo]:
        """
        Calculate ATO navigation information from the train's assigned path.

        Returns:
            ATOInfo with path/destination details, or None if no path assigned
        """
        train = self._get_train()
        if not train or not self.network:
            return None

        # Get assigned path
        path_id = train.assigned_path_id or self.ato_path_id
        if not path_id:
            return None

        # Get path from network
        path = None
        if hasattr(self.network, "paths"):
            path = self.network.paths.get(path_id)

        if not path:
            return ATOInfo(path_id=path_id)

        # Determine path heading and required cab
        path_heading = "a"  # Default
        required_cab = "A"  # Default

        # Check path direction to determine which cab should be active
        if hasattr(path, "end_a") and hasattr(path, "end_b"):
            # Path has defined ends - determine which end we're heading toward
            if train.current_pos:
                # If train is closer to end_a, we're heading to end_b
                # This is simplified - real implementation would trace path direction
                path_heading = "b"
                required_cab = "A"  # Cab A faces forward

        # Get destination info
        destination_block_id = None
        destination_station_name = None

        if self.lookahead:
            # Find last station in lookahead
            for lp in reversed(self.lookahead):
                if lp.station_id:
                    destination_block_id = lp.block_id
                    # Get station name
                    station = self.network.stations.get(lp.station_id)
                    if station:
                        destination_station_name = station.name
                    break

        return ATOInfo(
            path_id=path_id,
            destination_block_id=destination_block_id,
            destination_station_name=destination_station_name,
            path_heading=path_heading,
            required_cab=required_cab,
        )

    # -------------------------------------------------------------------------
    # Station Detection
    # -------------------------------------------------------------------------

    def _is_at_station(self) -> bool:
        """
        Check if the train is currently at a station (for state transition).

        A train is "at station" if:
        - It's in a block that contains a station
        - It's stopped (speed near zero)
        - The station is a scheduled stop on its route

        Returns:
            True if train is at a scheduled station stop
        """
        train = self._get_train()
        if not train or not self.network:
            return False

        # Must be stopped or nearly stopped
        if train.current_speed > STOPPED_SPEED_THRESHOLD_KMH:
            return False

        # Check if current block has a station
        current_block_id = self._get_current_block_id()
        if not current_block_id:
            return False

        station_id = self._get_station_in_block(current_block_id)
        if not station_id:
            return False

        # Check if this station is a scheduled stop on the train's route
        if train.assigned_route_id and hasattr(self.network, "routes"):
            route = self.network.routes.get(train.assigned_route_id)
            if route:
                # Check if station is in the route's waypoints
                for waypoint in route.waypoints:
                    if (
                        hasattr(waypoint, "station_id")
                        and waypoint.station_id == station_id
                    ):
                        return True

        # If no route, consider any station as a potential stop
        return True

    # -------------------------------------------------------------------------
    # ATO (Automatic Train Operation) Logic
    # -------------------------------------------------------------------------

    def _ato_calculate_handle(self) -> HandlePosition:
        """
        ATO automatically selects appropriate handle position.

        The ATO system:
        1. Respects ATC limits (never exceeds)
        2. Follows scheduled speeds
        3. Brakes smoothly for stops
        """
        train = self._get_train()
        if not train:
            return HandlePosition.N

        current_speed = train.current_speed

        # Get effective target (ATC-limited)
        effective_target = min(
            train.max_speed,
            self.atc_speed_limit,
            self.current_block_speed_limit,
        )

        # Need to stop (ATC limit = 0 or no path)
        if effective_target <= STOPPED_SPEED_THRESHOLD_KMH:
            if current_speed > STOPPED_SPEED_THRESHOLD_KMH:
                return self._calculate_braking_notch(current_speed)
            return HandlePosition.N

        # Check distance to next stop for smooth braking
        profile = self._get_speed_profile()
        braking_distance = profile.get_braking_distance_cm(current_speed)

        # If we need to brake soon, start braking
        if self.clearance_ahead_cm < braking_distance * 1.5:
            return self._calculate_braking_notch(current_speed)

        # Speed tolerance for coasting
        speed_tolerance = 2.0  # km/h

        # At target speed - coast
        if abs(current_speed - effective_target) < speed_tolerance:
            return HandlePosition.N

        # Below target - accelerate
        if current_speed < effective_target:
            return self._calculate_power_notch(current_speed, effective_target)

        # Above target - brake
        return self._calculate_braking_notch_for_speed(current_speed, effective_target)

    def _calculate_power_notch(
        self, current_speed: float, target_speed: float
    ) -> HandlePosition:
        """Calculate appropriate power notch for acceleration."""
        speed_diff = target_speed - current_speed

        if speed_diff > 30:
            return HandlePosition.P5
        elif speed_diff > 20:
            return HandlePosition.P4
        elif speed_diff > 10:
            return HandlePosition.P3
        elif speed_diff > 5:
            return HandlePosition.P2
        else:
            return HandlePosition.P1

    def _calculate_braking_notch(self, current_speed: float) -> HandlePosition:
        """Calculate appropriate braking notch to stop."""
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

    def _calculate_braking_notch_for_speed(
        self, current_speed: float, target_speed: float
    ) -> HandlePosition:
        """Calculate braking notch to reduce to target speed."""
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

    # -------------------------------------------------------------------------
    # Power Commands
    # -------------------------------------------------------------------------
    # Notification Methods
    # -------------------------------------------------------------------------

    def get_and_clear_notifications(self) -> List[str]:
        """
        Get pending notifications and clear them.

        Called by frontend to retrieve toast messages.

        Returns:
            List of notification messages
        """
        notifications = self.pending_notifications.copy()
        self.pending_notifications.clear()
        return notifications

    # -------------------------------------------------------------------------
    # Power Commands
    # -------------------------------------------------------------------------

    def power_on_cmd(self) -> CommandResult:
        """
        Turn on train power.

        Rules:
        - Train must be in OFF state
        - Transitions to IDLE state
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if self.power_on:
            return CommandResult(False, "Power already on")

        self.power_on = True
        self.state = TrainState.IDLE
        train.state = TrainState.IDLE
        self.handle_position = HandlePosition.N

        return CommandResult(True, "Power on")

    def power_off_cmd(self) -> CommandResult:
        """
        Turn off train power.

        Rules:
        - Train must be stopped
        - Transitions to OFF state
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not train.is_stopped():
            return CommandResult(False, "Cannot power off while moving")

        self.power_on = False
        self.active_cab = None
        self.ato_running = False
        self.state = TrainState.OFF
        train.state = TrainState.OFF

        return CommandResult(True, "Power off")

    def emergency_stop_cmd(self) -> CommandResult:
        """
        Activate emergency stop.

        Always allowed - safety override.
        Sets handle to EB, triggers maximum braking.
        """
        train = self._get_train()

        self.eb_active = True
        self.handle_position = HandlePosition.EB
        self.ato_running = False

        if train:
            train.emergency_brake = True

        return CommandResult(True, "Emergency stop activated")

    def release_emergency_cmd(self) -> CommandResult:
        """
        Release emergency stop.

        Rules:
        - Train must be stopped
        - Handle must be in N, EB, or B position
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not train.is_stopped():
            return CommandResult(False, "Cannot release emergency while moving")

        brake_positions = [
            HandlePosition.N,
            HandlePosition.EB,
            HandlePosition.B1,
            HandlePosition.B2,
            HandlePosition.B3,
            HandlePosition.B4,
            HandlePosition.B5,
            HandlePosition.B6,
            HandlePosition.B7,
            HandlePosition.B8,
        ]
        if self.handle_position not in brake_positions:
            return CommandResult(
                False, "Handle must be in N, EB, or B before releasing emergency"
            )

        self.eb_active = False
        if train:
            train.emergency_brake = False
        self.handle_position = HandlePosition.N

        return CommandResult(True, "Emergency stop released")

    # -------------------------------------------------------------------------
    # Handle Commands
    # -------------------------------------------------------------------------

    def set_handle(self, position: HandlePosition) -> CommandResult:
        """
        Set throttle/brake handle position.

        Rules:
        - Must be powered on
        - In manual mode: directly controls acceleration/braking
        - In ATO mode: handle is controlled by computer (read-only to user)
        - EB always allowed (safety override)
        """
        if not self.power_on:
            return CommandResult(False, "Power is off")

        if position == HandlePosition.EB:
            return self.emergency_stop_cmd()

        if self.eb_active:
            brake_positions = [
                HandlePosition.N,
                HandlePosition.B1,
                HandlePosition.B2,
                HandlePosition.B3,
                HandlePosition.B4,
                HandlePosition.B5,
                HandlePosition.B6,
                HandlePosition.B7,
                HandlePosition.B8,
            ]
            if position in brake_positions:
                return self.release_emergency_cmd()
            return CommandResult(False, "Release emergency stop first")

        if self.control_mode == "ato" and self.ato_running:
            return CommandResult(False, "Handle controlled by ATO while running")

        self.handle_position = position
        return CommandResult(True, f"Handle set to {position.value}")

    # -------------------------------------------------------------------------
    # Cab & Mode Commands
    # -------------------------------------------------------------------------

    def set_cab(self, cab: Optional[str]) -> CommandResult:
        """
        Set active cab. Flips facing_direction when switching cabs.

        Rules:
        - Train must be stopped (speed = 0)
        - Setting cab to 'A' or 'B' transitions to STANDBY
        - Setting cab to None transitions to IDLE
        - Switching from A to B (or vice versa) flips facing_direction
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not train.is_stopped():
            return CommandResult(False, "Cannot switch cab while moving")

        old_cab = self.active_cab

        # If switching between A and B, flip facing direction
        if old_cab in ("A", "B") and cab in ("A", "B") and old_cab != cab:
            if train.facing_direction:
                train.facing_direction = train.facing_direction.opposite()

        self.active_cab = cab

        # Update train state
        if cab is None:
            self.state = TrainState.IDLE
            train.state = TrainState.IDLE
            train.active_cab = None
        else:
            self.state = TrainState.STANDBY
            train.state = TrainState.STANDBY
            train.active_cab = cab
            if train.position:
                train.position.leading_end = cab.lower()

        return CommandResult(True, f"Cab set to {cab}")

    def set_mode(self, mode: str) -> CommandResult:
        """
        Set control mode (ATO or Manual).

        Rules:
        - Train must be stopped
        - 'ato' -> TrainState stays STANDBY (or IDLE if no cab)
        - 'manual' -> TrainState becomes MANUAL
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not train.is_stopped():
            return CommandResult(False, "Cannot switch mode while moving")

        if mode not in ("ato", "manual"):
            return CommandResult(False, f"Invalid mode: {mode}")

        self.control_mode = mode

        if mode == "manual":
            self.state = TrainState.MANUAL
            train.state = TrainState.MANUAL
            self.ato_running = False
        else:
            # Return to STANDBY or IDLE based on cab
            if self.active_cab:
                self.state = TrainState.STANDBY
                train.state = TrainState.STANDBY
            else:
                self.state = TrainState.IDLE
                train.state = TrainState.IDLE

        return CommandResult(True, f"Mode set to {mode}")

    def set_travel_direction(self, direction: str) -> CommandResult:
        """
        Set travel direction (forward/reverse) in manual mode.

        Rules:
        - Must be in manual mode
        - Train must be stopped
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if self.control_mode != "manual":
            return CommandResult(False, "Not in manual mode")

        if not train.is_stopped():
            return CommandResult(False, "Cannot change direction while moving")

        if direction not in ("forward", "reverse"):
            return CommandResult(False, f"Invalid direction: {direction}")

        self.travel_direction = direction
        train.travel_direction = direction

        return CommandResult(True, f"Travel direction set to {direction}")

    # -------------------------------------------------------------------------
    # ATO Commands
    # -------------------------------------------------------------------------

    def ato_go(self) -> CommandResult:
        """
        Start ATO movement toward destination.

        Rules:
        - Must be in ATO mode
        - Active cab must be set
        - Train must be in STANDBY state
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if self.control_mode != "ato":
            return CommandResult(False, "Not in ATO mode")

        if self.active_cab is None:
            return CommandResult(False, "No cab selected")

        if self.state != TrainState.STANDBY:
            return CommandResult(False, f"Train not in STANDBY (is {self.state.value})")

        self.state = TrainState.RUNNING_MOVING
        train.state = TrainState.RUNNING_MOVING
        self.ato_running = True

        return CommandResult(True, "ATO started")

    def ato_stop(self) -> CommandResult:
        """
        Stop ATO movement (service stop, not emergency).

        Train will decelerate to stop, then return to STANDBY.
        """
        if not self.ato_running:
            return CommandResult(False, "ATO not running")

        self.ato_running = False
        # ATO controller will see ato_running=False and apply braking

        return CommandResult(True, "ATO stopping")

    # -------------------------------------------------------------------------
    # State Transitions
    # -------------------------------------------------------------------------

    def transition_to_waiting(self, reason: Optional[str] = None) -> None:
        """Transition to RUNNING_WAITING state."""
        train = self._get_train()
        self.state = TrainState.RUNNING_WAITING
        if train:
            train.state = TrainState.RUNNING_WAITING
            train.wait_reason = reason

    def transition_to_moving(self) -> None:
        """Transition to RUNNING_MOVING state."""
        train = self._get_train()
        self.state = TrainState.RUNNING_MOVING
        if train:
            train.state = TrainState.RUNNING_MOVING
            train.wait_reason = None

    def transition_to_at_station(self) -> None:
        """Transition to RUNNING_AT_STATION state."""
        train = self._get_train()
        self.state = TrainState.RUNNING_AT_STATION
        if train:
            train.state = TrainState.RUNNING_AT_STATION

    def transition_to_standby(self) -> None:
        """Transition to STANDBY state."""
        train = self._get_train()
        self.state = TrainState.STANDBY
        self.ato_running = False
        if train:
            train.state = TrainState.STANDBY
            train.wait_reason = None

    def transition_to_idle(self) -> None:
        """Transition to IDLE state (route complete, no direction)."""
        train = self._get_train()
        self.state = TrainState.IDLE
        self.ato_running = False
        self.active_cab = None
        if train:
            train.state = TrainState.IDLE
            train.active_cab = None
            train.facing_direction = None
            train.wait_reason = None
            train.assigned_route_id = None

    # -------------------------------------------------------------------------
    # Route Executor Commands (for RouteExecutor integration)
    # -------------------------------------------------------------------------

    def depart(self) -> bool:
        """
        Start moving (STANDBY -> RUNNING_MOVING).

        Used by RouteExecutor when path is acquired and train should start moving.
        Also starts ATO if in ATO mode.

        Returns:
            True if successful, False if preconditions not met
        """
        train = self._get_train()
        if train is None:
            return False

        if self.state != TrainState.STANDBY:
            return False

        if train.facing_direction is None:
            return False

        self.state = TrainState.RUNNING_MOVING
        train.state = TrainState.RUNNING_MOVING
        train.wait_reason = None

        # Start ATO if in ATO mode
        if self.control_mode == "ato":
            self.ato_running = True

        return True

    def wait_for_path(self, reason: Optional[str] = None) -> bool:
        """
        Stop and wait for path/signal (RUNNING_MOVING -> RUNNING_WAITING).

        Used by RouteExecutor when path is blocked.

        Args:
            reason: Reason for waiting (displayed to user)

        Returns:
            True if successful, False if not in correct state
        """
        train = self._get_train()
        if train is None:
            return False

        if self.state != TrainState.RUNNING_MOVING:
            return False

        self.state = TrainState.RUNNING_WAITING
        self.target_speed = 0.0
        if train:
            train.state = TrainState.RUNNING_WAITING
            train.target_speed = 0.0
            train.wait_reason = reason

        return True

    def arrive_at_station(self) -> bool:
        """
        Arrive at station (RUNNING_MOVING -> RUNNING_AT_STATION).

        Used by RouteExecutor when train reaches a waypoint station.

        Returns:
            True if successful, False if not in correct state
        """
        train = self._get_train()
        if train is None:
            return False

        if self.state != TrainState.RUNNING_MOVING:
            return False

        self.state = TrainState.RUNNING_AT_STATION
        self.target_speed = 0.0
        self.ato_running = False  # Stop ATO at station
        if train:
            train.state = TrainState.RUNNING_AT_STATION
            train.target_speed = 0.0
            train.wait_reason = None

        return True

    def depart_from_station(self, has_more_waypoints: bool = True) -> bool:
        """
        Depart from station after dwell time.

        Used by RouteExecutor after dwell time completes.

        If has_more_waypoints is True, transitions to STANDBY.
        If False (route complete), transitions to IDLE and clears route assignment.

        Args:
            has_more_waypoints: Whether route has more waypoints

        Returns:
            True if successful, False if not in correct state
        """
        train = self._get_train()
        if train is None:
            return False

        if self.state != TrainState.RUNNING_AT_STATION:
            return False

        if has_more_waypoints:
            self.state = TrainState.STANDBY
            if train:
                train.state = TrainState.STANDBY
        else:
            # Route complete - unassign route and go to IDLE
            self.state = TrainState.IDLE
            self.ato_running = False
            if train:
                train.state = TrainState.IDLE
                train.assigned_route_id = None
                train.target_speed = 0.0

        if train:
            train.wait_reason = None

        return True

    def turn_around(self) -> bool:
        """
        Flip facing direction by switching cab. Only allowed when stopped.

        Unlike set_cab(), this method is used when the train needs to reverse
        direction to reach the next waypoint. It:
        - Flips the facing direction
        - Switches the active cab (A <-> B)
        - Updates leading_end to match new cab

        Does NOT swap end_a/end_b position data - those stay with physical ends.

        Returns:
            True if successful, False if preconditions not met

        Preconditions:
        - Train must be stopped
        - ATO must not be actively running (ato_running == False)
        - Train must have a facing direction
        """
        train = self._get_train()
        if train is None:
            return False

        if not train.is_stopped():
            return False

        if self.ato_running:
            return False

        if train.facing_direction is None:
            return False

        # Flip facing direction
        train.facing_direction = train.facing_direction.opposite()

        # Also swap active cab designation
        if self.active_cab == "A":
            self.active_cab = "B"
            train.active_cab = "B"
            if train.position:
                train.position.leading_end = "b"
        elif self.active_cab == "B":
            self.active_cab = "A"
            train.active_cab = "A"
            if train.position:
                train.position.leading_end = "a"

        return True

    def set_direction_and_standby(self, direction: Direction) -> bool:
        """
        Set direction and transition to STANDBY (IDLE -> STANDBY).

        Used by RouteExecutor when activating a route for a train.

        Args:
            direction: Facing direction to set

        Returns:
            True if successful, False if not in IDLE state
        """
        train = self._get_train()
        if train is None:
            return False

        if self.state != TrainState.IDLE:
            return False

        train.facing_direction = direction
        self.active_cab = "A"  # Setting direction implies Cab A is leading
        train.active_cab = "A"
        if train.position:
            train.position.leading_end = "a"

        self.state = TrainState.STANDBY
        train.state = TrainState.STANDBY

        return True

    def clear_direction(self) -> bool:
        """
        Clear facing direction and return to IDLE state (STANDBY -> IDLE).

        Allowed only when stopped and in STANDBY (or MANUAL when stopped).

        Returns:
            True if successful, False if not allowed
        """
        train = self._get_train()
        if train is None:
            return False

        if not train.is_stopped():
            return False

        if self.state not in (TrainState.STANDBY, TrainState.MANUAL):
            return False

        train.facing_direction = None
        train.travel_direction = "forward"
        train.manual_target_speed = 0.0
        train.target_speed = 0.0
        self.active_cab = None
        train.active_cab = None

        self.state = TrainState.IDLE
        train.state = TrainState.IDLE

        return True

    def clear_to_move(self) -> bool:
        """
        Resume movement (RUNNING_WAITING -> RUNNING_MOVING).

        Used when path/signal clears and train can proceed.

        Returns:
            True if successful, False if not in correct state
        """
        train = self._get_train()
        if train is None:
            return False

        if self.state != TrainState.RUNNING_WAITING:
            return False

        self.state = TrainState.RUNNING_MOVING
        if train:
            train.state = TrainState.RUNNING_MOVING
            train.wait_reason = None

        # Resume ATO if in ATO mode
        if self.control_mode == "ato":
            self.ato_running = True

        return True

    def enable_manual(self) -> bool:
        """
        Enable manual control mode. Must be stopped.

        Returns:
            True if successful, False if moving or OFF
        """
        train = self._get_train()
        if train is None:
            return False

        if not train.is_stopped():
            return False

        if self.state == TrainState.OFF:
            return False

        self.state = TrainState.MANUAL
        self.control_mode = "manual"
        self.ato_running = False
        if train:
            train.state = TrainState.MANUAL

        return True

    def disable_manual(self) -> bool:
        """
        Disable manual control mode. Must be stopped.

        Returns to STANDBY if facing_direction set, else IDLE.

        Returns:
            True if successful, False if not in MANUAL mode or moving
        """
        train = self._get_train()
        if train is None:
            return False

        if self.state != TrainState.MANUAL:
            return False

        if not train.is_stopped():
            return False

        self.control_mode = "ato"

        if train.facing_direction is not None:
            self.state = TrainState.STANDBY
            train.state = TrainState.STANDBY
        else:
            self.state = TrainState.IDLE
            train.state = TrainState.IDLE

        train.manual_target_speed = 0.0

        return True

    # -------------------------------------------------------------------------
    # Serialization
    # -------------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Convert controller state to dictionary for JSON serialization."""
        train = self._get_train()

        # Helper to convert infinity to None for JSON serialization
        def safe_float(value: float) -> Optional[float]:
            if value == float("inf") or value == float("-inf"):
                return None
            return value

        return {
            "train_id": self.train_id,
            "power_on": self.power_on,
            "eb_active": self.eb_active,
            "emergency_stop_active": self.eb_active,
            "atc_warning_active": self.atc_warning_active,
            "pending_notifications": self.pending_notifications.copy(),
            "active_cab": self.active_cab,
            "control_mode": self.control_mode,
            "travel_direction": self.travel_direction,
            "state": self.state.value,
            "target_speed": self.target_speed,
            "handle_position": self.handle_position.value,
            "ato_running": self.ato_running,
            "ato_destination_id": self.ato_destination_id,
            "ato_path_id": self.ato_path_id,
            "clearance_ahead_cm": safe_float(self.clearance_ahead_cm),
            "atc_speed_limit": safe_float(self.atc_speed_limit),
            "current_block_speed_limit": safe_float(self.current_block_speed_limit),
            "speed_limit_reason": self.speed_limit_reason,
            # Include lookahead for display
            "lookahead": [
                {
                    "distance_cm": lp.distance_cm,
                    "speed_limit_kmh": safe_float(lp.speed_limit_kmh),
                    "block_id": lp.block_id,
                    "signal_state": lp.signal_state.value if lp.signal_state else None,
                    "station_id": lp.station_id,
                    "is_path_end": lp.is_path_end,
                }
                for lp in self.lookahead
            ],
            # Include ATO info
            "ato_info": {
                "path_id": self.ato_info.path_id,
                "destination_block_id": self.ato_info.destination_block_id,
                "destination_station_name": self.ato_info.destination_station_name,
                "path_heading": self.ato_info.path_heading,
                "required_cab": self.ato_info.required_cab,
            }
            if self.ato_info
            else None,
            # Include some train state for convenience
            "current_speed": train.current_speed if train else 0.0,
            "is_stopped": train.is_stopped() if train else True,
        }

    @classmethod
    def from_dict(
        cls, data: Dict[str, Any], network: "RailwayNetwork"
    ) -> "TrainController":
        """Reconstruct from dictionary after deserialization."""
        handle_str = data.get("handle_position", "N")
        try:
            handle = HandlePosition(handle_str)
        except ValueError:
            handle = HandlePosition.N

        state_str = data.get("state", "off")
        try:
            state = TrainState(state_str)
        except ValueError:
            state = TrainState.OFF

        return cls(
            train_id=data["train_id"],
            network=network,
            power_on=data.get("power_on", False),
            eb_active=data.get("eb_active", data.get("emergency_stop_active", False)),
            atc_warning_active=data.get("atc_warning_active", False),
            active_cab=data.get("active_cab"),
            control_mode=data.get("control_mode", "ato"),
            travel_direction=data.get("travel_direction", "forward"),
            state=state,
            target_speed=data.get("target_speed", 0.0),
            handle_position=handle,
            ato_running=data.get("ato_running", False),
            ato_destination_id=data.get("ato_destination_id"),
            ato_path_id=data.get("ato_path_id"),
        )


# =============================================================================
# Speed Calculation Functions (from train_onboard.py)
# =============================================================================


def get_acceleration_for_handle(
    handle: HandlePosition,
    acceleration_rate: float,
    deceleration_rate: float,
    emergency_brake_rate: float,
) -> float:
    """
    Get acceleration/deceleration rate for current handle position.

    Args:
        handle: Current handle position
        acceleration_rate: Maximum acceleration rate (km/h/s)
        deceleration_rate: Maximum service deceleration rate (km/h/s)
        emergency_brake_rate: Emergency braking rate (km/h/s)

    Returns:
        Positive value for acceleration (km/h/s)
        Negative value for deceleration (km/h/s)
        Zero for neutral (coast)
    """
    # Power notches (acceleration)
    if handle in POWER_MULTIPLIERS:
        return acceleration_rate * POWER_MULTIPLIERS[handle]

    # Neutral (coast)
    if handle == HandlePosition.N:
        return 0.0

    # Brake notches (deceleration)
    if handle in BRAKE_MULTIPLIERS:
        return -deceleration_rate * BRAKE_MULTIPLIERS[handle]

    # Emergency brake
    if handle == HandlePosition.EB:
        return -emergency_brake_rate

    return 0.0


def update_speed_from_handle(
    current_speed: float,
    handle: HandlePosition,
    acceleration_rate: float,
    deceleration_rate: float,
    emergency_brake_rate: float,
    delta_seconds: float,
    max_speed: float,
    atc_limit: float,
) -> float:
    """
    Calculate new speed based on handle position and time elapsed.

    Args:
        current_speed: Current speed in km/h
        handle: Current handle position
        acceleration_rate: Maximum acceleration rate (km/h/s)
        deceleration_rate: Maximum service deceleration rate (km/h/s)
        emergency_brake_rate: Emergency braking rate (km/h/s)
        delta_seconds: Time since last update
        max_speed: Train's maximum speed
        atc_limit: ATC-enforced speed limit

    Returns:
        New speed in km/h
    """
    acceleration = get_acceleration_for_handle(
        handle, acceleration_rate, deceleration_rate, emergency_brake_rate
    )

    # Calculate effective speed limit (min of max_speed and atc_limit)
    effective_limit = min(max_speed, atc_limit)

    # Apply acceleration/deceleration
    new_speed = current_speed + (acceleration * delta_seconds)

    # Clamp to valid range
    new_speed = max(0.0, min(new_speed, effective_limit))

    # If accelerating and at/above limit, don't accelerate further
    if acceleration > 0 and current_speed >= effective_limit:
        new_speed = current_speed

    return new_speed


# =============================================================================
# Factory function
# =============================================================================


def create_controller_for_train(
    train_id: str, network: "RailwayNetwork"
) -> TrainController:
    """
    Create a TrainController instance for a train.

    Args:
        train_id: ID of the train to control
        network: Railway network

    Returns:
        TrainController instance
    """
    return TrainController(train_id=train_id, network=network)
