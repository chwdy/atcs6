"""Meet coordination — the per-tick meet assignment (design_new.md "Meet
coordination").

A meet is a global decision — which train takes a shared single-track section
and where the other waits — that no single train's Plan can make from its own
view. `RouteExecutor` builds one meet assignment per tick in the preamble (after
the contention snapshot), where the global view exists. The assignment is a
deterministic function of `RouteRun.effective_priority` and the stable
`order_runnable_trains` tie-break, so it is stable without persistence: the
admitted contender stays admitted until it clears the zone, and each held
contender keeps the same ceiling until then.

The assignment only caps how far authority is acquired (it feeds
`reservation_cap_block_id` per the "Reservation-cap rule", keeping the route
target unchanged); it never reserves a resource. All acquisition still runs
through the interlocked `BlockManager` / `SwitchManager` path, so a stale or
wrong ceiling costs at most one tick of a train aiming one block short. Meet coordination is the proactive
layer above the reactive guards (best-effort truncation, contention-driven
release, the reversal-trap exclusion), which stay the safety floor — including
for trains the assignment never governs, such as manual or unrouted trains.

Scope of this implementation:
- Contested-zone detection is block-level: a shared block the two candidate
  routes traverse in opposing block-relative directions (the single-track /
  head-on meet, the dominant case from the product use cases). A pure
  switch-route contested zone with no opposing shared block — a crossover/throat
  whose only conflict is at the switch — is left to the reactive floor (the
  item-11 junction-fouling truncation), which already holds a train one block
  short of a fouled switch.
- The insufficient-capacity branch (admit highest priority, cap each lower
  contender at the furthest holding point before the zone, diagnose when none
  exists) is implemented in full. The sufficient-capacity branch (a passing loop
  exists) writes no ceiling and lets the interlocking route the trains onto the
  distinct tracks the loop provides — within the doc's stated one-tick margin,
  since a passing loop means their reservations resolve onto distinct blocks.
"""

from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Set, Tuple


# One pathfinder step's identity: (resource_type, resource_id, entry_edge,
# exit_edge). The pinned candidate is the ordered tuple of these.
CandidateResourceKey = Tuple[str, str, str, str]


@dataclass(frozen=True)
class MeetCeiling:
    """A train's per-tick meet assignment: a candidate pin and an optional cap.

    `block_id` is the ceiling block, or `""` when the entry carries no ceiling:
    Plan passes a real ceiling as the `reservation_cap_block_id` so authority is
    acquired toward the real route target but stored no further than this block.
    `direction` is the block-relative `"forward"` / `"reverse"` the train
    traverses the ceiling block in, so the capped terminal frame is unique.
    `candidate_resource_keys` is the selected candidate's ordered
    `(resource_type, resource_id, entry_edge, exit_edge)` list; Plan passes it as
    `required_candidate_resource_keys` so the dispatcher acquires the same route
    meet coordination evaluated instead of any equally-ranked alternative.
    """

    block_id: str
    direction: str
    candidate_resource_keys: Tuple[CandidateResourceKey, ...] = ()


# Ordered route as [(block_id, block_direction)], one entry per distinct block.
RouteBlocks = List[Tuple[str, str]]


class MeetCoordinator:
    """Builds the per-tick `train_id -> MeetCeiling` meet assignment.

    Constructed fresh each tick with the live `RouteExecutor`; reads the
    executor's just-built `contention_snapshot`, the shared pathfinder, and
    `Block.is_holding_point` to compute ceilings.
    """

    def __init__(self, route_executor) -> None:
        self._re = route_executor

    # -- public -----------------------------------------------------------

    def build(self, ordered_train_ids: List[str]) -> Dict[str, MeetCeiling]:
        """Return the meet assignment for this tick."""
        # Trains this pass found no holding point at (the no-holding-point
        # diagnostic is a live per-tick condition, so `_emit_no_holding_point`
        # records the train here and the reconcile below resolves the ones that
        # are no longer held).
        self._no_holding_point_trains: Set[str] = set()
        assignment: Dict[str, MeetCeiling] = {}

        snapshot = getattr(self._re, "contention_snapshot", None)
        edges = getattr(snapshot, "edges", None) if snapshot is not None else None
        if edges:
            position = {tid: i for i, tid in enumerate(ordered_train_ids)}
            seen_pairs: set = set()
            route_cache: Dict[str, Optional[RouteBlocks]] = {}
            keys_cache: Dict[str, Tuple[CandidateResourceKey, ...]] = {}

            # Each contention edge already names the holder (resolved from live
            # state by get_waiting_conflict), so meet detection reads the pair
            # straight off the edge rather than re-deriving a block's holder.
            for edge in edges:
                waiter_id = edge.waiting_train_id
                holder_id = edge.holding_train_id
                if not holder_id or holder_id == waiter_id:
                    continue
                pair_key = frozenset((waiter_id, holder_id))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                self._assign_for_pair(
                    waiter_id, holder_id, position, assignment, route_cache, keys_cache
                )

        # Resolve the no-holding-point condition for any train that had it last
        # pass but does not this pass (design_new.md "Error events", Resolution
        # and recurrence). Runs even with no edges, so contention clearing
        # entirely still clears the diagnostics.
        self._reconcile_no_holding_point()
        return assignment

    # -- per-pair ---------------------------------------------------------

    def _assign_for_pair(
        self,
        waiter_id: str,
        holder_id: str,
        position: Dict[str, int],
        assignment: Dict[str, MeetCeiling],
        route_cache: Dict[str, Optional[RouteBlocks]],
        keys_cache: Dict[str, Tuple[CandidateResourceKey, ...]],
    ) -> None:
        w_route = self._route_blocks(waiter_id, route_cache)
        h_route = self._route_blocks(holder_id, route_cache)
        if not w_route or not h_route:
            return

        zone, w_near = self._contested_zone(w_route, h_route)
        if not zone:
            return  # routes do not conflict on any shared block

        # Existing-commitment rule (design_new.md, "Meet coordination"): a
        # contender that already occupies or holds authority on any resource in
        # the contested zone stays admitted and the opposing contender is held,
        # regardless of priority — a priority edit cannot reverse authority or
        # physical entry. Only when neither contender is committed does priority
        # decide who is held.
        w_committed = self._committed_in_zone(waiter_id, zone)
        h_committed = self._committed_in_zone(holder_id, zone)
        if w_committed and not h_committed:
            lo_id = holder_id
        elif h_committed and not w_committed:
            lo_id = waiter_id
        else:
            # Neither (or both) committed: lower-priority contender is held;
            # higher-priority is admitted.
            lo_id = self._lower_priority(waiter_id, holder_id, position)
        lo_route = w_route if lo_id == waiter_id else h_route
        _z2, lo_near = self._contested_zone(
            lo_route, w_route if lo_id == holder_id else h_route
        )
        if lo_near is None:
            return

        # Capacity: a passing loop (an alternative route for the held contender
        # to its target that avoids the contested zone) makes capacity
        # sufficient; then write no ceiling and let the interlocking route them
        # onto the distinct tracks the loop provides.
        if self._has_alternative_avoiding(lo_id, zone):
            return

        ceiling = self._holding_point_ceiling(lo_route, lo_near)
        if ceiling is None:
            # No holding place to meet at: fall back to best-effort truncation
            # and record one diagnostic (design_new.md "Meet coordination").
            self._emit_no_holding_point(lo_id)
            return
        # Pin the held train to the exact candidate route this assignment was
        # computed on, so Plan acquires that route to the ceiling instead of an
        # equally-ranked alternative (design_new.md "Meet coordination",
        # candidate_resource_keys). The keys come from the same best candidate
        # `_route_blocks` collapsed for the contested-zone math.
        ceiling = replace(
            ceiling, candidate_resource_keys=self._candidate_keys(lo_id, keys_cache)
        )
        # First (most-restrictive) assignment per train wins; do not overwrite.
        # Emit the operator event at the moment a ceiling is written
        # (design_new.md "Meet coordination", Plan consumption → Operator
        # event), so the operator sees why the train is held. Emit-on-change via
        # the shared meet-hold condition key; reconciled when the train is no
        # longer held.
        if lo_id not in assignment:
            assignment[lo_id] = ceiling
            if ceiling.block_id:
                self._emit_meet_ceiling(lo_id, ceiling.block_id)

    def _committed_in_zone(self, train_id: str, zone: set) -> bool:
        """True when ``train_id`` already occupies or **locks** any block in the
        contested ``zone`` (design_new.md "Meet coordination", Existing
        commitment decides before priority).

        Commitment is occupancy or a lock, never a bare reservation: a block
        this train has reserved but not yet locked does **not** commit the meet,
        because Preemption can still take it. Occupancy is read from
        ``Block.occupied_by``; a lock is a ``Block.locked_by_path`` naming a path
        this train owns.
        """
        blocks = getattr(self._re.network, "blocks", {}) or {}
        dispatcher = getattr(self._re, "path_dispatcher", None)
        paths = getattr(dispatcher, "paths", {}) if dispatcher is not None else {}
        for bid in zone:
            block = blocks.get(bid)
            if block is None:
                continue
            if getattr(block, "occupied_by", None) == train_id:
                return True
            locked_by_path = getattr(block, "locked_by_path", None)
            if locked_by_path is not None:
                path = paths.get(locked_by_path)
                if path is not None and getattr(path, "train_id", None) == train_id:
                    return True
        return False

    # -- contested-zone detection ----------------------------------------

    @staticmethod
    def _contested_zone(
        a_route: RouteBlocks, b_route: RouteBlocks
    ) -> Tuple[set, Optional[int]]:
        """Blocks both routes traverse in OPPOSING directions, plus the index of
        the first such block on ``a_route`` (the near entry for A)."""
        b_dir = {bid: d for bid, d in b_route}
        zone: set = set()
        a_near: Optional[int] = None
        for i, (bid, d) in enumerate(a_route):
            bd = b_dir.get(bid)
            if bd is not None and bd != d:
                zone.add(bid)
                if a_near is None:
                    a_near = i
        return zone, a_near

    def _holding_point_ceiling(
        self, route: RouteBlocks, near_index: int
    ) -> Optional[MeetCeiling]:
        """Furthest holding point strictly before ``near_index`` on ``route``."""
        blocks = getattr(self._re.network, "blocks", {}) or {}
        best: Optional[MeetCeiling] = None
        for i in range(near_index):
            bid, direction = route[i]
            block = blocks.get(bid)
            if block is not None and getattr(block, "is_holding_point", False):
                best = MeetCeiling(block_id=bid, direction=direction)  # latest wins
        return best

    def _has_alternative_avoiding(self, train_id: str, zone: set) -> bool:
        """True when ``train_id`` has a candidate route to its next target that
        avoids every block in ``zone`` (a passing loop → sufficient capacity)."""
        for route in self._candidate_routes(train_id, max_candidates=8):
            if not any(bid in zone for bid, _d in route):
                return True
        return False

    # -- route extraction -------------------------------------------------

    def _route_blocks(
        self, train_id: str, cache: Dict[str, Optional[RouteBlocks]]
    ) -> Optional[RouteBlocks]:
        if train_id in cache:
            return cache[train_id]
        routes = self._candidate_routes(train_id, max_candidates=1)
        result = routes[0] if routes else None
        cache[train_id] = result
        return result

    def _candidate_routes(
        self, train_id: str, *, max_candidates: int
    ) -> List[RouteBlocks]:
        """Candidate routes (each [(block_id, direction)]) for the train's next
        target, best-first, from the shared pathfinder."""
        out: List[RouteBlocks] = []
        for cand in self._raw_candidates(train_id, max_candidates=max_candidates):
            blocks = self._walk_to_route_blocks(getattr(cand, "ordered_walk", None))
            if blocks:
                out.append(blocks)
        return out

    def _candidate_keys(
        self,
        train_id: str,
        cache: Dict[str, Tuple[CandidateResourceKey, ...]],
    ) -> Tuple[CandidateResourceKey, ...]:
        """The selected (best) candidate's ordered `(resource_type, resource_id,
        entry_edge, exit_edge)` key list — the same route `_route_blocks`
        collapsed for the zone math, but keeping switches and edge frames so the
        dispatcher can pin its acquisition to it (design_new.md "Meet
        coordination")."""
        if train_id in cache:
            return cache[train_id]
        raw = self._raw_candidates(train_id, max_candidates=1)
        keys: Tuple[CandidateResourceKey, ...] = ()
        if raw:
            keys = self._walk_to_resource_keys(getattr(raw[0], "ordered_walk", None))
        cache[train_id] = keys
        return keys

    def _raw_candidates(self, train_id: str, *, max_candidates: int) -> List:
        """Raw `GraphWalkCandidate`s for the train's next target, best-first,
        from the shared pathfinder (the geometry the collapsed routes and the
        pinned keys are both derived from)."""
        start = self._head_block(train_id)
        if not start:
            return []
        proj = self._re._find_next_task_target(train_id)
        if proj is None or getattr(proj, "resolved_target", None) is None:
            return []
        target = proj.resolved_target.requested_destination_block_id
        direction = proj.resolved_target.requested_destination_direction
        dispatcher = self._re.path_dispatcher
        pathfinder = getattr(dispatcher, "_pathfinder", None)
        if pathfinder is None or not hasattr(pathfinder, "find_ordered_walk"):
            return []
        train = (self._re.network.trains.get(train_id)
                 if self._re.network else None)
        try:
            candidates = pathfinder.find_ordered_walk(
                start,
                target,
                direction_constraint=direction,
                max_candidates=max_candidates,
                train_min_radius_cm=getattr(train, "min_radius_cm", None),
                train_max_speed_kmh=getattr(train, "max_speed", None),
                train_max_slope_percent=getattr(train, "max_slope", None),
            )
        except Exception:
            return []
        return list(candidates or [])

    @staticmethod
    def _walk_to_resource_keys(ordered_walk) -> Tuple[CandidateResourceKey, ...]:
        """Every pathfinder step's `(resource_type, resource_id, entry_edge,
        exit_edge)` key, in traversal order (switches kept — they carry the
        frame that pins a crossover/throat)."""
        if not ordered_walk:
            return ()
        keys: List[CandidateResourceKey] = []
        for step in ordered_walk:
            keys.append((
                getattr(step, "resource_type", "") or "",
                getattr(step, "resource_id", "") or "",
                getattr(step, "entry_edge", "") or "",
                getattr(step, "exit_edge", "") or "",
            ))
        return tuple(keys)

    @staticmethod
    def _walk_to_route_blocks(ordered_walk) -> RouteBlocks:
        """Collapse a GraphWalkStep list to ordered distinct (block_id,
        direction) entries (switch steps carry no block frame and are skipped)."""
        if not ordered_walk:
            return []
        out: RouteBlocks = []
        for step in ordered_walk:
            if getattr(step, "resource_type", None) != "block":
                continue
            bid = getattr(step, "block_id", None)
            direction = getattr(step, "block_direction", None)
            if not bid or not direction:
                continue
            if not out or out[-1][0] != bid:
                out.append((bid, direction))
        return out

    def _head_block(self, train_id: str) -> Optional[str]:
        ctrl = self._re._controller_by_id(train_id)
        if ctrl is None:
            return None
        getter = getattr(ctrl, "_get_head_tile", None)
        tile = getter() if callable(getter) else None
        if tile is None or self._re.network is None:
            return None
        track = self._re.network.get_track(*tile)
        return getattr(track, "block_id", None) if track else None

    # -- helpers ----------------------------------------------------------

    def _lower_priority(
        self, a_id: str, b_id: str, position: Dict[str, int]
    ) -> str:
        """The lower-priority of two trains: lower effective_priority, ties
        broken by the stable order_runnable_trains order (later = lower)."""
        a_pri = self._re._effective_priority_for(a_id)
        b_pri = self._re._effective_priority_for(b_id)
        if a_pri != b_pri:
            return a_id if a_pri < b_pri else b_id
        # tie -> later in the runnable order is the lower one
        return a_id if position.get(a_id, 0) > position.get(b_id, 0) else b_id

    @staticmethod
    def _meet_hold_key(train_id: str) -> str:
        """The stable `condition_key` naming this train's live no-holding-point
        condition, shared by the emit and the resolve so the two match."""
        return f"train:{train_id}:meet_hold"

    def _emit_no_holding_point(self, train_id: str) -> None:
        """One NotificationCenter diagnostic: the layout offers no holding place
        for this train to meet at (design_new.md "Meet coordination").

        This is a live per-tick condition — `assign_meets` runs every Plan pass —
        so it carries a `condition_key`: `NotificationCenter` drops the repeat
        while the condition persists, and `_reconcile_no_holding_point()`
        resolves the key the first pass the train is no longer held."""
        self._no_holding_point_trains.add(train_id)
        sc = getattr(self._re, "simulation_controller", None)
        nc = getattr(sc, "notification_center", None) if sc is not None else None
        if nc is None:
            return
        try:
            from notification_center import Kind, train_ref
            from runtime_payloads import ErrorRecord

            nc.emit(
                source="RouteExecutor",
                operation="compute_meet_assignment",
                kind=Kind.WARNING,
                message=(
                    "meet coordination: no holding point before the "
                    "contested zone; falling back to best-effort truncation"
                ),
                entities=[train_ref(train_id)],
                error=ErrorRecord(
                    "no holding point before the contested zone", "meet_hold"
                ),
                condition_key=self._meet_hold_key(train_id),
            )
        except Exception:
            pass

    def _emit_meet_ceiling(self, train_id: str, ceiling_block: str) -> None:
        """One NotificationCenter diagnostic when a train is held at a meet
        ceiling (design_new.md "Meet coordination", Plan consumption → Operator
        event).

        Uses the shared per-train meet-hold condition key, so it is emit-on-
        change (a hold that moves to a different ceiling block re-emits with the
        new payload) and mutually exclusive with the no-holding-point event, and
        is reconciled by `_reconcile_no_holding_point()` the first pass the train
        is no longer held. The train is tracked in the same held-set so that
        reconciliation resolves the condition."""
        self._no_holding_point_trains.add(train_id)
        sc = getattr(self._re, "simulation_controller", None)
        nc = getattr(sc, "notification_center", None) if sc is not None else None
        if nc is None:
            return
        try:
            from notification_center import Kind, train_ref
            from runtime_payloads import ErrorRecord

            nc.emit(
                source="RouteExecutor",
                operation="compute_meet_assignment",
                kind=Kind.WARNING,
                message=(
                    f"meet coordination: train {train_id} held at meet ceiling "
                    f"{ceiling_block}"
                ),
                entities=[train_ref(train_id)],
                error=ErrorRecord(
                    f"held at meet ceiling {ceiling_block}", "meet_hold"
                ),
                # The event carries the assigned ceiling and the run's requested
                # destination so the operator can see why the train is stopped
                # (design_new.md "Meet coordination", Operator event). A ceiling
                # that moves is a changed payload and a new event.
                payload={
                    "meet_ceiling_block_id": ceiling_block,
                    "requested_destination_block_id": self._run_requested_destination(
                        train_id
                    ),
                },
                condition_key=self._meet_hold_key(train_id),
            )
        except Exception:
            pass

    def _run_requested_destination(self, train_id: str) -> Optional[str]:
        """The run's `requested_destination_block_id` for the meet-hold event
        (design_new.md "Meet coordination", Operator event), or `None` when it
        cannot be resolved."""
        try:
            proj = self._re._find_next_task_target(train_id)
        except Exception:
            return None
        resolved = getattr(proj, "resolved_target", None) if proj is not None else None
        return getattr(resolved, "requested_destination_block_id", None)

    def _reconcile_no_holding_point(self) -> None:
        """Resolve the no-holding-point condition for every train that had it on
        the previous pass but not this one (design_new.md "Error events",
        Resolution and recurrence).

        The active set of held trains lives on the `RouteExecutor`, not on this
        coordinator, because a fresh `MeetCoordinator` is built each tick. A
        train drops out of the set when it finds a holding point, gains an
        alternative route, or leaves contention entirely — every one of those
        is the condition's predicate going false."""
        held_now = self._no_holding_point_trains
        previous: Set[str] = getattr(self._re, "meet_hold_train_ids", set())
        sc = getattr(self._re, "simulation_controller", None)
        nc = getattr(sc, "notification_center", None) if sc is not None else None
        if nc is not None:
            for train_id in previous - held_now:
                try:
                    nc.resolve_condition(self._meet_hold_key(train_id))
                except Exception:
                    pass
        self._re.meet_hold_train_ids = set(held_now)
