"""Meet coordination — the per-tick meet assignment (design_new.md "Meet
coordination").

A meet is two route-managed trains whose routes run over the same blocks in
opposite directions. On a single line they can pass only where the track has a
second road, so one train waits at a passing place off the other train's way
while the other goes by. Which train waits, and where, is a decision no single
train's Plan can make from its own view, so `RouteExecutor` computes it once per
tick, after the contention snapshot and before the wedge report and the first
Plan. Nothing is kept from one tick to the next: each tick's assignment is
worked out again from the trains' positions, reservations, locks, priorities,
and runnable order, so a meet ends by itself once the routes no longer run
over each other.

The assignment chooses walks, caps how far a waiting train reserves, and keeps
the waiting train's way to its passing place from every other train's
acquisition and from Preemption. It never changes a route target and never
reserves a resource. Plan acts on it in step `66aa`.
"""

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

# One pathfinder step's identity: (resource_type, resource_id, entry_edge,
# exit_edge). An entry's `candidate_resource_keys` is the ordered tuple of these.
CandidateResourceKey = Tuple[str, str, str, str]

# A resource on a route or a way: (resource_type, resource_id).
ResourceKey = Tuple[str, str]


@dataclass(frozen=True)
class MeetEntry:
    """One train's entry in the meet assignment (design_new.md "Meet
    coordination", The meet assignment).

    `candidate_resource_keys` is the walk the train must take on its next
    dispatcher call, exactly as that call enumerates it, or `None` when it needs
    no new reservation to follow its chosen route. `meet_ceiling_block_id` is
    the furthest block it may hold reservation to this tick (its passing place),
    or `None`.
    """

    candidate_resource_keys: Optional[Tuple[CandidateResourceKey, ...]] = None
    meet_ceiling_block_id: Optional[str] = None


@dataclass
class MeetAssignment:
    """The tick's meet assignment plus what `RouteExecutor` keeps beside it.

    `entries` is the assignment itself. `kept_ways` maps each waiting train to
    its way — the resources from its head up to and including its passing place
    — which no other train may acquire or preempt. `waiting_for` maps each
    waiting train to the train it lets by, for the give-back `reason`.
    """

    entries: Dict[str, MeetEntry] = field(default_factory=dict)
    kept_ways: Dict[str, FrozenSet[ResourceKey]] = field(default_factory=dict)
    waiting_for: Dict[str, str] = field(default_factory=dict)

    def way_holder(self, resource_type: str, resource_id: str) -> Optional[str]:
        """The train whose kept way holds this resource, or None."""
        key = (resource_type, resource_id)
        for train_id, way in self.kept_ways.items():
            if key in way:
                return train_id
        return None


@dataclass
class _Step:
    """One resource on a route, in traversal order from the train's head."""

    resource_type: str
    resource_id: str
    block_direction: Optional[str]
    length_cm: float


@dataclass
class _Route:
    """A train's route: its live reservation from the head on, then one walk its
    next dispatcher call enumerates (design_new.md "Meet coordination",
    Conflict detection). `keys` names only the walk, never the live part."""

    steps: List[_Step]
    keys: Tuple[CandidateResourceKey, ...]
    rank: int
    block_ids: FrozenSet[str] = frozenset()
    block_directions: Dict[str, FrozenSet[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        directions: Dict[str, Set[str]] = {}
        for step in self.steps:
            if step.resource_type == "block" and step.block_direction:
                directions.setdefault(step.resource_id, set()).add(step.block_direction)
        self.block_ids = frozenset(
            step.resource_id for step in self.steps if step.resource_type == "block"
        )
        self.block_directions = {b: frozenset(d) for b, d in directions.items()}


@dataclass
class _RoutedTrain:
    train_id: str
    routes: List[_Route]  # best first, never empty
    live_step_count: int  # how many leading steps are the live reservation
    lock_step_index: int  # step index of the lock end; 0 with no live path
    position: int  # place in the runnable order
    priority: int
    path: Optional[object]  # the live Path, or None


@dataclass
class _PassingPlace:
    block_id: str
    route: _Route
    step_index: int
    distance_cm: float


_OPPOSITE = {"forward": "reverse", "reverse": "forward"}


def contested_blocks(a: _Route, b: _Route) -> Set[str]:
    """The blocks two routes traverse in opposite block-relative directions.
    Each maximal run of them is a contested zone."""
    contested: Set[str] = set()
    for block_id, a_dirs in a.block_directions.items():
        b_dirs = b.block_directions.get(block_id)
        if not b_dirs:
            continue
        if any(_OPPOSITE[d] in b_dirs for d in a_dirs):
            contested.add(block_id)
    return contested


class MeetCoordinator:
    """Builds the tick's `MeetAssignment` (design_new.md "Meet coordination").

    Constructed fresh each tick with the live `RouteExecutor`; reads the route
    registry, the dispatcher's live paths and its next-call walk list, the
    blocks' occupancy, reservation, lock, and `is_holding_point` fields.
    """

    def __init__(self, route_executor) -> None:
        self._re = route_executor
        self._network = getattr(route_executor, "network", None)
        self._dispatcher = getattr(route_executor, "path_dispatcher", None)
        # What earlier meets this tick wrote, which later meets treat as fixed.
        self._chosen_routes: Dict[str, _Route] = {}
        self._given_passing_places: Dict[str, str] = {}  # block_id -> train_id
        self._given_ways: Dict[ResourceKey, str] = {}
        self._go_route_blocks: Set[str] = set()

    # -- public -----------------------------------------------------------

    def build(self, ordered_train_ids: List[str]) -> MeetAssignment:
        """Return the meet assignment for this tick and emit its warnings."""
        assignment = MeetAssignment()
        held: Dict[str, str] = {}  # waiting train -> passing place
        no_place_pairs: List[Tuple[str, str]] = []

        trains = self._routed_trains(ordered_train_ids)
        for meet in self._meets_in_decision_order(trains):
            self._decide(meet, assignment, held, no_place_pairs)

        for train_id, block_id in held.items():
            self._emit_meet_ceiling(train_id, block_id)
        for a_id, b_id in no_place_pairs:
            self._emit_no_place_to_pass(a_id, b_id)
        self._reconcile_conditions(set(held), set(no_place_pairs))
        return assignment

    # -- routes -----------------------------------------------------------

    def _routed_trains(self, ordered_train_ids: List[str]) -> List[_RoutedTrain]:
        """Every train with a `RUNNING` run whose target resolves and that has a
        walk toward it, in runnable order."""
        if self._dispatcher is None or self._network is None:
            return []
        out: List[_RoutedTrain] = []
        for position, train_id in enumerate(ordered_train_ids):
            routed = self._routed_train(train_id, position)
            if routed is not None:
                out.append(routed)
        return out

    def _routed_train(self, train_id: str, position: int) -> Optional[_RoutedTrain]:
        if self._re.execution_state_for(train_id) is None:
            return None
        path = self._dispatcher.get_active_path(train_id)
        # The target Plan's next create/extend call aims at (design_new.md
        # "Meet coordination", The next call's target).
        target = self._re.next_call_target(train_id)
        if target is None:
            return None
        candidates = self._dispatcher.next_call_walk_candidates(
            train_id,
            target.requested_destination_block_id,
            target.requested_destination_direction,
            self._re._manual_override_for(train_id),
        )
        # A route's required walk is the train's only candidate here
        # (design_new.md Planning Kernel step `66aa`, Required walk).
        candidates = self._re.required_walk_candidates(train_id, candidates)
        if not candidates:
            return None

        live_steps: List[_Step] = []
        lock_step_index = 0
        reservation_end = None
        if path is not None:
            live_steps = self._live_steps(path)
            reservation_end = path.get_reservation_end_block_id()
            lock_end = path.get_lock_end_block_id()
            for i, step in enumerate(live_steps):
                if step.resource_type == "block" and step.resource_id == lock_end:
                    lock_step_index = i
                    break

        routes: List[_Route] = []
        for rank, candidate in enumerate(candidates):
            ordered_walk = getattr(candidate, "ordered_walk", None) or []
            walk_steps = self._walk_steps(ordered_walk)
            # The walk starts in the reservation-end block, which the live part
            # already lists.
            while (
                reservation_end
                and walk_steps
                and walk_steps[0].resource_type == "block"
                and walk_steps[0].resource_id == reservation_end
            ):
                walk_steps.pop(0)
            routes.append(
                _Route(
                    steps=live_steps + walk_steps,
                    keys=tuple(self._dispatcher._ordered_walk_keys(ordered_walk)),
                    rank=rank,
                )
            )
        return _RoutedTrain(
            train_id=train_id,
            routes=routes,
            live_step_count=len(live_steps),
            lock_step_index=lock_step_index,
            position=position,
            priority=self._re._effective_priority_for(train_id),
            path=path,
        )

    def _live_steps(self, path) -> List[_Step]:
        steps: List[_Step] = []
        start = int(getattr(path, "current_resource_index", 0) or 0)
        for entry in path.resource_sequence[start:]:
            direction = None
            block = (
                self._network.blocks.get(entry.resource_id)
                if entry.resource_type == "block"
                else None
            )
            if block is not None and entry.path_tiles:
                # The order the reservation walks the block's tiles; a one-tile
                # slice (the head's block) falls back to that tile's exit edge.
                direction = block.block_direction_for_tile_walk(
                    [(t.x, t.y) for t in entry.path_tiles]
                )
                if direction is None:
                    direction = block.block_direction_for_compass_exit(
                        entry.path_tiles[-1].exit_edge
                    )
            length = sum(
                float(self._network._get_tile_length_cm(t.x, t.y))
                for t in entry.path_tiles
            )
            steps.append(_Step(entry.resource_type, entry.resource_id, direction, length))
        return steps

    @staticmethod
    def _walk_steps(ordered_walk) -> List[_Step]:
        """Merge a walk's per-tile steps into one step per resource visit."""
        steps: List[_Step] = []
        for tile_step in ordered_walk:
            rtype = getattr(tile_step, "resource_type", "") or ""
            rid = getattr(tile_step, "resource_id", "") or ""
            length = float(getattr(tile_step, "length_cm", 0.0) or 0.0)
            if steps and (steps[-1].resource_type, steps[-1].resource_id) == (rtype, rid):
                steps[-1].length_cm += length
                continue
            steps.append(
                _Step(rtype, rid, getattr(tile_step, "block_direction", None), length)
            )
        return steps

    def _routes(self, train: _RoutedTrain) -> List[_Route]:
        """The train's routes, or only the one an earlier meet this tick chose."""
        chosen = self._chosen_routes.get(train.train_id)
        return [chosen] if chosen is not None else train.routes

    # -- meets ------------------------------------------------------------

    def _meets_in_decision_order(
        self, trains: List[_RoutedTrain]
    ) -> List[Tuple[_RoutedTrain, _RoutedTrain]]:
        """Every pair whose best routes run over a block in opposite directions,
        in the order meets are decided (design_new.md "Meet coordination", More
        than one meet). The first train of each pair comes earlier in the
        runnable order."""
        meets: List[Tuple[_RoutedTrain, _RoutedTrain]] = []
        for i, a in enumerate(trains):
            for b in trains[i + 1:]:
                if contested_blocks(a.routes[0], b.routes[0]):
                    meets.append((a, b))
        meets.sort(
            key=lambda pair: (
                -max(pair[0].priority, pair[1].priority),
                pair[0].position,
                pair[1].position,
            )
        )
        return meets

    def _decide(
        self,
        meet: Tuple[_RoutedTrain, _RoutedTrain],
        assignment: MeetAssignment,
        held: Dict[str, str],
        no_place_pairs: List[Tuple[str, str]],
    ) -> None:
        a, b = meet
        # An earlier meet may have chosen a route for either train; the pair is
        # decided on those routes, and is no meet if they no longer cross.
        contested = contested_blocks(self._routes(a)[0], self._routes(b)[0])
        if not contested:
            return

        places = {
            a.train_id: self._passing_places(a, b),
            b.train_id: self._passing_places(b, a),
        }
        waiting = self._who_waits(a, b, places, contested)
        if waiting is None:
            no_place_pairs.append((a.train_id, b.train_id))
            return
        going = b if waiting is a else a

        place = min(
            places[waiting.train_id], key=lambda p: (p.distance_cm, p.route.rank)
        )
        going_route = self._going_route(going, place.block_id)
        waiting_entry = MeetEntry(
            candidate_resource_keys=(
                None
                if place.step_index < waiting.live_step_count
                else place.route.keys
            ),
            meet_ceiling_block_id=place.block_id,
        )
        going_entry = (
            None
            if going_route is self._routes(going)[0]
            else MeetEntry(candidate_resource_keys=going_route.keys)
        )
        # A train gets at most one entry per tick: a later meet that would give
        # it a second writes nothing, and is decided again on a later tick.
        if waiting.train_id in assignment.entries or (
            going_entry is not None and going.train_id in assignment.entries
        ):
            return

        way = frozenset(
            (step.resource_type, step.resource_id)
            for step in place.route.steps[: place.step_index + 1]
        )
        assignment.entries[waiting.train_id] = waiting_entry
        assignment.kept_ways[waiting.train_id] = way
        assignment.waiting_for[waiting.train_id] = going.train_id
        held[waiting.train_id] = place.block_id
        self._chosen_routes[waiting.train_id] = place.route
        self._given_passing_places[place.block_id] = waiting.train_id
        for key in way:
            self._given_ways.setdefault(key, waiting.train_id)

        if going_entry is not None:
            assignment.entries[going.train_id] = going_entry
        self._chosen_routes[going.train_id] = going_route
        self._go_route_blocks |= going_route.block_ids

    def _who_waits(
        self,
        a: _RoutedTrain,
        b: _RoutedTrain,
        places: Dict[str, List[_PassingPlace]],
        contested: Set[str],
    ) -> Optional[_RoutedTrain]:
        """The train that waits, by the first rule that applies (design_new.md
        "Meet coordination", Who waits), or None when neither can wait."""
        a_can = bool(places[a.train_id])
        b_can = bool(places[b.train_id])
        if a_can != b_can:
            return a if a_can else b
        if not a_can:
            return None
        if a.priority != b.priority:
            return a if a.priority < b.priority else b
        a_in = self._reserved_into(a, contested)
        b_in = self._reserved_into(b, contested)
        if a_in != b_in:
            return b if a_in else a
        return a if a.position > b.position else b

    def _reserved_into(self, train: _RoutedTrain, contested: Set[str]) -> bool:
        """Whether the train's live reservation has a contested block ahead of
        its head. A block it occupies does not count."""
        path = train.path
        if path is None:
            return False
        head_index = int(getattr(path, "current_resource_index", 0) or 0)
        for entry in path.resource_sequence:
            if entry.sequence_index <= head_index or entry.resource_type != "block":
                continue
            if entry.resource_id not in contested:
                continue
            block = self._network.blocks.get(entry.resource_id)
            if getattr(block, "occupied_by", None) != train.train_id:
                return True
        return False

    # -- passing places ---------------------------------------------------

    def _passing_places(
        self, waiting: _RoutedTrain, going: _RoutedTrain
    ) -> List[_PassingPlace]:
        """The waiting train's passing places in a meet with `going`
        (design_new.md "Meet coordination", Passing place), one per block,
        each on the best route that reaches it."""
        found: Dict[str, _PassingPlace] = {}
        for route in self._routes(waiting):
            distance = 0.0
            for i, step in enumerate(route.steps):
                if step.resource_type == "block":
                    # W can get there: every block from the head up to the
                    # holding point is clear of other trains and of what
                    # earlier meets gave them.
                    if not self._clear_for(waiting.train_id, step.resource_id):
                        break
                    if (
                        step.resource_id not in found
                        and self._is_passing_place(waiting, going, route, i)
                    ):
                        found[step.resource_id] = _PassingPlace(
                            step.resource_id, route, i, distance
                        )
                distance += step.length_cm
        return list(found.values())

    def _is_passing_place(
        self, waiting: _RoutedTrain, going: _RoutedTrain, route: _Route, index: int
    ) -> bool:
        block_id = route.steps[index].resource_id
        block = self._network.blocks.get(block_id)
        if block is None or not getattr(block, "is_holding_point", False):
            return False
        # A block on a route already chosen for a train that goes is not one.
        if block_id in self._go_route_blocks:
            return False
        # W can still stop there: its lock end is not past it.
        if waiting.path is not None and index < waiting.lock_step_index:
            return False
        # W may end its reservation there: the reversal-trap exclusion does not
        # rule it out.
        next_block = next(
            (
                s.resource_id
                for s in route.steps[index + 1:]
                if s.resource_type == "block" and s.resource_id != block_id
            ),
            None,
        )
        if next_block is not None and self._dispatcher._terminal_traps_reversing_occupant(
            next_block, block_id, waiting.train_id
        ):
            return False
        # G can go by: it has a route that uses neither this block nor a
        # passing place given to another train.
        return any(
            self._route_avoids(going.train_id, r, block_id) for r in self._routes(going)
        )

    def _route_avoids(self, train_id: str, route: _Route, block_id: str) -> bool:
        if block_id in route.block_ids:
            return False
        return not any(
            holder != train_id and given in route.block_ids
            for given, holder in self._given_passing_places.items()
        )

    def _clear_for(self, train_id: str, block_id: str) -> bool:
        """No other train occupies, locks, or reserves the block, and it is not
        a passing place or on a way an earlier meet gave another train."""
        block = self._network.blocks.get(block_id)
        if block is None:
            return False
        occupant = getattr(block, "occupied_by", None)
        if occupant is not None and occupant != train_id:
            return False
        for path_id in (
            getattr(block, "locked_by_path", None),
            getattr(block, "reserved_by_path", None),
        ):
            owner = self._path_train(path_id)
            if owner is not None and owner != train_id:
                return False
        reserved_for = getattr(block, "reserved_for_train", None)
        if reserved_for is not None and reserved_for != train_id:
            return False
        holder = self._given_passing_places.get(block_id)
        if holder is not None and holder != train_id:
            return False
        holder = self._given_ways.get(("block", block_id))
        if holder is not None and holder != train_id:
            return False
        return True

    def _path_train(self, path_id: Optional[str]) -> Optional[str]:
        if not path_id:
            return None
        path = getattr(self._dispatcher, "paths", {}).get(path_id)
        return getattr(path, "train_id", None) if path is not None else None

    def _going_route(self, going: _RoutedTrain, passing_place: str) -> _Route:
        """The best route of the train that goes that uses neither the waiting
        train's passing place nor one given to another train, preferring one
        that uses no block another train occupies (design_new.md "Meet
        coordination", The train that goes)."""
        allowed = [
            r for r in self._routes(going)
            if self._route_avoids(going.train_id, r, passing_place)
        ]
        for route in allowed:
            if not any(self._occupied_by_other(going.train_id, b) for b in route.block_ids):
                return route
        return allowed[0]

    def _occupied_by_other(self, train_id: str, block_id: str) -> bool:
        block = self._network.blocks.get(block_id)
        occupant = getattr(block, "occupied_by", None) if block is not None else None
        return occupant is not None and occupant != train_id

    # -- operator warnings ------------------------------------------------

    @staticmethod
    def _meet_hold_key(train_id: str) -> str:
        """The condition key of this train's hold at a passing place."""
        return f"train:{train_id}:meet_hold"

    @staticmethod
    def _no_place_key(a_id: str, b_id: str) -> str:
        """The condition key of a pair that has no place to pass."""
        first, second = sorted((a_id, b_id))
        return f"meet:{first}:{second}:no_place_to_pass"

    def _notification_center(self):
        return getattr(self._re, "notification_center", None)

    def _emit_meet_ceiling(self, train_id: str, ceiling_block: str) -> None:
        """One warning when a ceiling is written (design_new.md "Meet
        coordination", Operator event), emitted again only when it changes."""
        nc = self._notification_center()
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
                    f"meet coordination: train {train_id} waits at passing place "
                    f"{ceiling_block}"
                ),
                entities=[train_ref(train_id)],
                error=ErrorRecord(
                    f"waits at passing place {ceiling_block}", "meet_hold"
                ),
                payload={
                    "train_id": train_id,
                    "meet_ceiling_block_id": ceiling_block,
                    "requested_destination_block_id": self._run_requested_destination(
                        train_id
                    ),
                },
                condition_key=self._meet_hold_key(train_id),
            )
        except Exception:
            pass

    def _emit_no_place_to_pass(self, a_id: str, b_id: str) -> None:
        """One warning that two trains in a meet have no place to pass
        (design_new.md "Meet coordination", Who waits, rule 2)."""
        nc = self._notification_center()
        if nc is None:
            return
        try:
            from notification_center import Kind, train_ref
            from runtime_payloads import ErrorRecord

            message = (
                f"meet coordination: trains {a_id} and {b_id} have no place to pass"
            )
            nc.emit(
                source="RouteExecutor",
                operation="compute_meet_assignment",
                kind=Kind.WARNING,
                message=message,
                entities=[train_ref(a_id), train_ref(b_id)],
                error=ErrorRecord("no place to pass", "meet_hold"),
                condition_key=self._no_place_key(a_id, b_id),
            )
        except Exception:
            pass

    def _run_requested_destination(self, train_id: str) -> Optional[str]:
        """The run's `requested_destination_block_id` for the hold warning, or
        `None` when it cannot be resolved."""
        try:
            proj = self._re._find_next_task_target(train_id)
        except Exception:
            return None
        resolved = getattr(proj, "resolved_target", None) if proj is not None else None
        return getattr(resolved, "requested_destination_block_id", None)

    def _reconcile_conditions(
        self, held_now: Set[str], no_place_now: Set[Tuple[str, str]]
    ) -> None:
        """Resolve each hold and each no-place-to-pass pair present on the last
        pass but not on this one (design_new.md "Error events", Resolution and
        recurrence). The sets live on `RouteExecutor`, because a fresh
        `MeetCoordinator` is built each tick."""
        nc = self._notification_center()
        previous_held: Set[str] = getattr(self._re, "meet_hold_train_ids", set())
        previous_pairs: Set[Tuple[str, str]] = getattr(
            self._re, "meet_no_place_pairs", set()
        )
        if nc is not None:
            for train_id in previous_held - held_now:
                try:
                    nc.resolve_condition(self._meet_hold_key(train_id))
                except Exception:
                    pass
            for a_id, b_id in previous_pairs - no_place_now:
                try:
                    nc.resolve_condition(self._no_place_key(a_id, b_id))
                except Exception:
                    pass
        self._re.meet_hold_train_ids = set(held_now)
        self._re.meet_no_place_pairs = set(no_place_now)
