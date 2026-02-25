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
    LeadingEnd,
    SignalState,
    TrainState,
)
from model_scale import get_speed_conversion_factor

if TYPE_CHECKING:
    from network import RailwayNetwork
    from path_dispatcher import PathDispatcher
    from speed_profile import SpeedProfile
    from train import Train


logger = logging.getLogger(__name__)

OPPOSITE_EDGES = {"N": "S", "S": "N", "E": "W", "W": "E"}


ATC_SAFETY_MARGIN_CM = 25.0
ATC_PLATFORM_SAFETY_MARGIN_CM = 5.0
ATC_SIGNAL_LOOKAHEAD_BLOCKS = 3
ATC_MIN_CLEARANCE_CM = 10.0
ATC_NO_BLOCK_FALLBACK_CM = 100000.0
YELLOW_SIGNAL_SPEED_KMH = 40.0
STOPPED_SPEED_THRESHOLD_KMH = 0.5

SPEED_TREND_EPSILON = 0.1
ATO_SPEED_TOLERANCE = 2.0
ATO_ENTRY_MARGIN = ATO_SPEED_TOLERANCE - 1.0
ATO_EXIT_MARGIN = ATO_SPEED_TOLERANCE + 1.0

# Speed curve constants
CURVE_SAMPLE_RESOLUTION_CM = (
    3.0  # 3cm resolution for speed curves (balance between precision and performance)
)
ATO_SPEED_MARGIN_KMH = 5.0  # ATO stays 5 km/h below ATC limit


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
    path_dispatcher: Optional["PathDispatcher"] = None

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
    ato_destination_block_id: Optional[str] = None  # Destination block ID (not station)
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

    _previous_speed: float = 0.0
    handle_reason: str = ""  # Diagnostic: reason for current handle position

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
            # NOTE: We no longer auto-correct cab during tick().
            # Previously, this would partially update cab (leading_end only) without
            # flipping facing_direction and entry edges, causing state corruption.
            # Cab selection should be done:
            # 1. Manually by the operator before starting ATO
            # 2. Automatically in ato_go() when cab is None (using required_cab)
            # The operator can freely change cab when ato_running=False.

            # Calculate handle position based on ato_running
            if self.ato_running:
                # ATO is running - calculate handle to reach target speed
                self.handle_position = self._ato_calculate_handle()
                logger.debug(
                    "[TrainController %s] ATO handle=%s target=%.2f",
                    self.train_id,
                    self.handle_position.value,
                    self.target_speed,
                )
            else:
                # ATO stopped - brake to stop, then transition to STANDBY
                self.handle_position = self._calculate_braking_notch_for_speed(
                    train.current_speed, 0.0
                )
                if train.current_speed <= STOPPED_SPEED_THRESHOLD_KMH:
                    self.transition_to_standby()
                    self.handle_position = HandlePosition.N
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
        # In ATO mode: if ato_running=False, target is 0 (braking to stop)
        if self.control_mode == "ato" and not self.ato_running:
            self.target_speed = 0.0
        else:
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
                # ato_running stays as-is - if user stopped ATO, it stays stopped
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

        When reserved_blocks is empty but ato_destination_id is set (path
        completed but still approaching station), calculates distance to
        the station stopping point using current_path.

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

        # If no blocks ahead but ato_destination_block_id is set, use path-based
        # clearance calculation to find distance to destination block end
        if not blocks_ahead and self.ato_destination_block_id:
            clearance = self._calculate_clearance_to_destination(clearance)

        return clearance

    def _calculate_clearance_to_destination(self, base_clearance: float) -> float:
        """
        Calculate clearance to ato_destination_block_id.

        Handles two scenarios:
        1. Train has current_path: uses path to trace ahead to destination block
        2. Train already IN destination block (path cleared): calculates remaining
           distance to end of current block, considering station position if present

        If the destination block contains a station, stops at the end of station.

        Args:
            base_clearance: Already calculated clearance from current block

        Returns:
            Total clearance to destination stopping point
        """
        train = self._get_train()
        if not train or not self.network:
            return base_clearance

        if not self.ato_destination_block_id:
            return base_clearance

        path_tiles = self.get_path_tiles()
        if path_tiles is None:
            path_tiles = train.current_path
        dest_block_id = self.ato_destination_block_id
        current_block_id = self._get_current_block_id()

        # Check if we're already in the destination block
        if current_block_id == dest_block_id:
            return self._calculate_clearance_to_end_of_current_block()

        # Not in destination block yet - need current_path to trace forward
        if not path_tiles:
            return base_clearance

        # Check if destination block has a station (for precise stopping)
        station_id = self._get_station_in_block(dest_block_id)
        station_positions: set = set()
        if station_id:
            station = self.network.platforms.get(station_id)
            if station:
                station_positions = set(station.positions)

        # Get head tile and path index
        head_tile = self._get_head_tile()
        if not head_tile:
            return base_clearance

        head_index = self._get_head_path_index(train, head_tile)
        if head_index < 0:
            return base_clearance

        # Sum distances from current position to end of destination block
        clearance = base_clearance
        in_dest_block = False

        # Determine scan direction so we iterate toward the train's
        # actual travel direction, not always forward in the array.
        scan_step = self._get_path_scan_step(train, head_index)
        if scan_step == -1:
            ahead_entries = list(reversed(path_tiles[:head_index]))
        else:
            ahead_entries = list(path_tiles[head_index + 1 :])

        # Start from next tile after head (in travel direction)
        for entry_idx, path_entry in enumerate(ahead_entries):
            tile_x, tile_y = path_entry[0], path_entry[1]
            tile_pos = (tile_x, tile_y)

            # Get track at this tile
            track = self.network.get_track(tile_x, tile_y)
            if not track:
                break

            # Check if this tile's block is obstructed (occupied by another train)
            if track.block_section_id and track.block_section_id != current_block_id:
                block = self.network.blocks.get(track.block_section_id)
                if block and self._is_block_obstructed(block, track.block_section_id):
                    break
                current_block_id = track.block_section_id

            # Track if we've entered the destination block
            if track.block_section_id == dest_block_id:
                in_dest_block = True

            # Add tile length
            tile_length = self.network._get_tile_length_cm(tile_x, tile_y)
            clearance += tile_length

            # If destination has a station, stop at end of station tiles
            if station_positions and tile_pos in station_positions:
                # Check if this is the last station tile in the remaining
                # ahead entries (direction-aware).
                remaining_ahead = ahead_entries[entry_idx + 1 :]
                has_more_station_tiles = any(
                    (p[0], p[1]) in station_positions for p in remaining_ahead
                )
                if not has_more_station_tiles:
                    # This is the last station tile - stopping point reached
                    break
            # If no station, stop at end of destination block
            elif in_dest_block and track.block_section_id != dest_block_id:
                # We've exited the destination block
                break

        return clearance

    def _calculate_clearance_to_end_of_current_block(self) -> float:
        """
        Calculate remaining distance to end of current block.

        Used when train is already in the destination block. Considers station
        position for precise stopping at end of station tiles.

        Returns:
            Remaining distance in cm to the stopping point
        """
        train = self._get_train()
        if not train or not self.network:
            return 0.0

        current_block_id = self._get_current_block_id()
        if not current_block_id:
            return 0.0

        block = self.network.blocks.get(current_block_id)
        if not block:
            return 0.0

        head_tile = self._get_head_tile()
        if not head_tile:
            return block.length_cm if block.length_cm else 0.0

        # Check if this block has a station
        station_id = self._get_station_in_block(current_block_id)
        station_positions: set = set()
        if station_id:
            station = self.network.platforms.get(station_id)
            if station:
                station_positions = set(station.positions)

        head_distance_cm = train.position.head_distance_cm if train.position else 0.0

        block_positions = self.network.get_block_positions(current_block_id)
        if not block_positions:
            # Fall back to simple remaining calculation
            tile_length = self.network._get_tile_length_cm(*head_tile)
            return max(0.0, tile_length - head_distance_cm)

        # Sort block positions by travel direction
        # This is critical for multi-tile stations to ensure we calculate
        # remaining distance in the correct order
        travel_direction = self._get_travel_direction()
        if travel_direction:
            block_positions = self._sort_positions_by_direction(
                block_positions, travel_direction
            )

        # Find current position within block
        try:
            current_idx = block_positions.index(head_tile)
        except ValueError:
            current_idx = -1

        if current_idx < 0:
            # Head tile not in block positions - use fallback
            tile_length = self.network._get_tile_length_cm(*head_tile)
            return max(0.0, tile_length - head_distance_cm)

        # Calculate remaining distance
        remaining = 0.0

        for i in range(current_idx, len(block_positions)):
            tile = block_positions[i]
            tile_length = self.network._get_tile_length_cm(*tile)

            if i == current_idx:
                # Current tile: remaining from current position to end
                remaining += max(0.0, tile_length - head_distance_cm)
            else:
                # Subsequent tiles: full length
                remaining += tile_length

            # Check if this is the last station tile (for station blocks)
            if station_positions and tile in station_positions:
                # Check if there are more station tiles after this
                has_more_station_tiles = any(
                    block_positions[j] in station_positions
                    for j in range(i + 1, len(block_positions))
                )
                if not has_more_station_tiles:
                    # Stop at end of this tile (last station tile)
                    break

        return remaining

    def _get_travel_direction(self) -> Optional[Direction]:
        """Get the train's current travel direction from path or facing direction."""
        train = self._get_train()
        if not train or not train.position:
            return None

        # First, try to get direction from current_path
        path_tiles = self.get_path_tiles()
        if path_tiles is None:
            path_tiles = train.current_path
        if path_tiles and len(path_tiles) > 0:
            head_tile = self._get_head_tile()
            if head_tile:
                # Find current position in path
                for i, path_entry in enumerate(path_tiles):
                    if (path_entry[0], path_entry[1]) == head_tile:
                        # Return direction from this path entry
                        if len(path_entry) > 2:
                            return path_entry[2]
                        # Or infer from next tile
                        if i + 1 < len(path_tiles):
                            next_tile = path_tiles[i + 1]
                            dx = next_tile[0] - path_entry[0]
                            dy = next_tile[1] - path_entry[1]
                            if dy > 0:
                                return Direction.NORTH
                            elif dy < 0:
                                return Direction.SOUTH
                            elif dx > 0:
                                return Direction.EAST
                            elif dx < 0:
                                return Direction.WEST
                        break

        # Fall back to facing direction
        return train.position.facing_direction

    def _sort_positions_by_direction(
        self,
        positions: List[Tuple[int, int]],
        direction: Direction,
    ) -> List[Tuple[int, int]]:
        """Sort positions in the given travel direction.

        For multi-tile blocks, we need to process tiles in the order
        the train will encounter them, not in arbitrary insertion order.

        Args:
            positions: List of (x, y) positions
            direction: Travel direction

        Returns:
            Positions sorted by travel direction
        """
        if not positions:
            return positions

        # Create a copy to avoid mutating the original
        positions = list(positions)

        if direction == Direction.NORTH:
            # Traveling north: sort by y ascending
            positions.sort(key=lambda p: p[1])
        elif direction == Direction.SOUTH:
            # Traveling south: sort by y descending
            positions.sort(key=lambda p: -p[1])
        elif direction == Direction.EAST:
            # Traveling east: sort by x ascending
            positions.sort(key=lambda p: p[0])
        elif direction == Direction.WEST:
            # Traveling west: sort by x descending
            positions.sort(key=lambda p: -p[0])

        return positions

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

        The safety margin is constant and determined by the destination type:
        - 5cm if destination (ato_destination_id) is a station
        - 25cm otherwise

        This does NOT change as the train moves - it's fixed when the path
        is assigned based on where the train is going.

        Returns:
            5cm if destination is a station/platform, 25cm otherwise
        """
        train = self._get_train()
        if not train or not self.network:
            return self.safety_margin_cm

        # Check if destination block contains a station - use platform margin
        if self.ato_destination_block_id:
            station_id = self._get_station_in_block(self.ato_destination_block_id)
            if station_id:
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
        """Get the head tile for the train."""
        train = self._get_train()
        if not train:
            return None

        if train.position:
            return train.position.head_tile

        return None

    def _get_head_distance_cm(self) -> float:
        """Get head distance within the current tile in cm."""
        train = self._get_train()
        if not train:
            return 0.0

        if train.position:
            return train.position.head_distance_cm

        return 0.0

    def get_path_tiles(self) -> Optional[List[Tuple[int, int, "Direction"]]]:
        """Get path tiles from PathDispatcher (single source of truth).

        Reads Path.path_tiles from the dispatcher and converts to
        (x, y, Direction) format. Returns None if no active path exists.

        This replaces reading from train.current_path, which was a stale
        copy that didn't reflect path extensions or cancellations.
        """
        train = self._get_train()
        if not train or not train.assigned_path_id:
            return None

        if not self.path_dispatcher:
            # Fallback: no dispatcher reference, use train's copy
            return train.current_path

        path = self.path_dispatcher.get_path(train.assigned_path_id)
        if not path or not path.path_tiles:
            return None

        from path_utils import convert_tiles_to_train_path

        return convert_tiles_to_train_path(path.path_tiles)

    def _get_head_path_index(self, train: "Train", head_tile: Tuple[int, int]) -> int:
        """Get the path index for the train head tile."""
        path_tiles = self.get_path_tiles()
        if path_tiles is None:
            path_tiles = train.current_path
        if not path_tiles:
            return -1

        if 0 <= train.path_index < len(path_tiles):
            path_tile = path_tiles[train.path_index]
            if (path_tile[0], path_tile[1]) == head_tile:
                return train.path_index

        matches = [i for i, (x, y, _) in enumerate(path_tiles) if (x, y) == head_tile]
        if not matches:
            return -1

        if train.path_index >= 0:
            return min(matches, key=lambda i: abs(i - train.path_index))

        return matches[0]

    def _direction_between_tiles(
        self, from_tile: Tuple[int, int], to_tile: Tuple[int, int]
    ) -> Optional[Direction]:
        """Convert an adjacent tile-to-tile delta into a Direction."""
        dx = to_tile[0] - from_tile[0]
        dy = to_tile[1] - from_tile[1]

        if dx == 1 and dy == 0:
            return Direction.EAST
        if dx == -1 and dy == 0:
            return Direction.WEST
        if dx == 0 and dy == 1:
            return Direction.NORTH
        if dx == 0 and dy == -1:
            return Direction.SOUTH
        return None

    def _direction_to_edge(self, direction: Direction) -> Optional[str]:
        """Convert Direction enum to edge token (N/E/S/W)."""
        if direction == Direction.NORTH:
            return "N"
        if direction == Direction.EAST:
            return "E"
        if direction == Direction.SOUTH:
            return "S"
        if direction == Direction.WEST:
            return "W"
        return None

    def _get_head_entry_edge(self, train: "Train") -> Optional[str]:
        """Get the current head tile entry edge based on leading end."""
        if not train.position:
            return None

        if train.position.leading_end == "a":
            return train.position.end_a_entry_edge
        if train.position.leading_end == "b":
            return train.position.end_b_entry_edge
        return None

    def _can_traverse_path_step(
        self,
        train: "Train",
        from_tile: Tuple[int, int],
        to_tile: Tuple[int, int],
        entry_edge_into_from: Optional[str],
    ) -> Tuple[bool, Optional[str]]:
        """
        Check whether traversal from one path tile to the next is currently usable.

        Returns:
            (is_traversable, entry_edge_into_to_tile)
        """
        if not self.network:
            return False, None

        from_track = self.network.get_track(from_tile[0], from_tile[1])
        to_track = self.network.get_track(to_tile[0], to_tile[1])
        if not from_track or not to_track:
            return False, None

        direction = self._direction_between_tiles(from_tile, to_tile)
        if direction is None:
            return False, None

        exit_edge = self._direction_to_edge(direction)
        if not exit_edge:
            return False, None

        to_entry_edge = OPPOSITE_EDGES.get(exit_edge)
        if not to_entry_edge:
            return False, None

        assigned_path_id = train.assigned_path_id

        for tile_track in (from_track, to_track):
            if tile_track.is_switch and tile_track.is_locked_by_path():
                if (
                    not assigned_path_id
                    or tile_track.locked_by_path != assigned_path_id
                ):
                    return False, None

        if (
            to_track.block_section_id
            and self.network
            and to_track.block_section_id in self.network.blocks
        ):
            target_block = self.network.blocks[to_track.block_section_id]
            if target_block.reserved_by_path:
                if (
                    not assigned_path_id
                    or target_block.reserved_by_path != assigned_path_id
                ):
                    return False, None

        if entry_edge_into_from:
            from_exits = from_track.get_exits(
                entry_edge_into_from, use_switch_state=True
            )
            if exit_edge not in from_exits:
                return False, None
        elif exit_edge not in from_track.edges:
            return False, None

        to_exits = to_track.get_exits(to_entry_edge, use_switch_state=True)
        if not to_exits:
            return False, None

        return True, to_entry_edge

    def _get_path_scan_step(self, train: "Train", head_path_index: int) -> int:
        """
        Determine whether lookahead should scan path forward (+1) or backward (-1).

        Lookahead direction follows the active cab's facing direction, not the stored
        path order. This allows monitor lookahead to flip correctly when switching cab.
        """
        path_tiles = self.get_path_tiles()
        if path_tiles is None:
            path_tiles = train.current_path
        if not path_tiles:
            return 1

        path_length = len(path_tiles)
        if path_length <= 1:
            return 1

        facing_direction = (
            train.position.facing_direction
            if train.position and train.position.facing_direction
            else None
        )
        if facing_direction is None:
            if head_path_index >= path_length - 1 and head_path_index > 0:
                return -1
            return 1

        head_entry = path_tiles[head_path_index]
        head_tile = (head_entry[0], head_entry[1])

        next_index = head_path_index + 1
        if next_index < path_length:
            next_entry = path_tiles[next_index]
            next_direction = self._direction_between_tiles(
                head_tile, (next_entry[0], next_entry[1])
            )
            if next_direction == facing_direction:
                return 1

        prev_index = head_path_index - 1
        if prev_index >= 0:
            prev_entry = path_tiles[prev_index]
            prev_direction = self._direction_between_tiles(
                head_tile, (prev_entry[0], prev_entry[1])
            )
            if prev_direction == facing_direction:
                return -1

        # facing_direction didn't match either neighbor (common on diagonal /
        # curved tracks after a cab switch).  Fall back to destination-aware
        # logic: find which end of the path contains the destination block.
        dest_block_id = self.ato_destination_block_id or getattr(
            train, "ato_destination_block_id", None
        )
        if dest_block_id and self.network:
            # Find the first path tile belonging to the destination block,
            # scanning from both ends toward the head.
            dest_forward_idx: Optional[int] = None
            dest_backward_idx: Optional[int] = None
            for i in range(head_path_index + 1, path_length):
                pt = path_tiles[i]
                tk = self.network.get_track(pt[0], pt[1])
                if tk and tk.block_section_id == dest_block_id:
                    dest_forward_idx = i
                    break
            for i in range(head_path_index - 1, -1, -1):
                pt = path_tiles[i]
                tk = self.network.get_track(pt[0], pt[1])
                if tk and tk.block_section_id == dest_block_id:
                    dest_backward_idx = i
                    break
            if dest_forward_idx is not None and dest_backward_idx is None:
                return 1
            if dest_backward_idx is not None and dest_forward_idx is None:
                return -1
            # Both found — prefer the one further away (more of the path left)
            if dest_forward_idx is not None and dest_backward_idx is not None:
                if (
                    dest_forward_idx - head_path_index
                    >= head_path_index - dest_backward_idx
                ):
                    return 1
                return -1

        # No destination available — use path_index progression heuristic:
        # if the head is in the first half of the path, it's likely moving
        # forward (+1); in the second half, backward (-1).
        if head_path_index <= path_length // 2:
            return 1
        return -1

    def _build_empty_monitor_lookahead(
        self,
        *,
        head_distance_cm: float = 0.0,
        travel_direction: Optional[str] = None,
        speed_limit_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build a consistent empty lookahead payload for Train Monitor."""
        return {
            "train_id": self.train_id,
            "head_tile": None,
            "head_distance_cm": head_distance_cm,
            "travel_direction": travel_direction,
            "cab": self.active_cab,
            "speed_limit_reason": speed_limit_reason,
            "lookahead": {
                "blocks": [],
                "signals": [],
                "switches": [],
                "stations": [],
                "clearance_cm": 0.0,
                "speed_curves": [],
            },
        }

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

        path_tiles = self.get_path_tiles()
        if path_tiles is None:
            path_tiles = train.current_path
        if train.position and path_tiles:
            head_index = self._get_head_path_index(train, head_tile)
            if head_index < 0:
                return block.length_cm

            head_tile_length = self.network._get_tile_length_cm(*head_tile)
            remaining = max(0.0, head_tile_length - train.position.head_distance_cm)

            # Determine scan direction: when the train travels in the
            # reverse direction relative to the path array (scan_step=-1),
            # tiles "ahead" are at *lower* indices.  Scanning forward
            # (head_index+1:) in that case would count tiles *behind* the
            # train, producing a wrong remaining distance.
            scan_step = self._get_path_scan_step(train, head_index)
            if scan_step == -1:
                # Scan backward in path array (tiles ahead of the train)
                ahead_tiles = reversed(path_tiles[:head_index])
            else:
                ahead_tiles = path_tiles[head_index + 1 :]

            for x, y, *_ in ahead_tiles:
                track = self.network.get_track(x, y)
                if not track or track.block_section_id != current_block_id:
                    break
                remaining += self.network._get_tile_length_cm(x, y)

            return remaining

        block_positions = self.network.get_block_positions(current_block_id)
        if not block_positions:
            return block.length_cm

        # Sort block positions by travel direction to ensure correct calculation
        # for multi-tile stations regardless of insertion order
        travel_direction = self._get_travel_direction()
        if travel_direction:
            block_positions = self._sort_positions_by_direction(
                block_positions, travel_direction
            )

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

        Uses the train's current_path when available to determine which
        blocks lie ahead in the actual travel direction. This avoids
        mismatches when the path index is not aligned with the reserved
        block order (e.g., return paths starting mid-block).

        When no current_path is available, falls back to reserved_blocks.
        """
        train = self._get_train()
        if not train:
            return []

        path_tiles = self.get_path_tiles()
        blocks: List[str] = []

        current_block_id = self._get_current_block_id()

        if path_tiles:
            head_tile = self._get_head_tile()
            if head_tile:
                head_index = self._get_head_path_index(train, head_tile)
                if head_index >= 0:
                    scan_step = self._get_path_scan_step(train, head_index)
                    loop_start = head_index + scan_step
                    loop_end = len(path_tiles) if scan_step == 1 else -1

                    for i in range(loop_start, loop_end, scan_step):
                        path_entry = path_tiles[i]
                        track = self.network.get_track(path_entry[0], path_entry[1])
                        block_id = track.block_section_id if track else None
                        if not block_id or block_id == current_block_id:
                            continue
                        if block_id not in blocks:
                            blocks.append(block_id)

                    if blocks:
                        return blocks

        if train.reserved_blocks:
            if current_block_id:
                try:
                    idx = train.reserved_blocks.index(current_block_id)

                    # Determine travel direction relative to path array
                    scan_step = 1  # default: forward
                    head_tile = self._get_head_tile()
                    if head_tile and path_tiles:
                        head_index = self._get_head_path_index(train, head_tile)
                        if head_index >= 0:
                            scan_step = self._get_path_scan_step(train, head_index)

                    if scan_step == -1:
                        # Traveling backward through path array -- blocks
                        # ahead are at *lower* indices in reserved_blocks.
                        blocks = list(reversed(train.reserved_blocks[:idx]))
                    else:
                        blocks = train.reserved_blocks[idx + 1 :]
                except ValueError:
                    blocks = list(train.reserved_blocks)

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
        Check if there's a red signal protecting this block for the train's
        direction of travel.

        Only considers signals whose detection_direction matches the train's
        facing direction. A signal with detection_direction='E' monitors
        eastbound traffic and should not block a westbound train.
        """
        if not self.network:
            return False

        # Get the train's travel direction as an edge string ("N"/"E"/"S"/"W")
        train = self._get_train()
        travel_edge = None
        if train and train.facing_direction:
            travel_edge = self._direction_to_edge(train.facing_direction)

        for signal_id, signal in self.network.signals.items():
            if (
                hasattr(signal, "monitored_block_id")
                and signal.monitored_block_id == block_id
            ):
                if signal.state == SignalState.RED:
                    # If we know the train's direction, only consider signals
                    # that monitor traffic in that direction
                    if travel_edge and hasattr(signal, "detection_direction"):
                        if signal.detection_direction != travel_edge:
                            continue
                    return True

        return False

    def _is_approaching_yellow_signal(self) -> bool:
        """
        Check if train is approaching a yellow signal in its direction of travel.

        Only considers signals whose detection_direction matches the train's
        facing direction, preventing opposite-direction yellow signals from
        triggering unnecessary speed reductions.
        """
        if not self.network:
            return False

        # Get the train's travel direction as an edge string ("N"/"E"/"S"/"W")
        train = self._get_train()
        travel_edge = None
        if train and train.facing_direction:
            travel_edge = self._direction_to_edge(train.facing_direction)

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
                    # Only consider signals matching the train's travel direction
                    if travel_edge and hasattr(signal, "detection_direction"):
                        if signal.detection_direction != travel_edge:
                            continue
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

        path_tiles = self.get_path_tiles()

        # Path ID is helpful for display, but destination should remain available
        # even when the active path has already been cleared.
        path_id = train.assigned_path_id or self.ato_path_id

        destination_block_id = self.ato_destination_block_id or getattr(
            train, "ato_destination_block_id", None
        )

        if not path_id and not destination_block_id:
            return None

        # Get path from network
        path = None
        if hasattr(self.network, "paths"):
            path = self.network.paths.get(path_id)

        if path_id and not path and not destination_block_id:
            return ATOInfo(path_id=path_id)

        # Determine path heading and required cab by comparing
        # the path's travel direction with the train's facing direction.
        path_heading = "a"  # Default
        required_cab = self.active_cab or "A"  # Default to current cab

        # Determine required cab from current_path direction vs facing_direction.
        # Find the head tile in the path and look at the next tile to determine
        # which direction the path is heading. Compare with facing_direction
        # to decide if the train needs to switch cabs (turn around).
        if (
            path_tiles
            and len(path_tiles) >= 2
            and train.position
            and train.facing_direction is not None
        ):
            path_direction = self._get_path_direction_at_head(train)
            if path_direction is not None:
                if path_direction == train.facing_direction:
                    # Path goes same direction as train faces -> keep current cab
                    required_cab = self.active_cab or "A"
                elif path_direction == train.facing_direction.opposite():
                    # Path goes opposite to train's facing -> need opposite cab
                    # Use required_cab (defaults to active_cab or "A") for flip,
                    # since active_cab may be None when auto-selecting
                    required_cab = "B" if required_cab == "A" else "A"
                else:
                    # Path goes at 90 degrees at the head tile.  This can
                    # happen on diagonal staircase track where the first
                    # move is perpendicular but the overall trajectory is
                    # actually opposite to the train's facing direction.
                    #
                    # Resolve by computing the dominant direction of the
                    # overall path vector (first tile → last tile), using
                    # the axis with the larger displacement.  If *that*
                    # direction is opposite to the train's facing, switch
                    # cabs.  Otherwise keep the current cab.
                    overall_dir = self._dominant_direction_from_tiles(
                        path_tiles[0], path_tiles[-1]
                    )
                    if (
                        overall_dir is not None
                        and overall_dir == train.facing_direction.opposite()
                    ):
                        required_cab = "B" if required_cab == "A" else "A"
                    else:
                        required_cab = self.active_cab or "A"

        # Get destination info - simple logic:
        # 1. Use ato_destination_block_id if set (controller/train)
        # 2. Fallback: use current block in heading direction (no destination case)
        destination_station_name = None

        # If no destination block set, fallback to current block
        if not destination_block_id:
            destination_block_id = self._get_current_block_id()

        # Derive station name from destination block
        if destination_block_id:
            station_id = self._get_station_in_block(destination_block_id)
            if station_id:
                station = self.network.platforms.get(station_id)
                if station:
                    destination_station_name = station.name

        return ATOInfo(
            path_id=path_id,
            destination_block_id=destination_block_id,
            destination_station_name=destination_station_name,
            path_heading=path_heading,
            required_cab=required_cab,
        )

    # -------------------------------------------------------------------------
    # Path Direction Detection
    # -------------------------------------------------------------------------

    def _get_path_direction_at_head(self, train: "Train") -> Optional[Direction]:
        """Determine the path's travel direction at or near the train's head tile.

        Looks up the head tile in current_path. If the path entry has a stored
        direction (3rd element), uses that. Otherwise infers direction from the
        vector to the next tile in the path.

        If the head tile is the last tile in the path, looks backward to infer
        the direction from the previous tile.

        Falls back to using the first two tiles of the path if the head tile
        is not found in the path (e.g., during initial placement).

        Args:
            train: Train with current_path and position set.

        Returns:
            Direction the path is heading at the train's head, or None.
        """
        path_tiles = self.get_path_tiles()
        if not path_tiles or len(path_tiles) < 2:
            return None
        if not train.position:
            return None

        head_tile = train.position.head_tile
        if not head_tile:
            # Fallback: use first two tiles
            return self._direction_from_tiles(path_tiles[0], path_tiles[1])

        # Find head tile in path
        head_index = -1
        for i, entry in enumerate(path_tiles):
            if (entry[0], entry[1]) == head_tile:
                head_index = i
                break

        if head_index >= 0:
            # Found head in path - get direction from this entry or next tile
            entry = path_tiles[head_index]
            if len(entry) > 2 and entry[2] is not None:
                return entry[2]
            # Infer from next tile
            if head_index + 1 < len(path_tiles):
                return self._direction_from_tiles(entry, path_tiles[head_index + 1])
            # Head is last tile - infer from previous tile (same direction)
            if head_index > 0:
                return self._direction_from_tiles(path_tiles[head_index - 1], entry)

        # Head tile not found in path - fallback to first two tiles
        return self._direction_from_tiles(path_tiles[0], path_tiles[1])

    @staticmethod
    def _direction_from_tiles(tile_a: Tuple, tile_b: Tuple) -> Optional[Direction]:
        """Compute direction from tile_a to tile_b."""
        dx = tile_b[0] - tile_a[0]
        dy = tile_b[1] - tile_a[1]
        if dx > 0:
            return Direction.EAST
        elif dx < 0:
            return Direction.WEST
        elif dy > 0:
            return Direction.NORTH
        elif dy < 0:
            return Direction.SOUTH
        return None

    @staticmethod
    def _dominant_direction_from_tiles(
        tile_a: Tuple, tile_b: Tuple
    ) -> Optional[Direction]:
        """Compute the dominant cardinal direction from tile_a to tile_b.

        Unlike ``_direction_from_tiles`` (which prioritises dx), this method
        picks the axis with the larger absolute displacement.  This is useful
        for computing the *overall* direction of a multi-tile path where the
        start→end vector may be diagonal.

        Args:
            tile_a: Starting tile (x, y, ...).
            tile_b: Ending tile (x, y, ...).

        Returns:
            The cardinal Direction along the axis with greater displacement,
            or None if both displacements are zero.
        """
        dx = tile_b[0] - tile_a[0]
        dy = tile_b[1] - tile_a[1]
        if dx == 0 and dy == 0:
            return None
        if abs(dx) >= abs(dy):
            return Direction.EAST if dx > 0 else Direction.WEST
        else:
            return Direction.NORTH if dy > 0 else Direction.SOUTH

    # -------------------------------------------------------------------------
    # Station Detection
    # -------------------------------------------------------------------------

    def _is_at_station(self) -> bool:
        """
        Check if the train is currently at a station (for state transition).

        A train is "at station" if:
        - It's in a block that contains a station
        - It's stopped (speed near zero)
        - The station is a scheduled stop on its route OR is the ATO destination

        When ato_destination_block_id is set (manual path), the train should
        only stop at the destination block, not at intermediate or departure
        stations. This prevents a false-positive "arrival" at the departure
        station on the first tick after depart() when speed is still 0.

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
                # Check if platform is in the route's waypoints
                for waypoint in route.waypoints:
                    if waypoint.platform_id == station_id:
                        return True

        # If ato_destination_block_id is set (manual path), only stop at the
        # destination block. This prevents stopping at the departure station
        # or at intermediate stations along the path.
        if self.ato_destination_block_id:
            return current_block_id == self.ato_destination_block_id

        # If no route and no destination, consider any station as a potential stop
        return True

    # -------------------------------------------------------------------------
    # ATO (Automatic Train Operation) Logic
    # -------------------------------------------------------------------------

    def _detect_speed_trend(self, current_speed: float) -> str:
        if current_speed > self._previous_speed + SPEED_TREND_EPSILON:
            return "rising"
        elif current_speed < self._previous_speed - SPEED_TREND_EPSILON:
            return "falling"
        return "stable"

    def _adjust_handle_toward_brake(self, current: HandlePosition) -> HandlePosition:
        from enums import HANDLE_POSITION_INDEX, INDEX_TO_HANDLE_POSITION

        current_idx = HANDLE_POSITION_INDEX.get(current, 5)
        new_idx = min(current_idx + 1, 13)
        return INDEX_TO_HANDLE_POSITION.get(new_idx, HandlePosition.B8)

    def _adjust_handle_toward_power(self, current: HandlePosition) -> HandlePosition:
        from enums import HANDLE_POSITION_INDEX, INDEX_TO_HANDLE_POSITION

        current_idx = HANDLE_POSITION_INDEX.get(current, 5)
        new_idx = max(current_idx - 1, 0)
        return INDEX_TO_HANDLE_POSITION.get(new_idx, HandlePosition.P5)

    def _is_power_handle(self, handle: HandlePosition) -> bool:
        return handle in (
            HandlePosition.P1,
            HandlePosition.P2,
            HandlePosition.P3,
            HandlePosition.P4,
            HandlePosition.P5,
        )

    def _is_brake_handle(self, handle: HandlePosition) -> bool:
        return handle in (
            HandlePosition.B1,
            HandlePosition.B2,
            HandlePosition.B3,
            HandlePosition.B4,
            HandlePosition.B5,
            HandlePosition.B6,
            HandlePosition.B7,
            HandlePosition.B8,
        )

    # -------------------------------------------------------------------------
    # Future-Aware ATO Helpers
    # -------------------------------------------------------------------------

    def _get_brake_level(self, handle: HandlePosition) -> int:
        """Convert brake handle to level number (1-8)."""
        brake_levels = {
            HandlePosition.B1: 1,
            HandlePosition.B2: 2,
            HandlePosition.B3: 3,
            HandlePosition.B4: 4,
            HandlePosition.B5: 5,
            HandlePosition.B6: 6,
            HandlePosition.B7: 7,
            HandlePosition.B8: 8,
        }
        return brake_levels.get(handle, 0)

    def _brake_level_to_handle(self, level: int) -> HandlePosition:
        """Convert level number (1-8) to brake handle."""
        handles = {
            1: HandlePosition.B1,
            2: HandlePosition.B2,
            3: HandlePosition.B3,
            4: HandlePosition.B4,
            5: HandlePosition.B5,
            6: HandlePosition.B6,
            7: HandlePosition.B7,
            8: HandlePosition.B8,
        }
        return handles.get(level, HandlePosition.B1)

    def _sample_curve_at(
        self, curve: List[Dict], distance_cm: float, key: str = "operational_limit"
    ) -> float:
        """
        Sample a speed curve at a specific distance with linear interpolation.

        Args:
            curve: List of curve points [{distance_cm, atc_limit, operational_limit}, ...]
            distance_cm: Distance to sample at
            key: Which limit to sample ('atc_limit' or 'operational_limit')

        Returns:
            Interpolated speed limit at the given distance
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

    def _simulate_forward(
        self,
        current_speed_kmh: float,
        handle: HandlePosition,
        distance_cm: float,
        dt: float = 0.02,
    ) -> float:
        """
        Simulate train forward in time and return predicted speed at given distance.

        Uses a simplified physics model based on TrainPhysics: converts handle
        position to acceleration rate via POWER_MULTIPLIERS/BRAKE_MULTIPLIERS,
        then integrates forward in small time steps until the target distance
        is reached.

        Note: Air resistance is not modeled. This makes predictions slightly
        optimistic at high speeds under power. For ATO control purposes this
        is acceptable since the evaluator compares handles relative to each
        other, and all candidates experience the same omission.

        Args:
            current_speed_kmh: Train's current speed in km/h
            handle: Handle position to simulate
            distance_cm: Distance ahead to predict speed at
            dt: Simulation time step in seconds (default 20ms for accuracy)

        Returns:
            Predicted speed in km/h at the given distance
        """
        if distance_cm <= 0:
            return current_speed_kmh

        profile = self._get_speed_profile()

        # Calculate acceleration for this handle (same as TrainPhysics)
        if handle in POWER_MULTIPLIERS:
            accel_kmh_s = profile.acceleration_rate * POWER_MULTIPLIERS[handle]
        elif handle in BRAKE_MULTIPLIERS:
            accel_kmh_s = -profile.deceleration_rate * BRAKE_MULTIPLIERS[handle]
        elif handle == HandlePosition.EB:
            accel_kmh_s = -profile.emergency_brake_rate
        else:
            accel_kmh_s = 0.0  # Neutral or unknown

        km_h_to_cm_s = get_speed_conversion_factor()
        speed_cm_s = current_speed_kmh * km_h_to_cm_s
        distance_remaining = distance_cm

        while distance_remaining > 0 and speed_cm_s >= 0:
            # Apply acceleration
            speed_cm_s += accel_kmh_s * km_h_to_cm_s * dt

            # Apply friction when coasting (neutral or unknown handle)
            if handle == HandlePosition.N and speed_cm_s > 0:
                speed_cm_s -= profile.friction_rate * km_h_to_cm_s * dt

            # Snap to zero when braking below stopped threshold (matches TrainPhysics)
            speed_kmh = speed_cm_s / km_h_to_cm_s
            if speed_kmh <= STOPPED_SPEED_THRESHOLD_KMH and accel_kmh_s <= 0:
                speed_cm_s = 0.0
                break

            # Clamp to non-negative
            if speed_cm_s <= 0:
                speed_cm_s = 0.0
                break

            # Advance distance
            step_dist = speed_cm_s * dt
            distance_remaining -= step_dist

        return speed_cm_s / km_h_to_cm_s

    def _evaluate_handles_against_curve(
        self,
        current_speed_kmh: float,
        curve: List[Dict],
        horizon_cm: float = 30.0,
        sample_count: int = 5,
    ) -> HandlePosition:
        """
        Evaluate all handle positions against speed curve and return best one.

        For each candidate handle, simulates the train forward and samples
        the predicted speed at multiple distances. Compares each sample
        against the operational_limit at that distance on the curve.

        Scoring:
        - Penalty for being ABOVE the curve (overshoot) - weighted heavily
        - Penalty for being too far BELOW the curve (inefficient)
        - Best handle minimizes total penalty

        Args:
            current_speed_kmh: Train's current speed in km/h
            curve: Speed curve points [{distance_cm, operational_limit, ...}, ...]
            horizon_cm: How far ahead to evaluate (default 30cm)
            sample_count: Number of sample points within horizon

        Returns:
            Best handle position
        """
        if not curve:
            return HandlePosition.N

        # Clamp horizon to curve extent
        max_curve_dist = curve[-1].get("distance_cm", 0)
        horizon_cm = min(horizon_cm, max_curve_dist)
        if horizon_cm <= 0:
            # At end of curve - just brake
            return self._calculate_braking_notch(current_speed_kmh)

        candidates = [
            HandlePosition.P5,
            HandlePosition.P4,
            HandlePosition.P3,
            HandlePosition.P2,
            HandlePosition.P1,
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

        sample_distances = [
            horizon_cm * (i + 1) / sample_count for i in range(sample_count)
        ]

        best_handle = HandlePosition.N
        best_score = float("inf")
        OVERSHOOT_WEIGHT = 10.0  # Penalize exceeding curve heavily

        for handle in candidates:
            score = 0.0

            for dist in sample_distances:
                predicted_speed = self._simulate_forward(
                    current_speed_kmh, handle, dist
                )
                curve_limit = self._sample_curve_at(curve, dist, "operational_limit")

                if predicted_speed > curve_limit:
                    # Above curve - heavy penalty
                    overshoot = predicted_speed - curve_limit
                    score += overshoot * OVERSHOOT_WEIGHT
                else:
                    # Below curve - mild penalty (want to be close to curve)
                    gap = curve_limit - predicted_speed
                    score += gap

            if score < best_score:
                best_score = score
                best_handle = handle

        return best_handle

    def _ato_calculate_handle(self) -> HandlePosition:
        """
        ATO handle calculation using curve-following approach.

        Reads the ATO operational speed curve at the train's current position
        (distance=0) as the target speed, then uses trend-aware 4-zone control
        to track it smoothly.

        The speed curve already accounts for:
        - Path end stopping distance with safety margins
        - Block speed limits and braking envelopes
        - 60% operational deceleration ratio for comfort
        - 5 km/h margin below ATC limit

        The control zones are:
        1. Stopping: target <= 0.5 -> brake until physics snaps to 0
        2. Speed-up: speed < target-3 -> release brakes, then optimal power
        3. Hysteresis: target-3 <= speed < target-1 -> maintain handle
        4. Slow-down: speed >= target-1 -> trend-aware braking

        Also sets self.handle_reason with diagnostic info.
        """
        train = self._get_train()
        if not train:
            self.handle_reason = "No train"
            return HandlePosition.N

        current_speed = train.current_speed
        current_handle = self.handle_position

        # =================================================================
        # TARGET ACQUISITION: Sample ATO curve ahead to account for reaction delay
        # =================================================================
        lookahead = self.get_monitor_lookahead()
        curves = lookahead.get("lookahead", {}).get("speed_curves", [])

        LOOKAHEAD_SAMPLE_CM = 30.0  # Sample 30cm ahead to account for reaction delay

        if curves:
            effective_target = self._sample_curve_at(
                curves, LOOKAHEAD_SAMPLE_CM, "operational_limit"
            )
        else:
            effective_target = 0.0

        # Always cap at real-time ATC constraints.
        # The curve may not reflect manual ATC overrides or real-time block
        # speed limits that were set independently of the curve calculation.
        effective_target = min(
            effective_target,
            train.max_speed,
            self.atc_speed_limit,
            self.current_block_speed_limit,
        )

        result_handle: HandlePosition = current_handle
        reason: str = ""

        # =================================================================
        # ZONE 1: STOPPING (effective_target <= 0.5)
        # =================================================================
        if effective_target <= STOPPED_SPEED_THRESHOLD_KMH:
            if current_speed > 0:
                result_handle = self._calculate_braking_notch(current_speed)
                reason = f"Stopping: target={effective_target:.1f}, braking at {current_speed:.1f}"
            else:
                result_handle = HandlePosition.N
                reason = "Stopped"
            self._previous_speed = current_speed
            self.handle_reason = reason
            return result_handle

        # Detect speed trend for zone decisions
        trend = self._detect_speed_trend(current_speed)
        speed_diff = effective_target - current_speed

        # =================================================================
        # ZONE 2: SPEED UP (speed < target - exit_margin)
        # =================================================================
        if current_speed < effective_target - ATO_EXIT_MARGIN:
            optimal_notch = self._calculate_power_notch(current_speed, effective_target)

            if self._is_brake_handle(current_handle):
                result_handle = self._adjust_handle_toward_power(current_handle)
                reason = f"SpeedUp: releasing brake, gap={speed_diff:.1f}"
            elif current_handle == HandlePosition.N:
                result_handle = optimal_notch
                reason = f"SpeedUp: N->{optimal_notch.value}, gap={speed_diff:.1f}"
            else:
                result_handle = optimal_notch
                reason = f"SpeedUp: optimal={optimal_notch.value}, gap={speed_diff:.1f}"

        # =================================================================
        # ZONE 3: HYSTERESIS (target-exit <= speed < target-entry)
        # =================================================================
        elif current_speed < effective_target - ATO_ENTRY_MARGIN:
            result_handle = current_handle
            reason = f"Hysteresis: maintaining {current_handle.value}, target={effective_target:.1f}"

        # =================================================================
        # ZONE 4: SLOW DOWN (speed >= target - entry_margin)
        # =================================================================
        else:
            speed_overshoot = current_speed - effective_target

            # Fast response for large overshoot (>= 3 km/h)
            if speed_overshoot >= 3.0:
                if (
                    self._is_power_handle(current_handle)
                    or current_handle == HandlePosition.N
                ):
                    result_handle = HandlePosition.B2
                    reason = (
                        f"SlowDown: overshoot={speed_overshoot:.1f}>=3, applying B2"
                    )
                elif self._is_brake_handle(current_handle):
                    current_level = self._get_brake_level(current_handle)
                    new_level = min(6, current_level + 2)
                    result_handle = self._brake_level_to_handle(new_level)
                    reason = f"SlowDown: overshoot={speed_overshoot:.1f}>=3, B{current_level}->B{new_level}"
                else:
                    result_handle = current_handle
                    reason = (
                        f"SlowDown: overshoot={speed_overshoot:.1f}>=3, maintaining"
                    )
            elif self._is_power_handle(current_handle):
                result_handle = HandlePosition.N
                reason = f"SlowDown: power->N, overshoot={speed_overshoot:.1f}"
            elif self._is_brake_handle(current_handle):
                if trend == "rising":
                    current_level = self._get_brake_level(current_handle)
                    new_level = min(6, current_level + 1)
                    result_handle = self._brake_level_to_handle(new_level)
                    reason = f"SlowDown: rising+brake, B{current_level}->B{new_level}"
                elif trend == "falling":
                    if speed_overshoot < 2.0:
                        result_handle = HandlePosition.N
                        reason = (
                            f"SlowDown: falling, overshoot={speed_overshoot:.1f}<2, ->N"
                        )
                    else:
                        result_handle = self._adjust_handle_toward_power(current_handle)
                        reason = "SlowDown: falling, releasing brake"
                else:
                    result_handle = current_handle
                    reason = f"SlowDown: stable, maintaining {current_handle.value}"
            else:  # Neutral handle
                if trend == "rising":
                    result_handle = HandlePosition.B1
                    reason = "SlowDown: N+rising, ->B1"
                else:
                    result_handle = HandlePosition.N
                    reason = f"SlowDown: N+{trend}, maintaining N"

        self._previous_speed = current_speed
        self.handle_reason = reason
        return result_handle

    def _calculate_power_notch(
        self, current_speed: float, target_speed: float
    ) -> HandlePosition:
        """Calculate appropriate power notch for acceleration.

        Uses full power (P5) when far from target for quick acceleration.
        Only uses graduated power notches when within 5 km/h of target
        for fine control to avoid overshooting.
        """
        speed_diff = target_speed - current_speed

        # When far from target (> 5 km/h gap), use full power for quick acceleration
        if speed_diff > 5:
            return HandlePosition.P5
        # Fine control zone: within 5 km/h of target
        elif speed_diff > 3:
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

    def calculate_speed_curves(
        self,
        max_distance_cm: float = 500.0,
        lookahead_blocks: Optional[List[Dict]] = None,
        lookahead_signals: Optional[List[Dict]] = None,
        lookahead_stations: Optional[List[Dict]] = None,
        target_stop_cm: Optional[float] = None,
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
        train = self._get_train()
        if not train:
            return []

        profile = self._get_speed_profile()
        max_speed = train.max_speed
        decel_rate = profile.deceleration_rate
        operational_decel = decel_rate * profile.operational_decel_ratio

        # Build list of speed restrictions ahead
        # Each restriction: (distance_cm, speed_limit_kmh, safety_margin_cm)
        restrictions: List[Tuple[float, float, float]] = []

        # Add path end as a stop (speed 0)
        # Use effective safety margin based on destination type:
        # - 5cm if ato_destination_id is a station
        # - 25cm otherwise
        # This ensures the curve matches what _get_effective_safety_margin() returns
        path_end_safety_margin = self._get_effective_safety_margin()
        restrictions.append((max_distance_cm, 0.0, path_end_safety_margin))

        # Add block speed limits as restrictions (for braking curve calculation)
        # This creates braking curves to slow down BEFORE entering speed-limited blocks
        if lookahead_blocks:
            for block in lookahead_blocks:
                block_start = block.get("start_cm", 0)
                block_speed = block.get("speed_limit_kmh")  # None if no limit
                if block_speed is not None and block_speed < max_speed:
                    # Need to slow down to block_speed at block_start
                    restrictions.append((block_start, block_speed, 0.0))

        # Signal restrictions are intentionally NOT used for ATC/ATO speed calculation.
        # This is a CBTC block-based system where:
        # - Speed control comes from BLOCKS (clearance distance, max_speed_kmh)
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
        # are applied through the block's max_speed_kmh property, not the station's
        # top_speed_kmh which is for visualization/informational purposes only.

        curves = []

        # Use 5cm resolution for smooth curves
        step = CURVE_SAMPLE_RESOLUTION_CM
        sample_distance = 0.0
        while sample_distance <= max_distance_cm:
            # For this sample point, find the minimum speed limit
            # considering all restrictions ahead

            atc_limits: List[float] = [max_speed]
            operational_limits: List[float] = [max_speed]

            for restriction_dist, restriction_speed, restriction_safety in restrictions:
                # Distance from sample point to this restriction
                dist_to_restriction = restriction_dist - sample_distance

                # Handle being AT or PAST a stop restriction (speed=0)
                # At or past the path end, speed must be 0
                if dist_to_restriction <= 0 and restriction_speed <= 0:
                    atc_limits.append(0.0)
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
                            atc_limits.append(0.0)
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
                            atc_limits.append(atc_limit)
                            operational_limits.append(operational_limit)
                    else:
                        # Must slow to target speed: use decel to target formula
                        atc_limit = calculate_speed_for_target(
                            dist_to_restriction, restriction_speed, decel_rate
                        )
                        operational_limit = calculate_speed_for_target(
                            dist_to_restriction, restriction_speed, operational_decel
                        )
                        atc_limits.append(atc_limit)
                        operational_limits.append(operational_limit)

            # Also apply block speed limit at current position
            if lookahead_blocks:
                for block in lookahead_blocks:
                    block_start = block.get("start_cm", 0)
                    block_end = block.get("end_cm", 0)
                    if block_start <= sample_distance < block_end:
                        block_speed = block.get("speed_limit_kmh")  # None if no limit
                        if block_speed is not None:
                            atc_limits.append(block_speed)
                            operational_limits.append(block_speed)
                        break

            # NOTE: Station speed limits are NOT applied here.
            # ATC/ATO only cares about BLOCKS. Station's top_speed_kmh is for
            # visualization only. Block speed limits (if any) are enforced above.

            # Take minimum of all limits
            atc_limit = min(atc_limits)
            operational_limit = min(operational_limits)

            # Cap at train max speed
            atc_limit = min(atc_limit, max_speed)
            operational_limit = min(operational_limit, max_speed)

            # Ensure ATO limit stays ATO_SPEED_MARGIN_KMH below ATC
            operational_limit = min(
                operational_limit, max(0.0, atc_limit - ATO_SPEED_MARGIN_KMH)
            )

            curves.append(
                {
                    "distance_cm": sample_distance,
                    "atc_limit": atc_limit,
                    "operational_limit": operational_limit,
                }
            )

            sample_distance += step

        # Add explicit curve points for cleaner visualization
        # 1. Target stop point (where train actually stops)
        if target_stop_cm is not None and target_stop_cm > 0:
            # Check if we already have a point at target_stop_cm (within step tolerance)
            has_target_point = any(
                abs(point["distance_cm"] - target_stop_cm) < step / 2
                for point in curves
            )
            if not has_target_point:
                # Add explicit stop point at target_stop_cm with speed 0
                curves.append(
                    {
                        "distance_cm": target_stop_cm,
                        "atc_limit": 0.0,
                        "operational_limit": 0.0,
                    }
                )

        # 2. Clearance end point (end of available distance)
        # Always add this to show the clearance boundary
        has_clearance_point = any(
            abs(point["distance_cm"] - max_distance_cm) < step / 2 for point in curves
        )
        if not has_clearance_point:
            curves.append(
                {
                    "distance_cm": max_distance_cm,
                    "atc_limit": 0.0,
                    "operational_limit": 0.0,
                }
            )

        # Sort by distance to ensure proper curve drawing
        curves.sort(key=lambda p: p["distance_cm"])

        return curves

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

        self.active_cab = cab

        # Update train state
        if cab is None:
            self.state = TrainState.IDLE
            train.set_cab(None)
        else:
            self.state = TrainState.STANDBY
            train.set_cab(cab)  # This only updates leading_end and state

            # When switching cabs (A <-> B), the physical train doesn't move.
            # The driver walks to the other end. What changes:
            # 1. leading_end flips (done by train.set_cab)
            # 2. facing_direction reverses (computed from leading_end for multi-tile trains)
            # 3. entry_edge for BOTH ends flips (since train will travel opposite direction)
            # 4. distance_cm for BOTH ends flips (10cm from W = tile_length - 10cm from E)
            if old_cab in ("A", "B") and old_cab != cab:
                # Sync facing_direction from the computed property.
                # For multi-tile trains, the computed property derives direction from
                # leading_end + tile positions, so changing leading_end automatically
                # gives the correct (reversed) direction.
                # For single-tile trains (end_a_tile == end_b_tile), the computed
                # property falls back to the legacy field, so we must explicitly flip.
                if train.position:
                    is_multi_tile = (
                        train.position.end_a_tile is not None
                        and train.position.end_b_tile is not None
                        and train.position.end_a_tile != train.position.end_b_tile
                    )
                    # For multi-tile trains, facing_direction is automatically recalculated
                    # from the already-updated leading_end. No manual sync needed.
                    # For single-tile trains, the direction is calculated from entry_edge
                    # and distance_cm, which are being flipped below.
                    # Therefore, no direction sync is needed at all - it's automatic.

                # Flip entry edges and distances for BOTH ends
                if train.position and self.network:
                    # Flip end_a entry edge
                    if train.position.end_a_tile and train.position.end_a_entry_edge:
                        track_a = self.network.get_track(*train.position.end_a_tile)
                        if track_a:
                            exits_a = track_a.get_exits(
                                train.position.end_a_entry_edge, use_switch_state=True
                            )
                            if exits_a:
                                train.position.end_a_entry_edge = exits_a[0]
                            # Flip distance: new_distance = tile_length - old_distance
                            tile_length = self.network._get_tile_length_cm(
                                train.position.end_a_tile[0],
                                train.position.end_a_tile[1],
                            )
                            train.position.end_a_distance_cm = (
                                tile_length - train.position.end_a_distance_cm
                            )

                    # Flip end_b entry edge
                    if train.position.end_b_tile and train.position.end_b_entry_edge:
                        track_b = self.network.get_track(*train.position.end_b_tile)
                        if track_b:
                            exits_b = track_b.get_exits(
                                train.position.end_b_entry_edge, use_switch_state=True
                            )
                            if exits_b:
                                train.position.end_b_entry_edge = exits_b[0]
                            # Flip distance
                            tile_length = self.network._get_tile_length_cm(
                                train.position.end_b_tile[0],
                                train.position.end_b_tile[1],
                            )
                            train.position.end_b_distance_cm = (
                                tile_length - train.position.end_b_distance_cm
                            )

                    # Update path_index to match new head position after cab switch
                    # This is critical for lookahead and speed limit calculations
                    path_tiles = self.get_path_tiles()
                    if path_tiles is None:
                        path_tiles = train.current_path
                    if path_tiles and train.position:
                        new_head_tile = train.position.head_tile
                        for i, (x, y, _) in enumerate(path_tiles):
                            if (x, y) == new_head_tile:
                                train.path_index = i
                                break
            # No need to sync direction - it's calculated automatically from position

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
        - Must be powered on
        - Train state must be IDLE or STANDBY

        ATO Automation:
        - Automatically selects cab from path direction when cab is OFF (None)
        - Automatically corrects cab direction to match path when already set
        - Uses set_cab() for proper state updates (facing_direction, entry edges,
          state transitions IDLE -> STANDBY, etc.)
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if self.control_mode != "ato":
            return CommandResult(False, "Not in ATO mode")

        if not self.power_on:
            return CommandResult(False, "Power is off")

        # Allow IDLE (no cab) or STANDBY (cab set) - auto-selection handles IDLE
        if self.state not in (TrainState.IDLE, TrainState.STANDBY):
            return CommandResult(
                False, f"Train not in IDLE or STANDBY (is {self.state.value})"
            )

        # Recalculate ATO info fresh to ensure cab direction matches
        # the current path (path may have been assigned since last tick)
        self.ato_info = self._calculate_ato_info()

        # ATO Auto-selection/correction: determine the required cab from path
        # direction and select or switch if needed. This handles both:
        #   1. cab=None (OFF): auto-select cab from path (IDLE -> STANDBY)
        #   2. cab=A/B but wrong: auto-correct cab to match path direction
        if self.ato_info and self.ato_info.required_cab:
            required_cab = self.ato_info.required_cab
            if self.active_cab != required_cab:
                old_cab = self.active_cab
                logger.info(
                    "[TrainController %s] ATO auto-%s cab: %s -> %s",
                    self.train_id,
                    "selecting" if old_cab is None else "correcting",
                    old_cab,
                    required_cab,
                )
                # Ensure train is physically stopped before switching cab.
                if train.current_speed > 0:
                    train.current_speed = 0.0
                result = self.set_cab(required_cab)
                if not result.success:
                    return CommandResult(
                        False,
                        f"ATO cannot start: cab auto-{'selection' if old_cab is None else 'correction'} failed - {result.message}",
                    )
                self.pending_notifications.append(
                    f"ATO auto-{'selected' if old_cab is None else 'switched'} cab to {required_cab} for path direction"
                )
        elif self.active_cab is None:
            # No ATO info available and no cab - cannot determine direction
            return CommandResult(
                False,
                "No cab selected and no path available to auto-select direction",
            )

        # After auto-selection, verify we're in the right state
        # set_cab() transitions IDLE -> STANDBY, so this should pass
        if self.state != TrainState.STANDBY:
            return CommandResult(False, f"Train not in STANDBY (is {self.state.value})")

        self.state = TrainState.RUNNING_MOVING
        train.state = TrainState.RUNNING_MOVING
        self.ato_running = True
        return CommandResult(True, "ATO started")

    def ato_stop(self) -> CommandResult:
        """
        Stop ATO movement (service stop, not emergency).

        Sets ato_running=False. The tick() will handle braking and
        transitioning to STANDBY when stopped.
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not self.ato_running:
            return CommandResult(False, "ATO not running")

        # Just set ato_running=False - tick() handles the rest
        self.ato_running = False

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
            # Clear leading_end to None (no direction)
            if train.position:
                train.position.leading_end = None
            train.wait_reason = None
            train.assigned_route_id = None

    # -------------------------------------------------------------------------
    # Route Executor Commands (for RouteExecutor integration)
    # -------------------------------------------------------------------------

    def depart(self) -> bool:
        """
        Start moving (STANDBY -> RUNNING_MOVING).

        Used by RouteExecutor when path is acquired and train should start moving.
        If in ATO mode, this calls ato_go() to start ATO - like pressing the Go button.

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

        # If in ATO mode, just call ato_go() - RouteExecutor is "pressing the Go button"
        if self.control_mode == "ato":
            # Auto-select cab if needed
            if self.active_cab is None:
                if self.ato_info and self.ato_info.required_cab:
                    self.active_cab = self.ato_info.required_cab
                    if train:
                        train.active_cab = self.ato_info.required_cab
                        if train.position:
                            # Convert cab string to LeadingEnd enum
                            train.position.leading_end = (
                                LeadingEnd.A
                                if self.ato_info.required_cab == "A"
                                else LeadingEnd.B
                            )

            result = self.ato_go()
            return result.success

        # Manual mode - just transition state
        self.state = TrainState.RUNNING_MOVING
        train.state = TrainState.RUNNING_MOVING
        train.wait_reason = None
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
        Flip facing direction by switching to the opposite cab.

        Delegates to set_cab() which handles all the necessary state changes:
        - Swaps active cab (A <-> B) and leading_end
        - Flips entry edges for both ends
        - Flips distances for both ends
        - Updates path_index for new head position
        - Syncs facing_direction from computed property

        Returns:
            True if successful, False if preconditions not met

        Preconditions:
        - Train must be stopped
        - ATO must not be actively running (ato_running == False)
        - Train must have an active cab (A or B)
        """
        train = self._get_train()
        if train is None:
            return False

        if not train.is_stopped():
            return False

        if self.ato_running:
            return False

        if self.active_cab not in ("A", "B"):
            return False

        # Delegate to set_cab with the opposite cab
        new_cab = "B" if self.active_cab == "A" else "A"
        result = self.set_cab(new_cab)
        return result.success

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

        # Set leading end to A (Cab A is front)
        if train.position:
            train.position.leading_end = LeadingEnd.A
        self.active_cab = "A"  # Setting direction implies Cab A is leading
        train.active_cab = "A"

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

        # Clear leading_end (no direction)
        if train.position:
            train.position.leading_end = None
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

        # ato_running stays as-is - if user stopped ATO, it stays stopped

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
            "ato_destination_block_id": self.ato_destination_block_id,
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
            # Diagnostic: reason for current handle position
            "handle_reason": self.handle_reason,
        }

    def _build_fallback_lookahead(
        self,
        train: "Train",
        head_tile: Tuple[int, int],
        head_distance_cm: float,
        travel_direction: Optional[str],
    ) -> Dict[str, Any]:
        """
        Build lookahead data when no path is available.

        This is used when:
        - Train is first placed (no path yet)
        - Path is canceled (train must stop safely)
        - Train enters last tile and path is cleared

        The lookahead is built from the train's occupied blocks and facing direction,
        calculating clearance to the end of the current block.
        """
        blocks: List[Dict[str, Any]] = []
        signals: List[Dict[str, Any]] = []
        switches: List[Dict[str, Any]] = []
        stations: List[Dict[str, Any]] = []

        # Get current block from head tile
        head_track = (
            self.network.get_track(head_tile[0], head_tile[1]) if self.network else None
        )
        current_block_id = head_track.block_section_id if head_track else None

        clearance_cm = 0.0
        block_length_cm = 0.0
        tile_count = 1

        # If controller has a known clearance (from simulation), use it as baseline
        # This ensures we don't underestimate clearance when path is temporarily unavailable
        if self.clearance_ahead_cm > 0:
            clearance_cm = self.clearance_ahead_cm

        if current_block_id and self.network and train.position:
            block = self.network.blocks.get(current_block_id)
            facing_dir = train.position.facing_direction

            # Calculate remaining distance in current tile
            current_tile_length = self.network._get_tile_length_cm(
                head_tile[0], head_tile[1]
            )
            remaining_in_tile = max(0, current_tile_length - head_distance_cm)

            # Calculate remaining distance in the ENTIRE block (not just current tile)
            # This is critical for first placement in multi-tile blocks
            if clearance_cm == 0 and block and block.length_cm > 0:
                # Get all tiles in the block to calculate proper remaining distance
                block_positions = self.network.get_block_positions(current_block_id)
                if block_positions:
                    tile_count = len(block_positions)
                    per_tile_length = block.length_cm / tile_count

                    # Calculate how far into the block the train is
                    # We need to find where head_tile is in the block
                    # and sum the lengths of tiles behind + distance in current tile
                    distance_into_block = head_distance_cm

                    # For tiles the train has already passed through in this block
                    # (tiles in occupied_tiles that are in this block, before head tile)
                    if train.position.occupied_tiles:
                        for tile in train.position.occupied_tiles:
                            if tile == head_tile:
                                break
                            tile_track = self.network.get_track(tile[0], tile[1])
                            if (
                                tile_track
                                and tile_track.block_section_id == current_block_id
                            ):
                                distance_into_block += self.network._get_tile_length_cm(
                                    tile[0], tile[1]
                                )

                    # Remaining distance = total block length - distance already traveled
                    clearance_cm = max(0.0, block.length_cm - distance_into_block)
                    block_length_cm = block.length_cm
                else:
                    # Fallback: use remaining in tile if we can't get block positions
                    clearance_cm = remaining_in_tile
                    block_length_cm = current_tile_length
            elif clearance_cm == 0:
                # No block info available, use remaining in tile as minimum
                clearance_cm = remaining_in_tile
                block_length_cm = current_tile_length
            else:
                # We have clearance from simulation, just get block length for info
                if block and block.length_cm > 0:
                    block_length_cm = block.length_cm
                    block_positions = self.network.get_block_positions(current_block_id)
                    if block_positions:
                        tile_count = len(block_positions)
                else:
                    block_length_cm = current_tile_length

            # The clearance is the remaining distance in the current block
            # For safety, we use the remaining tile distance as minimum
            # This ensures the train stops before the end of the known track

            # Build block info
            block_state = None
            block_speed_limit: Optional[float] = None
            if block:
                if block.state:
                    block_state = block.state.value
                if block.max_speed_kmh and block.max_speed_kmh < float("inf"):
                    block_speed_limit = block.max_speed_kmh

            blocks.append(
                {
                    "id": current_block_id,
                    "name": current_block_id,
                    "state": block_state or BlockState.OCCUPIED.value,
                    "start_cm": -head_distance_cm,  # Block starts behind train
                    "end_cm": clearance_cm,
                    "length_cm": block_length_cm
                    if block_length_cm > 0
                    else head_distance_cm + clearance_cm,
                    "tile_count": tile_count,
                    "speed_limit_kmh": block_speed_limit,
                }
            )

            # Check if there's a station in this block
            if head_track and head_track.station_id:
                station = self.network.platforms.get(head_track.station_id)
                if station:
                    # Get station length from station positions
                    station_tile_count = (
                        len(station.positions) if station.positions else 1
                    )
                    station_length_cm = (
                        station.length_cm if station.length_cm > 0 else block_length_cm
                    )
                    stations.append(
                        {
                            "station_id": head_track.station_id,
                            "name": station.name if station else head_track.station_id,
                            "distance_cm": 0,  # We're already in the station
                            "length_cm": station_length_cm,
                            "tile_count": station_tile_count,
                            "top_speed_kmh": station.top_speed_kmh if station else None,
                        }
                    )

        # Calculate safety margin
        effective_safety_margin = self._get_effective_safety_margin()
        target_stop_cm = max(0.0, clearance_cm - effective_safety_margin)

        # Calculate speed curves for the available clearance
        speed_curves = self.calculate_speed_curves(
            max_distance_cm=clearance_cm if clearance_cm > 0 else 50.0,
            lookahead_blocks=blocks,
            lookahead_signals=signals,
            lookahead_stations=stations,
            target_stop_cm=target_stop_cm,
        )

        return {
            "train_id": self.train_id,
            "head_tile": [head_tile[0], head_tile[1]],
            "head_distance_cm": head_distance_cm,
            "travel_direction": travel_direction,
            "cab": self.active_cab,
            "speed_limit_reason": self.speed_limit_reason,
            "lookahead": {
                "blocks": blocks,
                "signals": signals,
                "switches": switches,
                "stations": stations,
                "clearance_cm": clearance_cm,
                "safety_margin_cm": effective_safety_margin,
                "target_stop_cm": target_stop_cm,
                "speed_curves": speed_curves,
            },
        }

    def get_monitor_lookahead(self) -> Dict[str, Any]:
        train = self._get_train()
        if not train or not self.network:
            return self._build_empty_monitor_lookahead(
                speed_limit_reason=self.speed_limit_reason
            )

        if self.active_cab not in ("A", "B"):
            self.speed_limit_reason = "No cab selected"
            return self._build_empty_monitor_lookahead(
                speed_limit_reason=self.speed_limit_reason
            )

        head_tile = self._get_head_tile()
        head_distance_cm = self._get_head_distance_cm()
        travel_direction = (
            train.position.facing_direction.name
            if train.position and train.position.facing_direction
            else None
        )

        if not head_tile:
            return self._build_empty_monitor_lookahead(
                head_distance_cm=head_distance_cm,
                travel_direction=travel_direction,
                speed_limit_reason=self.speed_limit_reason,
            )

        path_tiles = self.get_path_tiles()
        if path_tiles is None:
            path_tiles = train.current_path
        head_path_index = self._get_head_path_index(train, head_tile)
        if head_path_index < 0 and path_tiles:
            # Head tile is not in the path.  This commonly happens right after
            # a return-path assignment + cab switch: the cab switch moves the
            # head to the opposite end of the train which may be one tile past
            # the path start.  If the head is in the same block as path[0] or
            # path[-1], anchor the scan at that path endpoint so the full
            # path-based lookahead is used instead of the fallback.
            head_track_obj = self.network.get_track(head_tile[0], head_tile[1])
            head_block = head_track_obj.block_section_id if head_track_obj else None
            if head_block:
                # Check first path tile
                first_entry = path_tiles[0]
                first_track = self.network.get_track(first_entry[0], first_entry[1])
                first_block = first_track.block_section_id if first_track else None
                # Check last path tile
                last_entry = path_tiles[-1]
                last_track = self.network.get_track(last_entry[0], last_entry[1])
                last_block = last_track.block_section_id if last_track else None

                if head_block == first_block:
                    head_path_index = 0
                    # Account for extra distance: head is behind path[0]
                    extra_tiles_cm = 0.0
                    # Scan from head to path[0] through the block
                    # (simplified: just add the head tile's length)
                    head_tile_length = self.network._get_tile_length_cm(
                        head_tile[0], head_tile[1]
                    )
                    extra_tiles_cm = head_tile_length - head_distance_cm
                    head_distance_cm = -extra_tiles_cm  # Negative = before path start
                elif head_block == last_block:
                    head_path_index = len(path_tiles) - 1

        if head_path_index < 0 or not path_tiles:
            # No path available - build lookahead from occupied blocks
            # This handles: initial placement, path canceled, entering last tile
            return self._build_fallback_lookahead(
                train, head_tile, head_distance_cm, travel_direction
            )

        blocks: List[Dict[str, Any]] = []
        signals: List[Dict[str, Any]] = []
        switches: List[Dict[str, Any]] = []
        stations: List[Dict[str, Any]] = []

        cumulative_distance_cm = -head_distance_cm
        path_scan_step = self._get_path_scan_step(train, head_path_index)

        current_block_id = None
        block_back_length_cm = 0.0
        block_back_tile_count = 0
        head_track = self.network.get_track(head_tile[0], head_tile[1])
        if head_track:
            current_block_id = head_track.block_section_id

        if current_block_id:
            behind_start = (
                head_path_index - 1 if path_scan_step == 1 else head_path_index + 1
            )
            behind_end = -1 if path_scan_step == 1 else len(path_tiles)
            for j in range(behind_start, behind_end, -path_scan_step):
                prev_entry = path_tiles[j]
                prev_x, prev_y = prev_entry[0], prev_entry[1]
                prev_track = self.network.get_track(prev_x, prev_y)
                prev_block_id = prev_track.block_section_id if prev_track else None
                if prev_block_id != current_block_id:
                    break
                block_back_length_cm += self.network._get_tile_length_cm(prev_x, prev_y)
                block_back_tile_count += 1

        current_block_start_cm = (
            cumulative_distance_cm - block_back_length_cm
            if current_block_id
            else cumulative_distance_cm
        )

        seen_blocks: set[str] = set()
        seen_signals: set[str] = set()
        seen_switches: set[Tuple[int, int]] = set()
        seen_stations: set[str] = set()
        current_entry_edge: Optional[str] = None
        neighbor_index = head_path_index - path_scan_step
        if 0 <= neighbor_index < len(path_tiles):
            neighbor_entry = path_tiles[neighbor_index]
            neighbor_tile = (neighbor_entry[0], neighbor_entry[1])
            direction_into_head = self._direction_between_tiles(
                neighbor_tile, head_tile
            )
            exit_edge = (
                self._direction_to_edge(direction_into_head)
                if direction_into_head
                else None
            )
            if exit_edge:
                current_entry_edge = OPPOSITE_EDGES.get(exit_edge)
        if current_entry_edge is None and train.position:
            current_entry_edge = train.position.head_entry_edge

        loop_end = len(path_tiles) if path_scan_step == 1 else -1
        for i in range(head_path_index, loop_end, path_scan_step):
            path_entry = path_tiles[i]
            x, y = path_entry[0], path_entry[1]
            track = self.network.get_track(x, y)
            tile_length = self.network._get_tile_length_cm(x, y) if track else 50.0
            tile_block_id = track.block_section_id if track else None
            tile_travel_direction = (
                path_entry[2].name
                if len(path_entry) > 2 and hasattr(path_entry[2], "name")
                else None
            )
            if (
                tile_travel_direction
                and path_scan_step == -1
                and hasattr(path_entry[2], "opposite")
            ):
                tile_travel_direction = path_entry[2].opposite().name
            point_element_start = max(0.0, cumulative_distance_cm)

            if tile_block_id and tile_block_id != current_block_id:
                current_block_id = tile_block_id
                current_block_start_cm = cumulative_distance_cm

            if tile_block_id and tile_block_id not in seen_blocks:
                seen_blocks.add(tile_block_id)
                block_length = 0.0
                tile_count = 0
                for j in range(i, loop_end, path_scan_step):
                    next_entry = path_tiles[j]
                    next_x, next_y = next_entry[0], next_entry[1]
                    next_track = self.network.get_track(next_x, next_y)
                    next_block_id = next_track.block_section_id if next_track else None
                    if next_block_id != tile_block_id:
                        break
                    block_length += self.network._get_tile_length_cm(next_x, next_y)
                    tile_count += 1

                if (
                    tile_block_id == current_block_id
                    and i == head_path_index
                    and block_back_length_cm > 0.0
                ):
                    block_length += block_back_length_cm
                    tile_count += block_back_tile_count

                block_state = None
                block_speed_limit: Optional[float] = None  # None means no limit
                block = self.network.blocks.get(tile_block_id)
                if block:
                    if block.state:
                        block_state = block.state.value
                    if block.max_speed_kmh and block.max_speed_kmh < float("inf"):
                        block_speed_limit = block.max_speed_kmh

                blocks.append(
                    {
                        "id": tile_block_id,
                        "name": tile_block_id,
                        "state": block_state or BlockState.FREE.value,
                        "start_cm": current_block_start_cm,
                        "end_cm": current_block_start_cm + block_length,
                        "length_cm": block_length,
                        "tile_count": tile_count,
                        "speed_limit_kmh": block_speed_limit,  # None if no limit
                    }
                )

            if track and track.signal_ids and tile_travel_direction:
                train_dir = tile_travel_direction.upper()
                for signal_id in track.signal_ids:
                    if signal_id in seen_signals:
                        continue
                    signal = self.network.signals.get(signal_id)
                    if not signal:
                        continue
                    sig_dir = (signal.detection_direction or "").upper()
                    dir_matches = (
                        sig_dir == train_dir
                        or sig_dir == train_dir[:1]
                        or sig_dir[:1] == train_dir[:1]
                    )
                    if not dir_matches:
                        continue
                    seen_signals.add(signal_id)
                    # Calculate precise signal distance accounting for train position
                    # When signal is on the same tile as train head (i == head_path_index),
                    # we need to subtract head_distance_cm to get actual relative distance
                    signal_distance = point_element_start
                    if i == head_path_index:
                        signal_distance = cumulative_distance_cm
                    signals.append(
                        {
                            "id": signal_id,
                            "name": signal.name or signal_id,
                            "state": signal.state.value if signal.state else "unknown",
                            "distance_cm": signal_distance,
                        }
                    )

            if track and track.is_switch:
                switch_key = (x, y)
                if switch_key not in seen_switches:
                    seen_switches.add(switch_key)
                    switch_entity = (
                        self.network.get_switch_at(x, y)
                        if hasattr(self.network, "get_switch_at")
                        else None
                    )
                    switches.append(
                        {
                            "x": x,
                            "y": y,
                            "name": switch_entity.name
                            if switch_entity and switch_entity.name
                            else f"Switch ({x},{y})",
                            "position": "main"
                            if track.switch_state == 0
                            else "diverging",
                            "distance_cm": point_element_start,
                            "locked": track.is_locked_by_path()
                            if hasattr(track, "is_locked_by_path")
                            else False,
                        }
                    )

            if track and track.station_id:
                station_id = track.station_id
                if station_id not in seen_stations:
                    seen_stations.add(station_id)
                    station = self.network.platforms.get(station_id)
                    station_length = station.length_cm if station else 0.0
                    station_tile_count = 0
                    if not station_length:
                        for j in range(i, len(path_tiles)):
                            next_entry = path_tiles[j]
                            next_x, next_y = next_entry[0], next_entry[1]
                            next_track = self.network.get_track(next_x, next_y)
                            if not next_track or next_track.station_id != station_id:
                                break
                            station_length += self.network._get_tile_length_cm(
                                next_x, next_y
                            )
                            station_tile_count += 1
                    stations.append(
                        {
                            "station_id": station_id,
                            "name": station.name if station else station_id,
                            "distance_cm": point_element_start,
                            "length_cm": station_length,
                            "tile_count": station_tile_count or 1,
                            "top_speed_kmh": station.top_speed_kmh if station else None,
                        }
                    )

            next_index = i + path_scan_step
            if 0 <= next_index < len(path_tiles):
                next_entry = path_tiles[next_index]
                direction = self._direction_between_tiles(
                    (x, y), (next_entry[0], next_entry[1])
                )
                exit_edge = self._direction_to_edge(direction) if direction else None
                if exit_edge and track:
                    if current_entry_edge is None or exit_edge not in track.get_exits(
                        current_entry_edge, use_switch_state=True
                    ):
                        inferred_entry = track.get_entry_for_exit(
                            exit_edge, use_switch_state=True
                        )
                        if inferred_entry:
                            current_entry_edge = inferred_entry

                can_traverse, next_entry_edge = self._can_traverse_path_step(
                    train,
                    (x, y),
                    (next_entry[0], next_entry[1]),
                    current_entry_edge,
                )
                cumulative_distance_cm += tile_length
                if not can_traverse:
                    break
                current_entry_edge = next_entry_edge
            else:
                cumulative_distance_cm += tile_length

        clearance_cm = max(0.0, cumulative_distance_cm)

        # Calculate effective safety margin (5cm for platform, 25cm otherwise)
        effective_safety_margin = self._get_effective_safety_margin()
        # Target stop point is clearance minus safety margin
        target_stop_cm = max(0.0, clearance_cm - effective_safety_margin)

        speed_curves = self.calculate_speed_curves(
            max_distance_cm=clearance_cm if clearance_cm > 0 else 500.0,
            lookahead_blocks=blocks,
            lookahead_signals=signals,
            lookahead_stations=stations,
            target_stop_cm=target_stop_cm,
        )

        return {
            "train_id": self.train_id,
            "head_tile": [head_tile[0], head_tile[1]],
            "head_distance_cm": head_distance_cm,
            "travel_direction": travel_direction,
            "cab": self.active_cab,
            "speed_limit_reason": self.speed_limit_reason,
            "lookahead": {
                "blocks": blocks,
                "signals": signals,
                "switches": switches,
                "stations": stations,
                "clearance_cm": clearance_cm,
                "safety_margin_cm": effective_safety_margin,
                "target_stop_cm": target_stop_cm,
                "speed_curves": speed_curves,
            },
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
            ato_destination_block_id=data.get("ato_destination_block_id"),
            ato_path_id=data.get("ato_path_id"),
        )


# =============================================================================
# Speed Calculation Functions (from train_onboard.py)
# =============================================================================


# =============================================================================
# Factory function
# =============================================================================


def create_controller_for_train(
    train_id: str,
    network: "RailwayNetwork",
    path_dispatcher: Optional["PathDispatcher"] = None,
) -> TrainController:
    """
    Create a TrainController instance for a train.

    Args:
        train_id: ID of the train to control
        network: Railway network
        path_dispatcher: PathDispatcher for reading path data (single source of truth)

    Returns:
        TrainController instance
    """
    return TrainController(
        train_id=train_id, network=network, path_dispatcher=path_dispatcher
    )
