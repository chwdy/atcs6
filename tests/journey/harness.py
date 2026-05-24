"""JourneyHarness — drives ticks, snapshots state, runs invariants and event
assertions, and produces structured failure reports for any failing pytest.

Public API:
    harness = JourneyHarness(scene, journey, dt=0.1, max_ticks=2000)
    result = harness.run()
    assert result.success, result.report()
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, List, Optional, Set, Tuple

from enums import AtcLimitReasonCode, TrainState

from .events import Event, Invariant, Journey
from .scenes.base import Scene, SceneFixture


# --------------------------------------------------------------------------- #
# Progress tracker
# --------------------------------------------------------------------------- #


class ProgressTracker:
    """Wraps `PathDispatcher.on_controller_progress` so the harness sees every
    block / switch entry and tail-vacate event, even when high `dt` causes a
    resource to be entered AND vacated within a single tick (the post-tick
    snapshot would miss those transient `occupied_by` writes).
    """

    def __init__(self, train_id: str):
        self.train_id = train_id
        self.blocks_entered: Set[str] = set()
        self.blocks_vacated: Set[str] = set()
        self.switches_entered: Set[str] = set()
        self.switches_vacated: Set[str] = set()
        # Per-tick entries — refreshed by harness before each snapshot.
        self.this_tick_entered_blocks: Set[str] = set()
        self.this_tick_entered_switches: Set[str] = set()
        self.this_tick_vacated_blocks: Set[str] = set()
        self.this_tick_vacated_switches: Set[str] = set()
        self._original = None
        self._dispatcher = None

    def attach(self, dispatcher) -> None:
        if dispatcher is None or self._dispatcher is dispatcher:
            return
        self._dispatcher = dispatcher
        self._original = dispatcher.on_controller_progress

        def wrapped(report):
            if report.train_id == self.train_id:
                for ev in report.entered_resources or []:
                    if ev.resource_type == "block":
                        self.blocks_entered.add(ev.resource_id)
                        self.this_tick_entered_blocks.add(ev.resource_id)
                    elif ev.resource_type == "switch":
                        self.switches_entered.add(ev.resource_id)
                        self.this_tick_entered_switches.add(ev.resource_id)
                for ev in report.tail_vacated_resources or []:
                    if ev.resource_type == "block":
                        self.blocks_vacated.add(ev.resource_id)
                        self.this_tick_vacated_blocks.add(ev.resource_id)
                    elif ev.resource_type == "switch":
                        self.switches_vacated.add(ev.resource_id)
                        self.this_tick_vacated_switches.add(ev.resource_id)
            return self._original(report)

        dispatcher.on_controller_progress = wrapped

    def detach(self) -> None:
        if self._dispatcher is not None and self._original is not None:
            self._dispatcher.on_controller_progress = self._original
        self._dispatcher = None
        self._original = None

    def reset_per_tick(self) -> None:
        self.this_tick_entered_blocks = set()
        self.this_tick_entered_switches = set()
        self.this_tick_vacated_blocks = set()
        self.this_tick_vacated_switches = set()


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #


@dataclass
class Snapshot:
    """All controller / network state needed by triggers, assertions, and
    invariants — captured once per tick so checks see a consistent view.
    """

    tick: int
    sim_time: float
    fixture: SceneFixture

    # Controller-derived
    state: TrainState
    cab: Optional[str]
    speed_kmh: float
    eb_active: bool
    ato_running: bool
    handle_position: Any
    handle_reason: str
    atc_limit_kmh: float
    atc_reason_code: Optional[AtcLimitReasonCode]
    atc_reason_str: Optional[str]
    head_tile: Optional[Tuple[int, int]]
    tail_tile: Optional[Tuple[int, int]]
    occupied_tiles: List[Tuple[int, int]]
    occupied_blocks: List[str]
    command_inbox_kinds: List[str]

    # Phase-3 latch view (booleans + cancel-request payload presence)
    latch_path_cancel_request: bool
    latch_route_abort: bool
    latch_power_off: bool
    latch_route_deactivate: bool

    # Path / route
    active_path_id: Optional[str]
    active_path_status: Optional[str]
    active_path_authority_end_block_id: Optional[str]
    assigned_route_id: Optional[str]

    # Cumulative resource-event tracker (populated by ProgressTracker; None for
    # unit tests that build a Snapshot without a tracker).
    tracker: Optional["ProgressTracker"] = None

    @property
    def controller(self):
        return self.fixture.sim_controller.get_or_create_controller(
            self.fixture.train_id
        )

    @property
    def network(self):
        return self.fixture.network

    @property
    def sim_controller(self):
        return self.fixture.sim_controller

    def short(self) -> str:
        """One-line summary used in failure reports' tick-history table."""
        head = (
            f"({self.head_tile[0]},{self.head_tile[1]})"
            if self.head_tile is not None
            else "?"
        )
        path = self.active_path_id or "-"
        return (
            f"head={head} speed={self.speed_kmh:5.2f} "
            f"atc={self.atc_limit_kmh:5.2f} state={self.state.value} "
            f"path={path} eb={int(self.eb_active)} ato={int(self.ato_running)}"
        )


def _capture_snapshot(
    tick: int,
    fixture: SceneFixture,
    tracker: Optional["ProgressTracker"] = None,
) -> Snapshot:
    sim = fixture.sim_controller
    network = fixture.network
    train_id = fixture.train_id

    controller = sim.get_or_create_controller(train_id)

    span = controller.physics_sync.current_span
    head_tile: Optional[Tuple[int, int]] = None
    tail_tile: Optional[Tuple[int, int]] = None
    occupied_tiles: List[Tuple[int, int]] = []
    occupied_blocks: List[str] = []
    if span is not None:
        head = span.head
        tail = span.tail
        if head is not None:
            head_tile = tuple(head.tile)
        if tail is not None:
            tail_tile = tuple(tail.tile)
        occupied_tiles = [tuple(t) for t in (span.occupied_tiles or [])]
        occupied_blocks = list(span.occupied_blocks or [])

    latches = controller.route_hooks.phase_latches
    cmd_kinds = [getattr(c, "kind", None) for c in (controller.commands.command_inbox or [])]

    # Path
    active_path_id = None
    active_path_status = None
    active_path_authority_end = None
    dispatcher = sim.path_dispatcher
    if dispatcher is not None:
        path = dispatcher.get_active_path(train_id)
        if path is not None:
            active_path_id = getattr(path, "path_id", None)
            status = getattr(path, "status", None)
            active_path_status = getattr(status, "value", str(status)) if status else None
            # Per design_new.md "Path field schema", `Path.path_tiles` and
            # `Path.authority_end_block_id` are exposed as functions
            # (`get_path_tiles()`, `get_authority_end_block_id()`).
            getter = getattr(path, "get_authority_end_block_id", None)
            active_path_authority_end = (
                getter() if callable(getter) else None
            ) or getattr(path, "end_block_id", None)

    # Route
    assigned_route_id = None
    if sim.route_executor is not None:
        try:
            assigned_route_id = sim.route_executor.assigned_route_id_for(train_id)
        except Exception:
            assigned_route_id = None

    # ATC reason — prefer the structured enum cached on the controller
    reason_code = getattr(controller.atc, "_last_limit_reason_code", None)
    reason_str = controller.speed_limit_reason

    return Snapshot(
        tick=tick,
        sim_time=sim.simulation_time,
        fixture=fixture,
        state=controller.ops.state,
        cab=controller.commands.cab,
        speed_kmh=float(controller.physics_sync.current_speed_kmh),
        eb_active=bool(controller.eb.active),
        ato_running=bool(controller.ato.running),
        handle_position=controller.ops.handle_position,
        handle_reason=controller.ops.handle_reason or "",
        atc_limit_kmh=float(controller.atc.speed_limit),
        atc_reason_code=reason_code,
        atc_reason_str=reason_str,
        head_tile=head_tile,
        tail_tile=tail_tile,
        occupied_tiles=occupied_tiles,
        occupied_blocks=occupied_blocks,
        command_inbox_kinds=cmd_kinds,
        latch_path_cancel_request=latches.path_cancel_request is not None,
        latch_route_abort=bool(latches.route_abort),
        latch_power_off=bool(latches.power_off),
        latch_route_deactivate=bool(latches.route_deactivate),
        active_path_id=active_path_id,
        active_path_status=active_path_status,
        active_path_authority_end_block_id=active_path_authority_end,
        assigned_route_id=assigned_route_id,
        tracker=tracker,
    )


# --------------------------------------------------------------------------- #
# Failure / result types
# --------------------------------------------------------------------------- #


@dataclass
class Failure:
    """One failed check (assertion, action, or invariant)."""

    kind: str  # "invariant" | "assertion" | "action" | "trigger_timeout"
    name: str
    message: str
    snapshot: Optional[Snapshot]
    pending_event_name: Optional[str]
    last_completed_event_name: Optional[str]
    history: List[Snapshot] = field(default_factory=list)

    def render(self, dt: float) -> str:
        snap = self.snapshot
        head = (
            f"tick {snap.tick} (sim_time={snap.sim_time:.2f}s, dt={dt:.2f})"
            if snap is not None
            else f"(no snapshot, dt={dt:.2f})"
        )
        lines = [
            f"JourneyHarness failure at {head}",
            f"Last completed event: {self.last_completed_event_name or '(none)'}",
            f"Pending event: {self.pending_event_name or '(none)'}",
            f"Failure: {self.kind}:{self.name}",
            f"  {self.message}",
        ]
        if snap is not None:
            lines.append("Snapshot:")
            lines.append(
                f"  state={snap.state.value} cab={snap.cab} "
                f"head={snap.head_tile} tail={snap.tail_tile} "
                f"speed={snap.speed_kmh:.2f}"
            )
            lines.append(
                f"  occupied_blocks={snap.occupied_blocks} "
                f"occupied_tiles={snap.occupied_tiles}"
            )
            path_desc = (
                f"{snap.active_path_id} ({snap.active_path_status} "
                f"-> {snap.active_path_authority_end_block_id})"
                if snap.active_path_id
                else "None"
            )
            lines.append(f"  active_path={path_desc} route={snap.assigned_route_id}")
            lines.append(
                f"  eb={snap.eb_active} ato={snap.ato_running} "
                f"handle={getattr(snap.handle_position, 'value', snap.handle_position)} "
                f"handle_reason={snap.handle_reason!r}"
            )
            lines.append(
                f"  atc_limit={snap.atc_limit_kmh:.2f} "
                f"reason_code={getattr(snap.atc_reason_code, 'value', snap.atc_reason_code)} "
                f"reason_str={snap.atc_reason_str!r}"
            )
            lines.append(
                f"  latches=path_cancel={snap.latch_path_cancel_request} "
                f"route_abort={snap.latch_route_abort} "
                f"power_off={snap.latch_power_off} "
                f"route_deactivate={snap.latch_route_deactivate}"
            )
            lines.append(f"  command_inbox={snap.command_inbox_kinds}")
            if snap.tracker is not None:
                lines.append(
                    f"  tracker.blocks_entered={sorted(snap.tracker.blocks_entered)}"
                )
                lines.append(
                    f"  tracker.blocks_vacated={sorted(snap.tracker.blocks_vacated)}"
                )
                lines.append(
                    f"  tracker.switches_entered={sorted(snap.tracker.switches_entered)} "
                    f"vacated={sorted(snap.tracker.switches_vacated)}"
                )
        if self.history:
            lines.append("Recent ticks:")
            for s in self.history:
                lines.append(f"  {s.tick:>5}: {s.short()}")
        return "\n".join(lines)


@dataclass
class JourneyResult:
    success: bool
    dt: float
    ticks_executed: int
    completed_events: List[str]
    failure: Optional[Failure] = None

    def report(self) -> str:
        if self.success:
            return (
                f"JourneyHarness OK (dt={self.dt:.2f}, "
                f"ticks={self.ticks_executed}, events={len(self.completed_events)})"
            )
        assert self.failure is not None
        return self.failure.render(self.dt)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class JourneyHarness:
    """Drive `scene` with `journey` at the given `dt` for at most `max_ticks`
    physical ticks (tick 0 is the pre-update snapshot).
    """

    def __init__(
        self,
        scene: Scene,
        journey: Journey,
        dt: float,
        max_ticks: int,
        history_size: int = 10,
    ):
        self.scene = scene
        self.journey = journey
        self.dt = float(dt)
        self.max_ticks = int(max_ticks)
        self.history: Deque[Snapshot] = deque(maxlen=history_size)

    # -- internal -- #

    def _record(self, snapshot: Snapshot) -> None:
        self.history.append(snapshot)

    def _make_failure(
        self,
        kind: str,
        name: str,
        message: str,
        snapshot: Optional[Snapshot],
        pending_event_name: Optional[str],
        last_completed_event_name: Optional[str],
    ) -> Failure:
        return Failure(
            kind=kind,
            name=name,
            message=message,
            snapshot=snapshot,
            pending_event_name=pending_event_name,
            last_completed_event_name=last_completed_event_name,
            history=list(self.history),
        )

    # -- run -- #

    def run(self) -> JourneyResult:
        fixture = self.scene.build()
        if fixture.train_id != self.journey.train_id:
            raise RuntimeError(
                f"Scene train_id {fixture.train_id!r} does not match Journey "
                f"train_id {self.journey.train_id!r}"
            )

        tracker = ProgressTracker(self.journey.train_id)
        tracker.attach(fixture.sim_controller.path_dispatcher)

        pending: List[Event] = list(self.journey.events)
        completed: List[str] = []
        last_completed: Optional[str] = None

        # Tick 0 — pre-update snapshot.
        snap = _capture_snapshot(0, fixture, tracker)
        self._record(snap)
        failure = self._run_invariants(snap, pending, last_completed)
        if failure is not None:
            return JourneyResult(False, self.dt, 0, completed, failure)
        result = self._process_events(snap, pending, completed, last_completed)
        pending, completed, last_completed, failure = result
        if failure is not None:
            return JourneyResult(False, self.dt, 0, completed, failure)

        # Ticks 1..max_ticks.
        for tick in range(1, self.max_ticks + 1):
            tracker.reset_per_tick()
            fixture.sim_controller.update(self.dt)
            snap = _capture_snapshot(tick, fixture, tracker)
            self._record(snap)

            failure = self._run_invariants(snap, pending, last_completed)
            if failure is not None:
                return JourneyResult(False, self.dt, tick, completed, failure)

            result = self._process_events(snap, pending, completed, last_completed)
            pending, completed, last_completed, failure = result
            if failure is not None:
                return JourneyResult(False, self.dt, tick, completed, failure)

            if not pending:
                return JourneyResult(True, self.dt, tick, completed, None)

        # Out of ticks with events still pending.
        first_pending = pending[0]
        failure = self._make_failure(
            kind="trigger_timeout",
            name=first_pending.name,
            message=(
                f"Event {first_pending.name!r} trigger never fired within "
                f"{self.max_ticks} ticks at dt={self.dt}"
            ),
            snapshot=self.history[-1] if self.history else None,
            pending_event_name=first_pending.name,
            last_completed_event_name=last_completed,
        )
        return JourneyResult(False, self.dt, self.max_ticks, completed, failure)

    def _run_invariants(
        self,
        snap: Snapshot,
        pending: List[Event],
        last_completed: Optional[str],
    ) -> Optional[Failure]:
        for inv in self.journey.invariants:
            try:
                inv.check(snap)
            except AssertionError as e:
                return self._make_failure(
                    kind="invariant",
                    name=inv.name,
                    message=str(e),
                    snapshot=snap,
                    pending_event_name=pending[0].name if pending else None,
                    last_completed_event_name=last_completed,
                )
        return None

    def _process_events(
        self,
        snap: Snapshot,
        pending: List[Event],
        completed: List[str],
        last_completed: Optional[str],
    ) -> Tuple[List[Event], List[str], Optional[str], Optional[Failure]]:
        # Drain every pending event whose trigger fires this tick.
        while pending:
            event = pending[0]
            try:
                fired = bool(event.trigger(snap))
            except Exception as e:
                failure = self._make_failure(
                    kind="trigger_error",
                    name=event.name,
                    message=f"trigger raised: {e!r}",
                    snapshot=snap,
                    pending_event_name=event.name,
                    last_completed_event_name=last_completed,
                )
                return pending, completed, last_completed, failure
            if not fired:
                break

            for assertion in event.assertions:
                try:
                    assertion(snap)
                except AssertionError as e:
                    failure = self._make_failure(
                        kind="assertion",
                        name=event.name,
                        message=str(e),
                        snapshot=snap,
                        pending_event_name=event.name,
                        last_completed_event_name=last_completed,
                    )
                    return pending, completed, last_completed, failure
            for action in event.actions:
                try:
                    action(snap)
                except AssertionError as e:
                    failure = self._make_failure(
                        kind="action",
                        name=event.name,
                        message=str(e),
                        snapshot=snap,
                        pending_event_name=event.name,
                        last_completed_event_name=last_completed,
                    )
                    return pending, completed, last_completed, failure

            pending = pending[1:]
            completed.append(event.name)
            last_completed = event.name

        return pending, completed, last_completed, None
