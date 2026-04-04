"""
Block Management System

Definition:
This module provides the Block class for centralized block management. A Block represents
an electrically isolated track section with physical properties (length, radius limits, speed).
Blocks are the minimum control unit for train movement in model railway layouts.

Key concepts:
- Block = electrically isolated track section (all tracks share voltage/current)
- Tracks reference blocks via track.block_section_id
- Block has physical properties set at block level, not per-tile
- Tiles are for UI layout; a block might have different physical length than tile count
- Signals mark block boundaries and monitor block occupancy
- Switches are block boundaries (connect blocks but not IN blocks)
"""

from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, Any
from enums import BlockState


@dataclass
class Block:
    """
    Centralized block entity for track section management.

    A Block is an electrically isolated track section where all tracks
    share voltage/current. It represents the minimum control unit for
    train movement.
    """

    block_id: str  # Unique ID (e.g., "BLK_001")
    name: str = ""  # Human-readable name

    # Physical properties (set on BLOCK, not tiles)
    length_cm: float = 0.0  # Total track length in cm
    min_radius_cm: Optional[float] = None  # Tightest curve radius
    max_speed_kmh: Optional[float] = None  # Speed limit for block

    # State
    state: BlockState = field(default=BlockState.FREE)
    occupied_by: Optional[str] = None  # Train ID if occupied

    # Voltage control (Phase 2 - DC analog control)
    current_voltage: float = 0.0  # Current voltage output to this block

    # Path reservation (dispatcher system)
    # Note: "path" = dispatcher's reserved route; "route" = train's travel plan
    reserved_by_path: Optional[str] = None  # Path ID that reserved this block
    reserved_direction: Optional[str] = (
        None  # Direction of travel for reserved path (N/E/S/W)
    )
    disabled: bool = False  # If True, block cannot be used

    def to_dict(self) -> Dict[str, Any]:
        """Convert block to dictionary for serialization"""
        return {
            "block_id": self.block_id,
            "name": self.name,
            "length_cm": self.length_cm,
            "min_radius_cm": self.min_radius_cm,
            "max_speed_kmh": self.max_speed_kmh,
            "state": self.state.value,
            "occupied_by": self.occupied_by,
            "current_voltage": self.current_voltage,
            "reserved_by_path": self.reserved_by_path,
            "reserved_direction": self.reserved_direction,
            "disabled": self.disabled,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Block":
        """Reconstruct block from dictionary"""
        # Handle state enum conversion
        state_value = data.get("state", "free")
        if isinstance(state_value, str):
            state = BlockState(state_value)
        else:
            state = state_value

        return cls(
            block_id=data["block_id"],
            name=data.get("name", ""),
            length_cm=data.get("length_cm", 0.0),
            min_radius_cm=data.get("min_radius_cm"),
            max_speed_kmh=data.get("max_speed_kmh"),
            state=state,
            occupied_by=data.get("occupied_by"),
            current_voltage=data.get("current_voltage", 0.0),
            reserved_by_path=data.get("reserved_by_path"),
            reserved_direction=data.get("reserved_direction"),
            disabled=data.get("disabled", False),
        )

    def can_enter(self, train_id: str) -> bool:
        """Check if train can enter this block"""
        if self.disabled:
            return False
        return self.state == BlockState.FREE or (
            self.state == BlockState.RESERVED and self.occupied_by == train_id
        )

    def occupy(self, train_id: str) -> bool:
        """Mark block as occupied by specified train"""
        if self.can_enter(train_id):
            self.state = BlockState.OCCUPIED
            self.occupied_by = train_id
            return True
        return False

    def release(self):
        """Release block, making it available"""
        self.state = BlockState.FREE
        self.occupied_by = None

    def set_to_caution(self) -> bool:
        """
        Set block to CAUTION state (train just left).

        Precondition: Block should be OCCUPIED
        Postcondition: Block state becomes CAUTION, occupied_by cleared

        Returns:
            True if successful, False if block wasn't occupied
        """
        if self.state == BlockState.OCCUPIED:
            self.state = BlockState.CAUTION
            self.occupied_by = None
            return True
        return False

    def clear(self) -> bool:
        """
        Clear block from CAUTION to FREE state.

        Used after a train has left and the next block is now clear.
        Completes the state transition: OCCUPIED -> CAUTION -> FREE

        Returns:
            True if successful, False if block wasn't in CAUTION state
        """
        if self.state == BlockState.CAUTION:
            self.state = BlockState.FREE
            return True
        return False

    def is_free(self) -> bool:
        """Check if block is free and available for use"""
        return self.state == BlockState.FREE and not self.disabled

    def is_reserved(self) -> bool:
        """Check if block is reserved"""
        return self.state == BlockState.RESERVED

    # ========================================================================
    # Path Reservation Methods (Dispatcher System)
    # ========================================================================

    def reserve_for_path(
        self,
        path_id: str,
        train_id: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> bool:
        """
        Reserve block for a specific path.

        This is used by the dispatcher system to pre-reserve blocks along a path.
        Unlike reserve() which takes a train_id, this associates the block with
        a path_id for tracking which path owns the reservation.

        Note: "path" = dispatcher's reserved route; "route" = train's travel plan

        Args:
            path_id: The path ID reserving this block
            train_id: Optional train ID that will use this block
            direction: Direction of travel through this block (N/E/S/W)

        Returns:
            True if reservation successful, False otherwise
        """
        if self.disabled:
            return False
        if self.state == BlockState.FREE:
            self.state = BlockState.RESERVED
            self.reserved_by_path = path_id
            self.reserved_direction = direction.upper() if direction else None
            self.occupied_by = train_id
            return True
        if self.state == BlockState.RESERVED and self.reserved_by_path == path_id:
            return True
        if (
            self.state == BlockState.OCCUPIED
            and train_id
            and self.occupied_by == train_id
        ):
            self.reserved_by_path = path_id
            self.reserved_direction = direction.upper() if direction else None
            return True
        return False

    def release_path_reservation(self) -> bool:
        """
        Release path reservation and return block to FREE state.

        This clears both the path reservation and any train assignment,
        making the block available for other paths.

        Returns:
            True if released, False if block wasn't reserved
        """
        if self.state == BlockState.RESERVED and self.reserved_by_path:
            self.state = BlockState.FREE
            self.reserved_by_path = None
            self.reserved_direction = None
            self.occupied_by = None
            return True
        return False

    def is_available_for_path(
        self, path_id: str, train_id: Optional[str] = None
    ) -> bool:
        """
        Check if block is available for a path (for validation).

        A block is available if:
        - It's FREE
        - It's already reserved by this path
        - It's OCCUPIED by the train that will use this path (starting block)

        Args:
            path_id: Path ID checking availability
            train_id: Train ID that would use the path

        Returns:
            True if block is available for this path
        """
        if self.disabled:
            return False
        if self.state == BlockState.FREE:
            return True
        if self.state == BlockState.RESERVED and self.reserved_by_path == path_id:
            return True
        if (
            self.state == BlockState.OCCUPIED
            and train_id
            and self.occupied_by == train_id
        ):
            return True
        return False
