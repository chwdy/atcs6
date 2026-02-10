"""
Switch Entity Module

Definition:
This module defines the Switch entity which represents a railway turnout (switch)
with configurable physical properties for each branch (MAIN and DIVERGE paths).

Design Notes:
- Each switch has separate length and radius for MAIN (state 0) and DIVERGE (state 1) paths
- Switch state is still stored on TrackSegment for backward compatibility
- Switches are auto-created when switch tracks are placed
- Default length is 0.0 cm (unconfigured) which triggers warnings but allows operation
- Switch locking prevents state changes during path execution

TODO List:
- [ ] Add switch type library with predefined dimensions
- [ ] Add validation for radius/length relationship
- [ ] Add automatic length calculation based on track geometry
"""

from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from network import RailwayNetwork


@dataclass
class Switch:
    """
    Railway switch (turnout) configuration.

    A switch is placed at a specific track tile and defines the physical
    characteristics of each path through the turnout mechanism.

    Locking:
    - Switches can be locked by a path to prevent state changes during train movement
    - locked_by_path: ID of the path that holds the lock
    - locked_state: The state (0/1) the switch is locked to
    - Only the path that locked a switch can unlock it
    """

    switch_id: str  # Unique identifier (e.g., "SW_5_10")
    x: int  # Track tile X coordinate
    y: int  # Track tile Y coordinate

    # Physical properties - PER BRANCH
    # Requirement: Record length and radius separately for each path
    main_length_cm: float = 0.0  # Length of MAIN path (state 0) - default unconfigured
    diverge_length_cm: float = (
        0.0  # Length of DIVERGE path (state 1) - default unconfigured
    )

    # Radii - also per branch
    main_radius_cm: Optional[float] = None  # Radius of MAIN path (None = straight)
    diverge_radius_cm: Optional[float] = None  # Radius of DIVERGE path

    # Switch type metadata
    switch_type: str = "standard"  # "standard", "wye", "crossover", "slip", "scissor"
    name: str = ""  # User-friendly name

    # Metadata
    notes: str = ""  # User notes about this switch

    # ========================================================================
    # SWITCH LOCKING (moved from TrackSegment)
    # ========================================================================
    locked_by_path: Optional[str] = None  # Path ID that locked this switch
    locked_state: Optional[int] = None  # The state the switch is locked to (0 or 1)

    def get_active_length(self, state: int) -> float:
        """
        Get length for the active path based on switch state.

        Args:
            state: Switch state (0=MAIN, 1=DIVERGE)

        Returns:
            Length in cm for the active path
        """
        return self.main_length_cm if state == 0 else self.diverge_length_cm

    def get_active_radius(self, state: int) -> Optional[float]:
        """
        Get radius for the active path based on switch state.

        Args:
            state: Switch state (0=MAIN, 1=DIVERGE)

        Returns:
            Radius in cm for the active path, or None if straight
        """
        return self.main_radius_cm if state == 0 else self.diverge_radius_cm

    # ========================================================================
    # SWITCH LOCKING METHODS
    # ========================================================================

    def is_locked(self) -> bool:
        """Check if switch is currently locked by any path."""
        return self.locked_by_path is not None

    def lock(self, path_id: str, state: int) -> bool:
        """
        Lock this switch in a specific state for a path.

        Args:
            path_id: The path ID requesting the lock
            state: The switch state to lock to (0=MAIN, 1=DIVERGE)

        Returns:
            True if lock successful, False if already locked by different path
        """
        # Already locked by same path in same state - OK
        if self.locked_by_path == path_id and self.locked_state == state:
            return True

        # Already locked by different path
        if self.locked_by_path is not None and self.locked_by_path != path_id:
            return False

        # Lock the switch
        self.locked_by_path = path_id
        self.locked_state = state
        return True

    def unlock(self, path_id: str) -> bool:
        """
        Unlock this switch if it was locked by the specified path.

        Args:
            path_id: The path ID that should own the lock

        Returns:
            True if unlocked, False if not locked by this path
        """
        if self.locked_by_path == path_id:
            self.locked_by_path = None
            self.locked_state = None
            return True
        return False

    def request_state_change(
        self, new_state: int, network: "RailwayNetwork"
    ) -> Tuple[bool, str]:
        """
        Request to change switch state. Fails if switch is locked.

        This method checks both the Switch entity lock and the TrackSegment lock
        to ensure path-locked switches cannot be changed via the API.

        Args:
            new_state: Desired switch state (0=MAIN, 1=DIVERGE)
            network: Railway network to update track state

        Returns:
            Tuple of (success: bool, message: str)
            - On success: (True, "Switch state changed to MAIN/DIVERGE")
            - On failure: (False, "Switch locked by path {path_id}")
        """
        # Check Switch entity lock
        if self.is_locked():
            if self.locked_state == new_state:
                return (
                    True,
                    f"Switch already in requested state (locked by {self.locked_by_path})",
                )
            return (False, f"Switch locked by path {self.locked_by_path}")

        # Check TrackSegment lock (switches locked by path dispatcher)
        track = network.get_track(self.x, self.y)
        if track and track.is_locked_by_path():
            lock_info = track.get_lock_info()
            path_id = lock_info.get("path_id") if lock_info else "unknown"
            if track.locked_state == new_state:
                return (
                    True,
                    f"Switch already in requested state (locked by {path_id})",
                )
            return (False, f"Switch locked by path {path_id}")

        # Get track and change state
        track = network.get_track(self.x, self.y)
        if not track:
            return (False, f"No track at switch position ({self.x}, {self.y})")

        if not track.is_switch:
            return (False, f"Track at ({self.x}, {self.y}) is not a switch")

        track.set_switch_state(new_state)
        state_name = "MAIN" if new_state == 0 else "DIVERGE"
        return (True, f"Switch state changed to {state_name}")

    def is_configured(self, state: Optional[int] = None) -> bool:
        """
        Check if switch is configured (has non-zero length).

        Args:
            state: If provided, check specific path. If None, check if any path is configured.

        Returns:
            True if configured, False otherwise
        """
        if state is None:
            # Check if either path is configured
            return self.main_length_cm > 0 or self.diverge_length_cm > 0
        elif state == 0:
            return self.main_length_cm > 0
        else:
            return self.diverge_length_cm > 0

    def get_current_state(self, network: "RailwayNetwork") -> int:
        """
        Get current switch state from track tile.

        Args:
            network: Railway network to query

        Returns:
            Current switch state (0=MAIN, 1=DIVERGE)
        """
        track = network.get_track(self.x, self.y)
        if track and track.is_switch:
            return track.switch_state
        return 0  # Default to MAIN if track not found

    def to_dict(self, network: "RailwayNetwork" = None) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization.

        Args:
            network: Optional network to check TrackSegment lock status

        Returns:
            Dictionary with switch data including lock information
        """
        data = {
            "switch_id": self.switch_id,
            "x": self.x,
            "y": self.y,
            "main_length_cm": self.main_length_cm,
            "diverge_length_cm": self.diverge_length_cm,
            "main_radius_cm": self.main_radius_cm,
            "diverge_radius_cm": self.diverge_radius_cm,
            "switch_type": self.switch_type,
            "name": self.name,
            "notes": self.notes,
            "locked_by_path": self.locked_by_path,
            "locked_state": self.locked_state,
        }

        # Include TrackSegment lock info if network is provided
        if network:
            track = network.get_track(self.x, self.y)
            if track:
                data["track_locked"] = track.is_locked_by_path()
                lock_info = track.get_lock_info()
                if lock_info:
                    data["track_locked_by_path"] = lock_info.get("path_id")
                    data["track_locked_state"] = lock_info.get("locked_state")

        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Switch":
        """Reconstruct from dictionary after deserialization."""
        switch = cls(
            switch_id=data["switch_id"],
            x=data["x"],
            y=data["y"],
            main_length_cm=data.get("main_length_cm", 0.0),
            diverge_length_cm=data.get("diverge_length_cm", 0.0),
            main_radius_cm=data.get("main_radius_cm"),
            diverge_radius_cm=data.get("diverge_radius_cm"),
            switch_type=data.get("switch_type", "standard"),
            name=data.get("name", ""),
            notes=data.get("notes", ""),
            locked_by_path=data.get("locked_by_path"),
            locked_state=data.get("locked_state"),
        )
        return switch

    def __repr__(self) -> str:
        """String representation for debugging."""
        lock_info = f", locked by {self.locked_by_path}" if self.is_locked() else ""
        return (
            f"Switch({self.switch_id} at ({self.x},{self.y}): "
            f"MAIN={self.main_length_cm}cm, DIVERGE={self.diverge_length_cm}cm{lock_info})"
        )
