"""
Block State Manager - Single Source of Truth for Block Occupancy

Definition:
This module provides the BlockManager class which serves as the centralized
authority for all block state transitions in the railway network. It replaces
the previous distributed block management where multiple components (PathDispatcher,
Train.update_occupancy, manual fallback) competed for block state control.

Architecture:
    SimulationController (Orchestrator)
             ↓
        TrainMover (Movement logic)
             ↓
        BlockManager (SINGLE SOURCE OF TRUTH)
             ↓
          Block (State storage only)

Key Responsibilities:
- Enforce block state machine transitions (FREE → RESERVED → OCCUPIED → CAUTION → FREE)
- Prevent race conditions by providing atomic state change operations
- Maintain block occupancy tracking for safety margins
- Validate state transitions before applying them

Design Principles:
1. Single Authority: Only BlockManager modifies block states
2. Atomic Operations: All state changes happen through BlockManager methods
3. State Machine Enforcement: Illegal transitions are rejected
4. Separation of Concerns: Block class only stores state, doesn't manage transitions

TODO List:
- [x] Define BlockManager class with core state management methods
- [x] Integrate with RailwayNetwork
- [x] Add comprehensive test suite (9 tests in test_backend.py)
- [x] Migrate PathDispatcher to use BlockManager
- [x] Avoid train.update_occupancy in production (kept for backward compatibility/tests)
- [x] Document migration guide (see docs/ARCHITECTURE.md, AGENTS.md)
"""

from typing import Optional, Dict, List
from block import Block, BlockState


class BlockManager:
    """Centralized block state manager - single source of truth for block occupancy.

    This class ensures that all block state transitions follow the correct state
    machine and prevents race conditions caused by multiple components trying to
    update block states simultaneously.

    State Machine:
        FREE → RESERVED → OCCUPIED → CAUTION → FREE

    State Descriptions:
        FREE: No train reservation or occupation
        RESERVED: Reserved for a train's planned path (interlocking locked)
        OCCUPIED: Physical train presence detected
        CAUTION: Train recently cleared but safety margin maintained
    """

    def __init__(self, blocks: Dict[str, Block]):
        """Initialize BlockManager with existing blocks.

        Args:
            blocks: Dictionary mapping block_id to Block instances
        """
        self.blocks = blocks

    def occupy_block(self, block_id: str, train_id: str) -> bool:
        """Mark a block as occupied by a train.

        This is called when a train physically enters a block. The block must
        be either FREE, RESERVED, or already OCCUPIED by the same train.

        Args:
            block_id: ID of the block to occupy
            train_id: ID of the train occupying the block

        Returns:
            True if occupation succeeded, False if block doesn't exist or invalid state

        State Transitions:
            FREE → OCCUPIED (manual mode without reservation)
            RESERVED → OCCUPIED (normal case with path dispatcher)
            OCCUPIED → OCCUPIED (idempotent, same train)
        """
        block = self.blocks.get(block_id)
        if not block:
            return False

        # Idempotent: already occupied by same train
        if block.state == BlockState.OCCUPIED and block.occupied_by == train_id:
            return True

        # Allow FREE → OCCUPIED (manual mode)
        if block.state == BlockState.FREE:
            block.state = BlockState.OCCUPIED
            block.occupied_by = train_id
            return True

        # Allow RESERVED → OCCUPIED (normal path following)
        if block.state == BlockState.RESERVED:
            # TODO: Should we validate that block.occupied_by matches train_id?
            # Currently RESERVED might be set by PathDispatcher with a train_id
            block.state = BlockState.OCCUPIED
            block.occupied_by = train_id
            return True

        # Block in incompatible state (CAUTION or OCCUPIED by different train)
        return False

    def set_block_to_caution(self, block_id: str) -> bool:
        """Set a block to CAUTION state (safety margin after train clears).

        This is called when a train's head exits a block but the tail might still
        be near. CAUTION state prevents premature block release and maintains
        safety margins for following trains.

        Args:
            block_id: ID of the block to set to CAUTION

        Returns:
            True if state change succeeded, False if block doesn't exist or invalid state

        State Transitions:
            OCCUPIED → CAUTION (normal case)
            CAUTION → CAUTION (idempotent)
        """
        block = self.blocks.get(block_id)
        if not block:
            return False

        # Idempotent: already in CAUTION
        if block.state == BlockState.CAUTION:
            return True

        # Normal transition: OCCUPIED → CAUTION
        if block.state == BlockState.OCCUPIED:
            block.state = BlockState.CAUTION
            # Keep occupied_by field for tracking
            return True

        # Invalid transition
        return False

    def clear_block(self, block_id: str) -> bool:
        """Clear a block back to FREE state (complete release).

        This is called when we're certain the train has completely cleared the block
        and safety margins are satisfied. This completes the block lifecycle.

        Args:
            block_id: ID of the block to clear

        Returns:
            True if clearing succeeded, False if block doesn't exist

        State Transitions:
            CAUTION → FREE (normal case)
            OCCUPIED → FREE (manual override)
            FREE → FREE (idempotent)
        """
        block = self.blocks.get(block_id)
        if not block:
            return False

        # Idempotent: already FREE
        if block.state == BlockState.FREE:
            block.occupied_by = None
            block.reserved_by_path = None
            block.reserved_direction = None
            return True

        # Normal transition: CAUTION → FREE
        if block.state == BlockState.CAUTION:
            block.state = BlockState.FREE
            block.occupied_by = None
            block.reserved_by_path = None
            block.reserved_direction = None
            return True

        # Allow OCCUPIED → FREE (manual override for cleanup)
        if block.state == BlockState.OCCUPIED:
            block.state = BlockState.FREE
            block.occupied_by = None
            block.reserved_by_path = None
            block.reserved_direction = None
            return True

        # RESERVED → FREE (path cancellation)
        if block.state == BlockState.RESERVED:
            block.state = BlockState.FREE
            block.occupied_by = None
            block.reserved_by_path = None
            block.reserved_direction = None
            return True

        return True  # Shouldn't reach here, but be permissive

    def release_block(self, block_id: str, train_id: Optional[str] = None) -> bool:
        """Release a block from occupation/reservation.

        This is a high-level method that transitions through the proper state machine:
        OCCUPIED → CAUTION → FREE

        If called with train_id, validates that the block is occupied by that train
        before releasing.

        Args:
            block_id: ID of the block to release
            train_id: Optional train ID for validation

        Returns:
            True if release succeeded, False if validation failed
        """
        block = self.blocks.get(block_id)
        if not block:
            return False

        # Validate train_id if provided
        if train_id and block.occupied_by != train_id:
            return False

        # Transition based on current state
        if block.state == BlockState.OCCUPIED:
            # OCCUPIED → CAUTION (maintain safety margin)
            return self.set_block_to_caution(block_id)
        elif block.state == BlockState.CAUTION:
            # CAUTION → FREE (complete release)
            return self.clear_block(block_id)
        elif block.state == BlockState.RESERVED:
            # RESERVED → FREE (path cancellation)
            return self.clear_block(block_id)
        elif block.state == BlockState.FREE:
            # Already free
            return True

        return False

    def get_block_state(self, block_id: str) -> Optional[BlockState]:
        """Get current state of a block.

        Args:
            block_id: ID of the block to query

        Returns:
            BlockState enum value, or None if block doesn't exist
        """
        block = self.blocks.get(block_id)
        return block.state if block else None

    def get_block_occupant(self, block_id: str) -> Optional[str]:
        """Get ID of train occupying a block.

        Args:
            block_id: ID of the block to query

        Returns:
            Train ID string, or None if block is not occupied or doesn't exist
        """
        block = self.blocks.get(block_id)
        return block.occupied_by if block else None

    def is_block_available(
        self, block_id: str, for_train_id: Optional[str] = None
    ) -> bool:
        """Check if a block is available for occupation.

        A block is available if it's FREE, or if it's RESERVED/OCCUPIED by the
        same train (for re-entry scenarios).

        Args:
            block_id: ID of the block to check
            for_train_id: Optional train ID to check reservation status

        Returns:
            True if block is available for the train, False otherwise
        """
        block = self.blocks.get(block_id)
        if not block:
            return False

        if block.state == BlockState.FREE:
            return True

        # If checking for specific train, allow if already reserved/occupied by that train
        if for_train_id and block.occupied_by == for_train_id:
            return True

        return False
