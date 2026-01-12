"""
Train Movement Logic - Extracted movement calculations and track advancement

Definition:
This module provides the TrainMover class which handles the physics and geometry
of train movement through the railway network. It encapsulates all track-following
logic, distance calculations, and direction transitions.

Responsibilities:
- Calculate train position updates based on speed and time delta
- Handle track segment transitions (advancing from tile to tile)
- Manage entry/exit edge calculations for track geometry
- Coordinate with BlockManager for safe block transitions
- Validate movement legality (track connectivity, switch states)

Separation from SimulationController:
Previously, movement logic was intertwined with simulation orchestration in
SimulationController. This extraction creates clean separation:

    SimulationController: Orchestrates simulation loop, train scheduling
    TrainMover: Pure movement calculations and track navigation
    BlockManager: State management for block occupancy

Design Principles:
1. Pure Functions: Movement calculations don't modify global state
2. Geometry Focus: Handles 6-bit track system and direction math
3. No Business Logic: Doesn't decide speed/signals, only executes movement
4. Testable: Can test movement in isolation from simulation

TODO List:
- [x] Define TrainMover class skeleton
- [ ] Implement advance_to_next_track() method
- [ ] Add distance calculation helpers
- [ ] Add comprehensive test suite
- [ ] Migrate SimulationController to use TrainMover
- [ ] Remove duplicated movement logic from simulation.py
"""

from typing import Optional, Tuple, List
from train import Train, Direction
from network import RailwayNetwork
from track import TrackSegment


class TrainMover:
    """Handles train movement calculations and track navigation.

    This class encapsulates the geometry and physics of train movement through
    the track network. It does NOT manage train state (speed, mode) or block
    reservations - those are handled by SimulationController and BlockManager.
    """

    def __init__(self, network: RailwayNetwork):
        self.network = network

    def advance_to_next_track(
        self, train: Train, use_switch_state: bool = True
    ) -> bool:
        """Advance train from current track to next connected track.

        This handles the complex logic of:
        1. Finding current track segment
        2. Determining exit edge based on entry direction
        3. Finding neighbor tile in exit direction
        4. Calculating new entry direction for neighbor
        5. Updating train position

        Args:
            train: Train object to advance
            use_switch_state: If True, respect switch positions (normal operation)
                            If False, explore all possible exits (pathfinding)

        Returns:
            True if train successfully advanced to next track
            False if no valid next track (dead end, wrong switch, edge of map)
        """
        current_track = self.network.get_track(train.position.x, train.position.y)
        if not current_track:
            return False

        entry_edge = self._direction_to_edge(train.position.direction)
        exit_edges = current_track.get_exits(
            entry_edge, use_switch_state=use_switch_state
        )

        if not exit_edges:
            return False

        exit_edge = exit_edges[0]

        neighbor_track, new_entry_edge = self.network.get_neighbor(
            train.position.x, train.position.y, exit_edge
        )

        if not neighbor_track:
            return False

        new_direction = self._edge_to_direction(new_entry_edge)

        dx, dy = self._get_direction_delta(exit_edge)
        train.position.x += dx
        train.position.y += dy
        train.position.direction = new_direction
        train.position.distance = 0.0

        return True

    def calculate_distance_to_advance(
        self,
        current_speed_cm_s: float,
        time_delta_s: float,
        track_length_cm: float,
        current_distance: float,
    ) -> Tuple[float, int]:
        """Calculate how far train should advance given speed and time.

        Args:
            current_speed_cm_s: Current speed in cm/s
            time_delta_s: Time elapsed since last update (seconds)
            track_length_cm: Length of current track segment (cm)
            current_distance: Current position on track (0.0 = start, 1.0 = end)

        Returns:
            Tuple of (new_distance, tiles_to_advance):
                new_distance: New position on track (0.0-1.0)
                tiles_to_advance: Number of complete tiles traversed
        """
        distance_traveled_cm = current_speed_cm_s * time_delta_s

        distance_fraction = (
            distance_traveled_cm / track_length_cm if track_length_cm > 0 else 0.0
        )

        new_distance = current_distance + distance_fraction

        tiles_to_advance = int(new_distance)
        final_distance = new_distance - tiles_to_advance

        return (final_distance, tiles_to_advance)

    def get_next_track_in_direction(
        self, x: int, y: int, direction: Direction, use_switch_state: bool = True
    ) -> Optional[Tuple[TrackSegment, int, int, Direction]]:
        """Get next track segment in given direction.

        Args:
            x, y: Current position
            direction: Current travel direction
            use_switch_state: Whether to respect switch positions

        Returns:
            Tuple of (track, new_x, new_y, new_direction) or None if no valid next track
        """
        current_track = self.network.get_track(x, y)
        if not current_track:
            return None

        entry_edge = self._direction_to_edge(direction)
        exit_edges = current_track.get_exits(
            entry_edge, use_switch_state=use_switch_state
        )

        if not exit_edges:
            return None

        exit_edge = exit_edges[0]

        neighbor_track, new_entry_edge = self.network.get_neighbor(x, y, exit_edge)

        if not neighbor_track:
            return None

        new_direction = self._edge_to_direction(new_entry_edge)
        dx, dy = self._get_direction_delta(exit_edge)

        return (neighbor_track, x + dx, y + dy, new_direction)

    def _direction_to_edge(self, direction: Direction) -> str:
        """Convert Direction enum to edge string (N/E/S/W)."""
        mapping = {
            Direction.NORTH: "S",
            Direction.EAST: "W",
            Direction.SOUTH: "N",
            Direction.WEST: "E",
        }
        return mapping.get(direction, "N")

    def _edge_to_direction(self, edge: str) -> Direction:
        """Convert edge string (N/E/S/W) to Direction enum."""
        mapping = {
            "N": Direction.NORTH,
            "E": Direction.EAST,
            "S": Direction.SOUTH,
            "W": Direction.WEST,
        }
        return mapping.get(edge, Direction.NORTH)

    def _get_direction_delta(self, edge: str) -> Tuple[int, int]:
        """Get (dx, dy) coordinate delta for moving through an edge.

        Coordinate system:
            N (North) = Y+ direction (0, +1)
            E (East)  = X+ direction (+1, 0)
            S (South) = Y- direction (0, -1)
            W (West)  = X- direction (-1, 0)
        """
        mapping = {
            "N": (0, 1),
            "E": (1, 0),
            "S": (0, -1),
            "W": (-1, 0),
        }
        return mapping.get(edge, (0, 0))
