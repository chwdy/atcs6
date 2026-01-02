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
    block_id: str                              # Unique ID (e.g., "BLK_001")
    name: str = ""                             # Human-readable name

    # Physical properties (set on BLOCK, not tiles)
    length_cm: float = 0.0                     # Total track length in cm
    min_radius_cm: Optional[float] = None      # Tightest curve radius
    max_speed_kmh: Optional[float] = None      # Speed limit for block

    # State
    state: BlockState = field(default=BlockState.FREE)
    occupied_by: Optional[str] = None          # Train ID if occupied

    def to_dict(self) -> Dict[str, Any]:
        """Convert block to dictionary for serialization"""
        return {
            'block_id': self.block_id,
            'name': self.name,
            'length_cm': self.length_cm,
            'min_radius_cm': self.min_radius_cm,
            'max_speed_kmh': self.max_speed_kmh,
            'state': self.state.value,
            'occupied_by': self.occupied_by,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Block':
        """Reconstruct block from dictionary"""
        # Handle state enum conversion
        state_value = data.get('state', 'free')
        if isinstance(state_value, str):
            state = BlockState(state_value)
        else:
            state = state_value

        return cls(
            block_id=data['block_id'],
            name=data.get('name', ''),
            length_cm=data.get('length_cm', 0.0),
            min_radius_cm=data.get('min_radius_cm'),
            max_speed_kmh=data.get('max_speed_kmh'),
            state=state,
            occupied_by=data.get('occupied_by'),
        )

    def can_enter(self, train_id: str) -> bool:
        """Check if train can enter this block"""
        return (self.state == BlockState.FREE or
                (self.state == BlockState.RESERVED and self.occupied_by == train_id))

    def reserve(self, train_id: str) -> bool:
        """Reserve block for specified train"""
        # Only genuinely free blocks may be reserved; CAUTION means the next block is occupied
        # and must not be reserved until fully cleared.
        if self.state == BlockState.FREE:
            self.state = BlockState.RESERVED
            self.occupied_by = train_id
            return True
        return False

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
        """Check if block is free"""
        return self.state == BlockState.FREE

    def is_occupied(self) -> bool:
        """Check if block is occupied"""
        return self.state == BlockState.OCCUPIED

    def is_reserved(self) -> bool:
        """Check if block is reserved"""
        return self.state == BlockState.RESERVED
