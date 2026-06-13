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
from typing import Dict, FrozenSet, List, Any, Optional, Tuple, TYPE_CHECKING

from enums import SwitchState
from runtime_payloads import SwitchLockClaim
from track_resource import TrackResource

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


def routes_conflict(r1: FrozenSet[str], r2: FrozenSet[str]) -> bool:
    """Return True iff routes ``r1`` and ``r2`` physically conflict on a
    single switch tile.

    Two routes conflict when they share any point: either an edge midpoint
    (their edge sets intersect) or the tile center (both are NS/EW straights).
    Same-route comparison (r1 == r2) returns True — a route conflicts with
    itself, so the route_owners map allows at most one train per route key.
    """
    if r1 & r2:
        return True
    if r1 in _STRAIGHTS_THROUGH_CENTER and r2 in _STRAIGHTS_THROUGH_CENTER:
        return True
    return False


def make_route_key(entry_edge: str, exit_edge: str) -> FrozenSet[str]:
    """Build the canonical route key from a directed (entry, exit) pair.

    Routes are unordered for occupancy purposes — a train traversing N→W
    occupies the same physical NW arc as a train traversing W→N.
    """
    return frozenset({entry_edge, exit_edge})


@dataclass
class Switch(TrackResource):
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
      see design_new.md:651) is derived from `source_switch_id is None`,
      not stored as a separate key field.
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

    route_owners: Dict[FrozenSet[str], str] = field(default_factory=dict)
    """Route-keyed physical occupancy: maps each route currently in use
    (frozenset of two edges, e.g. ``frozenset({"N","W"})``) to the
    ``train_id`` traversing it. A new route may be added iff it does not
    conflict (per :func:`routes_conflict`) with any existing entry."""

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
    # ROUTE-KEYED OCCUPANCY (v1)
    # ========================================================================

    def has_conflicting_route(
        self,
        route: FrozenSet[str],
        train_id: Optional[str] = None,
    ) -> bool:
        """Return True iff any current ``route_owners`` entry physically
        conflicts with ``route``. When ``train_id`` is supplied, ownership
        by that same train counts as compatible (the train is already on
        the conflicting route, e.g. an idempotent re-entry report); a
        different ``train_id`` always counts as a conflict.
        """
        for existing_route, existing_train in self.route_owners.items():
            if train_id is not None and existing_train == train_id:
                continue
            if routes_conflict(existing_route, route):
                return True
        return False

    def add_route_occupant(
        self, route: FrozenSet[str], train_id: str
    ) -> bool:
        """Record that ``train_id`` physically occupies ``route`` on this
        switch tile. Returns True on success; False if another train holds
        a conflicting route (caller treats this as the fatal
        `SWITCH_OCCUPIED_BY_OTHER_TRAIN` conflict per design_new.md:471).
        """
        if self.has_conflicting_route(route, train_id=train_id):
            return False
        self.route_owners[route] = train_id
        return True

    def remove_route_occupant(
        self, route: FrozenSet[str], train_id: str
    ) -> bool:
        """Clear a single route entry owned by ``train_id``. Returns True
        if the entry existed and was owned by ``train_id``; False
        otherwise.
        """
        existing_train = self.route_owners.get(route)
        if existing_train is None or existing_train != train_id:
            return False
        del self.route_owners[route]
        return True

    @property
    def occupied_by(self) -> Optional[str]:
        """Any train currently occupying this switch via ``route_owners``,
        or ``None`` when unoccupied. Derived, not stored: for the
        single-owner physical-occupancy contract it is exactly the lone
        occupant; when two trains hold non-conflicting routes (e.g. NW and
        SE on a scissors crossover) it returns one of them, so it stays a
        diagnostic read — new consumers should query ``route_owners``.
        """
        return next(iter(self.route_owners.values()), None)

    # ========================================================================
    # MULTI-OWNER LOCK METHODS (v1)
    # ========================================================================

    def is_locked(self) -> bool:
        """Check if switch holds any future-authority lock claim or train-owned
        physical protection."""
        return bool(self.locks) or bool(self.train_protection_claims)

    def can_lock(self, state: int, manual_override: bool = False) -> bool:
        """Read-only predicate: can a future lock be acquired for `state`?

        Returns True iff:
        - the switch is not disabled,
        - `self.manual` is False OR `manual_override` is True (per the
          `disabled` and `manual` availability flags canonical home in
          design_new.md under TrackResource),
        - no existing future lock requires a different state, and
        - no train-owned protection requires a different state.
        """
        if self.disabled:
            return False
        if self.manual and not manual_override:
            return False
        for _existing_train_id, existing_state in self.locks.values():
            if existing_state != state:
                return False
        for _claim_train_id, _claim_source_id, claim_state in self.train_protection_claims:
            if claim_state != state:
                return False
        return True

    def acquire_lock(self, claim: "SwitchLockClaim") -> bool:
        """Acquire a future-authority lock claim on the switch.

        `claim` is the structured payload carrying `path_id`, `train_id`,
        `locked_state`, and optional `source_switch_id` (see the switch lock
        claim payload in the Switch And SwitchManager section). The role
        (primary vs flank) is derived from `source_switch_id is None`.
        Compatible same-state locks may co-exist. The acquisition succeeds
        when every existing lock has the same `locked_state` as the requested
        one, no train-owned protection requires a different state, and
        physical occupancy does not block a switch throw.

        The entity-level method returns `bool` (True=success, False=conflict).
        `SwitchManager` wraps this into the structured `LockAcquireResult` so
        manager-level callers receive the structured result and unpack
        `.success`.

        Returns:
            True if recorded (or already present idempotently); False on conflict.
        """
        # Disabled switches cannot be locked. No override bypasses this.
        if self.disabled:
            return False

        # Manual switches are filtered for every automatic caller; only an
        # operator-initiated call carrying claim.manual_override == True may
        # acquire a lock on them. See the `disabled` and `manual` availability
        # flags canonical home in design_new.md (under TrackResource).
        if self.manual and not getattr(claim, "manual_override", False):
            return False

        path_id = claim.path_id
        train_id = claim.train_id
        locked_state = claim.locked_state
        source_switch_id = claim.source_switch_id

        key = (path_id, source_switch_id)
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

        # Cross-train primary co-locking is gated by route geometry. Two
        # paths from different trains may both hold primary locks on the
        # same switch ONLY when their routes (entry/exit edge pairs)
        # don't physically conflict — e.g. NW+SE on a scissors crossover
        # in DIVERGE state. When either claim is missing route info
        # (legacy callers / pre-route tests), fall back to the
        # conservative "no cross-train primary" rule because we can't
        # prove non-conflict without geometry.
        claim_route = getattr(claim, "route", None)
        if source_switch_id is None:
            for (other_path_id, other_source), (other_train_id, _state) in (
                self.locks.items()
            ):
                if other_source is not None:
                    continue
                if other_train_id == train_id:
                    continue
                other_route = self.lock_routes.get((other_path_id, other_source))
                if claim_route is None or other_route is None:
                    # Geometry unknown on at least one side → conservative
                    # cross-train primary reject.
                    return False
                if routes_conflict(claim_route, other_route):
                    return False

        # Reject if any train-owned protection requires a different state.
        for _claim_train_id, _claim_source_id, claim_state in self.train_protection_claims:
            if claim_state != locked_state:
                return False

        # Physical occupancy by a conflicting route blocks new locks. With
        # route-keyed occupancy a different train can be on the switch
        # provided its route doesn't conflict with the claim's route.
        if claim_route is not None:
            if self.has_conflicting_route(claim_route, train_id=train_id):
                return False
        else:
            # No route on the claim → fall back to the old whole-switch
            # rule: any other train's physical occupancy blocks.
            if self.occupied_by is not None and self.occupied_by != train_id:
                return False

        # If the requested state differs from the current physical state, a
        # switch throw is required; that is forbidden while the switch is
        # physically occupied by any train (including the same train).
        if int(self.position) != locked_state and bool(self.route_owners):
            return False

        self.locks[key] = (train_id, locked_state)
        if claim_route is not None:
            self.lock_routes[key] = claim_route
        return True

    def release_lock(
        self,
        path_id: str,
        source_switch_id: Optional[str] = None,
    ) -> bool:
        """Release a single future-authority lock entry by composite key.

        Per design_new.md "Switch entity" — keyed by `(path_id, source_switch_id)`.

        Returns:
            True if a matching entry was removed; False if none found.
        """
        key = (path_id, source_switch_id)
        if key in self.locks:
            del self.locks[key]
            self.lock_routes.pop(key, None)
            return True
        return False

    def release_train_protection_from(
        self, source_switch_id: str, train_id: str
    ) -> bool:
        """Remove all train_protection_claims with the given (source, train) combo.

        Per design_new.md "Switch entity" — returns ``True`` if at least one
        matching claim was removed; ``False`` if none. Idempotent. Does not
        inspect or mutate ``occupied_by``; the caller is responsible for the
        ``train_id == self.occupied_by`` ordering check before invoking this
        helper.
        """
        before = len(self.train_protection_claims)
        self.train_protection_claims = [
            claim
            for claim in self.train_protection_claims
            if not (claim[0] == train_id and claim[1] == source_switch_id)
        ]
        return len(self.train_protection_claims) < before

    def on_train_tail_vacated(
        self,
        train_id: str,
        network: Optional["RailwayNetwork"] = None,
        route: Optional[FrozenSet[str]] = None,
    ) -> None:
        """Handle a tail-vacate event for `train_id` on this switch.

        Per design_new.md "Tail-vacate release" canonical home: when the
        supplied ``route`` matches a current ``route_owners`` entry owned by
        ``train_id``, that entry is removed; when ``route`` is supplied but
        does not match any entry yet ``train_id`` still owns one or more
        entries on this switch (controller emitted a stale or geometrically
        derived key that drifted from the recorded entry), emit a
        ``NotificationCenter`` diagnostic and recover by removing the single
        entry owned by ``train_id``. When no entry is owned by ``train_id``
        at all, emit a diagnostic and leave state unchanged.

        ``route`` may legitimately be ``None`` only for tests / editor
        callers that don't have route geometry; production tail-vacate
        flows always supply it.

        `network` is required to walk neighbours; production callers
        (`SwitchManager.on_switch_tail_vacated`) inject it. In editor / unit
        contexts where no neighbour walk is needed it may be omitted.
        """
        cleared = False

        if route is not None and route in self.route_owners:
            if self.route_owners[route] == train_id:
                self.remove_route_occupant(route, train_id)
                cleared = True

        if not cleared:
            # Either route is None, or the supplied key didn't match. Look
            # for any entry owned by train_id and clear that. This is the
            # defensive recovery path for controller-side route-key drift.
            owned_routes = [
                r for r, owner in self.route_owners.items()
                if owner == train_id
            ]
            if owned_routes:
                if route is not None:
                    print(
                        f"switch {self.switch_id} tail-vacate route key mismatch "
                        f"for train {train_id}: supplied {sorted(route) if route else route}, "
                        f"owned routes {[sorted(r) for r in owned_routes]}; "
                        f"recovering by clearing owned entry"
                    )
                # Clear exactly one owned entry. When the train owns
                # multiple entries on this tile (rare but possible during
                # certain scissors-crossover flows), this single tail-vacate
                # only releases one; the next tail-vacate release will pick
                # up the remaining entry on its own pass.
                self.remove_route_occupant(owned_routes[0], train_id)
                cleared = True

        if not cleared:
            # No ownership at all by this train on this switch — diagnose.
            other = self.occupied_by if self.occupied_by != train_id else None
            print(
                f"switch {self.switch_id} release for train {train_id} skipped: "
                f"no route_owners entry held by this train"
                + (f" (occupied_by={other})" if other is not None else "")
            )
            return

        # If this train still owns another route on the switch, don't
        # release its flank protection yet — it hasn't fully cleared.
        any_routes_remaining_for_train = any(
            owner == train_id for owner in self.route_owners.values()
        )
        if any_routes_remaining_for_train:
            return

        # Release the train-owned protection that head-entry created on each
        # flank neighbour. This walks THIS switch's own `flank_protection`
        # list — the same list the create side (`_convert_locks_on_entry`)
        # iterates to build the claims. The relationship is directional: a
        # neighbour can be in `self.flank_protection` while `self` is absent
        # from the neighbour's list (one points at the other in exactly one
        # state, the reverse in both/neither — see `_neighbour_blocking_state`).
        # Iterating the neighbour's list instead would strand the claim on any
        # asymmetric pair. `release_train_protection_from` is idempotent, so a
        # neighbour that got the route-point exemption (no claim created) is a
        # harmless no-op. Without a network reference we cannot resolve
        # neighbours.
        if network is None:
            return
        switches = getattr(network, "switches", None)
        if not switches:
            return
        for neighbour_switch_id, _blocking_state in self.flank_protection:
            neighbour = switches.get(neighbour_switch_id)
            if neighbour is None:
                continue
            neighbour.release_train_protection_from(
                source_switch_id=self.switch_id, train_id=train_id
            )

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

        # Mutate authoritative position.
        self.position = SwitchState(new_position)

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
            "occupied_by": self.occupied_by,
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
                    # Derived role for diagnostic readers; per design_new.md:651
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
            "disabled": self.disabled,
            "manual": self.manual,
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
        switch.disabled = bool(data.get("disabled", False))
        switch.manual = bool(data.get("manual", False))
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
