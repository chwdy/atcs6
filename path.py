"""
Path and Dispatcher System

Definition:
This module provides the Path class and related data structures for the dispatcher system.
A Path represents a reserved path through the railway network with exclusive access to
blocks, switches, and other resources needed for safe train movement.

Key concepts:
- Path = reserved path from start to end with exclusive resource access
- Paths are directional (trains move forward only)
- Paths have priorities (1-10, higher = higher priority, default = 5)
- Approach locking prevents unsafe path cancellation
- Flank protection locks nearby switches to safe positions
- Overlap provides safety margin beyond destination

Note: "Path" refers to the dispatcher's reserved route through the network.
      "Route" is reserved for the train's travel plan (waypoints, schedule).
      See Reference_Train_System.md for the full naming convention.

See docs/DISPATCHER_DESIGN.md for full specification.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple, Any
from enums import direction_from_delta, OPPOSITE_DIRECTIONS


# ============================================================================
# Configuration Constants
# ============================================================================

DEFAULT_PATH_PRIORITY = 5
MIN_PRIORITY = 1
MAX_PRIORITY = 10

# Safety distances (in centimeters)
REAR_SAFETY_DISTANCE_CM = 300.0  # Min distance behind train before releasing block
OVERLAP_DISTANCE_CM = 200.0  # Safety margin beyond destination
FLANK_PROTECTION_DISTANCE_CM = 500.0  # Distance to check for flank protection switches

# Braking
DEFAULT_MAX_BRAKING_RATE = 100.0  # cm/s^2
EMERGENCY_BRAKING_MULTIPLIER = 1.5  # Multiplier for emergency stops

# UI
CONFLICT_FLASH_DURATION_SEC = 2.0  # How long to flash conflict indicators


# ============================================================================
# Enums
# ============================================================================


class PathStatus(Enum):
    """Path lifecycle states."""

    PENDING = "pending"  # Path calculated, waiting for resource acquisition
    SETTING = "setting"  # Switches moving to correct positions
    SET = "set"  # All switches locked, blocks reserved, ready for train
    ACTIVE = "active"  # Train is physically on the path
    CLEARING = "clearing"  # Train in final block, releasing resources progressively
    COMPLETED = "completed"  # Train arrived, all resources freed, path disposed
    CANCELLED = "cancelled"  # Emergency cancelled, train stopping, releasing resources


class ValidationErrorType(Enum):
    """Types of path validation errors."""

    UNBLOCKED_TILE = "unblocked_tile"  # Tile not in any block
    BLOCK_OCCUPIED = "block_occupied"  # Block occupied by different train
    BLOCK_RESERVED = "block_reserved"  # Block reserved for different path
    BLOCK_DISABLED = "block_disabled"  # Block is disabled
    SWITCH_LOCKED = "switch_locked"  # Switch locked by another path
    FLANK_SWITCH_CONFLICT = "flank_switch_conflict"  # Flank protection switch conflict
    NO_PATH = "no_path"  # No path exists between endpoints
    INVALID_ENDPOINT = "invalid_endpoint"  # Invalid start or end point


class ConflictResolutionType(Enum):
    """Types of conflict resolution outcomes."""

    BLOCKED = "blocked"  # Path blocked, cannot proceed
    PRIORITY_PROMPT = "priority_prompt"  # Higher priority, prompt user for pre-emption


# ============================================================================
# Data Classes - Pathfinding
# ============================================================================


@dataclass
class PathSegment:
    """
    Represents a potential path segment for a dispatcher path.

    This is the output of pathfinding before validation and resource acquisition.
    """

    tiles: List[Tuple[int, int]]  # Ordered list of track positions
    distance_cm: float  # Total path distance
    required_switches: Dict[
        Tuple[int, int], int
    ]  # Switch positions needed: {(x,y): state}
    blocks: List[str]  # Ordered block IDs along path

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "tiles": self.tiles,
            "distance_cm": self.distance_cm,
            "required_switches": {
                f"{k[0]},{k[1]}": v for k, v in self.required_switches.items()
            },
            "blocks": self.blocks,
        }


@dataclass
class ValidationError:
    """Represents a single validation error for a path."""

    type: ValidationErrorType
    message: str
    tile: Optional[Tuple[int, int]] = None
    block_id: Optional[str] = None
    position: Optional[Tuple[int, int]] = None
    locked_by_path: Optional[str] = None
    occupied_by: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "type": self.type.value,
            "message": self.message,
            "tile": self.tile,
            "block_id": self.block_id,
            "position": self.position,
            "locked_by_path": self.locked_by_path,
            "occupied_by": self.occupied_by,
        }


@dataclass
class ValidationResult:
    """Result of path validation."""

    valid: bool
    errors: List[ValidationError]
    path: Optional[PathSegment] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "valid": self.valid,
            "errors": [e.to_dict() for e in self.errors],
            "path": self.path.to_dict() if self.path else None,
        }


# ============================================================================
# Data Classes - Path Operations Results
# ============================================================================


@dataclass
class PathResult:
    """Result of path creation attempt."""

    success: bool
    path: Optional["Path"] = None
    error: Optional[str] = None
    attempted_paths: Optional[List[Tuple[PathSegment, ValidationResult]]] = None
    conflict_tiles: Optional[List[Tuple[int, int]]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {
            "success": self.success,
            "error": self.error,
            "conflict_tiles": self.conflict_tiles,
        }
        if self.path:
            result["path"] = self.path.to_dict()
        if self.attempted_paths:
            result["attempted_paths"] = [
                {"path": p.to_dict(), "validation": v.to_dict()}
                for p, v in self.attempted_paths
            ]
        return result


@dataclass
class ExtensionResult:
    """Result of path extension attempt."""

    success: bool
    extended_blocks: Optional[List[str]] = None
    error: Optional[str] = None
    conflict_tiles: Optional[List[Tuple[int, int]]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "extended_blocks": self.extended_blocks,
            "error": self.error,
            "conflict_tiles": self.conflict_tiles,
        }


@dataclass
class CancellationResult:
    """Result of path cancellation."""

    success: bool
    blocks_released: List[str] = field(default_factory=list)
    switches_released: List[Tuple[int, int]] = field(default_factory=list)
    approach_locked: List[str] = field(default_factory=list)
    message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "blocks_released": self.blocks_released,
            "switches_released": self.switches_released,
            "approach_locked": self.approach_locked,
            "message": self.message,
        }


@dataclass
class ConflictResolution:
    """How to handle path conflicts."""

    type: ConflictResolutionType
    message: str
    conflicting_paths: List["Path"] = field(default_factory=list)
    default_action: str = "NO"  # Default is to not pre-empt

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "type": self.type.value,
            "message": self.message,
            "conflicting_paths": [p.path_id for p in self.conflicting_paths],
            "default_action": self.default_action,
        }


# ============================================================================
# Main Path Class
# ============================================================================


@dataclass
class Path:
    """
    A reserved path through the railway network with exclusive resource access.

    Paths manage:
    - Block reservations along the path
    - Switch locks (both path switches and flank protection)
    - Train assignment and tracking
    - Approach locking based on braking distance
    - Overlap beyond destination
    - Progressive resource release as train advances
    """

    # Identity
    path_id: str  # Human-readable: "PATH_CENTRAL_TO_NORTH_001"
    status: PathStatus = PathStatus.PENDING  # Current lifecycle state
    priority: int = DEFAULT_PATH_PRIORITY  # 1-10, higher = higher priority

    # Endpoints
    start_block_id: str = ""  # Starting block
    end_block_id: str = ""  # Destination block
    start_station_id: Optional[str] = None  # If started from station
    end_station_id: Optional[str] = None  # If ending at station

    # Path data
    path_tiles: List[Tuple[int, int]] = field(
        default_factory=list
    )  # Ordered track positions
    block_sequence: List[str] = field(default_factory=list)  # Ordered blocks along path
    switch_positions: Dict[Tuple[int, int], int] = field(
        default_factory=dict
    )  # Required switch states

    # Ownership
    train_id: Optional[str] = None  # Assigned train (None until train enters)
    dispatcher_id: str = "system"  # Who created this path

    # State tracking
    current_block_index: int = 0  # Train's position in block_sequence
    blocks_cleared: Set[str] = field(
        default_factory=set
    )  # Blocks train has fully exited
    rear_hold_distance_cm: float = (
        REAR_SAFETY_DISTANCE_CM  # Safety distance behind train
    )

    # Protection zones
    flank_protection_switches: Dict[Tuple[int, int], int] = field(default_factory=dict)
    overlap_blocks: List[str] = field(default_factory=list)  # Blocks beyond destination
    overlap_distance_cm: float = OVERLAP_DISTANCE_CM  # Required overlap distance

    # Approach locking
    approach_locked_blocks: Set[str] = field(default_factory=set)  # Cannot be cancelled

    # Timestamps
    created_at: datetime = field(default_factory=datetime.now)
    activated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def get_tile_directions(self) -> Dict[str, str]:
        """
        Calculate the direction of travel for each tile in the path.

        Returns a dictionary mapping "x,y" -> direction (N/E/S/W).
        Direction represents where the path is heading TO at that tile.
        """
        tile_directions: Dict[str, str] = {}

        if not self.path_tiles or len(self.path_tiles) < 2:
            return tile_directions

        # For each tile, calculate direction to the next tile
        for i in range(len(self.path_tiles) - 1):
            curr_tile = self.path_tiles[i]
            next_tile = self.path_tiles[i + 1]

            dx = next_tile[0] - curr_tile[0]
            dy = next_tile[1] - curr_tile[1]

            direction = direction_from_delta(dx, dy)
            if direction is None:
                continue  # Same tile (shouldn't happen)

            key = f"{curr_tile[0]},{curr_tile[1]}"
            tile_directions[key] = direction

        # For the last tile, use the same direction as the previous tile
        if len(self.path_tiles) >= 2:
            prev_tile = self.path_tiles[-2]
            last_tile = self.path_tiles[-1]

            dx = last_tile[0] - prev_tile[0]
            dy = last_tile[1] - prev_tile[1]

            direction = direction_from_delta(dx, dy)
            if direction:
                key = f"{last_tile[0]},{last_tile[1]}"
                tile_directions[key] = direction

        return tile_directions

    def to_dict(self) -> Dict[str, Any]:
        """Convert path to dictionary for serialization."""
        return {
            "path_id": self.path_id,
            "status": self.status.value,
            "priority": self.priority,
            "start_block_id": self.start_block_id,
            "end_block_id": self.end_block_id,
            "start_station_id": self.start_station_id,
            "end_station_id": self.end_station_id,
            "path_tiles": self.path_tiles,
            "tile_directions": self.get_tile_directions(),
            "exit_direction": self.get_exit_direction(),
            "block_sequence": self.block_sequence,
            "switch_positions": {
                f"{k[0]},{k[1]}": v for k, v in self.switch_positions.items()
            },
            "train_id": self.train_id,
            "dispatcher_id": self.dispatcher_id,
            "current_block_index": self.current_block_index,
            "blocks_cleared": list(self.blocks_cleared),
            "rear_hold_distance_cm": self.rear_hold_distance_cm,
            "flank_protection_switches": {
                f"{k[0]},{k[1]}": v for k, v in self.flank_protection_switches.items()
            },
            "overlap_blocks": self.overlap_blocks,
            "overlap_distance_cm": self.overlap_distance_cm,
            "approach_locked_blocks": list(self.approach_locked_blocks),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "activated_at": self.activated_at.isoformat()
            if self.activated_at
            else None,
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Path":
        """Reconstruct path from dictionary."""
        # Parse switch positions back to tuple keys
        switch_positions = {}
        for key, value in data.get("switch_positions", {}).items():
            x, y = map(int, key.split(","))
            switch_positions[(x, y)] = value

        flank_protection_switches = {}
        for key, value in data.get("flank_protection_switches", {}).items():
            x, y = map(int, key.split(","))
            flank_protection_switches[(x, y)] = value

        # Parse timestamps
        created_at = None
        if data.get("created_at"):
            created_at = datetime.fromisoformat(data["created_at"])

        activated_at = None
        if data.get("activated_at"):
            activated_at = datetime.fromisoformat(data["activated_at"])

        completed_at = None
        if data.get("completed_at"):
            completed_at = datetime.fromisoformat(data["completed_at"])

        return cls(
            path_id=data["path_id"],
            status=PathStatus(data.get("status", "pending")),
            priority=data.get("priority", DEFAULT_PATH_PRIORITY),
            start_block_id=data.get("start_block_id", ""),
            end_block_id=data.get("end_block_id", ""),
            start_station_id=data.get("start_station_id"),
            end_station_id=data.get("end_station_id"),
            path_tiles=[tuple(t) for t in data.get("path_tiles", [])],
            block_sequence=data.get("block_sequence", []),
            switch_positions=switch_positions,
            train_id=data.get("train_id"),
            dispatcher_id=data.get("dispatcher_id", "system"),
            current_block_index=data.get("current_block_index", 0),
            blocks_cleared=set(data.get("blocks_cleared", [])),
            rear_hold_distance_cm=data.get(
                "rear_hold_distance_cm", REAR_SAFETY_DISTANCE_CM
            ),
            flank_protection_switches=flank_protection_switches,
            overlap_blocks=data.get("overlap_blocks", []),
            overlap_distance_cm=data.get("overlap_distance_cm", OVERLAP_DISTANCE_CM),
            approach_locked_blocks=set(data.get("approach_locked_blocks", [])),
            created_at=created_at or datetime.now(),
            activated_at=activated_at,
            completed_at=completed_at,
        )

    # ========================================================================
    # State Queries
    # ========================================================================

    def is_active(self) -> bool:
        """Check if path is currently active (train on path)."""
        return self.status == PathStatus.ACTIVE

    def is_set(self) -> bool:
        """Check if path is set and ready for train."""
        return self.status == PathStatus.SET

    def is_completed(self) -> bool:
        """Check if path has completed."""
        return self.status == PathStatus.COMPLETED

    def is_cancelled(self) -> bool:
        """Check if path was cancelled."""
        return self.status == PathStatus.CANCELLED

    def can_be_extended(self) -> bool:
        """Check if path can be extended."""
        return self.status in [PathStatus.SET, PathStatus.ACTIVE]

    def can_be_cancelled(self) -> bool:
        """Check if path can be cancelled (ignoring approach locking)."""
        return self.status not in [PathStatus.COMPLETED, PathStatus.CANCELLED]

    def has_train(self) -> bool:
        """Check if a train has claimed this path."""
        return self.train_id is not None

    def get_current_block(self) -> Optional[str]:
        """Get the block the train is currently in."""
        if 0 <= self.current_block_index < len(self.block_sequence):
            return self.block_sequence[self.current_block_index]
        return None

    def get_remaining_blocks(self) -> List[str]:
        """Get blocks ahead of the train."""
        if self.current_block_index < len(self.block_sequence):
            return self.block_sequence[self.current_block_index :]
        return []

    def get_cleared_block_count(self) -> int:
        """Get number of blocks the train has cleared."""
        return len(self.blocks_cleared)

    def get_progress_percentage(self) -> float:
        """Get path progress as percentage (0-100)."""
        if not self.block_sequence:
            return 0.0
        return (self.current_block_index / len(self.block_sequence)) * 100

    # ========================================================================
    # Direction Queries (for path extension)
    # ========================================================================

    def get_exit_direction(self) -> Optional[str]:
        """
        Get the direction the path is heading at its end (exit direction).

        This is the direction from the second-to-last tile to the last tile.
        Used for determining valid extension directions.

        Returns:
            Direction string (N/E/S/W) or None if path has < 2 tiles.

        Coordinate system:
            N (North) = Y+ direction (0, +1)
            E (East)  = X+ direction (+1, 0)
            S (South) = Y- direction (0, -1)
            W (West)  = X- direction (-1, 0)
        """
        if not self.path_tiles or len(self.path_tiles) < 2:
            return None

        prev_tile = self.path_tiles[-2]
        last_tile = self.path_tiles[-1]

        dx = last_tile[0] - prev_tile[0]
        dy = last_tile[1] - prev_tile[1]

        return direction_from_delta(dx, dy)

    def get_last_tile_entry_edge(self) -> Optional[str]:
        """
        Get the edge through which the path enters the last tile.

        This is the opposite of the exit direction from the second-to-last tile.
        For example, if we're heading East (X+), we enter the last tile from the West.

        Returns:
            Edge string (N/E/S/W) representing entry edge, or None if path has < 2 tiles.
        """
        exit_dir = self.get_exit_direction()
        if exit_dir is None:
            return None

        return OPPOSITE_DIRECTIONS.get(exit_dir)

    # ========================================================================
    # All Switch Positions
    # ========================================================================

    def get_all_locked_switches(self) -> Dict[Tuple[int, int], int]:
        """Get all switches locked by this path (path + flank protection)."""
        all_switches = dict(self.switch_positions)
        all_switches.update(self.flank_protection_switches)
        return all_switches
