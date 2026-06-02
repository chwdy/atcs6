"""
Switch State Manager - single source of truth for switch lock and physical
occupancy events.

`SwitchManager` is the database-style lock manager for switch resources and
the single serialization point for atomic multi-switch lock acquisition,
release, and physical-entry handling. It mirrors `BlockManager`'s contract:

- Path-driven multi-switch lock acquire is atomic / all-or-nothing.
- Path-driven release is keyed by `path_id` and walks the registry once.
- Physical-entry handling owns the flank-lock-to-train-protection conversion
  and the fatal-conflict reporting path that earlier lived inline on
  `PathDispatcher`.
- Position-change requests route through `SwitchManager.request_position_change(...)`,
  which calls `Switch.apply_position_change(...)` for the data update and
  then notifies `BlockManager.on_switch_position_changed(...)`. Callers must
  not invoke `Switch.apply_position_change(...)` directly; the manager is
  the single entry point so the cross-resource notification is never
  skipped.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, TYPE_CHECKING

from runtime_payloads import (
    FatalPhysicalEntryConflict,
    FatalConflictKind,
    SwitchLockClaim,
)

if TYPE_CHECKING:
    from network import RailwayNetwork
    from switch import Switch


# ============================================================================
# Canonical value types
# ============================================================================


@dataclass
class SwitchLockRequest:
    """Canonical value type for one requested switch lock.

    Per design_new.md "SwitchManager" — `source_switch_id` is None for
    primary (main) claims and the originating switch_id for flank claims.
    The role is derived from this field, not stored separately.

    `route` is the unordered ``frozenset({entry_edge, exit_edge})`` the
    path traverses through this switch (e.g. ``frozenset({"N","W"})`` for
    an NW arc). When supplied, two requests at the same ``locked_state``
    may coexist if their routes are non-conflicting per
    ``switch.routes_conflict(...)`` — what lets two trains share a
    scissors crossover in DIVERGE state. ``None`` falls back to the
    conservative same-train cross-primary rule.
    """

    switch_id: str
    locked_state: int
    source_switch_id: Optional[str] = None
    route: Optional[frozenset] = None


@dataclass
class LockAcquireResult:
    """Result of an atomic `acquire_locks_for_segment(...)` call.

    `success == True` means every requested lock was recorded. `success ==
    False` means the call rejected before any persistent state change; locks
    that succeeded earlier in the call are rolled back inside the method.
    """

    success: bool
    failed_switch_id: Optional[str] = None
    reason: Optional[str] = None
    locked_switch_ids: List[str] = field(default_factory=list)


# ============================================================================
# Manager
# ============================================================================


class SwitchManager:
    """Centralized switch lock manager.

    Owns the dispatcher-facing API for path-level switch lock acquire/release,
    tail-vacate, clean physical entry, and switch-position-change requests.
    Mirrors `BlockManager`. The underlying `Switch` entity stays the canonical
    owner of `position` writes via its data-only helper
    `Switch.apply_position_change(...)`; the manager is the single caller that
    pairs that write with the cross-resource notification to `BlockManager`.
    """

    def __init__(
        self,
        switches: Dict[str, "Switch"],
        simulation_controller: Optional[object] = None,
        network: Optional["RailwayNetwork"] = None,
    ):
        self.switches = switches
        self.simulation_controller = simulation_controller
        self.network = network

    # ------------------------------------------------------------------
    # Registry accessor (prefer network over fallback dict)
    # ------------------------------------------------------------------

    @property
    def _switch_registry(self) -> Dict[str, "Switch"]:
        if self.network is not None:
            return self.network.switches
        return self.switches

    # ------------------------------------------------------------------
    # Path-level multi-switch acquire (all-or-nothing)
    # ------------------------------------------------------------------

    def acquire_locks_for_segment(
        self,
        requests: List[SwitchLockRequest],
        path_id: str,
        train_id: str,
        manual_override: bool = False,
    ) -> LockAcquireResult:
        """Atomically acquire every requested switch lock and align the
        switch's physical position to the requested state.

        Acquiring a lock with `locked_state` only RECORDS the path's
        requirement; it does not by itself move the switch. If the switch is
        currently in a different position and not physically occupied, the
        manager throws it via `apply_position_change(...)` here so the train
        can actually traverse the locked segment when it arrives. If a switch
        is occupied by another train, `Switch.acquire_lock` will already
        have rejected the request before we reach the throw.

        `manual_override` (default False) is propagated from the calling
        `PathDispatcher.create_path(...)` / `extend_path(...)` per the
        `disabled` and `manual` availability flags canonical home in
        design_new.md (under TrackResource). It is forwarded to every
        `Switch.acquire_lock(claim)` call via `claim.manual_override`, and
        to every `Switch.apply_position_change(...)` call performed by this
        method, so manual switches participate when (and only when) the
        operator explicitly overrode.

        On any failure, release the locks recorded in this call and return a
        failure result.
        """
        # Fix #12: expand primary requests to include flank-protection requests.
        # Per design_new.md line 938 — for each main switch claim, enumerate
        # `switch.flank_protection` and append a flank request with
        # `source_switch_id = main_switch.switch_id`. The existing
        # `acquire_locks_for_segment` already handles flank claims
        # (source_switch_id != None); only the producer was missing.
        #
        # A switch the path traverses as a primary (a route point) is set and
        # locked by its own main claim; it must NOT also be flank-locked by a
        # sibling switch on the same path. When two switches sit in series on a
        # route (e.g. the demo throat SW_XOVER_N -> SW_LEAD onto the depot
        # lead), each is in the other's flank_protection list, so without this
        # guard one switch's flank claim pins the other to the opposite state
        # and `Switch.acquire_lock` then rejects that switch's own primary
        # claim — the whole path acquisition fails and no route through the
        # throat can ever form. A route point provides its own flank protection
        # by being correctly thrown, so skip flank claims that target another
        # primary on this same request set (design_new.md "Flank Protection
        # Computation").
        primary_switch_ids = {
            req.switch_id for req in requests if req.source_switch_id is None
        }

        # Same-path conflict exemption (the switch-"triangle" case). Pre-compute
        # the safe_state that each primary's flank protection would demand of
        # each non-route-point neighbour. When two primaries on THIS path demand
        # opposite states of the same neighbour, no single position aims that
        # neighbour away from both route points: it sits at the apex of a switch
        # triangle with one route point on each of its two diverging legs (e.g.
        # the depot-lead crossover apex, whose legs reach SW_LEAD on the
        # mainline and SW_8_10 on the N track). It therefore cannot flank-protect
        # this path at all, and locking it to either state would reject one
        # route point's own primary. Leave it unlocked rather than fail the
        # segment — the path's own block and switch reservations still keep a
        # second train from intruding through it. This generalises the
        # route-point exemption below (design_new.md "switch-first acquisition
        # step"): a path is never blocked by its own flank protection.
        flank_demands: Dict[str, Set[int]] = {}
        for req in requests:
            if req.source_switch_id is not None:
                continue
            main_switch = self._switch_registry.get(req.switch_id)
            if main_switch is None:
                continue
            for neighbour_switch_id, blocking_state in main_switch.flank_protection:
                if neighbour_switch_id in primary_switch_ids:
                    continue
                flank_demands.setdefault(neighbour_switch_id, set()).add(
                    1 - blocking_state
                )
        conflicted_neighbours = {
            nid for nid, states in flank_demands.items() if len(states) > 1
        }

        expanded: List[SwitchLockRequest] = []
        for req in requests:
            expanded.append(req)
            if req.source_switch_id is not None:
                # Already a flank request; don't double-expand.
                continue
            main_switch = self._switch_registry.get(req.switch_id)
            if main_switch is None:
                continue
            for neighbour_switch_id, blocking_state in main_switch.flank_protection:
                if neighbour_switch_id in primary_switch_ids:
                    # Route-point exemption: this neighbour is a route point on
                    # the same path; its own main claim governs its state.
                    # Flank-locking it here would make the path self-conflict.
                    continue
                if neighbour_switch_id in conflicted_neighbours:
                    # Same-path conflict exemption (triangle apex): two route
                    # points on this path demand opposite states of this
                    # neighbour, so it cannot protect the path. Leave it
                    # unlocked rather than fail the segment (see above).
                    continue
                # design_new.md:687/990: lock the neighbour to the SAFE state,
                # which is the binary complement of the blocking_state (the
                # state that would aim the neighbour's branch toward this
                # switch). v1 switches are binary, so safe_state = 1 - blocking.
                safe_state = 1 - blocking_state
                expanded.append(
                    SwitchLockRequest(
                        switch_id=neighbour_switch_id,
                        locked_state=safe_state,
                        source_switch_id=req.switch_id,
                    )
                )

        recorded: List[SwitchLockRequest] = []
        for req in expanded:
            switch = self._switch_registry.get(req.switch_id)
            if switch is None:
                self._rollback(recorded, path_id)
                return LockAcquireResult(
                    success=False,
                    failed_switch_id=req.switch_id,
                    reason="unknown_switch",
                )
            # Per design_new.md cross-train primary co-locking rule: primary
            # claims (source_switch_id is None) must always carry a route in
            # v1 so the cross-train route-conflict check can run with
            # geometry. A primary claim missing the route is a path-
            # construction bug — reject the whole acquire request rather
            # than silently downgrading to the conservative single-owner
            # fallback (which used to leave Switch.lock_routes empty and
            # break later route-aware acquires on the same switch).
            if req.source_switch_id is None and req.route is None:
                self._rollback(recorded, path_id)
                return LockAcquireResult(
                    success=False,
                    failed_switch_id=req.switch_id,
                    reason="missing_route_on_primary_claim",
                )
            claim = SwitchLockClaim(
                path_id=path_id,
                train_id=train_id,
                locked_state=req.locked_state,
                source_switch_id=req.source_switch_id,
                manual_override=manual_override,
                route=req.route,
            )
            # Fix #23 (design_new.md line 617): SwitchManager wraps the entity
            # bool into a structured LockAcquireResult so manager-level callers
            # always unpack .success. The entity returns bool for unit-test compat.
            ok = switch.acquire_lock(claim)
            if not ok:
                self._rollback(recorded, path_id)
                return LockAcquireResult(
                    success=False,
                    failed_switch_id=req.switch_id,
                    reason="acquire_lock_rejected",
                )
            # Align the physical position with the locked state. The lock
            # accept already ruled out the unsafe "switch occupied by another
            # train + position change required" case (Switch.acquire_lock
            # rejects it). Misalignment with no occupant means the path
            # acquired the right to throw — do it now so clearance walks and
            # train physics see a usable route.
            if int(switch.position) != int(req.locked_state):
                ok_throw, msg = switch.apply_position_change(
                    int(req.locked_state), manual_override=manual_override
                )
                if not ok_throw:
                    # Roll back this lock and any earlier ones acquired in
                    # this segment so the request is fully atomic.
                    switch.release_lock(
                        path_id=path_id,
                        source_switch_id=req.source_switch_id,
                    )
                    self._rollback(recorded, path_id)
                    return LockAcquireResult(
                        success=False,
                        failed_switch_id=req.switch_id,
                        reason=f"apply_position_change_rejected: {msg}",
                    )
                block_manager = self._resolve_block_manager()
                if block_manager is not None:
                    block_manager.on_switch_position_changed(req.switch_id)
            # Per design_new.md, `Switch.locks` is the canonical lock store.
            # `TrackSegment` no longer exposes any lock surface; UI and API
            # readers resolve the owning `Switch` (via
            # `network.switches[switch_id]`) and read `Switch.locks` directly,
            # so there is no Track-side mirror to maintain here.
            recorded.append(req)
        return LockAcquireResult(
            success=True,
            locked_switch_ids=[r.switch_id for r in recorded],
        )

    def _rollback(
        self, recorded: List[SwitchLockRequest], path_id: str
    ) -> None:
        for req in recorded:
            switch = self._switch_registry.get(req.switch_id)
            if switch is None:
                continue
            switch.release_lock(path_id, req.source_switch_id)

    # ------------------------------------------------------------------
    # Path-level release
    # ------------------------------------------------------------------

    def release_path_locks(self, path_id: str) -> List[str]:
        """Release every lock owned by `path_id` across every switch."""
        released: List[str] = []
        for switch in self._switch_registry.values():
            keys = [k for k in list(switch.locks.keys()) if k[0] == path_id]
            if not keys:
                continue
            for (_pid, source_id) in keys:
                switch.release_lock(path_id, source_id)
            released.append(switch.switch_id)
        return released

    def release_path_locks_for_switches(
        self, switch_ids: List[str], path_id: str
    ) -> List[str]:
        """Release `path_id`'s locks scoped to the given switch_ids only."""
        released: List[str] = []
        for sid in switch_ids:
            switch = self._switch_registry.get(sid)
            if switch is None:
                continue
            keys = [k for k in list(switch.locks.keys()) if k[0] == path_id]
            if not keys:
                continue
            for (_pid, source_id) in keys:
                switch.release_lock(path_id, source_id)
            released.append(sid)
        return released

    def release_segment_locks(
        self, path_id: str, primary_switch_ids: List[str]
    ) -> List[str]:
        """Release the locks one segment acquired for `path_id`, scoped to the
        segment's primary (main-usage) switches AND the flank locks those
        primaries placed on neighbours.

        This is the precise inverse of the acquire-time expansion in
        `acquire_locks_for_segment(...)`: for each primary switch P, release
        P's own main claim `(path_id, None)` and every flank claim
        `(path_id, P)` on P's `flank_protection` neighbours. Unlike
        `release_path_locks_for_switches(...)`, which drops every `path_id`
        key on each listed switch, this releases by exact `(path_id, source)`
        key — so it can never drop a lock keyed by a different source (e.g. a
        committed segment's flank lock that happens to sit on the same
        neighbour). `release_lock` no-ops on a missing key, so a
        route-point-exempted neighbour (no flank lock placed) is harmless.

        Used by segment rollback, where only the still-future holds this
        failed segment placed must be released and the live path's committed
        authority must be left untouched (the caller excludes committed
        primaries from `primary_switch_ids`).

        Returns the switch_ids that had at least one lock removed.
        """
        released: List[str] = []
        for pid in primary_switch_ids:
            primary = self._switch_registry.get(pid)
            if primary is None:
                continue
            if primary.release_lock(path_id, None):
                released.append(pid)
            for neighbour_switch_id, _blocking_state in primary.flank_protection:
                neighbour = self._switch_registry.get(neighbour_switch_id)
                if neighbour is None:
                    continue
                if neighbour.release_lock(path_id, pid):
                    released.append(neighbour_switch_id)
        return released

    # ------------------------------------------------------------------
    # Tail-vacate
    # ------------------------------------------------------------------

    def on_switch_tail_vacated(
        self,
        switch_id: str,
        train_id: str,
        route: Optional[frozenset] = None,
    ) -> None:
        """Switch tail-vacate entry — called by PathDispatcher per event.

        ``route`` is the unordered frozenset of edges the train used through
        this switch (derived from the train's vacating `TileTraversal`).
        When provided, only that route's ownership is cleared from
        ``route_owners``; other routes (potentially held by other trains)
        remain. When ``None``, the legacy whole-switch clear applies.
        """
        switch = self._switch_registry.get(switch_id)
        if switch is None:
            return
        switch.on_train_tail_vacated(train_id, network=self.network, route=route)

    # ------------------------------------------------------------------
    # Switch position change request (admin / API / UI)
    # ------------------------------------------------------------------

    def request_position_change(
        self, switch_id: str, new_position: int, manual_override: bool = False
    ) -> tuple[bool, str]:
        """Request a position change on `switch_id`.

        Validates and applies the change via `Switch.apply_position_change(...)`,
        then notifies `BlockManager.on_switch_position_changed(switch_id)` so
        the block reachability graph stays in sync. Returns the
        `(success, message)` tuple from the data-layer helper.

        `manual_override` (default False) is the operator-override flag per
        the `disabled` and `manual` availability flags canonical home in
        design_new.md (under TrackResource). Without the override, a
        switch with `Switch.manual == True` rejects the request; with the
        override, the manual filter is bypassed (the disabled filter always
        applies regardless).
        """
        switch = self._switch_registry.get(switch_id)
        if switch is None:
            return (False, f"Unknown switch {switch_id}")

        success, message = switch.apply_position_change(
            new_position, manual_override=manual_override
        )
        if not success:
            return (False, message)

        block_manager = self._resolve_block_manager()
        if block_manager is not None:
            block_manager.on_switch_position_changed(switch_id)
        return (True, message)

    def _resolve_block_manager(self):
        """Locate the BlockManager — preferred via SimulationController, with
        a network fallback for editor/test setups that wired the manager
        only on the network object."""
        if self.simulation_controller is not None:
            bm = getattr(self.simulation_controller, "block_manager", None)
            if bm is not None:
                return bm
        if self.network is not None:
            return getattr(self.network, "block_manager", None)
        return None

    # ------------------------------------------------------------------
    # Clean physical entry + flank conversion + fatal-conflict reporting
    # ------------------------------------------------------------------

    def on_physical_entry(
        self,
        switch_id: str,
        train_id: str,
        path_id: Optional[str],
        route: Optional[frozenset] = None,
    ) -> bool:
        """Record a clean physical entry of `train_id` into `switch_id`.

        ``route`` is the unordered ``frozenset({entry_edge, exit_edge})`` the
        train uses through this switch (derived by the dispatcher from the
        train's `TileTraversal`). When supplied, occupancy is recorded
        under that route key in ``Switch.route_owners`` and only routes
        that physically conflict (per ``switch.routes_conflict``) cause a
        fatal `SWITCH_OCCUPIED_BY_OTHER_TRAIN`. When ``route`` is ``None``,
        the legacy whole-switch single-owner rule applies — any other
        train on the switch is treated as a conflict.

        Returns True on clean entry; False on disabled-switch entry or
        route-conflict by a different train (after reporting the typed
        fatal conflict). When `path_id` is provided, also consumes the
        path-owned primary lock for this switch and converts each required
        flank-protection claim on neighbours into a train-owned
        `train_protection_claim`.
        """
        switch = self._switch_registry.get(switch_id)
        if switch is None:
            return False

        if getattr(switch, "disabled", False):
            self._report_fatal_conflict(
                switch_id=switch_id,
                train_id=train_id,
                conflict_kind=FatalConflictKind.DISABLED_RESOURCE_ENTRY,
                prior_owner_train_id=None,
            )
            return False

        if route is None:
            # Per design_new.md "ResourceCoordinator.on_physical_entry(...)"
            # canonical home and FatalConflictKind.MALFORMED_ROUTE_KEY: the
            # dispatcher must always supply a non-None route for switch
            # physical-entry events. A route=None argument is a v1 contract
            # violation that we report as a fatal conflict rather than
            # silently falling through to a single-owner write that would
            # leave Switch.route_owners empty and let later route-aware
            # acquires miss the occupant.
            self._report_fatal_conflict(
                switch_id=switch_id,
                train_id=train_id,
                conflict_kind=FatalConflictKind.MALFORMED_ROUTE_KEY,
                prior_owner_train_id=None,
            )
            return False

        # Route-keyed entry: only fatal if a *conflicting* route is
        # already held by a different train. Two trains using
        # non-conflicting routes (NW + SE on a scissors crossover)
        # coexist cleanly.
        from switch import routes_conflict
        for existing_route, existing_train in switch.route_owners.items():
            if existing_train == train_id:
                continue
            if not routes_conflict(existing_route, route):
                continue
            self._report_fatal_conflict(
                switch_id=switch_id,
                train_id=train_id,
                conflict_kind=FatalConflictKind.SWITCH_OCCUPIED_BY_OTHER_TRAIN,
                prior_owner_train_id=existing_train,
            )
            return False
        switch.add_route_occupant(route, train_id)

        if path_id:
            self._convert_locks_on_entry(switch, train_id, path_id)
        return True

    def _convert_locks_on_entry(
        self, switch_entity: "Switch", train_id: str, path_id: str
    ) -> None:
        """Consume the path-owned primary lock for this switch and convert
        each required flank-protection claim on neighbours into a train-owned
        `train_protection_claim`. Per design_new.md, the lock key is
        `(path_id, source_switch_id)` where `source_switch_id is None` for
        primary claims and the originating switch_id for flank claims.
        After the train physically occupies this switch, the neighbour's
        flank lock is replaced by a `(train_id, this_switch_id, locked_state)`
        entry in `train_protection_claims`, later cleared on tail-vacate via
        `Switch.release_train_protection_from(...)`.
        """
        primary_key = (path_id, None)
        if primary_key in switch_entity.locks:
            switch_entity.release_lock(path_id, None)

        for neighbour_switch_id, _blocking_state in list(
            getattr(switch_entity, "flank_protection", []) or []
        ):
            neighbour = self._switch_registry.get(neighbour_switch_id)
            if neighbour is None:
                continue
            flank_key = (path_id, switch_entity.switch_id)
            if flank_key not in neighbour.locks:
                continue
            _claim_train_id, locked_state = neighbour.locks[flank_key]
            neighbour.release_lock(path_id, switch_entity.switch_id)
            claim = (train_id, switch_entity.switch_id, int(locked_state))
            if claim not in neighbour.train_protection_claims:
                neighbour.train_protection_claims.append(claim)

    def _report_fatal_conflict(
        self,
        *,
        switch_id: str,
        train_id: str,
        conflict_kind: FatalConflictKind,
        prior_owner_train_id: Optional[str],
    ) -> None:
        """Forward a fatal switch-entry conflict to the runtime.

        Mirrors `BlockManager._report_fatal_conflict`. Receiver issues must
        not mask the underlying conflict, so this helper does not swallow
        exceptions raised by the wired controller.
        """
        if self.simulation_controller is None:
            return
        active_path = None
        dispatcher = getattr(self.simulation_controller, "path_dispatcher", None)
        if dispatcher is not None:
            getter = getattr(dispatcher, "get_active_path", None)
            if callable(getter):
                active_path = getter(train_id)
        payload = FatalPhysicalEntryConflict(
            resource_type="switch",
            resource_id=switch_id,
            incoming_train_id=train_id,
            prior_owner_train_id=prior_owner_train_id,
            conflict_kind=conflict_kind,
            incoming_path_id=active_path.path_id if active_path else None,
        )
        self.simulation_controller.enter_failed_runtime(payload)
