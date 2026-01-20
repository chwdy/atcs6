"""
Train Physics - Physical Movement, Block Management, and Voltage Output

Definition:
This module provides the TrainPhysics class which handles the physical execution
of train movement. While TrainController decides WHAT speed the train should have,
TrainPhysics executes HOW the train moves.

Responsibilities:
- Convert handle position to acceleration/deceleration rates
- Update train speed based on acceleration and time delta
- Move train position along the track (head and tail)
- Manage block occupancy (occupy new blocks, release vacated blocks)
- Output voltage to BlockPowerController

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
        +-------+                  |
                                   v
                           +------------------+
                           |BlockPowerController|
                           +------------------+

Design Principles:
1. Pure Physics: Only handles movement calculations and world interactions
2. Reads from Controller: Gets handle_position, power state from TrainController
3. Writes to Train: Updates position, current_speed on Train entity
4. Writes to World: Updates BlockManager states, BlockPowerController voltages
5. No Decision Making: Doesn't decide speed limits or handle positions

TODO List:
- [x] Define TrainPhysics class with core methods
- [x] Implement tick() method
- [x] Implement speed calculation from handle
- [x] Implement position movement
- [x] Implement block occupancy management
- [x] Implement voltage output
- [ ] Add comprehensive test suite
- [ ] Migrate SimulationController to use TrainPhysics
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from enums import (
    BRAKE_MULTIPLIERS,
    POWER_MULTIPLIERS,
    HandlePosition,
    TrainState,
)
from train_config import (
    DEFAULT_ACCELERATION_RATE,
    DEFAULT_DECELERATION_RATE,
    DEFAULT_EMERGENCY_BRAKE_RATE,
    DEFAULT_LIGHTING_VOLTAGE,
    STOPPED_SPEED_THRESHOLD_KMH,
    kmh_to_cm_per_sec,
)

if TYPE_CHECKING:
    from block_manager import BlockManager
    from block_power import BlockPowerController
    from network import RailwayNetwork
    from speed_profile import SpeedProfile
    from train import Train
    from train_controller import TrainController


# Edge opposites for track geometry
OPPOSITE_EDGES = {"N": "S", "S": "N", "E": "W", "W": "E"}


@dataclass
class TrainPhysics:
    """
    Physical movement, block management, and voltage output for one train.

    TrainPhysics executes the physical simulation:
    - Converts handle position to acceleration/deceleration
    - Updates train speed and position
    - Manages block occupancy (occupy/release)
    - Outputs voltage to BlockPowerController

    It does NOT decide:
    - Speed limits (that's TrainController/ATC)
    - Handle position (that's TrainController/ATO)
    - When to start/stop (that's TrainController)

    Attributes:
        train_id: ID of the train this physics instance controls
        network: Reference to the network for track/block access
        controller: Reference to TrainController for reading state
    """

    train_id: str
    network: Optional["RailwayNetwork"] = None
    controller: Optional["TrainController"] = None

    # Track last occupied blocks for delta calculations
    _last_occupied_blocks: Set[str] = field(default_factory=set)

    # =========================================================================
    # Internal Helpers
    # =========================================================================

    def _get_train(self) -> Optional["Train"]:
        """Get the Train entity this physics instance controls."""
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

    def _get_block_manager(self) -> Optional["BlockManager"]:
        """Get the BlockManager from network."""
        if self.network and hasattr(self.network, "block_manager"):
            return self.network.block_manager
        return None

    def _get_power_controller(self) -> Optional["BlockPowerController"]:
        """Get the BlockPowerController from network."""
        if self.network and hasattr(self.network, "power_controller"):
            return self.network.power_controller
        return None

    # =========================================================================
    # Tick Method (Called Each Simulation Tick)
    # =========================================================================

    def tick(self, delta: float) -> None:
        """
        Called each simulation tick by SimulationController.

        Executes physical movement based on TrainController state.

        Args:
            delta: Time elapsed since last tick (seconds)
        """
        train = self._get_train()
        if not train:
            return

        controller = self.controller
        if not controller:
            return

        # 1. Skip if powered off - set all blocks to 0V
        if not controller.power_on:
            self._set_voltage_for_blocks(train, 0.0)
            return

        # 2. Get acceleration from handle position
        profile = self._get_speed_profile()
        acceleration = self._get_acceleration_for_handle(
            controller.handle_position,
            profile.acceleration_rate,
            profile.deceleration_rate,
            profile.emergency_brake_rate,
        )

        # 3. Handle special case: target_speed < current_speed requires deceleration
        # regardless of handle position (ATC override)
        target = controller.target_speed if controller.target_speed >= 0 else 0.0
        if target < train.current_speed:
            # Force deceleration when target is below current speed
            if acceleration > 0:
                acceleration = -profile.deceleration_rate

        # 4. Update speed based on acceleration
        new_speed = train.current_speed + (acceleration * delta)

        # 5. Clamp speed to valid range [0, target_speed]
        new_speed = max(0.0, min(new_speed, target))

        # 6. If braking and speed is low enough, stop completely
        if acceleration < 0 and new_speed <= STOPPED_SPEED_THRESHOLD_KMH:
            new_speed = 0.0

        # 7. When coasting (N), apply minimal friction/resistance
        if controller.handle_position == HandlePosition.N and train.current_speed > 0:
            # Gentle deceleration from friction (using profile's friction_rate)
            friction_decel = profile.friction_rate * delta
            new_speed = max(0.0, new_speed - friction_decel)

        train.current_speed = new_speed

        # 7. Calculate distance to move
        distance_cm = self._speed_to_distance(new_speed, delta)

        # 8. Move train position
        if distance_cm > 0:
            self._move_train(train, distance_cm)

        # 9. Update block occupancy
        self._update_block_occupancy(train)

        # 10. Output voltage to occupied blocks
        self._update_voltage_output(train, controller)

    # =========================================================================
    # Speed and Acceleration
    # =========================================================================

    def _get_acceleration_for_handle(
        self,
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

    def _speed_to_distance(self, speed_kmh: float, delta: float) -> float:
        """
        Convert speed (km/h) and time (seconds) to distance (cm).

        Uses kmh_to_cm_per_sec which applies model scale.

        Args:
            speed_kmh: Speed in km/h
            delta: Time in seconds

        Returns:
            Distance in cm
        """
        cm_per_second = kmh_to_cm_per_sec(speed_kmh)
        return cm_per_second * delta

    # =========================================================================
    # Movement Logic
    # =========================================================================

    def _move_train(self, train: "Train", distance_cm: float) -> None:
        """
        Move train forward by distance_cm along the track.

        Updates both head and tail positions, handling tile transitions.

        Args:
            train: Train to move
            distance_cm: Distance to move in cm
        """
        if not train.position:
            # Fall back to legacy position system
            self._move_train_legacy(train, distance_cm)
            return

        position = train.position

        # Determine which end is head based on leading_end
        if position.leading_end == "a":
            # end_a is head
            self._advance_end(
                train,
                position.end_a_tile,
                position.end_a_distance_cm,
                position.end_a_entry_edge,
                distance_cm,
                is_end_a=True,
            )
        else:
            # end_b is head
            self._advance_end(
                train,
                position.end_b_tile,
                position.end_b_distance_cm,
                position.end_b_entry_edge,
                distance_cm,
                is_end_a=False,
            )

        # Recalculate tail position based on train length
        self._update_tail_position(train)

        # Update occupied tiles list
        self._update_occupied_tiles(train)

    def _advance_end(
        self,
        train: "Train",
        current_tile: Tuple[int, int],
        current_distance: float,
        entry_edge: str,
        distance_cm: float,
        is_end_a: bool,
    ) -> None:
        """
        Advance one end of the train by distance_cm.

        Handles tile transitions when end moves past tile boundary.
        """
        position = train.position
        if not position:
            return

        new_distance = current_distance + distance_cm
        new_tile = current_tile
        new_entry_edge = entry_edge

        # Handle tile transition if distance exceeds tile length
        tile_length = self._get_tile_length_cm(new_tile)

        while new_distance >= tile_length and tile_length > 0:
            new_distance -= tile_length

            # Get exit edge based on track geometry
            exit_edge = self._calculate_exit_edge(new_tile, new_entry_edge)
            if not exit_edge:
                # End of track - stop at tile boundary
                new_distance = tile_length
                train.current_speed = 0.0
                break

            # Get next tile
            next_tile = self._get_neighbor_tile(new_tile, exit_edge)
            if next_tile is None:
                # End of track - stop at tile boundary
                new_distance = tile_length
                train.current_speed = 0.0
                break

            # Move to next tile
            new_tile = next_tile
            new_entry_edge = OPPOSITE_EDGES[exit_edge]
            tile_length = self._get_tile_length_cm(new_tile)

        # Update position
        if is_end_a:
            position.end_a_tile = new_tile
            position.end_a_distance_cm = new_distance
            position.end_a_entry_edge = new_entry_edge
        else:
            position.end_b_tile = new_tile
            position.end_b_distance_cm = new_distance
            position.end_b_entry_edge = new_entry_edge

    def _move_train_legacy(self, train: "Train", distance_cm: float) -> None:
        """
        DEPRECATED: Move train using legacy position system (distance_on_track).

        This is a fallback for trains without TrainPosition.
        This method will be removed in a future version.
        All trains should use TrainPosition for accurate head/tail tracking.
        """
        import warnings

        warnings.warn(
            f"Train {train.train_id} is using legacy position system. "
            "Please migrate to TrainPosition.",
            DeprecationWarning,
            stacklevel=3,
        )

        if not train.current_pos:
            return

        # Update distance on current track
        train.distance_on_track = getattr(train, "_distance_on_track", 0.0)
        train.distance_on_track += distance_cm

        # Check if we need to advance to next tile
        tile_length = self._get_tile_length_cm(train.current_pos)

        while train.distance_on_track >= tile_length and tile_length > 0:
            train.distance_on_track -= tile_length

            # Try to advance to next track
            if not self._advance_train_to_next_track(train):
                # End of track
                train.distance_on_track = tile_length
                train.current_speed = 0.0
                break

            tile_length = self._get_tile_length_cm(train.current_pos)

    def _advance_train_to_next_track(self, train: "Train") -> bool:
        """
        Advance train to next connected track segment.

        Returns:
            True if successfully advanced, False if end of track
        """
        if not train.current_pos or not self.network:
            return False

        current_track = self.network.get_track(*train.current_pos)
        if not current_track:
            return False

        # Get entry edge from direction
        entry_edge = self._direction_to_edge(train.current_direction)
        exit_edges = current_track.get_exits(entry_edge, use_switch_state=True)

        if not exit_edges:
            return False

        exit_edge = exit_edges[0]

        # Get neighbor tile
        neighbor_info = self.network.get_neighbor(
            train.current_pos[0], train.current_pos[1], exit_edge
        )

        if not neighbor_info or not neighbor_info[0]:
            return False

        neighbor_track, new_entry_edge = neighbor_info

        # Update position
        dx, dy = self._get_direction_delta(exit_edge)
        train.current_pos = (train.current_pos[0] + dx, train.current_pos[1] + dy)
        train.current_direction = self._edge_to_direction(new_entry_edge)
        train.distance_on_track = 0.0

        return True

    def _update_tail_position(self, train: "Train") -> None:
        """
        Calculate tail position by tracing backward from head by train length.

        The tail is always train.length_cm behind the head along the track.
        """
        position = train.position
        if not position:
            return

        # Get head position
        if position.leading_end == "a":
            head_tile = position.end_a_tile
            head_entry = position.end_a_entry_edge
            head_distance = position.end_a_distance_cm
        else:
            head_tile = position.end_b_tile
            head_entry = position.end_b_entry_edge
            head_distance = position.end_b_distance_cm

        # Calculate remaining length to trace backward
        remaining_length = train.length_cm

        # Subtract head's distance from entry (how far we are into this tile)
        remaining_length -= head_distance

        current_tile = head_tile
        current_entry = head_entry

        # If train fits in current tile
        if remaining_length <= 0:
            if position.leading_end == "a":
                position.end_b_tile = current_tile
                position.end_b_entry_edge = current_entry
                position.end_b_distance_cm = head_distance - train.length_cm
            else:
                position.end_a_tile = current_tile
                position.end_a_entry_edge = current_entry
                position.end_a_distance_cm = head_distance - train.length_cm
            return

        # Trace backward through tiles
        while remaining_length > 0:
            # Go to previous tile (enter from entry edge direction)
            prev_tile = self._get_neighbor_tile(current_tile, current_entry)

            if prev_tile is None:
                # Start of track - tail is at entry edge of first tile
                break

            # Move to previous tile
            current_tile = prev_tile
            current_entry = OPPOSITE_EDGES.get(current_entry, current_entry)

            tile_length = self._get_tile_length_cm(current_tile)
            remaining_length -= tile_length

        # Tail is in this tile
        tail_distance = abs(remaining_length) if remaining_length < 0 else 0.0

        if position.leading_end == "a":
            position.end_b_tile = current_tile
            position.end_b_entry_edge = current_entry
            position.end_b_distance_cm = (
                tile_length - remaining_length if remaining_length < 0 else 0.0
            )
        else:
            position.end_a_tile = current_tile
            position.end_a_entry_edge = current_entry
            position.end_a_distance_cm = (
                tile_length - remaining_length if remaining_length < 0 else 0.0
            )

    def _update_occupied_tiles(self, train: "Train") -> None:
        """
        Update the list of tiles occupied by the train.

        Traces from tail to head and records all tiles.
        """
        position = train.position
        if not position:
            return

        # Get head and tail tiles
        if position.leading_end == "a":
            head_tile = position.end_a_tile
            tail_tile = position.end_b_tile
            head_entry = position.end_a_entry_edge
            tail_entry = position.end_b_entry_edge
        else:
            head_tile = position.end_b_tile
            tail_tile = position.end_a_tile
            head_entry = position.end_b_entry_edge
            tail_entry = position.end_a_entry_edge

        # Simple case: head and tail on same tile
        if head_tile == tail_tile:
            position.occupied_tiles = [head_tile]
            return

        # Trace from tail to head
        occupied = [tail_tile]
        current_tile = tail_tile
        current_entry = tail_entry

        max_iterations = 100  # Safety limit
        for _ in range(max_iterations):
            if current_tile == head_tile:
                break

            # Get exit edge and next tile
            exit_edge = self._calculate_exit_edge(current_tile, current_entry)
            if not exit_edge:
                break

            next_tile = self._get_neighbor_tile(current_tile, exit_edge)
            if not next_tile:
                break

            occupied.append(next_tile)
            current_tile = next_tile
            current_entry = OPPOSITE_EDGES[exit_edge]

        position.occupied_tiles = occupied

    # =========================================================================
    # Block Occupancy Management
    # =========================================================================

    def _update_block_occupancy(self, train: "Train") -> None:
        """
        Update which blocks this train occupies based on head/tail positions.

        - Occupy new blocks as head enters them
        - Release old blocks as tail exits them
        """
        block_manager = self._get_block_manager()
        if not block_manager:
            return

        # Calculate current occupied blocks from position
        new_occupied = self._get_blocks_for_train(train)

        old_occupied = self._last_occupied_blocks

        # Occupy new blocks
        for block_id in new_occupied - old_occupied:
            block_manager.occupy_block(block_id, train.train_id)

        # Release old blocks (tail has exited)
        for block_id in old_occupied - new_occupied:
            # Use set_block_to_caution for proper safety transition
            # (OCCUPIED -> CAUTION -> FREE, not OCCUPIED -> FREE)
            block_manager.set_block_to_caution(block_id)

        # Update tracking
        self._last_occupied_blocks = new_occupied

        # Update TrainPosition's occupied_blocks if available
        if train.position:
            train.position.occupied_blocks = list(new_occupied)

    def _get_blocks_for_train(self, train: "Train") -> Set[str]:
        """
        Get all block IDs occupied by the train.

        Uses occupied_tiles from TrainPosition or falls back to current_pos.
        """
        blocks: Set[str] = set()

        if not self.network:
            return blocks

        # Get occupied tiles
        if train.position and train.position.occupied_tiles:
            tiles = train.position.occupied_tiles
        elif train.current_pos:
            tiles = [train.current_pos]
        else:
            return blocks

        # Get block for each tile
        for tile in tiles:
            track = self.network.tracks.get(tile)
            if track and track.block_section_id:
                blocks.add(track.block_section_id)

        return blocks

    # =========================================================================
    # Voltage Output
    # =========================================================================

    def _update_voltage_output(
        self, train: "Train", controller: "TrainController"
    ) -> None:
        """
        Output voltage to all blocks occupied by this train.

        Uses SpeedProfile to convert target speed to voltage.
        """
        power_controller = self._get_power_controller()
        if not power_controller:
            return

        profile = self._get_speed_profile()

        # Determine voltage based on train state
        if train.state == TrainState.OFF:
            voltage = 0.0
        elif train.state in (
            TrainState.IDLE,
            TrainState.STANDBY,
            TrainState.RUNNING_WAITING,
            TrainState.RUNNING_AT_STATION,
        ):
            # Lighting voltage for non-moving states
            voltage = profile.lighting_voltage
        elif train.state == TrainState.RUNNING_MOVING:
            # Speed-based voltage
            voltage = profile.speed_to_voltage(controller.target_speed)
        elif train.state == TrainState.MANUAL:
            # Manual mode - use manual target speed
            voltage = profile.speed_to_voltage(train.manual_target_speed)
        else:
            voltage = 0.0

        # Emergency stop override
        if controller.emergency_stop_active:
            voltage = 0.0

        # Set voltage for all occupied blocks
        self._set_voltage_for_blocks(train, voltage)

    def _set_voltage_for_blocks(self, train: "Train", voltage: float) -> None:
        """Set specific voltage for all occupied blocks."""
        power_controller = self._get_power_controller()
        if not power_controller:
            return

        occupied = self._get_blocks_for_train(train)
        for block_id in occupied:
            power_controller.set_block_voltage(block_id, voltage)

    # =========================================================================
    # Track Geometry Helpers
    # =========================================================================

    def _get_tile_length_cm(self, tile: Tuple[int, int]) -> float:
        """Get length of a tile in cm."""
        if not self.network:
            return 100.0  # Default fallback

        return self.network._get_tile_length_cm(tile[0], tile[1])

    def _calculate_exit_edge(
        self, tile: Tuple[int, int], entry_edge: str
    ) -> Optional[str]:
        """
        Calculate exit edge based on track geometry and entry edge.

        Uses the 6-bit track system to determine valid paths through the tile.
        """
        if not self.network:
            return None

        track = self.network.get_track(*tile)
        if not track:
            return None

        # Get all possible exits from entry edge based on track bits
        possible_exits = track.get_exits(entry_edge, use_switch_state=True)

        if not possible_exits:
            return None

        # If train has a path, follow the path
        train = self._get_train()
        if train and train.current_path:
            next_tile_in_path = self._get_next_tile_in_path(train, tile)
            if next_tile_in_path:
                # Determine which edge leads to the next tile
                for exit_edge in possible_exits:
                    neighbor = self._get_neighbor_tile(tile, exit_edge)
                    if neighbor == next_tile_in_path:
                        return exit_edge

        # Default: use first available exit
        return possible_exits[0] if possible_exits else None

    def _get_next_tile_in_path(
        self, train: "Train", current_tile: Tuple[int, int]
    ) -> Optional[Tuple[int, int]]:
        """Get next tile in train's path after current tile."""
        if not train.current_path:
            return None

        # Find current tile in path
        for i, (x, y, _) in enumerate(train.current_path):
            if (x, y) == current_tile:
                # Return next tile if exists
                if i + 1 < len(train.current_path):
                    next_x, next_y, _ = train.current_path[i + 1]
                    return (next_x, next_y)
                break

        return None

    def _get_neighbor_tile(
        self, tile: Tuple[int, int], edge: str
    ) -> Optional[Tuple[int, int]]:
        """Get neighbor tile in given direction."""
        dx, dy = self._get_direction_delta(edge)
        neighbor = (tile[0] + dx, tile[1] + dy)

        # Check if neighbor has a track
        if self.network and self.network.get_track(*neighbor):
            return neighbor

        return None

    def _get_direction_delta(self, edge: str) -> Tuple[int, int]:
        """Get (dx, dy) coordinate delta for moving through an edge."""
        mapping = {
            "N": (0, 1),
            "E": (1, 0),
            "S": (0, -1),
            "W": (-1, 0),
        }
        return mapping.get(edge, (0, 0))

    def _direction_to_edge(self, direction) -> str:
        """Convert Direction enum to edge string (N/E/S/W)."""
        from enums import Direction

        mapping = {
            Direction.NORTH: "S",  # Entering from south means heading north
            Direction.EAST: "W",
            Direction.SOUTH: "N",
            Direction.WEST: "E",
        }
        return mapping.get(direction, "N")

    def _edge_to_direction(self, edge: str):
        """Convert edge string (N/E/S/W) to Direction enum."""
        from enums import Direction

        mapping = {
            "N": Direction.NORTH,
            "E": Direction.EAST,
            "S": Direction.SOUTH,
            "W": Direction.WEST,
        }
        return mapping.get(edge, Direction.NORTH)

    # =========================================================================
    # Serialization
    # =========================================================================

    def to_dict(self) -> Dict[str, Any]:
        """Convert physics state to dictionary for JSON serialization."""
        return {
            "train_id": self.train_id,
            "last_occupied_blocks": list(self._last_occupied_blocks),
        }

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        network: "RailwayNetwork",
        controller: "TrainController",
    ) -> "TrainPhysics":
        """Reconstruct from dictionary after deserialization."""
        physics = cls(
            train_id=data["train_id"],
            network=network,
            controller=controller,
        )
        physics._last_occupied_blocks = set(data.get("last_occupied_blocks", []))
        return physics


# =============================================================================
# Factory Function
# =============================================================================


def create_physics_for_train(
    train_id: str,
    network: "RailwayNetwork",
    controller: "TrainController",
) -> TrainPhysics:
    """
    Create a TrainPhysics instance for a train.

    Args:
        train_id: ID of the train
        network: Railway network
        controller: TrainController for this train

    Returns:
        TrainPhysics instance
    """
    return TrainPhysics(
        train_id=train_id,
        network=network,
        controller=controller,
    )
