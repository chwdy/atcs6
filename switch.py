"""
Switch Entity Module

Definition:
This module defines the Switch entity which represents a railway turnout (switch)
with configurable physical properties for each branch (MAIN and DIVERGE paths).

Design Notes:
- Each switch has separate length and radius for MAIN (position 0) and DIVERGE (position 1) paths
- Switch position is authoritative on the Switch entity (not on Tile) in v1
- Switches are auto-created when switch tracks are placed
- Default length is 0.0 cm (unconfigured) which triggers warnings but allows operation
- Multi-owner future-authority locks: same-position path locks may co-exist; physical
  occupancy (`physical_occupants`) is route-keyed and may hold multiple trains on
  non-conflicting routes. `occupied_by` is a lossy single-value projection of it.
"""

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Any, Optional, Tuple, TYPE_CHECKING

from enums import SwitchState

if TYPE_CHECKING:
    from network import RailwayNetwork


# ============================================================================
# Route-keyed switch occupancy
# ============================================================================
#
# A "route" through a switch tile is the unordered pair of edges the train
# uses (entry edge + exit edge), e.g. `frozenset({"N", "W"})` for an NW arc.
# Two routes physically conflict on the same tile iff they share any point
# in the tile interior or any edge midpoint. Per the geometry encoded in
# `track.BIT_TO_EDGES`:
#   - NS and EW pass through tile center (vertical/horizontal straights)
#   - NE, NW, SE, SW are 90° arcs in one quadrant each
# Conflict matrix derivation:
#   - Two routes sharing any edge (frozenset intersection non-empty) →
#     they meet at that edge's midpoint → conflict.
#   - NS (frozenset{N,S}) and EW (frozenset{E,W}) share no edge but both
#     pass through tile center → conflict.
#   - The only pairs that share no edge AND don't both pass through center
#     are NW+SE (opposite quadrants, no contact) and NE+SW (likewise) →
#     non-conflict.
# For a scissors crossover (SCISSORS_BITS = NS+EW+NW+SE) in DIVERGE state,
# the active routes are NW and SE — non-conflict, so two trains can
# physically co-occupy the tile in different routes.

_STRAIGHTS_THROUGH_CENTER: FrozenSet[FrozenSet[str]] = frozenset({
    frozenset({"N", "S"}),
    frozenset({"E", "W"}),
})



@dataclass
class Switch:
    """
    Railway switch (turnout) configuration and runtime state.

    A switch is placed at a specific track tile and defines the physical
    characteristics of each path through the turnout mechanism, plus the
    authoritative runtime state (position, occupied_by, locks, protection).

    Locking model (v1):
    - `locks` is a multi-owner future-authority lock collection keyed by
      `(path_id, source_switch_id)` mapped to `(train_id, locked_state)`.
      Compatible same-position lock claims may co-exist. The claim role
      (`claim_role` = "main" when `source_switch_id is None`, else "flank";
      see design_new.md, "Switch entity") is derived from `source_switch_id is None`,
      not stored as a separate key field.
    - `train_protection_claims` is the train-owned physical protection set used
      after the train head has entered a switch and consumed the path lock.
    - Physical occupancy (`physical_occupants`) is route-keyed and may hold two
      trains at once on non-conflicting routes (e.g. a scissors crossover).
      `occupied_by` is a lossy single-value projection over it, not a stored
      single-owner field.
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

    position_known: bool = True
    """Whether the app knows where the points are (design_new.md "Switch
    entity", `position_known`). Activating Virtual Hardware or Real Hardware
    sets it False on every switch, because a fresh session has thrown no coil;
    any write through `apply_position_change(...)` or
    `record_detected_position(...)` sets it True. Runtime only, never saved."""

    flip_count: int = 0
    """Maintenance counter: number of times the switch actually changed
    alignment. Incremented by `apply_position_change(...)` only on a real
    change. Unlike the transient runtime state below, this is persisted with
    the layout (see design_new.md "Maintenance counters")."""

    route_owners: Dict[FrozenSet[str], str] = field(default_factory=dict)
    """Route-keyed physical occupancy: maps each route currently in use
    (frozenset of two edges, e.g. ``frozenset({"N","W"})``) to the
    ``train_id`` traversing it. A new route may be added iff it does not
    conflict (per :func:`routes_conflict`) with any existing entry."""

    allow_exit_from_either_leg: bool = False
    motor_unavailable: bool = False
    disabled: bool = False
    """When True, the switch is treated as missing track / open circuit. No
    operator override bypasses this; physical entry is a fatal conflict."""

    manual: bool = False
    """When True, automatic systems treat the switch as if disabled (pathfinding
    skips it, lock acquisition fails, manual/API switch-state commands fail),
    but an operator command carrying `manual_override=True` bypasses the filter.
    Physical entry into a manual (but not disabled) switch is ordinary clean
    entry. See the `disabled` and `manual` availability flags canonical home
    in design_new.md (under TrackResource)."""

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
    locks: Dict[Tuple[str, Optional[str]], Tuple[str, int]] = field(
        default_factory=dict
    )
    """Composite-key lock collection.

    Key: (path_id, source_switch_id)
      - source_switch_id is None for primary (main) claims; the source
        switch_id for flank-protection claims. Role ("primary" vs "flank")
        is derived from this field, not stored separately.
    Value: (train_id, locked_state)
    """

    lock_routes: Dict[Tuple[str, Optional[str]], FrozenSet[str]] = field(
        default_factory=dict
    )
    """Parallel route-info map for ``locks`` (same composite key).

    Stores the `frozenset({entry_edge, exit_edge})` that each path uses
    through this switch. Lives alongside ``locks`` (not inside its value
    tuple) so that legacy 2-tuple unpacks of ``locks.values()`` keep
    working; route-aware callers consult both. A missing entry means the
    holding claim did not carry route info; cross-claim compatibility
    then falls back to the cross-train primary rule."""

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


    # ========================================================================
    # ROUTE-KEYED OCCUPANCY (v1)
    # ========================================================================



    @property
    def occupied_by(self) -> Optional[str]:
        """Any train currently occupying this switch via ``route_owners``,
        or ``None`` when unoccupied. Derived, not stored, and lossy: when two
        trains hold non-conflicting routes (e.g. NW and SE on a scissors
        crossover) it returns only one of them, so it stays a diagnostic
        read — new consumers should query ``route_owners`` directly.
        """
        return next(iter(self.route_owners.values()), None)

    # ========================================================================
    # MULTI-OWNER LOCK METHODS (v1)
    # ========================================================================

    def is_locked(self) -> bool:
        """Check if switch holds any future-authority lock claim or train-owned
        physical protection."""
        return bool(self.locks) or bool(self.train_protection_claims)



    def apply_position_change(
        self, new_position: int, manual_override: bool = False
    ) -> Tuple[bool, str]:
        """Validate and apply a position change to `self.position`.

        Pure data operation: checks availability (disabled, manual) /
        occupied / lock conflicts / train-protection conflicts, and on
        success mutates `self.position`. Does NOT notify any external
        entity. The cross-resource notification to `BlockManager` is owned
        by `SwitchManager.request_position_change`.

        Args:
            new_position: Desired switch position (0=MAIN, 1=DIVERGE).
            manual_override: When True (operator opt-in), allow position
                changes on a manual switch. The disabled filter always
                applies regardless. Per the `disabled` and `manual`
                availability flags canonical home in design_new.md.

        Returns:
            (success, message) tuple.
        """
        # Disabled switches cannot be targeted by manual/API position-change commands.
        if self.disabled:
            return (False, f"Switch {self.switch_id} is disabled")

        # Manual switches require an explicit operator override.
        if self.manual and not manual_override:
            return (
                False,
                f"Switch {self.switch_id} is set to manual (manual_override required)",
            )

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

        # Mutate authoritative position. Count a maintenance flip only when the
        # alignment actually changes; re-aligning to the position the switch
        # already holds is a no-op flip (see design_new.md "Maintenance counters").
        if self.position != SwitchState(new_position):
            self.flip_count += 1
        self.position = SwitchState(new_position)
        self.position_known = True

        position_name = "MAIN" if new_position == 0 else "DIVERGE"
        return (True, f"Switch position changed to {position_name}")


    # ========================================================================
    # SERIALIZATION
    # ========================================================================

    def to_dict(self, network: "RailwayNetwork" = None) -> Dict[str, Any]:
        """Convert to dictionary for the live frontend payload.

        Includes runtime state (position, occupied_by, locks,
        flank_protection, train_protection_claims) for UI rendering.
        Persistence does NOT use this path; see `to_layout_dict()` instead.
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
            "position_known": self.position_known,
            "flip_count": self.flip_count,
            "occupied_by": self.occupied_by,
            "allow_exit_from_either_leg": self.allow_exit_from_either_leg,
            "motor_unavailable": self.motor_unavailable,
            "disabled": self.disabled,
            "manual": self.manual,
            "flank_protection": [
                [neighbour_id, blocking_state]
                for (neighbour_id, blocking_state) in self.flank_protection
            ],
            "train_protection_claims": [
                [train_id, source_id, locked_state]
                for (train_id, source_id, locked_state) in self.train_protection_claims
            ],
            # Locks: dict keys are non-JSON tuples, so serialize as a list of records.
            # Per design_new.md "Switch entity" — key is `(path_id, source_switch_id)`;
            # the role is derived from `source_switch_id is None`.
            "locks": [
                {
                    "path_id": path_id,
                    "source_switch_id": source_switch_id,
                    "train_id": train_id,
                    "locked_state": locked_state,
                    # Derived role for diagnostic readers; per design_new.md, "Switch entity"
                    # `claim_role` is "main" for a primary claim (source_switch_id
                    # is None), else "flank".
                    "claim_role": "main" if source_switch_id is None else "flank",
                }
                for (
                    (path_id, source_switch_id),
                    (train_id, locked_state),
                ) in self.locks.items()
            ],
        }

    def to_layout_dict(self) -> Dict[str, Any]:
        """Layout-only serialization used by persistence.

        Per design_new.md Out of Scope, runtime state (`position`,
        `occupied_by`, `flank_protection`, `train_protection_claims`,
        `locks`) is excluded — the switch loads at the dataclass default
        position (MAIN) with empty locks and protection sets.
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
            "allow_exit_from_either_leg": self.allow_exit_from_either_leg,
            "motor_unavailable": self.motor_unavailable,
            "disabled": self.disabled,
            "manual": self.manual,
            "flip_count": self.flip_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Switch":
        """Reconstruct from a layout snapshot.

        Runtime fields (`position`, `occupied_by`, `flank_protection`,
        `train_protection_claims`, `locks`) are not loaded from disk;
        defaults apply and the engine repopulates them. Legacy snapshots
        that carry those keys are silently ignored.
        """
        if "main_length_cm" not in data or "diverge_length_cm" not in data:
            raise ValueError(
                f"Switch.from_dict: switch '{data.get('switch_id')}' is missing "
                "required 'main_length_cm'/'diverge_length_cm'. Lengths must be "
                "supplied explicitly (0 is allowed); there is no silent default."
            )
        switch = cls(
            switch_id=data["switch_id"],
            x=data["x"],
            y=data["y"],
            main_length_cm=float(data["main_length_cm"]),
            diverge_length_cm=float(data["diverge_length_cm"]),
            main_radius_cm=data.get("main_radius_cm"),
            diverge_radius_cm=data.get("diverge_radius_cm"),
            switch_type=data.get("switch_type", "standard"),
            name=data.get("name", ""),
            notes=data.get("notes", ""),
        )
        switch.allow_exit_from_either_leg = data.get("allow_exit_from_either_leg") is True
        switch.motor_unavailable = data.get("motor_unavailable") is True
        switch.disabled = bool(data.get("disabled", False))
        switch.manual = bool(data.get("manual", False))
        switch.flip_count = int(data.get("flip_count", 0))
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
