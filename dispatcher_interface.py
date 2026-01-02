"""
Dispatcher Interface for Railway Route Management

This module provides the RouteDispatcher class for managing train routes,
block reservations, switch positions, and signal assignments.

The dispatcher uses in-memory state (not persisted). If the system restarts,
all routes are cleared and must be re-established. This is the fail-safe default
behavior consistent with real-world interlocking systems.

Key concepts:
- Route: A path from signal to destination, including blocks, switches, and signals
- Reservation: Blocks are reserved for specific trains before they enter
- Lock: Switches can be locked to prevent other dispatchers from changing them
- Staged Release: Blocks transition OCCUPIED → CAUTION → FREE as train progresses
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Set, Any
from enum import Enum


class RouteStatus(Enum):
    """Status of a route."""
    ACTIVE = "active"       # Route is set and in use
    COMPLETED = "completed" # Train has cleared all blocks
    CANCELLED = "cancelled" # Route was manually cancelled


@dataclass
class Route:
    """
    Represents an active route for a train.
    
    Attributes:
        train_id: ID of the train using this route
        signal_ids: List of signal IDs assigned to this train
        block_ids: List of block IDs reserved for this train
        switch_positions: Dict of (x,y) -> switch_state for locked switches
        status: Current route status
        blocks_cleared: Set of block IDs the train has already left
    """
    train_id: str
    signal_ids: List[str] = field(default_factory=list)
    block_ids: List[str] = field(default_factory=list)
    switch_positions: Dict[Tuple[int, int], int] = field(default_factory=dict)
    status: RouteStatus = RouteStatus.ACTIVE
    blocks_cleared: Set[str] = field(default_factory=set)
    current_block_id: Optional[str] = None  # Block the train is currently in


@dataclass
class SwitchLock:
    """
    Represents a lock on a switch.
    
    Attributes:
        position: (x, y) position of the switch
        dispatcher_id: ID of the dispatcher who locked it
        train_id: ID of the train this lock is for
        locked_state: The switch state (0=MAIN, 1=DIVERGE) when locked
    """
    position: Tuple[int, int]
    dispatcher_id: str
    train_id: str
    locked_state: int


class RouteDispatcher:
    """
    Manages train routes, block reservations, and switch locks.
    
    This is the main interface for dispatchers to control train movements.
    State is stored in-memory and not persisted.
    
    Usage:
        dispatcher = RouteDispatcher(network)
        
        # Set a route for a train
        success, failed_at = dispatcher.set_route(
            train_id="TRAIN_001",
            signal_ids=["SIG_1", "SIG_2"],
            block_ids=["BLK_A", "BLK_B", "BLK_C"],
            switch_positions={(3, 0): 0}  # Set switch at (3,0) to MAIN
        )
        
        # As train progresses
        dispatcher.advance_train("TRAIN_001", "BLK_B")  # Train entered BLK_B
        
        # Manual cancellation if needed
        dispatcher.cancel_route("TRAIN_001")
    """
    
    def __init__(self, network):
        """
        Initialize dispatcher with reference to railway network.
        
        Args:
            network: RailwayNetwork instance
        """
        self.network = network
        self.routes: Dict[str, Route] = {}  # train_id -> Route
        self.switch_locks: Dict[Tuple[int, int], SwitchLock] = {}  # position -> SwitchLock
    
    def set_route(
        self,
        train_id: str,
        signal_ids: List[str],
        block_ids: List[str],
        switch_positions: Dict[Tuple[int, int], int],
        dispatcher_id: str = "default"
    ) -> Tuple[bool, Optional[str]]:
        """
        Set up a route for a train.
        
        This method:
        1. Checks all blocks are available (FREE or already reserved for this train)
        2. Checks all switches can be locked (not locked by another dispatcher)
        3. Reserves all blocks for the train
        4. Sets and locks all switches
        5. Assigns approaching_train_id to all signals
        
        Args:
            train_id: ID of the train
            signal_ids: List of signal IDs to assign to this train
            block_ids: List of block IDs to reserve (in order of travel)
            switch_positions: Dict mapping (x,y) -> desired switch state (0=MAIN, 1=DIVERGE)
            dispatcher_id: ID of the dispatcher setting this route
            
        Returns:
            Tuple of (success: bool, failed_at: Optional[str])
            - If successful: (True, None)
            - If failed: (False, "ID of first resource that couldn't be acquired")
        """
        from block import BlockState
        
        # Check if train already has a route
        if train_id in self.routes and self.routes[train_id].status == RouteStatus.ACTIVE:
            return False, f"Train {train_id} already has active route"
        
        # Phase 1: Validate all resources are available
        
        # Check blocks
        for block_id in block_ids:
            block = self.network.blocks.get(block_id)
            if not block:
                return False, f"Block {block_id} not found"
            
            # Block must be FREE or already reserved for this train
            if block.state == BlockState.OCCUPIED:
                return False, f"Block {block_id} is occupied"
            if block.state == BlockState.RESERVED and block.occupied_by != train_id:
                return False, f"Block {block_id} is reserved by {block.occupied_by}"
            if block.state == BlockState.CAUTION:
                # Caution indicates the block ahead is occupied; treat as unavailable.
                return False, f"Block {block_id} is in CAUTION state"
        
        # Check switches
        for pos, desired_state in switch_positions.items():
            # Check if switch exists
            track = self.network.get_track(*pos)
            if not track or not track.is_switch:
                return False, f"No switch at position {pos}"
            
            # Check if locked by another dispatcher
            if pos in self.switch_locks:
                lock = self.switch_locks[pos]
                if lock.train_id != train_id:
                    return False, f"Switch at {pos} is locked by {lock.dispatcher_id} for train {lock.train_id}"
        
        # Check signals
        for signal_id in signal_ids:
            signal = self.network.signals.get(signal_id)
            if not signal:
                return False, f"Signal {signal_id} not found"
            # Check if signal is already assigned to another train
            if signal.approaching_train_id and signal.approaching_train_id != train_id:
                return False, f"Signal {signal_id} is assigned to train {signal.approaching_train_id}"
        
        # Phase 2: Acquire all resources
        
        # Reserve blocks
        for block_id in block_ids:
            block = self.network.blocks.get(block_id)
            if block.state == BlockState.FREE or block.state == BlockState.CAUTION:
                block.reserve(train_id)
        
        # Set and lock switches
        for pos, desired_state in switch_positions.items():
            track = self.network.get_track(*pos)
            track.switch_state = desired_state
            
            # Create lock
            self.switch_locks[pos] = SwitchLock(
                position=pos,
                dispatcher_id=dispatcher_id,
                train_id=train_id,
                locked_state=desired_state
            )
        
        # Assign signals to train
        for signal_id in signal_ids:
            signal = self.network.signals.get(signal_id)
            signal.approaching_train_id = train_id
        
        # Create route record
        self.routes[train_id] = Route(
            train_id=train_id,
            signal_ids=signal_ids,
            block_ids=block_ids,
            switch_positions=switch_positions,
            status=RouteStatus.ACTIVE,
            current_block_id=block_ids[0] if block_ids else None
        )
        
        # Trigger signal recalculation
        self.network.update_signals_from_blocks()
        
        return True, None
    
    def cancel_route(self, train_id: str) -> bool:
        """
        Cancel a route and release all resources immediately.
        
        This is for manual intervention when plans change. All blocks,
        switches, and signals are released immediately.
        
        Args:
            train_id: ID of the train whose route to cancel
            
        Returns:
            True if cancelled, False if no active route found
        """
        from block import BlockState
        
        if train_id not in self.routes:
            return False
        
        route = self.routes[train_id]
        if route.status != RouteStatus.ACTIVE:
            return False
        
        # Release all blocks (set to FREE)
        for block_id in route.block_ids:
            block = self.network.blocks.get(block_id)
            if block and block.occupied_by == train_id:
                block.release()
        
        # Unlock all switches
        for pos in route.switch_positions:
            if pos in self.switch_locks and self.switch_locks[pos].train_id == train_id:
                del self.switch_locks[pos]
        
        # Clear signal assignments
        for signal_id in route.signal_ids:
            signal = self.network.signals.get(signal_id)
            if signal and signal.approaching_train_id == train_id:
                signal.approaching_train_id = None
        
        # Mark route as cancelled
        route.status = RouteStatus.CANCELLED
        
        # Trigger signal recalculation
        self.network.update_signals_from_blocks()
        
        return True
    
    def advance_train(self, train_id: str, entered_block_id: str) -> bool:
        """
        Called when a train enters a new block. Triggers staged release of previous blocks.
        
        Event-based staged release:
        - When train enters block B, block A transitions OCCUPIED → CAUTION
        - When train enters block C, block A transitions CAUTION → FREE
        
        This provides a two-block buffer behind the train for realistic signaling.
        
        Args:
            train_id: ID of the train
            entered_block_id: ID of the block the train just entered
            
        Returns:
            True if successful, False if no route found or block not in route
        """
        from block import BlockState
        
        if train_id not in self.routes:
            return False
        
        route = self.routes[train_id]
        if route.status != RouteStatus.ACTIVE:
            return False
        
        # Verify block is in route
        if entered_block_id not in route.block_ids:
            return False
        
        # Get the entered block
        entered_block = self.network.blocks.get(entered_block_id)
        if not entered_block:
            return False
        
        # Mark entered block as OCCUPIED
        entered_block.occupy(train_id)
        
        # Get previous block (if any)
        block_index = route.block_ids.index(entered_block_id)
        previous_block_id = route.current_block_id
        
        if previous_block_id and previous_block_id != entered_block_id:
            previous_block = self.network.blocks.get(previous_block_id)
            if previous_block:
                # Previous block: OCCUPIED → CAUTION
                previous_block.set_to_caution()
                
                # Add to cleared set for tracking
                route.blocks_cleared.add(previous_block_id)
        
        # Check for block before previous (to release to FREE)
        # This is the "two-block buffer" logic
        if block_index >= 2:
            two_back_block_id = route.block_ids[block_index - 2]
            two_back_block = self.network.blocks.get(two_back_block_id)
            if two_back_block and two_back_block.state == BlockState.CAUTION:
                # Two blocks back: CAUTION → FREE
                two_back_block.clear()
                
                # Also unlock any switches in that block's area
                # (simplified: unlock switches as we clear blocks)
                self._release_switches_for_block(train_id, two_back_block_id)
        
        # Update current block
        route.current_block_id = entered_block_id
        
        # Check if route is complete (train in last block)
        if block_index == len(route.block_ids) - 1:
            # Train is in final block - start completion sequence
            pass  # Could mark route for completion after train leaves
        
        # Trigger signal recalculation
        self.network.update_signals_from_blocks()
        
        return True
    
    def train_exited_block(self, train_id: str, exited_block_id: str) -> bool:
        """
        Called when a train completely exits a block.
        
        This is typically called when the train's tail end leaves a block.
        Triggers the staged release if not already handled by advance_train.
        
        Args:
            train_id: ID of the train
            exited_block_id: ID of the block the train just left
            
        Returns:
            True if successful
        """
        from block import BlockState
        
        if train_id not in self.routes:
            return False
        
        route = self.routes[train_id]
        
        # Ensure block transitions to CAUTION (if still OCCUPIED)
        block = self.network.blocks.get(exited_block_id)
        if block and block.state == BlockState.OCCUPIED and block.occupied_by == train_id:
            block.set_to_caution()
        
        # Trigger signal recalculation
        self.network.update_signals_from_blocks()
        
        return True
    
    def complete_route(self, train_id: str) -> bool:
        """
        Complete a route after the train has cleared all blocks.
        
        Releases any remaining resources and marks route as completed.
        
        Args:
            train_id: ID of the train
            
        Returns:
            True if successful
        """
        from block import BlockState
        
        if train_id not in self.routes:
            return False
        
        route = self.routes[train_id]
        
        # Release all remaining blocks
        for block_id in route.block_ids:
            block = self.network.blocks.get(block_id)
            if block and block.occupied_by == train_id:
                if block.state in (BlockState.RESERVED, BlockState.OCCUPIED, BlockState.CAUTION):
                    block.release()
        
        # Unlock all switches
        for pos in route.switch_positions:
            if pos in self.switch_locks and self.switch_locks[pos].train_id == train_id:
                del self.switch_locks[pos]
        
        # Clear signal assignments
        for signal_id in route.signal_ids:
            signal = self.network.signals.get(signal_id)
            if signal and signal.approaching_train_id == train_id:
                signal.approaching_train_id = None
        
        # Mark route as completed
        route.status = RouteStatus.COMPLETED
        
        # Trigger signal recalculation
        self.network.update_signals_from_blocks()
        
        return True
    
    def _release_switches_for_block(self, train_id: str, block_id: str) -> None:
        """
        Release switches that were only needed for a specific block.
        
        This is a simplified implementation. In a full system, we'd track
        which switches are needed for which parts of the route.
        """
        route = self.routes.get(train_id)
        if not route:
            return

        # If the block is not on this route, do nothing
        if block_id not in route.block_ids:
            return

        # Unlock all switches allocated for this route that are now behind the train.
        for pos in list(route.switch_positions.keys()):
            lock = self.switch_locks.get(pos)
            if lock and lock.train_id == train_id:
                del self.switch_locks[pos]
    
    def lock_switch(
        self,
        position: Tuple[int, int],
        desired_state: int,
        dispatcher_id: str,
        train_id: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Lock a switch to a specific position.
        
        First-come-first-served: If another dispatcher has already locked
        the switch, this will fail.
        
        Args:
            position: (x, y) position of the switch
            desired_state: Switch state (0=MAIN, 1=DIVERGE)
            dispatcher_id: ID of the dispatcher requesting the lock
            train_id: ID of the train this lock is for
            
        Returns:
            Tuple of (success, error_message)
        """
        # Check if switch exists
        track = self.network.get_track(*position)
        if not track or not track.is_switch:
            return False, f"No switch at position {position}"
        
        # Check if already locked by another
        if position in self.switch_locks:
            lock = self.switch_locks[position]
            if lock.train_id != train_id:
                return False, f"Switch locked by {lock.dispatcher_id} for train {lock.train_id}"
        
        # Set switch position
        track.switch_state = desired_state
        
        # Create lock
        self.switch_locks[position] = SwitchLock(
            position=position,
            dispatcher_id=dispatcher_id,
            train_id=train_id,
            locked_state=desired_state
        )
        
        # Trigger signal recalculation (switch position affects signals)
        self.network.update_signals_from_blocks()
        
        return True, None
    
    def unlock_switch(
        self,
        position: Tuple[int, int],
        dispatcher_id: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Unlock a switch.
        
        Only the dispatcher who locked it can unlock it.
        
        Args:
            position: (x, y) position of the switch
            dispatcher_id: ID of the dispatcher requesting the unlock
            
        Returns:
            Tuple of (success, error_message)
        """
        if position not in self.switch_locks:
            return False, f"Switch at {position} is not locked"
        
        lock = self.switch_locks[position]
        if lock.dispatcher_id != dispatcher_id:
            return False, f"Switch locked by {lock.dispatcher_id}, not {dispatcher_id}"
        
        del self.switch_locks[position]
        return True, None
    
    def get_locked_switches(self) -> Dict[Tuple[int, int], SwitchLock]:
        """
        Get all currently locked switches.
        
        Returns:
            Dict mapping position -> SwitchLock
        """
        return dict(self.switch_locks)
    
    def get_route(self, train_id: str) -> Optional[Route]:
        """
        Get route for a train.
        
        Args:
            train_id: ID of the train
            
        Returns:
            Route if exists, None otherwise
        """
        return self.routes.get(train_id)
    
    def get_all_routes(self) -> Dict[str, Route]:
        """
        Get all routes.
        
        Returns:
            Dict mapping train_id -> Route
        """
        return dict(self.routes)
    
    def is_switch_locked(self, position: Tuple[int, int]) -> bool:
        """
        Check if a switch is locked.
        
        Args:
            position: (x, y) position of the switch
            
        Returns:
            True if locked
        """
        return position in self.switch_locks
    
    def get_switch_lock(self, position: Tuple[int, int]) -> Optional[SwitchLock]:
        """
        Get lock info for a switch.
        
        Args:
            position: (x, y) position of the switch
            
        Returns:
            SwitchLock if locked, None otherwise
        """
        return self.switch_locks.get(position)
