"""
Block Section System

Definition:
This module implements the block section system for railway safety. Block sections are segments
of track that can only be occupied by one train at a time, preventing collisions and ensuring
safe train separation.

TODO List:
- [ ] Add automatic block detection and creation
- [ ] Implement block section timing and performance metrics
- [ ] Add support for different block section types (automatic, manual, etc.)
- [ ] Implement block section conflict resolution
- [ ] Add integration with signal systems for automatic block control
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from enums import BlockState


@dataclass
class BlockSection:
    """
    Block section for railway safety - only one train allowed at a time.
    
    Block sections are fundamental safety elements in railway systems that prevent
    multiple trains from occupying the same track section simultaneously.
    """
    block_id: str
    tracks: List[Tuple[int, int]] = field(default_factory=list)
    state: BlockState = BlockState.FREE
    occupied_by: Optional[str] = None  # Train ID
    entry_signal: Optional[Tuple[int, int]] = None  # Entry signal position
    exit_signal: Optional[Tuple[int, int]] = None   # Exit signal position
    
    def can_enter(self, train_id: str) -> bool:
        """Check if train can enter this block section"""
        return (self.state == BlockState.FREE or 
                (self.state == BlockState.RESERVED and self.occupied_by == train_id))
    
    def reserve(self, train_id: str) -> bool:
        """
        Reserve block section for specified train.
        
        Returns:
            bool: True if reservation successful, False otherwise
        """
        if self.state == BlockState.FREE:
            self.state = BlockState.RESERVED
            self.occupied_by = train_id
            return True
        return False
    
    def occupy(self, train_id: str) -> bool:
        """
        Mark block section as occupied by specified train.
        
        Returns:
            bool: True if occupation successful, False otherwise
        """
        if self.can_enter(train_id):
            self.state = BlockState.OCCUPIED
            self.occupied_by = train_id
            return True
        return False
    
    def release(self):
        """Release block section, making it available for other trains"""
        self.state = BlockState.FREE
        self.occupied_by = None
    
    def lock(self):
        """Lock block section (for maintenance or emergency)"""
        self.state = BlockState.LOCKED
    
    def unlock(self):
        """Unlock block section"""
        if self.state == BlockState.LOCKED:
            self.state = BlockState.FREE
    
    def is_free(self) -> bool:
        """Check if block section is free"""
        return self.state == BlockState.FREE
    
    def is_occupied(self) -> bool:
        """Check if block section is occupied"""
        return self.state == BlockState.OCCUPIED
    
    def is_reserved(self) -> bool:
        """Check if block section is reserved"""
        return self.state == BlockState.RESERVED
    
    def get_occupying_train(self) -> Optional[str]:
        """Get ID of train currently occupying or reserving this block"""
        return self.occupied_by