"""
DEPRECATED: This pathfinder is used only for UI/editor operations.
Train navigation uses routes + path_dispatcher (BFS), NOT dynamic A* pathfinding.
See docs/PRIORITY_2_NECESSITY_ANALYSIS.md for details.
"""
"""
Railway Pathfinding System

Definition:
This module implements A* pathfinding for trains on railway networks. It considers track
connections, signal states, switch positions, and train physical constraints (like minimum
curve radius) to find optimal paths between positions.

TODO List:
- [ ] Implement alternative pathfinding algorithms (Dijkstra, etc.)
- [ ] Add dynamic re-routing for blocked paths
- [ ] Implement path caching for frequently used routes
- [ ] Add support for speed-optimized vs distance-optimized paths
- [ ] Implement collision avoidance in pathfinding
- [ ] Add support for multi-train coordinated pathfinding
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import heapq
from enums import Direction, TrackType, SignalState
from train import Train

logger = logging.getLogger(__name__)


@dataclass
class RailPathNode:
    """
    Node in railway pathfinding graph.
    
    Represents a position and orientation state in the pathfinding process.
    The entry_direction indicates where the train enters FROM (opposite of movement).
    """
    x: int
    y: int
    entry_direction: Direction  # Direction from which train enters this track (opposite of movement)
    g_cost: float  # Cost from start to this node
    h_cost: float  # Heuristic cost from this node to goal
    parent: Optional['RailPathNode'] = None
    occupied_segments: List[Tuple[int, int]] = field(default_factory=list)
    
    @property
    def f_cost(self):
        """Total cost (g + h)"""
        return self.g_cost + self.h_cost
    
    def __lt__(self, other):
        """Comparison for heap queue"""
        return self.f_cost < other.f_cost
    
    def __hash__(self):
        """Hash for set membership"""
        return hash((self.x, self.y, self.entry_direction))
    
    def __eq__(self, other):
        """Equality for set membership"""
        return (self.x == other.x and self.y == other.y and 
                self.entry_direction == other.entry_direction)
    
    def __post_init__(self):
        if self.occupied_segments is None:
            self.occupied_segments = []
    
    @property 
    def movement_direction(self) -> Direction:
        """The direction the train is moving (opposite of entry_direction)"""
        return self.entry_direction.opposite()


class RailwayPathfinder:
    """
    A* pathfinding for railway networks.
    
    Finds optimal paths for trains considering track topology, signals,
    switches, and physical constraints.
    """
    
    def __init__(self, network):
        """
        Initialize pathfinder with railway network.
        
        Args:
            network: RailwayNetwork instance
        """
        self.network = network
    
    def find_path(self, train: Train, 
                  start_pos: Tuple[int, int], 
                  goal_pos: Tuple[int, int],
                  start_direction: Direction) -> Optional[List[Tuple[int, int, Direction]]]:
        """
        Find path from start to goal for specified train.
        
        Args:
            train: Train instance requiring path
            start_pos: Starting position (x, y)
            goal_pos: Goal position (x, y)
            start_direction: Direction train is currently moving/heading
            
        Returns:
            List of (x, y, movement_direction) tuples, or None if no path found
        """
        start_track = self.network.get_track(*start_pos)
        goal_track = self.network.get_track(*goal_pos)
        
        if not start_track or not goal_track:
            return None
        
        open_set = []
        closed_set = set()
        
        # Convert train's movement direction to track entry direction
        # If train is heading EAST, it enters the starting track from WEST
        start_entry_direction = start_direction.opposite()
        
        start_node = RailPathNode(
            start_pos[0], start_pos[1],
            entry_direction=start_entry_direction,
            g_cost=0,
            h_cost=self._heuristic(start_pos, goal_pos)
        )
        
        heapq.heappush(open_set, start_node)
        
        while open_set:
            logger.debug("Pathfinding: open_set size=%d, closed_set size=%d", len(open_set), len(closed_set))
            current = heapq.heappop(open_set)
            
            if (current.x, current.y) == goal_pos:
                return self._reconstruct_path(current)
            
            node_key = (current.x, current.y, current.entry_direction)
            if node_key in closed_set:
                continue
            
            closed_set.add(node_key)
            
            neighbors = self._get_valid_neighbors(current, train)
            logger.debug("Pathfinding: valid neighbors count=%d", len(neighbors))
            for neighbor_pos, movement_direction, move_cost in neighbors:
                nx, ny = neighbor_pos
                # Convert movement direction to entry direction for the neighbor track
                neighbor_entry_dir = movement_direction.opposite()
                
                neighbor_node = RailPathNode(
                    nx, ny,
                    entry_direction=neighbor_entry_dir,
                    g_cost=current.g_cost + move_cost,
                    h_cost=self._heuristic(neighbor_pos, goal_pos),
                    parent=current
                )
                
                neighbor_key = (nx, ny, neighbor_entry_dir)
                if neighbor_key not in closed_set:
                    heapq.heappush(open_set, neighbor_node)
        
        return None
    
    def _get_valid_neighbors(self, current: RailPathNode, train: Train) -> List[Tuple[Tuple[int, int], Direction, float]]:
        """
        Get valid neighboring positions from current node.
        
        Returns:
            List of (neighbor_position, movement_direction, cost) tuples
        """
        current_track = self.network.get_track(current.x, current.y)
        if not current_track:
            return []
        
        neighbors = []
        current_movement_dir = current.movement_direction
        possible_exits = current_track.get_exit_directions(current_movement_dir.opposite())
        logger.debug(
            "From (%d,%d) moving %s, possible exits: %s",
            current.x,
            current.y,
            current_movement_dir.name,
            [d.name for d in possible_exits]
        )
        
        for exit_dir in possible_exits:
            dx, dy = exit_dir.value
            next_x, next_y = current.x + dx, current.y + dy
            next_track = self.network.get_track(next_x, next_y)
            logger.debug("  Checking exit %s to (%d,%d)", exit_dir.name, next_x, next_y)
            if not next_track:
                continue
            
            if not self._check_all_constraints(
                current_track, next_track, train,
                current_movement_dir, exit_dir
            ):
                continue
            
            move_cost = self._calculate_move_cost(current_track, next_track, current_movement_dir, exit_dir, train)
            neighbors.append(((next_x, next_y), exit_dir, move_cost))
        
        return neighbors
    
    def _check_all_constraints(self, current_track, next_track, 
                               train: Train, movement_dir: Direction,
                               exit_dir: Direction) -> bool:
        """Check if movement from current to next track is valid"""
        
        # Track availability check
        if not self.network.is_track_available(next_track.x, next_track.y, train.train_id):
            return False

        # Signal check
        if current_track.track_type == TrackType.SIGNAL:
            signals = self.network.get_signals_at(current_track.x, current_track.y)
            if signals:
                # Find signal where detection_direction matches train's travel direction
                aligned = next((s for s in signals if s.detection_direction.upper() == exit_dir.name), None)
                sig = aligned or signals[0]
                if sig.state == SignalState.RED:
                    return False

        # Curve radius check (only if caller provided one); curves are orientation changes
        if next_track.curve_radius is not None:
            # If the next traversal will be a turn, enforce minimum radius
            next_entry_dir = exit_dir.opposite()
            exits_from_next = next_track.get_exit_directions(next_entry_dir)
            if exits_from_next:
                # If any exit would curve too sharply, reject
                # Use first exit as representative because switch state already resolved
                if next_track.is_turn(next_entry_dir, exits_from_next[0]):
                    if next_track.curve_radius < train.min_curve_radius:
                        return False

        # Validate that the chosen exit is actually available from the current entry
        entry_dir = movement_dir.opposite()
        if exit_dir not in current_track.get_exit_directions(entry_dir):
            return False

        # Ensure the next tile accepts the incoming edge
        next_entry_dir = exit_dir.opposite()
        if not next_track.get_exit_directions(next_entry_dir):
            return False

        return True
    
    def _calculate_move_cost(self, current_track, next_track, movement_dir: Direction, exit_dir: Direction, train: Train) -> float:
        """Calculate movement cost from current to next track using orientation groups."""
        base_cost = next_track.length

        entry_dir = movement_dir.opposite()
        if current_track.is_turn(entry_dir, exit_dir):
            base_cost *= 1.5

        if current_track.track_type == TrackType.SWITCH:
            base_cost *= 1.3

        if current_track.track_type == TrackType.SIGNAL:
            signals = self.network.get_signals_at(current_track.x, current_track.y)
            if signals:
                # Find signal where detection_direction matches train's travel direction
                aligned = next((s for s in signals if s.detection_direction.upper() == exit_dir.name), None)
                sig = aligned or signals[0]
                if sig.state == SignalState.YELLOW:
                    base_cost *= 2.0

        return base_cost
    
    def _heuristic(self, pos1: Tuple[int, int], pos2: Tuple[int, int]) -> float:
        """Manhattan distance heuristic"""
        return abs(pos1[0] - pos2[0]) + abs(pos1[1] - pos2[1])
    
    def _reconstruct_path(self, node: RailPathNode) -> List[Tuple[int, int, Direction]]:
        """
        Reconstruct path from goal node back to start.
        
        Returns:
            List of (x, y, movement_direction) tuples
        """
        path = []
        current = node
        
        while current.parent:
            # Calculate movement direction from parent to current
            dx = current.x - current.parent.x
            dy = current.y - current.parent.y
            
            movement_dir = None
            for direction in Direction:
                if direction.value == (dx, dy):
                    movement_dir = direction
                    break
            
            if movement_dir:
                path.append((current.parent.x, current.parent.y, movement_dir))
            
            current = current.parent
        
        path.reverse()
        return path