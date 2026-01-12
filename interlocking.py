"""
Interlocking System - LEGACY IMPLEMENTATION (Not Currently Active)

NOTE: This module represents a legacy/alternative implementation of railway interlocking.
It is NOT currently used in the codebase. The active route management system is:
  - path_dispatcher.py (PathDispatcher class) - for train path reservation and block management

This code is preserved for:
- Future integration with centralized traffic control
- Alternative route-based interlocking approach
- Reference implementation of classic interlocking patterns

To activate this system, integrate with network.py and update server.py API routes.

---

Definition:
This module implements the railway interlocking system which ensures safe route setting
by preventing conflicting train movements. It manages route locking, switch positions,
and signal states to maintain operational safety.

TODO List:
- [x] Add automatic route conflict detection
- [x] Implement route approach locking
- [ ] Add support for complex junction interlocking
- [ ] Implement route timing and automatic route release
- [ ] Add integration with centralized traffic control systems
- [ ] Integrate with network.py for active use
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from enums import SwitchPosition, SignalState, TrackType


@dataclass
class InterlockingRoute:
    """
    Interlocking route definition with safety requirements.

    An interlocking route defines a safe path for train movement from one signal
    to another, including all required switch positions and conflicting routes.
    """

    route_id: str
    start_signal: Tuple[int, int]
    end_signal: Tuple[int, int]
    path: List[Tuple[int, int]]  # Track positions included in route
    required_switch_positions: Dict[Tuple[int, int], SwitchPosition] = field(
        default_factory=dict
    )
    conflicting_routes: Set[str] = field(default_factory=set)
    is_locked: bool = False
    locked_by: Optional[str] = None  # Train ID that locked this route

    def can_lock(self, train_id: str) -> bool:
        """Check if route can be locked by specified train"""
        if self.is_locked and self.locked_by != train_id:
            return False
        return True

    def lock(self, train_id: str) -> bool:
        """
        Lock route for specified train.

        Returns:
            bool: True if lock successful, False otherwise
        """
        if self.can_lock(train_id):
            self.is_locked = True
            self.locked_by = train_id
            return True
        return False

    def unlock(self):
        """Unlock route, making it available for other trains"""
        self.is_locked = False
        self.locked_by = None

    def add_conflicting_route(self, route_id: str):
        """Add a conflicting route ID"""
        self.conflicting_routes.add(route_id)

    def has_conflict_with(self, other_route_id: str) -> bool:
        """Check if this route conflicts with another route"""
        return other_route_id in self.conflicting_routes


class InterlockingSystem:
    """
    Railway interlocking system for safe route management.

    The interlocking system prevents conflicting train movements by managing
    route locking, switch positions, and signal aspects according to safety rules.
    """

    def __init__(self):
        self.routes: Dict[str, InterlockingRoute] = {}

    def add_route(self, route: InterlockingRoute):
        """Add a new interlocking route to the system"""
        self.routes[route.route_id] = route
        self.detect_conflicts(route)

    def set_route_conflict(self, route_id1: str, route_id2: str):
        """Set two routes as mutually conflicting"""
        if route_id1 in self.routes and route_id2 in self.routes:
            self.routes[route_id1].add_conflicting_route(route_id2)
            self.routes[route_id2].add_conflicting_route(route_id1)

    def detect_conflicts(self, new_route: InterlockingRoute):
        """
        Automatically detect conflicts with existing routes.

        A conflict exists if:
        1. Routes share any track segment.
        2. Routes share a switch but require different positions (covered by 1 usually).
        3. Routes share a signal (start/end).
        """
        new_path_set = set(new_route.path)

        for existing_route in self.routes.values():
            if existing_route.route_id == new_route.route_id:
                continue

            # Check for shared tracks
            existing_path_set = set(existing_route.path)
            if not new_path_set.isdisjoint(existing_path_set):
                self.set_route_conflict(new_route.route_id, existing_route.route_id)
                print(
                    f"Auto-detected conflict: {new_route.route_id} <-> {existing_route.route_id}"
                )

    def request_route(self, route_id: str, train_id: str, network) -> bool:
        """
        Request to lock a route for a train.

        Args:
            route_id: ID of the route to lock
            train_id: ID of the requesting train
            network: Railway network instance for track access

        Returns:
            bool: True if route successfully locked, False otherwise
        """
        route = self.routes.get(route_id)
        if not route:
            return False

        # Check for conflicting routes
        for conflict_id in route.conflicting_routes:
            conflict_route = self.routes[conflict_id]
            if conflict_route.is_locked:
                print(
                    f"  ✗ Route {route_id} blocked by conflicting route {conflict_id}"
                )
                return False

        # Check and set required switch positions
        for switch_pos, required_position in route.required_switch_positions.items():
            switch_track = network.get_track(*switch_pos)
            if switch_track and switch_track.track_type == TrackType.SWITCH:
                if switch_track.switch_position == SwitchPosition.LOCKED:
                    print(f"  ✗ Switch at {switch_pos} is locked")
                    return False
                switch_track.switch_position = required_position
                switch_track.reserved_by = train_id

        # Lock the route
        if route.lock(train_id):
            # Set entry signal to green
            entry_signal = network.get_signal_at(*route.start_signal)
            if entry_signal:
                entry_signal.state = SignalState.GREEN

            print(f"  ✓ Route {route_id} locked for train {train_id}")
            return True

        return False

    def can_release_route(self, route_id: str, network) -> bool:
        """
        Check if route can be safely released (Approach Locking).

        A route cannot be released if a train is approaching the start signal
        and is within braking distance (or close proximity).
        """
        route = self.routes.get(route_id)
        if not route or not route.is_locked:
            return True

        # If locked by a train, check that train's position
        train_id = route.locked_by
        if not train_id:
            return True  # Should not happen if locked

        train = network.trains.get(train_id)
        if not train:
            return True

        # If train is already ON the route, we can't release it (route locking)
        # But usually we release behind the train.
        # Here we are talking about cancelling a route before the train enters.

        # Check distance to start signal
        start_pos = route.start_signal

        # If train is not moving towards start signal, we can release?
        # Simplified: If train is within X distance of start signal, lock it.

        if train.current_pos:
            # Calculate distance (Manhattan for simplicity or path distance)
            # Ideally use pathfinder distance
            dist = abs(train.current_pos[0] - start_pos[0]) + abs(
                train.current_pos[1] - start_pos[1]
            )

            # If very close (e.g. < 3 units) and moving, don't release
            if dist < 3 and train.current_speed > 0:
                print(f"  ⚠ Approach locking active for train {train_id} (dist {dist})")
                return False

        return True

    def release_route(self, route_id: str, network):
        """
        Release a locked route.

        Args:
            route_id: ID of the route to release
            network: Railway network instance for track access
        """
        if not self.can_release_route(route_id, network):
            print(f"  ✗ Cannot release route {route_id}: Approach locked")
            return

        route = self.routes.get(route_id)
        if not route:
            return

        # Release switch reservations
        for switch_pos in route.required_switch_positions.keys():
            switch_track = network.get_track(*switch_pos)
            if switch_track:
                switch_track.reserved_by = None

        # Set signals to red
        entry_signal = network.get_signal_at(*route.start_signal)
        if entry_signal:
            entry_signal.state = SignalState.RED

        route.unlock()
        print(f"  Route {route_id} released")

    def get_route(self, route_id: str) -> Optional[InterlockingRoute]:
        """Get route by ID"""
        return self.routes.get(route_id)

    def get_locked_routes(self) -> List[InterlockingRoute]:
        """Get all currently locked routes"""
        return [route for route in self.routes.values() if route.is_locked]

    def get_routes_for_train(self, train_id: str) -> List[InterlockingRoute]:
        """Get all routes locked by a specific train"""
        return [
            route
            for route in self.routes.values()
            if route.is_locked and route.locked_by == train_id
        ]
