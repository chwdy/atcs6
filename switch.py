"""
Switch Entity Module

Definition:
This module defines the Switch entity which represents a railway turnout (switch)
with configurable physical properties for each branch (MAIN and DIVERGE paths).

Design Notes:
- Each switch has separate length and radius for MAIN (position 0) and DIVERGE (position 1) paths
- Switch position is authoritative on the Switch entity (not on TrackSegment) in v1
- Switches are auto-created when switch tracks are placed
- Default length is 0.0 cm (unconfigured) which triggers warnings but allows operation
- Multi-owner future-authority locks: same-position path locks may co-exist; physical
  occupancy (`occupied_by`) is single-owner
"""

from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Tuple, TYPE_CHECKING

from enums import SwitchState
from track_resource import TrackResource

if TYPE_CHECKING:
    from network import RailwayNetwork


@dataclass
class Switch(TrackResource):
    """
    Railway switch (turnout) configuration and runtime state.

    A switch is placed at a specific track tile and defines the physical
    characteristics of each path through the turnout mechanism, plus the
    authoritative runtime state (position, occupied_by, locks, protection).

    Locking model (v1):
    - `locks` is a multi-owner future-authority lock collection keyed by
      `(path_id, claim_role, source_switch_id)` mapped to `(train_id, locked_state)`.
      Compatible same-position lock claims may co-exist.
    - `train_protection_claims` is the train-owned physical protection set used
      after the train head has entered a switch and consumed the path lock.
    - Physical occupancy (`occupied_by`) is single-owner.
    """

    switch_id: str  # Unique identifier (e.g., "SW_5_10")
    x: int  # Track tile X coordinate
    y: int  # Track tile Y coordinate

    # Physical properties - PER BRANCH
    main_length_cm: float = 0.0  # Length of MAIN path (state 0)
    diverge_length_cm: float = 0.0  # Length of DIVERGE path (state 1)

    # Radii - per branch
    main_radius_cm: Optional[float] = None  # Radius of MAIN path (None = straight)
    diverge_radius_cm: Optional[float] = None  # Radius of DIVERGE path

    # Switch type metadata
    switch_type: str = "standard"  # "standard", "wye", "crossover", "slip", "scissor"
    name: str = ""  # User-friendly name
    notes: str = ""  # User notes about this switch

    # ========================================================================
    # AUTHORITATIVE RUNTIME STATE (v1)
    # ========================================================================
    position: SwitchState = SwitchState.MAIN
    """Authoritative physical alignment of the switch (MAIN=0, DIVERGE=1)."""

    occupied_by: Optional[str] = None
    """train_id of the train physically straddling the switch tile, or None."""

    disabled: bool = False
    """When True, the switch is treated as missing track / open circuit."""

    flank_protection: List[Tuple[str, int]] = field(default_factory=list)
    """List of (neighbour_switch_id, blocking_state) tuples computed at network
    load time. `blocking_state` is the neighbour state that would create a flank
    conflict if the neighbour were aligned toward this switch."""

    train_protection_claims: List[Tuple[str, str, int]] = field(default_factory=list)
    """List of (train_id, source_switch_id, locked_state) tuples representing
    train-owned physical protection. Cleared via tail-vacate cleanup."""

    # ========================================================================
    # MULTI-OWNER FUTURE-AUTHORITY LOCKS
    # ========================================================================
    locks: Dict[Tuple[str, str, Optional[str]], Tuple[str, int]] = field(
        default_factory=dict
    )
    """Composite-key lock collection.

    Key: (path_id, claim_role, source_switch_id)
      - claim_role is one of "primary" / "flank" / "overlap".
      - source_switch_id is None for "primary"; the source switch_id for "flank".
    Value: (train_id, locked_state)
    """

    # ========================================================================
    # TrackResource identity
    # ========================================================================

    @property
    def resource_id(self) -> str:
        return self.switch_id

    @property
    def resource_type(self) -> str:
        return "switch"

    # ========================================================================
    # PHYSICAL HELPERS
    # ========================================================================

    def get_active_length(self, state: int) -> float:
        """Get length for the active path based on switch state."""
        return self.main_length_cm if state == 0 else self.diverge_length_cm

    def get_active_radius(self, state: int) -> Optional[float]:
        """Get radius for the active path based on switch state."""
        return self.main_radius_cm if state == 0 else self.diverge_radius_cm

    def is_configured(self, state: Optional[int] = None) -> bool:
        """Check if switch is configured (has non-zero length).

        Args:
            state: If provided, check specific path. If None, check if any path is configured.
        """
        if state is None:
            return self.main_length_cm > 0 or self.diverge_length_cm > 0
        elif state == 0:
            return self.main_length_cm > 0
        else:
            return self.diverge_length_cm > 0

    def get_current_position(self, network: "RailwayNetwork") -> int:
        """Get current switch position.

        v1: position is authoritative on the Switch entity. The `network`
        argument is retained for signature compatibility with legacy callers.
        """
        return int(self.position)

    # ========================================================================
    # MULTI-OWNER LOCK METHODS (v1)
    # ========================================================================

    def is_locked(self) -> bool:
        """Check if switch holds any future-authority lock claim or train-owned
        physical protection."""
        return bool(self.locks) or bool(self.train_protection_claims)

    def can_lock(self, state: int) -> bool:
        """Read-only predicate: can a future lock be acquired for `state`?

        Returns True iff:
        - the switch is not disabled,
        - no existing future lock requires a different state, and
        - no train-owned protection requires a different state.
        """
        if self.disabled:
            return False
        for _existing_train_id, existing_state in self.locks.values():
            if existing_state != state:
                return False
        for _claim_train_id, _claim_source_id, claim_state in self.train_protection_claims:
            if claim_state != state:
                return False
        return True

    def acquire_lock(
        self,
        path_id: str,
        claim_role: str,
        train_id: str,
        locked_state: int,
        source_switch_id: Optional[str] = None,
    ) -> bool:
        """Acquire a future-authority lock claim on the switch.

        Compatible same-state locks may co-exist. The acquisition succeeds when
        every existing lock has the same `locked_state` as the requested one,
        no train-owned protection requires a different state, and physical
        occupancy does not block a switch throw.

        Args:
            path_id: Path that owns this future claim.
            claim_role: "primary", "flank", or "overlap".
            train_id: Train associated with this claim.
            locked_state: Required physical state (0=MAIN, 1=DIVERGE).
            source_switch_id: Source main switch_id for "flank" claims; None for "primary".

        Returns:
            True if recorded (or already present idempotently); False on conflict.
        """
        # Disabled switches cannot be locked.
        if self.disabled:
            return False

        key = (path_id, claim_role, source_switch_id)
        # Idempotent re-acquire of the same canonical claim with the same state.
        if key in self.locks:
            _existing_train_id, existing_state = self.locks[key]
            if existing_state == locked_state:
                return True
            return False

        # Reject if any existing lock requires a different state.
        for _existing_train_id, existing_state in self.locks.values():
            if existing_state != locked_state:
                return False

        # Reject if any train-owned protection requires a different state.
        for _claim_train_id, _claim_source_id, claim_state in self.train_protection_claims:
            if claim_state != locked_state:
                return False

        # Physical occupancy by a different train blocks new locks.
        if self.occupied_by is not None and self.occupied_by != train_id:
            return False

        # If the requested state differs from the current physical state, a
        # switch throw is required; that is forbidden while the switch is
        # physically occupied (even by the same train).
        if int(self.position) != locked_state and self.occupied_by is not None:
            return False

        self.locks[key] = (train_id, locked_state)
        return True

    def release_lock(
        self,
        path_id: str,
        claim_role: str,
        source_switch_id: Optional[str] = None,
    ) -> bool:
        """Release a single future-authority lock entry by composite key.

        Returns:
            True if a matching entry was removed; False if none found.
        """
        key = (path_id, claim_role, source_switch_id)
        if key in self.locks:
            del self.locks[key]
            return True
        return False

    def release_train_protection_from(
        self, source_switch_id: str, train_id: str
    ) -> None:
        """Remove all train_protection_claims with the given (source, train) combo.

        Called on tail-vacate cleanup. Does not inspect or mutate `occupied_by`;
        the caller is responsible for the `train_id == self.occupied_by` ordering
        check before invoking this helper.
        """
        self.train_protection_claims = [
            claim
            for claim in self.train_protection_claims
            if not (claim[0] == train_id and claim[1] == source_switch_id)
        ]

    def on_train_tail_vacated(
        self, train_id: str, network: Optional["RailwayNetwork"] = None
    ) -> None:
        """Handle a tail-vacate event for `train_id` on this switch.

        If `self.occupied_by` matches the releasing `train_id`, clear it and
        walk every neighbour switch that lists this switch as a flank-protection
        source, releasing the train-owned protection sourced from this switch.
        If `self.occupied_by` is set to a different train, leave state unchanged
        and emit a diagnostic.

        `network` is required to walk neighbours; production callers
        (`SwitchManager.on_switch_tail_vacated`) inject it. In editor / unit
        contexts where no neighbour walk is needed it may be omitted.
        """
        if self.occupied_by != train_id:
            other = self.occupied_by
            print(
                f"switch {self.switch_id} release for train {train_id} skipped: "
                f"currently occupied by {other}"
            )
            return

        self.occupied_by = None

        # Walk neighbour switches and release train-owned protection sourced
        # from this switch. Without a network reference we cannot discover
        # neighbours.
        if network is None:
            return
        switches = getattr(network, "switches", None)
        if not switches:
            return
        for neighbour in switches.values():
            if neighbour is self:
                continue
            for entry in neighbour.flank_protection:
                if entry[0] == self.switch_id:
                    neighbour.release_train_protection_from(
                        source_switch_id=self.switch_id, train_id=train_id
                    )
                    break

    def apply_position_change(self, new_position: int) -> Tuple[bool, str]:
        """Validate and apply a position change to `self.position`.

        Pure data operation: checks disabled / occupied / lock conflicts /
        train-protection conflicts, and on success mutates `self.position`.
        Does NOT notify any external entity. The cross-resource notification
        to `BlockManager` is owned by `SwitchManager.request_position_change`.

        Args:
            new_position: Desired switch position (0=MAIN, 1=DIVERGE).

        Returns:
            (success, message) tuple.
        """
        # Disabled switches cannot be targeted by manual/API position-change commands.
        if self.disabled:
            return (False, f"Switch {self.switch_id} is disabled")

        # Physical occupancy blocks any position mutation.
        if self.occupied_by is not None:
            return (
                False,
                f"Switch occupied by train {self.occupied_by}",
            )

        # Block any future lock that requires a different position.
        for key, (_train_id, locked_state) in self.locks.items():
            if locked_state != new_position:
                path_id = key[0]
                return (False, f"Switch locked by path {path_id}")

        # Block any train-owned protection that requires a different position.
        for claim_train_id, claim_source_id, claim_state in self.train_protection_claims:
            if claim_state != new_position:
                return (
                    False,
                    f"Switch protected by train {claim_train_id} (source {claim_source_id})",
                )

        # Mutate authoritative position.
        self.position = SwitchState(new_position)

        position_name = "MAIN" if new_position == 0 else "DIVERGE"
        return (True, f"Switch position changed to {position_name}")

    # ========================================================================
    # SERIALIZATION
    # ========================================================================

    def to_dict(self, network: "RailwayNetwork" = None) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization.

        Note: `simulation_controller` is intentionally NOT serialized.
        """
        return {
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
            "position": int(self.position),
            "occupied_by": self.occupied_by,
            "disabled": self.disabled,
            "flank_protection": [
                [neighbour_id, blocking_state]
                for (neighbour_id, blocking_state) in self.flank_protection
            ],
            "train_protection_claims": [
                [train_id, source_id, locked_state]
                for (train_id, source_id, locked_state) in self.train_protection_claims
            ],
            # Locks: dict keys are non-JSON tuples, so serialize as a list of records.
            "locks": [
                {
                    "path_id": path_id,
                    "claim_role": claim_role,
                    "source_switch_id": source_switch_id,
                    "train_id": train_id,
                    "locked_state": locked_state,
                }
                for (
                    (path_id, claim_role, source_switch_id),
                    (train_id, locked_state),
                ) in self.locks.items()
            ],
        }

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
        )
        switch.position = SwitchState(int(data.get("position", 0)))
        switch.occupied_by = data.get("occupied_by")
        switch.disabled = bool(data.get("disabled", False))
        switch.flank_protection = [
            (neighbour_id, int(blocking_state))
            for (neighbour_id, blocking_state) in data.get("flank_protection", [])
        ]
        switch.train_protection_claims = [
            (train_id, source_id, int(locked_state))
            for (train_id, source_id, locked_state) in data.get(
                "train_protection_claims", []
            )
        ]
        switch.locks = {}
        for entry in data.get("locks", []):
            key = (
                entry["path_id"],
                entry["claim_role"],
                entry.get("source_switch_id"),
            )
            switch.locks[key] = (
                entry["train_id"],
                int(entry["locked_state"]),
            )
        return switch

    def __repr__(self) -> str:
        """String representation for debugging."""
        lock_info = f", locks={len(self.locks)}" if self.locks else ""
        occ = f", occupied_by={self.occupied_by}" if self.occupied_by else ""
        return (
            f"Switch({self.switch_id} at ({self.x},{self.y}): "
            f"position={self.position.name}, MAIN={self.main_length_cm}cm, "
            f"DIVERGE={self.diverge_length_cm}cm{lock_info}{occ})"
        )
