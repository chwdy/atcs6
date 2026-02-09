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
REACTION_TIME_SECONDS = 1.5  # Base look-ahead time for future-aware braking
LOOKAHEAD_REFERENCE_SPEED_KMH = 30.0  # Reference speed for quadratic scaling
LOOKAHEAD_MIN_DISTANCE_CM = (
    30.0  # Minimum lookahead distance (ensures we see ahead even at low speeds)
)


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

        dest_block_id = self.ato_destination_block_id
        current_block_id = self._get_current_block_id()

        # Check if we're already in the destination block
        if current_block_id == dest_block_id:
            return self._calculate_clearance_to_end_of_current_block()

        # Not in destination block yet - need current_path to trace forward
        if not train.current_path:
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

        # Start from next tile after head
        for path_entry in train.current_path[head_index + 1 :]:
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
                # Check if this is the last station tile in the path
                remaining_path = train.current_path[
                    train.current_path.index(path_entry) + 1 :
                ]
                has_more_station_tiles = any(
                    (p[0], p[1]) in station_positions for p in remaining_path
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
        if not train.current_path:
            return 1

        path_length = len(train.current_path)
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

        head_entry = train.current_path[head_path_index]
        head_tile = (head_entry[0], head_entry[1])

        next_index = head_path_index + 1
        if next_index < path_length:
            next_entry = train.current_path[next_index]
            next_direction = self._direction_between_tiles(
                head_tile, (next_entry[0], next_entry[1])
            )
            if next_direction == facing_direction:
                return 1

        prev_index = head_path_index - 1
        if prev_index >= 0:
            prev_entry = train.current_path[prev_index]
            prev_direction = self._direction_between_tiles(
                head_tile, (prev_entry[0], prev_entry[1])
            )
            if prev_direction == facing_direction:
                return -1

        if self.active_cab == "B":
            if head_path_index > 0:
                return -1
            if head_path_index < path_length - 1:
                return 1

        # Cab A follows path order and should not implicitly reverse at terminal tiles.
        if self.active_cab == "A":
            return 1

        return 1

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

        # Determine path heading and required cab
        path_heading = "a"  # Default
        required_cab = "A"  # Default

        # Check path direction to determine which cab should be active
        if hasattr(path, "end_a") and hasattr(path, "end_b"):
            # Path has defined ends - determine which end we're heading toward
            if train.position:
                # If train is closer to end_a, we're heading to end_b
                # This is simplified - real implementation would trace path direction
                path_heading = "b"
                required_cab = "A"  # Cab A faces forward

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
                # Check if platform is in the route's waypoints
                for waypoint in route.waypoints:
                    if waypoint.platform_id == station_id:
                        return True

        # If no route, consider any station as a potential stop
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

    def _get_reaction_distance_cm(self, current_speed_kmh: float) -> float:
        """
        Calculate reaction distance based on current speed with quadratic scaling.

        Uses quadratic scaling because braking distance grows with v² (kinetic energy).
        At higher speeds, we need to look proportionally further ahead to have time
        to react to speed restrictions.

        Formula: distance = base_distance * (speed / reference_speed)²
        - At reference speed (30 km/h): uses base reaction time
        - At 60 km/h: looks 4x further ahead
        - At 15 km/h: looks 0.25x the base distance (but minimum floor applies)

        A minimum floor ensures we always look far enough ahead to see upcoming
        speed limits, even when starting from a stop.

        This matches real driving: faster = look much further ahead.

        Args:
            current_speed_kmh: Current train speed in km/h

        Returns:
            Distance in cm the train should look ahead
        """
        # Convert km/h to cm/s
        km_h_to_cm_s = get_speed_conversion_factor()
        speed_cm_s = current_speed_kmh * km_h_to_cm_s

        # Base distance at reference speed
        base_distance = speed_cm_s * REACTION_TIME_SECONDS

        # Quadratic scaling factor: (current_speed / reference_speed)²
        # This makes high-speed trains look much further ahead
        if current_speed_kmh > 0 and LOOKAHEAD_REFERENCE_SPEED_KMH > 0:
            speed_factor = current_speed_kmh / LOOKAHEAD_REFERENCE_SPEED_KMH
            # Apply quadratic scaling, but cap at reasonable limits
            # At 60 km/h: factor = 2, scaled = 4x
            # At 90 km/h: factor = 3, scaled = 9x (capped to 6x for stability)
            quadratic_factor = min(speed_factor * speed_factor, 6.0)
            scaled_distance = base_distance * quadratic_factor
        else:
            scaled_distance = base_distance

        # Apply minimum floor to ensure we see ahead even at low speeds
        # This is critical when accelerating from a station - we need to see
        # the higher speed limits ahead to know we can accelerate more
        return max(scaled_distance, LOOKAHEAD_MIN_DISTANCE_CM)

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

        # Handle edge cases
        if distance_cm <= curve[0].get("distance_cm", 0):
            return curve[0].get(key, 0.0)

        if distance_cm >= curve[-1].get("distance_cm", 0):
            return curve[-1].get(key, 0.0)

        # Find bracketing points and interpolate
        for i in range(len(curve) - 1):
            d1 = curve[i].get("distance_cm", 0)
            d2 = curve[i + 1].get("distance_cm", 0)
            if d1 <= distance_cm <= d2:
                v1 = curve[i].get(key, 0.0)
                v2 = curve[i + 1].get(key, 0.0)
                # Linear interpolation
                t = (distance_cm - d1) / (d2 - d1) if d2 != d1 else 0
                return v1 + t * (v2 - v1)

        return curve[-1].get(key, 0.0)

    def _get_future_aware_target(self, current_speed_kmh: float) -> float:
        """
        Get future-aware target speed by looking ahead in the ATO curve.

        Uses multi-point sampling to smooth out abrupt speed curve changes.
        Samples at 0, 1/3, 2/3, and full reaction distance, then takes a
        weighted average (more weight on nearer points) to create a smooth
        transition rather than abrupt target changes.

        Args:
            current_speed_kmh: Current train speed

        Returns:
            Future-aware target speed in km/h
        """
        train = self._get_train()
        if not train:
            return 0.0

        # Get the speed curves from lookahead
        lookahead = self.get_monitor_lookahead()
        curves = lookahead.get("lookahead", {}).get("speed_curves", [])

        if not curves:
            # Fallback: use traditional limits with ATO margin
            return min(
                train.max_speed,
                self.atc_speed_limit - ATO_SPEED_MARGIN_KMH,
                self.current_block_speed_limit,
            )

        # Multi-point sampling for smooth target transitions
        # Sample at 4 points along the reaction distance
        reaction_dist = self._get_reaction_distance_cm(current_speed_kmh)

        # Sample points: 0, 1/3, 2/3, and full reaction distance
        # Weights: nearer points have more influence (4, 3, 2, 1)
        sample_distances = [
            0.0,
            reaction_dist / 3,
            reaction_dist * 2 / 3,
            reaction_dist,
        ]
        weights = [4.0, 3.0, 2.0, 1.0]  # More weight on current position

        weighted_sum = 0.0
        weight_total = 0.0
        min_sampled = float("inf")

        for dist, weight in zip(sample_distances, weights):
            limit = self._sample_curve_at(curves, dist, "operational_limit")
            weighted_sum += limit * weight
            weight_total += weight
            min_sampled = min(min_sampled, limit)

        # Use weighted average for smoothness, but also respect minimum for safety
        # Blend: 70% weighted average + 30% minimum (safety margin)
        weighted_avg = weighted_sum / weight_total if weight_total > 0 else 0.0
        effective_target = weighted_avg * 0.7 + min_sampled * 0.3

        # Respect train max speed
        effective_target = min(effective_target, train.max_speed)

        return max(0.0, effective_target)

    def _calculate_distance_aware_handle(
        self,
        current_speed: float,
        current_handle: HandlePosition,
        target_speed: float,
        distance_cm: float,
        curves: List[Dict],
    ) -> HandlePosition:
        """
        Calculate handle based on distance-aware deceleration requirements.

        Instead of using speed difference thresholds, this calculates the actual
        deceleration needed to reach target_speed in the remaining distance,
        then maps that to an appropriate handle notch.

        This prevents:
        1. Jitter around speed limits - handle is tied to physics, not speed diff
        2. B8 at low speeds - required decel is proportional to distance remaining

        Args:
            current_speed: Current train speed (km/h)
            current_handle: Current handle position
            target_speed: Target speed at the restriction point (km/h)
            distance_cm: Distance to the restriction point (cm)
            curves: Speed curve data for context

        Returns:
            HandlePosition based on required deceleration
        """
        profile = self._get_speed_profile()

        # Calculate required deceleration to reach target_speed in distance_cm
        # Using: v_f^2 = v_i^2 - 2*a*d  =>  a = (v_i^2 - v_f^2) / (2*d)
        speed_diff = current_speed - target_speed

        # If we're already at or below target, no braking needed
        if speed_diff <= 0:
            return (
                current_handle
                if self._is_power_handle(current_handle)
                else HandlePosition.N
            )

        # Calculate required deceleration rate (km/h/s)
        # Convert speeds to consistent units and apply formula
        km_h_to_cm_s = get_speed_conversion_factor()
        v_current_cm_s = current_speed * km_h_to_cm_s
        v_target_cm_s = target_speed * km_h_to_cm_s

        # Required deceleration in cm/s^2
        if distance_cm > 0:
            required_decel_cm_s2 = (v_current_cm_s**2 - v_target_cm_s**2) / (
                2 * distance_cm
            )
            # Convert back to km/h/s
            required_decel_kmh_s = required_decel_cm_s2 / km_h_to_cm_s
        else:
            # At the restriction point, must match target immediately
            required_decel_kmh_s = float("inf") if speed_diff > 0 else 0

        # Map required deceleration to handle notch using profile rates
        # Each brake notch provides a certain deceleration rate
        # B1 = light brake, B8 = full service brake
        service_brake_rate = profile.deceleration_rate  # km/h/s for full brake

        if service_brake_rate <= 0:
            return (
                current_handle
                if self._is_power_handle(current_handle)
                else HandlePosition.N
            )

        # Calculate what fraction of service brake we need
        if required_decel_kmh_s <= 0:
            # No braking required
            return (
                current_handle
                if self._is_power_handle(current_handle)
                else HandlePosition.N
            )

        brake_fraction = required_decel_kmh_s / service_brake_rate

        # Map brake fraction to handle notch with hysteresis
        # B1 = ~12.5% brake (1/8), B8 = 100% brake
        # Add some headroom - aim for 80% of required to avoid overshoot
        brake_fraction *= 0.8

        # Clamp to valid range
        brake_fraction = max(0.0, min(1.0, brake_fraction))

        # Convert fraction to notch (B1-B8)
        # B1 = 1/8, B2 = 2/8, ..., B8 = 8/8
        notch_level = max(1, round(brake_fraction * 8))

        # Handle notch transition with hysteresis to prevent jitter
        if self._is_brake_handle(current_handle):
            # Already braking - only change if significant difference (>1 notch)
            current_level = self._get_brake_level(current_handle)
            if abs(notch_level - current_level) > 1:
                return self._brake_level_to_handle(min(8, notch_level))
            return current_handle
        else:
            # Not braking yet - only start if we need at least B2
            if notch_level >= 2:
                return self._brake_level_to_handle(min(8, notch_level))
            return current_handle

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

    def _ato_calculate_handle(self) -> HandlePosition:
        """
        ATO handle calculation with distance-aware braking.

        Uses distance-aware deceleration calculation instead of speed difference
        thresholds. This creates smoother braking that follows the physics curve
        rather than reactive speed-difference logic.

        The control logic is:
        1. Get speed curves and find upcoming speed restrictions
        2. Calculate required deceleration based on distance to restriction
        3. Map deceleration to handle notch (prevents B8 at low speeds)
        4. Use hysteresis to prevent handle jitter

        Also sets self.handle_reason with diagnostic info about why the handle was chosen.
        """
        train = self._get_train()
        if not train:
            self.handle_reason = "No train"
            return HandlePosition.N

        current_speed = train.current_speed
        current_handle = self.handle_position

        # Determine the result handle - will be updated and returned at the end
        result_handle: HandlePosition = current_handle
        reason: str = ""

        # Get speed curves for distance-aware calculation
        lookahead = self.get_monitor_lookahead()
        curves = lookahead.get("lookahead", {}).get("speed_curves", [])

        # Use future-aware target for anticipatory braking
        effective_target = self._get_future_aware_target(current_speed)

        # Also cap at traditional limits as fallback
        effective_target = min(
            effective_target,
            train.max_speed,
            self.atc_speed_limit,
            self.current_block_speed_limit,
        )

        # Find the next speed restriction from the curves
        next_restriction_speed = effective_target
        next_restriction_distance = float("inf")

        if curves:
            # Find where the curve drops below current speed
            for point in curves:
                curve_speed = point.get("operational_limit", current_speed)
                distance = point.get("distance_cm", 0)
                if curve_speed < current_speed - 1.0:  # At least 1 km/h drop
                    next_restriction_speed = curve_speed
                    next_restriction_distance = distance
                    break

        # Check if we need to stop (clearance limit or path end)
        profile = self._get_speed_profile()

        # Handle stopping case - ALWAYS use traditional braking logic
        # Stopping takes priority over distance-aware smoothing
        if effective_target <= STOPPED_SPEED_THRESHOLD_KMH:
            if current_speed > STOPPED_SPEED_THRESHOLD_KMH:
                result_handle = self._calculate_braking_notch(current_speed)
                reason = f"Stopping: target={effective_target:.1f}, braking at {current_speed:.1f}"
            else:
                result_handle = HandlePosition.N
                reason = "Stopped"
            self._previous_speed = current_speed
            self.handle_reason = reason
            return result_handle

        # Use distance-aware handle calculation for speed restrictions
        distance_aware_handle = None

        # Only use distance-aware for reasonably close restrictions (< 100m)
        # For far restrictions, traditional trend-aware logic works better
        DISTANCE_AWARE_THRESHOLD_CM = 10000.0  # 100 meters

        # Check if we should skip distance-aware braking because we're accelerating
        # This happens when leaving a slow block (e.g., station) for a fast block
        is_accelerating_scenario = (
            current_speed <= self.current_block_speed_limit - ATO_SPEED_TOLERANCE
            and effective_target
            >= self.current_block_speed_limit - ATO_SPEED_MARGIN_KMH
        )

        if (
            next_restriction_distance < float("inf")
            and next_restriction_distance < DISTANCE_AWARE_THRESHOLD_CM
            and next_restriction_speed < current_speed - 1.0
            and not is_accelerating_scenario
        ):
            # We have an upcoming restriction within reasonable distance - use distance-aware
            distance_aware_handle = self._calculate_distance_aware_handle(
                current_speed,
                current_handle,
                next_restriction_speed,
                next_restriction_distance,
                curves,
            )

        if distance_aware_handle is not None:
            # Add hysteresis: if speed is within tolerance of target and stable, maintain handle
            if abs(current_speed - effective_target) < ATO_ENTRY_MARGIN:
                trend = self._detect_speed_trend(current_speed)
                if trend == "stable":
                    result_handle = current_handle
                    reason = f"DistAware hysteresis: stable near target ({effective_target:.1f})"
                    self._previous_speed = current_speed
                    self.handle_reason = reason
                    return result_handle
            result_handle = distance_aware_handle
            reason = f"DistAware: restrict {next_restriction_speed:.1f} in {next_restriction_distance:.0f}cm"
            self._previous_speed = current_speed
            self.handle_reason = reason
            return result_handle

        # No restriction ahead - maintain speed using traditional logic
        trend = self._detect_speed_trend(current_speed)
        speed_diff = effective_target - current_speed

        # Speed Up Zone
        if current_speed < effective_target - ATO_EXIT_MARGIN:
            optimal_notch = self._calculate_power_notch(current_speed, effective_target)

            if self._is_brake_handle(current_handle):
                # Transition from brake to neutral first (one step at a time)
                result_handle = self._adjust_handle_toward_power(current_handle)
                reason = f"SpeedUp: releasing brake, gap={speed_diff:.1f}"
            elif current_handle == HandlePosition.N:
                # From neutral, go directly to optimal power notch
                result_handle = optimal_notch
                reason = f"SpeedUp: N→{optimal_notch.value}, gap={speed_diff:.1f}"
            else:
                # Already powering - use optimal notch for the speed difference
                # This ensures we accelerate properly, not stuck at P1
                result_handle = optimal_notch
                reason = f"SpeedUp: optimal={optimal_notch.value}, gap={speed_diff:.1f}"

        # Hysteresis band - maintain current handle
        elif current_speed < effective_target - ATO_ENTRY_MARGIN:
            result_handle = current_handle
            reason = f"Hysteresis: maintaining {current_handle.value}, target={effective_target:.1f}"

        # Slow Down Zone (but no major restriction) - gentle adjustment
        else:
            speed_overshoot = current_speed - effective_target

            # Fast response: large overshoot (>= 3 km/h) needs immediate braking
            # Note: With ATO margin of 5 km/h, overshoot of 3 means we're 2 km/h from ATC limit
            if speed_overshoot >= 3.0:
                if (
                    self._is_power_handle(current_handle)
                    or current_handle == HandlePosition.N
                ):
                    result_handle = HandlePosition.B2  # Moderate braking
                    reason = f"SlowDown: overshoot={speed_overshoot:.1f}≥3, applying B2"
                elif self._is_brake_handle(current_handle):
                    # Already braking - increase by at most 2 notches to prevent B8 jumps
                    current_level = self._get_brake_level(current_handle)
                    # Cap at B6 to leave room for ATC, and limit increase
                    new_level = min(6, current_level + 2)
                    result_handle = self._brake_level_to_handle(new_level)
                    reason = f"SlowDown: overshoot={speed_overshoot:.1f}≥3, B{current_level}→B{new_level}"
                else:
                    result_handle = current_handle
                    reason = f"SlowDown: overshoot={speed_overshoot:.1f}≥3, maintaining"
            elif self._is_power_handle(current_handle):
                result_handle = HandlePosition.N
                reason = f"SlowDown: power→N, overshoot={speed_overshoot:.1f}"
            elif self._is_brake_handle(current_handle):
                if trend == "rising":
                    # Braking but speed still rising - brake harder, but limit increase
                    # Only increase by 1 notch to prevent sudden B8 jumps
                    current_level = self._get_brake_level(current_handle)
                    new_level = min(6, current_level + 1)  # Cap at B6
                    result_handle = self._brake_level_to_handle(new_level)
                    reason = f"SlowDown: rising+brake, B{current_level}→B{new_level}"
                elif trend == "falling":
                    # Speed is falling - release brakes faster to prevent over-braking
                    # Jump to neutral if we're close to target, otherwise step down
                    if speed_overshoot < 2.0:
                        result_handle = HandlePosition.N
                        reason = (
                            f"SlowDown: falling, overshoot={speed_overshoot:.1f}<2, →N"
                        )
                    else:
                        result_handle = self._adjust_handle_toward_power(current_handle)
                        reason = f"SlowDown: falling, releasing brake"
                else:
                    # Stable - maintain current brake
                    result_handle = current_handle
                    reason = f"SlowDown: stable, maintaining {current_handle.value}"
            else:  # Neutral handle
                if trend == "rising":
                    # Neutral but speed rising above target - start gentle brake
                    result_handle = HandlePosition.B1
                    reason = f"SlowDown: N+rising, →B1"
                else:
                    result_handle = HandlePosition.N
                    reason = f"SlowDown: N+{trend}, maintaining N"

        # Update previous speed for trend detection on next tick
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

            if train.position and train.position.facing_direction is not None:
                train.facing_direction = train.position.facing_direction

            # When switching cabs (A <-> B), the physical train doesn't move.
            # The driver walks to the other end. What changes:
            # 1. leading_end flips (done by train.set_cab)
            # 2. facing_direction reverses
            # 3. entry_edge for BOTH ends flips (since train will travel opposite direction)
            # 4. distance_cm for BOTH ends flips (10cm from W = tile_length - 10cm from E)
            if old_cab in ("A", "B") and old_cab != cab:
                # Flip facing_direction
                new_direction: Optional[Direction] = None
                if train.facing_direction is not None:
                    new_direction = train.facing_direction.opposite()
                elif train.position and train.position.facing_direction is not None:
                    new_direction = train.position.facing_direction

                if new_direction is not None:
                    train.facing_direction = new_direction
                    if train.position:
                        train.position.facing_direction = new_direction

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
                    if train.current_path and train.position:
                        new_head_tile = train.position.head_tile
                        for i, (x, y, _) in enumerate(train.current_path):
                            if (x, y) == new_head_tile:
                                train.path_index = i
                                break

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

        ATO Automation:
        - Automatically checks and corrects cab direction to match path
        - Uses set_cab() for proper state updates (facing_direction, entry edges, etc.)
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

        # ATO Auto-correction: Check and adjust cab to match path direction
        if self.ato_info and self.ato_info.required_cab:
            required_cab = self.ato_info.required_cab
            if self.active_cab != required_cab:
                # Cab doesn't match path direction - ATO should auto-correct
                logger.info(
                    "[TrainController %s] ATO auto-correcting cab: %s -> %s",
                    self.train_id,
                    self.active_cab,
                    required_cab,
                )
                result = self.set_cab(required_cab)
                if not result.success:
                    return CommandResult(
                        False,
                        f"ATO cannot start: cab auto-correction failed - {result.message}",
                    )
                self.pending_notifications.append(
                    f"ATO auto-switched cab to {required_cab} for path direction"
                )

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
                        train.active_cab = self.active_cab
                        if train.position:
                            train.position.leading_end = self.active_cab.lower()

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

        head_path_index = self._get_head_path_index(train, head_tile)
        if head_path_index < 0 or not train.current_path:
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
            behind_end = -1 if path_scan_step == 1 else len(train.current_path)
            for j in range(behind_start, behind_end, -path_scan_step):
                prev_entry = train.current_path[j]
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

        loop_end = len(train.current_path) if path_scan_step == 1 else -1
        for i in range(head_path_index, loop_end, path_scan_step):
            path_entry = train.current_path[i]
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
                    next_entry = train.current_path[j]
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
                        for j in range(i, len(train.current_path)):
                            next_entry = train.current_path[j]
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
            if 0 <= next_index < len(train.current_path):
                next_entry = train.current_path[next_index]
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
