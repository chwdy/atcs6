"""
Simulation Controller - Train Movement and Control

Definition:
This module provides the SimulationController class which runs the train
simulation using the TrainController + TrainPhysics architecture, following
the Observe / Plan / Action tick transaction defined in design_new.md
(Core Runtime Loop): Observe runs once as a batch over every train, then
Plan and Action run per train in the runnable order.

Architecture:
    SimulationController
            |
            v
    +-----------------+     +---------------+
    | TrainController |---->| TrainPhysics  |
    | (Control Logic) |     | (Movement)    |
    +-----------------+     +---------------+
            |                      |
            v                      v
        +-------+           +-------------+
        | Train |           | BlockManager|
        | (Data)|           +-------------+
        +-------+
"""

import logging
import time
from datetime import datetime
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Set,
    Tuple,
    TYPE_CHECKING,
)

from unidentified_trains import UnidentifiedTrainTracker
from network import RailwayNetwork
from train import Train, TrainCar
from enums import CommandResult, Direction, HandlePosition, OperationMode, PathStatus
import operation_environment
from block_power import BlockPowerController
from switch_power import (
    SimulatedSwitchDriver,
    SwitchOutputOwner,
    SwitchPowerController,
)
from block_occupancy import SimulatedOccupancyDriver, BlockOccupancyDetector
from hardware.safety import SafeStateError
from hardware.watchdog import WatchdogTripRecord
from notification_center import (
    EntityRef,
    Kind,
    NotificationCenter,
    block_ref,
    make_notification_emitter,
    path_ref,
    switch_ref,
    train_ref,
)
from route_executor import RouteExecutor

from train_controller import TrainController, create_controller_for_train
from train_physics import TrainPhysics, create_physics_for_train
from runtime_payloads import (
    ErrorRecord,
    FatalConflictKind,
    FatalPhysicalEntryConflict,
    HardwareFailure,
)

if TYPE_CHECKING:
    from path_dispatcher import PathDispatcher
    from session_state import SessionState

logger = logging.getLogger(__name__)


def _fatal_conflict_entities(
    payload: FatalPhysicalEntryConflict,
) -> List[EntityRef]:
    """Everything a fatal physical-entry conflict is about: both trains and the
    resource they both claim."""
    refs: List[EntityRef] = []
    if payload.incoming_train_id:
        refs.append(train_ref(payload.incoming_train_id))
    if payload.prior_owner_train_id:
        refs.append(train_ref(payload.prior_owner_train_id))
    if payload.resource_id:
        maker = switch_ref if payload.resource_type == "switch" else block_ref
        refs.append(maker(payload.resource_id))
    return refs


def _hardware_entity_ref(payload: HardwareFailure) -> Optional[EntityRef]:
    """The block or switch a hardware failure names, when it names one. A lost
    controller or a failed detector transaction names no single entity."""
    if not payload.resource_id:
        return None
    if payload.component == "switch_output":
        return switch_ref(payload.resource_id)
    return block_ref(payload.resource_id)


class SimulationController(SwitchOutputOwner):
    """
    Controls train simulation and movement using the DC analog control model.

    Integrates:
    - TrainController for control decisions (ATC + ATO)
    - TrainPhysics for physical movement and block management
    - BlockPowerController for voltage output to track blocks
    - RouteExecutor for route/waypoint execution
    """

    def __init__(
        self,
        network: RailwayNetwork,
        path_dispatcher: Optional["PathDispatcher"] = None,
        notification_center: Optional[NotificationCenter] = None,
        operation_mode: Any = OperationMode.SIM,
        input_interface: Optional[Any] = None,
        block_output_interface: Optional[Any] = None,
        switch_output_interface: Optional[Any] = None,
        environment_active: bool = True,
        activation_issues: Optional[List[Dict[str, Any]]] = None,
    ):
        self.network = network
        self.path_dispatcher = path_dispatcher
        self.last_update_time = datetime.now()

        # Run mode (design_new.md "Session mode and operating environment"): the hardware backend is chosen
        # once when the operation session is built and never switched mid-run.
        # Sim mode (the v1 default) wires the simulated interfaces; a real
        # backend injects real implementations of each interface behind the same
        # contract. No code above the interfaces branches on the mode — it
        # manifests only as which interfaces are wired below.
        self.operation_mode: OperationMode = (
            operation_mode
            if isinstance(operation_mode, OperationMode)
            else operation_environment.parse(operation_mode)
        )
        self.environment_active = bool(environment_active)
        self.activation_issues = list(activation_issues or [])
        self._input_interface_override = input_interface
        self._block_output_interface_override = block_output_interface
        self._switch_output_interface_override = switch_output_interface

        # Single notification owner for the whole runtime. If the caller did
        # not supply one, construct a fresh instance.
        self.notification_center: NotificationCenter = (
            notification_center if notification_center is not None else NotificationCenter()
        )

        # Halt flag set by enter_failed_runtime(...). While True, train ticks
        # are skipped (only simulation_time still advances). The typed fatal
        # payload is retained for inspection until manual cleanup releases
        # the gate.
        self.failed_runtime: bool = False
        self.failed_runtime_payload: Optional[Any] = None
        # simulation_time at which the halt was entered, for the UI banner.
        self.failed_runtime_at: Optional[float] = None

        # The hardware I/O halt is its own latch, on the same gate as
        # `failed_runtime` but with a different cause and a different way out
        # (design_new.md "Power", Hardware I/O failure). A fatal conflict is
        # something the operator sorts out on the layout and clears in place; a
        # hardware failure revoked output permission, and only activation grants
        # that again, so reactivation is the whole of its recovery surface.
        self.hardware_failure: bool = False
        # The halt retains only the bool latch above. It keeps no ErrorRecord
        # or timestamp: the diagnostic lives in
        # the event stream, and every "what went wrong here" read filters that
        # one stream (design_new.md "Error events"; "Power", Hardware I/O
        # failure — "It retains no ErrorRecord or second diagnostic payload
        # across ticks").

        # TrainController + TrainPhysics instances (shared across components)
        self.controllers: Dict[str, TrainController] = {}
        self.physics_instances: Dict[str, TrainPhysics] = {}

        # Trains this tick's global detector read attributed a block entry or
        # vacate to. Rewritten every `_detect_block_occupancy()`; read by the
        # Observe gate for a still-OFF transaction (design_new.md, "Movement feedback
        # runs only when movement is calculated; detection feedback always runs").
        self._detected_change_train_ids: Set[str] = set()

        # Blocks the detector confirmed clear while their holding train still
        # stands on them (its calculated tail has not reached the block's exit
        # seam) — a detection fault, not a vacate. Maps block_id -> holding
        # train_id. The block is held (occupied_by and voltage kept) and
        # re-offered each read until the tail reaches the seam or current
        # returns (design_new.md "Block occupancy detection", A clear read from
        # under the train is a detection fault; A held clear is re-offered until
        # the tail reaches the seam). Used to report the fault once per block.
        self._held_clear_blocks: Dict[str, str] = {}

        # Blocks whose attribution result on the last read was a `conflict`:
        # more than one train touches the block and none has it reserved, so it
        # goes to no train. A conflict block stays confirmed occupied and owned
        # by no train, so it never reads as a fresh entry again; it is matched
        # again on every read until at most one train touches it
        # (design_new.md "Block occupancy detection", An overrun entry or a
        # conflict raises the emergency cut-out).
        self._conflict_blocks: Set[str] = set()

        # Wall time of the previous pass of the runtime loop, running or
        # stopped. Every pass's `occupied_seconds` accrual measures the unscaled
        # wall seconds against it — running and stopped alike, never the
        # playback-scaled physics delta — see
        # `_accrue_block_occupied_seconds()`. `None` until the first pass.
        self._last_pass_wall_time: Optional[float] = None

        # Initialize components with shared controllers. The block/switch power
        # output interfaces come from the session-build override (real backend)
        # or default to the simulated interfaces (sim mode) — design_new.md
        # "Session mode and operating environment", "the backend is chosen once when the session is built".
        self.power_controller = (
            BlockPowerController(
                network, output_interface=self._block_output_interface_override
            )
            if self._block_output_interface_override is not None
            else BlockPowerController(network)
        )
        network.power_controller = self.power_controller
        self.switch_output_interface = (
            self._switch_output_interface_override
            if self._switch_output_interface_override is not None
            else SimulatedSwitchDriver()
        )
        network.switch_output_interface = self.switch_output_interface
        # The switch-side counterpart of `power_controller`: it owns the throw
        # output and the switch's rail feed, and is the only caller of
        # `SwitchOutput` (design_new.md "Power", Switch power-controller role).
        self.switch_power_controller = SwitchPowerController(
            network,
            output_interface=self.switch_output_interface,
            sim_controller=self,
        )
        network.switch_power_controller = self.switch_power_controller

        # Block occupancy detection: the global per-block current sensor plus the
        # debounce/attribution detector (design_new.md "Block occupancy
        # detection"). The simulated sensor reads the live spans of all
        # controllers; `self.controllers` is shared by reference and populated as
        # trains are added, so the detector sees every train without re-wiring.
        self.input_interface = (
            self._input_interface_override
            if self._input_interface_override is not None
            else SimulatedOccupancyDriver(network, self.controllers)
        )
        network.input_interface = self.input_interface
        self.block_occupancy_detector = BlockOccupancyDetector(
            network=network,
            input_interface=self.input_interface,
            controllers=self.controllers,
            notify=lambda operation, message: self.notification_center.emit(
                source="BlockOccupancyDetector",
                operation=operation,
                kind=Kind.WARNING,
                message=message,
            ),
        )
        # Unidentified trains (design_new.md "Unidentified trains"): the Observe owner of
        # every confirmed entry that matches no existing train. It creates and
        # retires the unidentified train `Train` records; their block commits then run through
        # the ordinary per-train chain like any other train's.
        self.unidentified_trains = UnidentifiedTrainTracker(network, self)
        # Attribution needs the tracker's undetected-track walk to decide whether
        # a train could physically be standing on a block it reserved, so the
        # detector gets it as soon as the tracker exists (design_new.md "Power",
        # Attribution).
        self.block_occupancy_detector.reachable_blocks = (
            self.unidentified_trains.reachable_blocks
        )
        # Adjacency says two things are next to each other and nothing about
        # which way either is going, so every adjacency match is also gated on
        # whether the train could be entering the block at all — moving, and
        # driving toward it (design_new.md "Power", Attribution is gated on
        # plausibility).
        self.block_occupancy_detector.can_enter = self._train_could_be_entering
        # The clock the unidentified train tracker's hold windows are measured against — see
        # `_unidentified_clock` for why it is wall-clock and not `simulation_time`.
        # Replaceable so a test can age a window out without sleeping.
        self.unidentified_clock: Callable[[], float] = time.monotonic
        # The `AppState.operating_time` clock, for the route layer's
        # `"operating"` timing basis (design_new.md "Clocks"): monotonic
        # seconds of *operation*, advanced once per running pass by that pass's
        # playback-scaled delta and by nothing else. It is accumulated rather
        # than read off a real clock, so a scene that runs 400 ticks in a
        # millisecond advances it by the 40 s those ticks represent.
        #
        # The accumulator belongs on `AppState`, because a layout load builds a
        # fresh `SimulationController` and must not reset the clock a schedule
        # holds against. These two seams are how it gets there: `AppState`
        # rebinds both, and a controller built without one — a unit test, a
        # scenario runner — keeps the local accumulator below.
        self._operating_time_local: float = 0.0
        self.operating_clock: Callable[[], float] = (
            lambda: self._operating_time_local
        )
        self.operating_clock_advance: Callable[[float], None] = (
            self._advance_local_operating_time
        )
        self.route_executor = RouteExecutor(network, path_dispatcher, self.controllers)
        self.speed_profile_registry = network.speed_profiles
        self.simulation_time: float = 0.0
        # Speed calibration (design_new.md "Speed calibration"): measures a
        # locomotive's response and writes it into its SpeedProfile. Wired with
        # this controller so progress/results surface on the command_result
        # stream owned by NotificationCenter.
        from speed_calibration import SpeedCalibration

        self.speed_calibration = SpeedCalibration(self)

        # Bumped by every safe-state entry. A `CalibrationRun` captures it when
        # it opens and compares the live value before issuing each command and
        # before accepting each sample, so a session stop or any other
        # safe-state request abandons the pass instead of letting it measure
        # across the gap (design_new.md "Speed calibration").
        self.safe_state_generation: int = 0
        # The Real Hardware loss-of-control watchdog's trip record, wired from
        # the active block/switch output interface in `_bind_interfaces`. `None`
        # in Simulation / Virtual Hardware, which start no watchdog.
        self._watchdog_trip_record: Optional[WatchdogTripRecord] = None

        # Set by the layout emergency cut-out and cleared by Reset; while set
        # the session reads Halted for `layout_emergency_cut_out`
        # (design_new.md "Session state"). The halts and the cut-out each write
        # their own flag and never `AppState.simulation_running`, which is only
        # the Play/Pause bit.
        self.layout_cut_out_active: bool = False

        # Wire back-references so peer components can reach the notification
        # center, the route executor, and each other.
        if self.path_dispatcher is not None:
            # Inject PathDispatcher's named dependencies rather than a general
            # back-reference (design_new.md, "Preemption" and "Dispatcher/route-executor inputs"): the route executor (its
            # cancellation/completion callbacks + preemption/reversal resolvers),
            # the occupant-controller lookup registry, and the event-log sink.
            self.path_dispatcher.wire_dependencies(
                route_executor=self.route_executor,
                notification_center=self.notification_center,
                controllers=self.controllers,
            )
        if self.route_executor is not None:
            self.route_executor.simulation_controller = self

        # Wire BlockManager so it can report fatal physical-entry conflicts
        # via enter_failed_runtime(...) and read network topology directly.
        # Per design_new.md, expose `block_manager`, `switch_manager`, and
        # `resource_coordinator` on SimulationController as the canonical
        # access points for external callers.
        if self.network is not None:
            ensure_managers = getattr(
                self.network, "_ensure_resource_managers", None
            )
            if callable(ensure_managers):
                ensure_managers()
        self.block_manager = (
            getattr(self.network, "block_manager", None) if self.network is not None else None
        )
        self.switch_manager = (
            getattr(self.network, "switch_manager", None) if self.network is not None else None
        )
        self.resource_coordinator = (
            getattr(self.network, "resource_coordinator", None)
            if self.network is not None
            else None
        )
        # Inject the narrow callbacks each manager needs in place of a whole
        # SimulationController back-reference (design_new.md "BlockManager" /
        # "Switch-manager model": a manager never receives or looks up
        # SimulationController). The notification callback is bound to
        # NotificationCenter with the manager's own source; the fatal callback
        # to enter_failed_runtime(...). SwitchManager also gets the
        # operating-environment gate and the active-path lookup for a fatal
        # payload's incoming_path_id. An unissued throw is reported by
        # SwitchPowerController, not SwitchManager (design_new.md "Power", An
        # unissued throw is fatal to a hardware-backed session).
        if self.block_manager is not None:
            self.block_manager.network = self.network
            self.block_manager.notification_emitter = make_notification_emitter(
                self.notification_center, "BlockManager"
            )
            self.block_manager.fatal_conflict_reporter = self.enter_failed_runtime
            self.block_manager.active_path_getter = self._active_path_for_train
        if self.switch_manager is not None:
            self.switch_manager.network = self.network
            self.switch_manager.notification_emitter = make_notification_emitter(
                self.notification_center, "SwitchManager"
            )
            self.switch_manager.fatal_conflict_reporter = self.enter_failed_runtime
            self.switch_manager.environment_active_getter = (
                lambda: self.environment_active
            )
            self.switch_manager.active_path_getter = self._active_path_for_train

    def _active_path_for_train(self, train_id: str):
        """Return `train_id`'s active `Path`, or `None`.

        Handed to `SwitchManager` as its `active_path_getter` so it can fill a
        fatal switch-conflict payload's `incoming_path_id` without holding a
        SimulationController back-reference. Resolves the dispatcher live and
        degrades to `None` when there is no dispatcher or no lookup, matching
        the manager's old inline behaviour."""
        dispatcher = self.path_dispatcher
        if dispatcher is None:
            return None
        getter = getattr(dispatcher, "get_active_path", None)
        return getter(train_id) if callable(getter) else None

    # =========================================================================
    # Top-level update loop (per design_new.md "Core Runtime Loop")
    # =========================================================================

    def update(
        self, delta_seconds: float, operating_delta_seconds: float | None = None
    ) -> None:
        """Advance the simulation by one tick.

        `delta_seconds` is the physics integration step: wall delta x
        simulation_speed, capped per pass at `MAX_SIM_DELTA_SECONDS` so one
        stalled pass cannot integrate a giant jump. `operating_delta_seconds`,
        when given, is the UNCAPPED wall delta x simulation_speed used to
        advance `AppState.operating_time` (design_new.md "Clocks"): the
        operating clock is a faithful wall clock a schedule measures against, so
        it is never capped. Callers that do not distinguish (tests, scenario
        replay) omit it and the operating clock advances by `delta_seconds`.

        Per design_new.md per-train tick transaction (Observe, Plan, Action):
          1. Advance simulation_time.
          2. The global detector read runs once per block; RouteExecutor.update(...)
             refreshes route-managed scheduling state; order_runnable_trains(...)
             returns the ordered runnable train ids.
          3. OBSERVE (batch): for every train, integrate movement and commit
             occupancy via _observe_train(...), before any train plans, so each
             train plans against where every other train really is.
          4. For each ordered train, PLAN (RouteExecutor planning, skipped while
             controller is OFF) then ACTION (emergency-request step, control, voltage, and
             teardown via _act_train(...)) — one train fully commits before the
             next train's Plan.
          5. After all trains have committed, refresh signals from blocks.
        """
        # Consume a loss-of-control watchdog trip first: it is the sole
        # operation allowed through the fatal/hardware gate below (design_new.md
        # step `ee19`), because a failed safe-state drop must still escalate to
        # hardware failure (design_new.md "Power", Trip-record consumption
        # escalates a failed drop to hardware failure). It reads neither clock,
        # so running it ahead of the clock advance changes nothing on a normal
        # pass.
        self.consume_watchdog_trip()

        # A fatal physical-entry conflict (`failed_runtime`) halts train ticks
        # (Observe/Plan/Action skipped) in every environment; its release path is
        # the explicit `clear_failed_runtime()`. A hardware I/O failure
        # (`hardware_failure`) halts only a hardware-backed environment:
        # Simulation keeps running with a latched hardware_failure, so an operator
        # whose hardware broke can work in Simulation while repairing it, then
        # reactivate and explicitly clear the latch (design_new.md "Power",
        # Recovery is reactivation followed by an explicit clear; Simulation is
        # independent of Hardware Setup). When either gate applies neither clock
        # advances (design_new.md step `ee19`): the clock advances sit past the
        # gate, so a halted pass freezes both `simulation_time` and
        # `AppState.operating_time`.
        if not self.environment_active or self.failed_runtime:
            return
        if self.hardware_failure and self.operation_mode is not OperationMode.SIMULATION:
            return

        self.simulation_time += delta_seconds
        # One running pass of the runtime loop; a stopped pass calls
        # `poll_hardware_occupancy()` instead and adds nothing, which is what
        # keeps an operator's stop from consuming a dwell (design_new.md
        # "Clocks"). The operating clock advances by the uncapped wall x speed
        # delta when the caller supplies it, so a hitch or a high playback
        # multiplier never makes a schedule measure short.
        self.operating_clock_advance(
            operating_delta_seconds
            if operating_delta_seconds is not None
            else delta_seconds
        )

        # The per-train lookahead memo is a tick-local optimization: it is only
        # ever read/written while this flag is True, so every between-tick /
        # paused / UI caller of get_lookahead() recomputes live and can never
        # see a stale payload. `bump_lookahead_world_version()` is called at each
        # phase boundary below so a cached payload is never reused across a state
        # change within the tick either. The try/finally guarantees the flag is
        # cleared even if a phase raises (the runtime loop logs and continues).
        net = self.network
        net._lookahead_memo_active = True
        net.bump_lookahead_world_version()
        try:
            # Observe - global block-current read + debounce + attribution
            # (design_new.md "Block occupancy detection"). One world-wide
            # current read per tick reflecting end-of-last-tick positions. Confirmed
            # entered/vacated blocks are attributed to trains and stashed per
            # controller; each train commits its slice in Observe through the normal
            # on_controller_progress flow, and the precise position is snapped to the
            # block seam where the hardware leads it.
            self._detect_block_occupancy()
            # A detector read that failed just escalated to hardware failure and
            # revoked output; a hardware-backed session must not run Plan/Action
            # this tick (design_new.md "Power", The affected hardware-backed
            # operation loop stops).
            if self.occupancy_is_real and self.hardware_failure:
                return
            # An open speed-calibration pass reads the calibration train's
            # detected-entry slice here, inside the global detector-read window:
            # attribution has just written it, and the train's own movement
            # callback has not yet folded and cleared it (design_new.md "Speed
            # calibration", The clock is two confirmed entries). It is a read
            # against an existing detector-read handoff and writes nothing.
            self.speed_calibration.observe_detected_entries()
            net.bump_lookahead_world_version()

            # Refresh route-managed scheduling state (time advancement only).
            self.route_executor.update(delta_seconds)
            net.bump_lookahead_world_version()

            # Build candidate train list and let RouteExecutor decide ordering.
            candidate_train_ids = list(self.network.trains.keys())
            ordered_train_ids = self.route_executor.order_runnable_trains(
                candidate_train_ids
            )

            # Build the per-tick contention snapshot + meet assignment before any
            # train's Plan (design_new.md "Contention snapshot" + "Meet
            # coordination"): both read tick-start committed reservation state
            # plus the runnable order, and Plan step 66aa consumes them.
            self.route_executor.build_tick_coordination(ordered_train_ids)

            # === OBSERVE (all trains, one batch) ===
            # Before any train plans, work out where every train is and write it
            # down: integrate movement over the elapsed time, then commit
            # occupancy and recognize a normal stop arrival. Committing every
            # train's position before planning means each train plans against
            # where every other train really is, not a stale guess for the trains
            # ordered later this tick (design_new.md Core Runtime Loop, step 2).
            for train_id in ordered_train_ids:
                train = self.network.trains.get(train_id)
                if train is None:
                    continue
                self._observe_train(train, delta_seconds)
                # A fatal physical-entry conflict during an Observe commit freezes
                # the runtime "at the moment of detection" (design_new.md `b9a2`):
                # abort the rest of this tick so all state is left as it was for
                # inspection until the operator releases the runtime failure.
                # A latched hardware_failure does NOT abort here: in Simulation
                # mode the top-of-update gate lets the session run with it set
                # (work in Simulation while repairing hardware), and in
                # Virtual/Real mode that same gate already returned before the
                # loop. failed_runtime is the only halt raisable mid-tick.
                if self.failed_runtime:
                    return
            # Observe moved heads and committed occupancy — invalidate before Plan.
            net.bump_lookahead_world_version()

            # === PLAN then ACTION, one train at a time in priority order ===
            # One train's Plan and Action fully commit before the next train's
            # Plan begins (design_new.md Core Runtime Loop, step 4).
            for train_id in ordered_train_ids:
                train = self.network.trains.get(train_id)
                if train is None:
                    continue

                # Plan. Skip while the controller is OFF; the Action command
                # drain must run first so that power_on_cmd() can be accepted.
                # Also skip a ghost (presence_unknown): its teardown is in flight
                # through the power-off + inclusive-cancel latches Observe wrote,
                # and Action is the sole teardown committer — Plan must not plan
                # or acquire for a record no occupancy backs (design_new.md
                # "Power", Presence unknown).
                controller = self._get_or_create_controller(train)
                if (
                    controller.powered
                    and not controller.presence_unknown
                ):
                    self.route_executor.tick_train_plan(train_id, delta_seconds)
                # Plan may have reserved blocks, locked/aligned switches, or
                # (re)built this train's path — invalidate before Action reads.
                net.bump_lookahead_world_version()

                self._act_train(train, delta_seconds)
                # Action set the track voltage and consumed teardown latches —
                # invalidate before the next train (and next tick) reads.
                net.bump_lookahead_world_version()

                # Only a mid-tick failed_runtime aborts; a latched
                # hardware_failure is runnable in Simulation (see the Observe
                # guard above).
                if self.failed_runtime:
                    return

            # Unidentified train identity follows connectivity, and only the committed
            # `Block.occupied_by` says which blocks each unidentified train ended the tick
            # holding — so the unidentified train reconciliation (merge, split, gap hold,
            # retirement) runs once here, after every train's transaction
            # (design_new.md "Unidentified trains").
            self.unidentified_trains.reconcile(self._unidentified_clock())

            self._settle_after_commits()

            self._accrue_block_occupied_seconds()

            net.bump_lookahead_world_version()
        finally:
            net._lookahead_memo_active = False

    def _advance_local_operating_time(self, delta_seconds: float) -> None:
        """Advance the fallback operating clock for a controller with no
        `AppState` wired. See `operating_clock` in `__init__`."""
        self._operating_time_local += max(delta_seconds, 0.0)

    def _accrue_block_occupied_seconds(self) -> None:
        """Maintenance counter: add this pass's occupied time to every block a
        train is standing on (design_new.md "Maintenance counters").

        It is an observing measurement — a locomotive standing on a block wears
        that block whether or not the session is running — so both the running
        tick and the stopped detector pass call it, immediately after the
        de-energize drain and the signal refresh.

        Both passes add the unscaled wall seconds elapsed since the previous
        pass — never the playback-scaled `delta_seconds` physics and route
        bookkeeping consume — because a block's physical wear tracks real
        elapsed time regardless of playback speed. Every pass stamps the wall
        time, so a pass always measures from the immediately preceding pass, and
        the first pass of all accrues nothing because it has no interval behind
        it.
        """
        now = time.time()
        previous = self._last_pass_wall_time
        self._last_pass_wall_time = now
        if previous is None:
            return
        delta_seconds = max(0.0, now - previous)
        if delta_seconds <= 0.0:
            return
        for block in self.network.blocks.values():
            if block.occupied_by is not None:
                block.occupied_seconds += delta_seconds

    def _unidentified_clock(self) -> float:
        """The clock `UnidentifiedTrainTracker` measures its hold windows against.

        Wall-clock monotonic, not `simulation_time`. An unidentified train is a physical thing
        rolling on physical track: the gap hold it sits in while it crosses an
        undetected switch tile elapses in real seconds, and it goes on elapsing
        while the app is stopped — `poll_hardware_occupancy` keeps detecting and
        committing there, and an unidentified train that lost its blocks under a frozen
        `simulation_time` would never age out of the hold and never retire.

        The tracker only ever uses this to compare two of its own stamps
        (how long a hold has run, which of two unidentified trains is older), never against
        anything on the timeline `simulation_time` measures, so the swap is
        contained to it. Unidentified trains also exist only in real mode — the simulated
        detector builds current from live spans, so it produces no unattributed
        entries — and there the two clocks track each other anyway.

        `unidentified_clock` is the seam a test drives to age a hold window out without
        sleeping; nothing in the runtime replaces it.
        """
        return self.unidentified_clock()

    def _settle_after_commits(self, write_outputs: bool = True) -> None:
        """Drain the de-energize queue and refresh signals, after every train's
        Action has committed.

        `write_outputs` is False while no environment owns the outputs — in
        Setup, where the Setup tools own every write. The drain then issues no
        device write: it clears the queued block IDs as already handled, since
        each tool completes the safe state when it ends (design_new.md "Running
        and stopped", The stopped pass, step 3).

        After every train has emitted its voltages, drop any block that left the
        energized set this pass to 0V. Two things put a block in that state and
        both are drained here, because neither sees the other:

          - its holder cleared (tail vacate / reservation release), which
            `BlockManager` queues as it happens and re-checks is still unheld;
          - it stayed reserved but stopped being the train's *next* reserved
            block, which only the emission side knows — a train that reversed on
            its corridor, or a replan that reordered one. `BlockManager` counts
            such a block as held, so nothing would ever queue it and it would
            hold its last commanded voltage for good (design_new.md "Power",
            Only the next reserved block is pre-energized).

        The all-blocks sweep is the fallback for a runtime with no BlockManager
        wired. Signals are then refreshed so they reflect every block state
        change committed above.

        Shared by the tick and by `poll_hardware_occupancy`: a detector-driven
        commit changes block state exactly like a moving train's does, so the
        voltage and signal consequences must follow it either way.

        **The drain tests both views of occupancy, never one alone.** The final
        hold test is the same four-term emission set the drive side computes: the
        calculated span, the confirmed-occupancy blocks (`Block.occupied_by`),
        the pending-entry block, and the one next reserved block. It tests the
        calculated span in addition to debounced `Block.occupied_by`, never
        instead of it, so neither view of occupancy alone can zero a rail: a
        detector that briefly reads clear under a stationary locomotive still
        leaves the block held by its span, and a block held only by confirmed
        occupancy the span does not cover is held by that term. Because
        `_calculated_hold_blocks()` is exactly the set the emission side drives,
        a block under a span is never driven and zeroed on the same tick
        (design_new.md "Power", The drain tests both views of occupancy, never
        one alone).
        """
        held = self._calculated_hold_blocks()
        dropped: Set[str] = set()
        for physics in self.physics_instances.values():
            dropped |= physics.take_blocks_dropped_from_energized()
        if self.block_manager is not None:
            queued = set(self.block_manager.take_blocks_to_deenergize())
            if write_outputs:
                for block_id in sorted(queued | dropped):
                    if block_id in held:
                        continue
                    self.power_controller.set_block_voltage(block_id, 0.0)
        elif write_outputs:
            self.power_controller.deenergize_unheld_blocks(skip=held)

        self.network.update_signals_from_blocks()

    def _train_could_be_entering(self, train_id: str, block_id: str) -> bool:
        """Whether `train_id` could be the current that just appeared on
        `block_id` (design_new.md "Power", Attribution is gated on plausibility).

        The physics instance owns the answer — it holds the span, the direction
        of travel, and the speed. This is the seam that hands it to the detector,
        which knows only blocks. An unidentified train is never a candidate: it
        is never energized and integrates no motion, so it enters nothing under
        its own power, and its own blocks arrive through its detected span.
        """
        train = self.network.trains.get(train_id)
        if train is None or getattr(train, "is_unidentified", False):
            return False
        physics = self.physics_instances.get(train_id)
        if physics is None:
            return False
        return physics.could_be_entering(block_id)

    def _entries_for_adjacent_trains(
        self, unmatched: Set[str], claimed: Dict[str, Set[str]]
    ) -> Tuple[Dict[str, Set[str]], Dict[str, List[str]]]:
        """Give each unmatched confirmed entry to the one ordinary train that
        touches it, and report every entry more than one train touches as a
        conflict (design_new.md "Block occupancy detection", Attribution).

        Returns `(entries, conflicts)`: `entries` maps train_id -> the blocks it
        gained here, and `conflicts` maps block_id -> the sorted candidate train
        ids. A conflict block goes to no train — not to any candidate and not to
        an unidentified train — and each candidate keeps the blocks it already
        held. A block no train touches is left in neither map, for the
        unidentified-train step.

        A locomotive standing across a block boundary draws current on both
        blocks. The far one matches no reservation and no span, so without this
        it would be read as a second object and handed an unidentified train — which then
        de-energizes it, because an unidentified train is never energized, stranding the very
        locomotive the app is driving. Adjacency is the same connectivity walk
        unidentified train identity uses, and `BlockOccupancyDetector` owns it:
        only the first block across undetected track counts, so a block with
        empty blocks between it and every train is still an unidentified train.

        `claimed` is what attribution has already matched this tick. It is
        passed in, and grown as this step resolves blocks of its own, so a train
        that crossed several blocks between two detector reads is still read as
        one train: each block it just gained is evidence for the next. Matching
        runs in rounds, each round testing every pending block against the
        evidence the round started with, so the result does not depend on the
        order the blocks are visited in. A conflict block is never evidence.
        """
        detector = self.block_occupancy_detector
        entries: Dict[str, Set[str]] = {}
        conflicts: Dict[str, List[str]] = {}
        evidence = {train_id: set(blocks) for train_id, blocks in claimed.items()}
        pending = set(unmatched)
        while pending:
            touching = {
                block_id: detector.touching_trains_for_block(block_id, evidence)
                for block_id in sorted(pending)
            }
            resolved = False
            for block_id, train_ids in touching.items():
                if len(train_ids) == 1:
                    train_id = train_ids[0]
                    entries.setdefault(train_id, set()).add(block_id)
                    evidence.setdefault(train_id, set()).add(block_id)
                    pending.discard(block_id)
                    resolved = True
                elif len(train_ids) > 1:
                    conflicts[block_id] = train_ids
                    pending.discard(block_id)
            if not resolved:
                break
        return entries, conflicts

    def _standing_conflict_blocks(self, detector) -> Set[str]:
        """The conflict blocks to match again on this read.

        A conflict block stays confirmed occupied and owned by no train, so it
        never shows up as a fresh entry again; it is re-offered to attribution on
        every read instead (design_new.md "Block occupancy detection", An
        overrun entry or a conflict raises the emergency cut-out). A block that
        has read clear, or that some train now holds, is no longer a conflict
        and is dropped.
        """
        confirmed = detector.confirmed_occupied
        return {
            block_id
            for block_id in self._conflict_blocks
            if block_id in confirmed
            and getattr(self.network.blocks.get(block_id), "occupied_by", None)
            is None
        }

    def _log_unmatched_attribution(
        self, unmatched: Set[str], claimed: Dict[str, Set[str]]
    ) -> None:
        """Record why each controllable train declined a confirmed entry that is
        about to be handed to an unidentified train.

        Purely observational — it writes no model state and changes no
        attribution. It exists to trace a ghost that should have been an existing
        train: for each still-unmatched block it reports, per controllable train,
        the one reason that train did not claim it — no calculated span at all
        (`no_span`), no held/span block next to the entry (`not_adjacent`), or a
        neighbour but the plausibility gate said no (`plausibility_veto`). The
        distinction between `no_span` and the others is the whole point: a train
        whose span went `None` around adoption, presence-unknown, or a stalled
        tick declines every entry under it and loses each to a ghost.
        """
        center = getattr(self, "notification_center", None)
        if center is None:
            return
        detector = self.block_occupancy_detector
        reachable = getattr(detector, "reachable_blocks", None)
        for block_id in sorted(unmatched):
            neighbours = set(reachable(block_id)) if reachable is not None else set()
            reserved_for = getattr(
                self.network.blocks.get(block_id), "reserved_for_train", None
            )
            rows: List[Dict[str, object]] = []
            for train_id, controller in self.controllers.items():
                train = self.network.trains.get(train_id)
                if train is None or getattr(train, "is_unidentified", False):
                    continue
                span = getattr(
                    getattr(controller, "physics_sync", None), "current_span", None
                )
                span_blocks = (
                    list(span.occupied_blocks or []) if span is not None else None
                )
                held = sorted(
                    bid
                    for bid, block in self.network.blocks.items()
                    if getattr(block, "occupied_by", None) == train_id
                )
                covered = set(span_blocks or []) | set(held) | set(
                    claimed.get(train_id, set())
                )
                adjacent = bool(neighbours & covered)
                try:
                    can_enter = (
                        bool(detector.can_enter(train_id, block_id))
                        if detector.can_enter is not None
                        else None
                    )
                except Exception:
                    can_enter = "error"
                if span is None:
                    reason = "no_span"
                elif not adjacent:
                    reason = "not_adjacent"
                elif can_enter is False:
                    reason = "plausibility_veto"
                else:
                    reason = "unexpected_would_match"
                rows.append(
                    {
                        "train_id": train_id,
                        "reason": reason,
                        "span_blocks": span_blocks,
                        "held_blocks": held,
                        "adjacent": adjacent,
                        "can_enter": can_enter,
                    }
                )
            try:
                center.emit(
                    source="SimulationController",
                    operation="_diagnose_unmatched_entry",
                    kind=Kind.DEBUG,
                    message=(
                        f"confirmed entry on {block_id} matched no controllable "
                        f"train; per-train decline reasons recorded"
                    ),
                    entities=[],
                    payload={
                        "block": block_id,
                        "neighbours": sorted(neighbours),
                        "reserved_for": reserved_for,
                        "trains": rows,
                    },
                    event_time=time.time(),
                )
            except Exception:
                pass

    def _log_braking_approach(
        self, train: Train, controller: TrainController, physics
    ) -> None:
        """Record a routed, moving train's profile and its braking approach so an
        overrun can be tied to an uncalibrated speed profile.

        Purely observational — writes no state. Fires only for a train that holds
        a live path plan and is moving (or already past its authority), so an
        idle train never logs. It answers, per tick: is the train on a default
        (uncalibrated) profile, and is it still carrying speed as its head reaches
        and crosses the authority end? A fast head at the authority end on an
        uncalibrated profile is the overrun this exists to confirm.
        """
        center = getattr(self, "notification_center", None)
        if center is None:
            return
        projection = getattr(controller, "path_projection", None)
        plan = getattr(projection, "received_path_plan", None)
        if plan is None:
            return
        try:
            speed = float(
                getattr(controller.physics_sync, "current_speed_kmh", 0.0) or 0.0
            )
            # A cut-out train is stopped (speed 0) but is an abnormal state worth
            # logging; an overrun now raises the cut-out rather than a physics
            # overrun test (design_new.md "Emergency cut-out").
            cut_out = bool(getattr(controller.eb, "cut_out", False))
            if speed <= 0.1 and not cut_out:
                return
            profiles = getattr(self.network, "speed_profiles", None)
            profile = profiles.get(train.speed_profile_id) if profiles else None
            calibrated = (
                getattr(profile, "calibrated_at", None) is not None
                if profile is not None
                else None
            )
            try:
                target = float(controller.target_speed)
            except Exception:
                target = None
            try:
                voltage = float(physics.compute_track_voltage(controller, profile))
            except Exception:
                voltage = None
            head_tile = controller._get_head_tile()
            head_block = (
                self.network.tile_to_block_id(head_tile[0], head_tile[1])
                if head_tile
                else None
            )
            span = getattr(controller.physics_sync, "current_span", None)
            span_blocks = list(span.occupied_blocks or []) if span else None
            authority_end = getattr(plan, "reservation_end_block_id", None)
            center.emit(
                source="SimulationController",
                operation="_diagnose_braking_approach",
                kind=Kind.DEBUG,
                message=(
                    f"{train.train_id} approach: speed={speed:.1f} "
                    f"target={target} v={voltage} head={head_block} "
                    f"authority_end={authority_end} past_authority={past} "
                    f"calibrated={calibrated}"
                ),
                entities=[train_ref(train.train_id)],
                payload={
                    "train_id": train.train_id,
                    "speed_profile_id": getattr(train, "speed_profile_id", None),
                    "profile_calibrated": calibrated,
                    "current_speed_kmh": speed,
                    "commanded_target_speed": target,
                    "track_voltage": voltage,
                    "head_block": head_block,
                    "span_blocks": span_blocks,
                    "authority_end_block": authority_end,
                    "head_past_authority": past,
                },
                event_time=time.time(),
            )
        except Exception:
            pass

    def _calculated_hold_blocks(self) -> Set[str]:
        """Every block some controllable train energizes this tick, by
        calculation.

        The union, over every train the app can steer, of the four emission-set
        terms: its calculated span's `occupied_blocks`, the blocks confirmed
        occupied by it (`Block.occupied_by`), its `pending-entry block`, and the
        one next block of its reserved corridor — exactly the emission set
        `TrainPhysics` drives each tick (design_new.md "Power", Energized set:
        where the train stands plus one block ahead). The confirmed-occupancy
        term is here because a block attribution gave a train for neighbouring
        its span is in neither the span nor the reservations, yet the emission
        side drives it, so the drain must hold it too or it would be driven and
        zeroed on the same tick (design_new.md "Power", The drain tests both
        views of occupancy, never one alone). The rest of the corridor is
        deliberately absent: a reserved block the train has not reached yet is
        not energized, so it must not be held against the drain either. An
        unidentified train and a ghosted train (`presence_unknown`) are excluded
        because they command no `track_voltage`, so a block held only by such a
        train is driven to `0`. So are the blocks a train's emission left
        unpowered because they lie past its authority end
        (`TrainPhysics.blocks_past_authority`): the emission side does not drive
        them, so the drain must not hold them, or a block powered on the tick
        before the train ran past its authority would keep that voltage
        (design_new.md "Power", A block past the authority end is never
        energized).
        """
        held: Set[str] = set()
        for train_id, physics in self.physics_instances.items():
            train = self.network.trains.get(train_id)
            if train is None or getattr(train, "is_unidentified", False):
                continue
            controller = self.controllers.get(train_id)
            if getattr(controller, "presence_unknown", False):
                continue
            train_held: Set[str] = set()
            span = getattr(
                getattr(controller, "physics_sync", None), "current_span", None
            )
            if span is not None:
                train_held.update(span.occupied_blocks or [])
            train_held.update(
                block_id
                for block_id, block in self.network.blocks.items()
                if getattr(block, "occupied_by", None) == train_id
            )
            pending = physics.pending_entry_block
            if pending is not None:
                train_held.add(pending)
            next_reserved = physics.next_reserved_block
            if next_reserved is not None:
                train_held.add(next_reserved)
            held |= train_held - physics.blocks_past_authority
        return held

    def poll_hardware_occupancy(self) -> None:
        """Read the block detectors and commit what they say, with the
        simulation stopped (design_new.md "Block occupancy detection").

        Real hardware does not stop reporting when the operator presses Stop. A
        loco lifted onto the rails, a hand-pushed cut of wagons, a train left
        standing from the last session — all of it is on the track and the
        detectors see it, so the app has to show it. Without this the '165 is
        only ever scanned from inside `update()`, which the runtime loop skips
        while stopped, and every block reads FREE no matter what is standing on
        it — including at boot, which is stopped by definition.

        This is the tick's Observe read and commit, and nothing else: no Plan
        planning, no Action, no physics integration. Stopped has
        to keep meaning stopped — queued operator commands still wait for the
        resume, no route is planned, no train is moved. What does run is the
        path design_new.md already defines for an occupant the app does not
        drive ("Running and stopped"): the global detector read attributes the confirmed
        transitions, each affected train enqueues a detected-blocks-only report,
        and Observe commits that report alone (`observing_only`).

        No latch is consumed here and no arrival is recognized, because both
        are authored work (design_new.md "Latch Table": "A stopped session
        consumes no latch"). Each latch keeps its payload across the stop and
        is consumed by the first pass of the resumed session whose gate holds.
        The `occupied_seconds` accrual does run: a locomotive standing on a
        block wears it whether or not the session is running.

        Sim mode is left alone. Its simulated detector derives current from the
        live spans, so a poll with no motion between it and the last one can
        only re-report what is already committed — real work for a guaranteed
        no-op, on every idle pass.

        It runs whenever real detectors are bound, including in Setup, where no
        environment is active but the session's `OccupancyInput` stays open
        (design_new.md "Hardware setup", The detector pipeline keeps running
        throughout Setup). With no environment active the outputs belong to the
        Setup tools, so the drain writes nothing.
        """
        if self.operation_mode is OperationMode.SIMULATION or not self.occupancy_is_real:
            return
        if self.runtime_halted:
            return

        net = self.network
        net._lookahead_memo_active = True
        net.bump_lookahead_world_version()
        try:
            self._detect_block_occupancy()
            # A failed detector read escalated to hardware failure; a stopped
            # hardware-backed poll then commits nothing (design_new.md "Power",
            # The affected hardware-backed operation loop stops).
            if self.occupancy_is_real and self.hardware_failure:
                return
            # No calibration read here. A stopped pass runs the observing steps
            # and nothing else, so no physics callback follows to consume a stashed
            # slice; stashing one would leave a stale entry for the first tick
            # after the resume to mistake for a fresh confirmation.
            net.bump_lookahead_world_version()

            # Only trains the global detector read attributed something to: every
            # other train's slice is empty, so Observe would have nothing to commit for it.
            for train_id in sorted(self._detected_change_train_ids):
                train = self.network.trains.get(train_id)
                controller = self.controllers.get(train_id)
                if train is None or controller is None:
                    continue
                # The Observe movement callback would normally turn the slice
                # into a progress report; there is no movement here, so the
                # controller is asked for that report directly.
                controller.build_detected_progress_report(time.time())
                self._commit_train_observe(controller, train, observing_only=True)
                net.bump_lookahead_world_version()
                if self.runtime_halted:
                    return

            self.unidentified_trains.reconcile(self._unidentified_clock())
            self._settle_after_commits(write_outputs=self.environment_active)
            self._accrue_block_occupied_seconds()
            net.bump_lookahead_world_version()
        finally:
            net._lookahead_memo_active = False

    def enter_safe_state(self) -> None:
        """Put all hardware outputs into their safe state — every block track
        voltage at 0 and every switch motor de-energized (design_new.md Power,
        Hardware interface boundary).

        This is the sequence itself. Points are dropped before block voltages,
        and each half is its controller's non-recording device write, so this
        call touches no recorded model field and is safe from any thread —
        including the Real Hardware `OutputWatchdog` daemon, which is the sole
        direct caller. Every runtime-thread caller goes through
        `enter_output_safe_state()` instead, which advances the safe-state
        generation and reconciles the recorded fields afterwards.
        """
        if self.switch_power_controller is not None:
            self.switch_power_controller.de_energize_device_outputs()
        if self.power_controller is not None:
            self.power_controller.zero_device_outputs()

    def enter_output_safe_state(self) -> None:
        """The runtime-thread entry to the safe state.

        Per design_new.md "Power" (Safe state / fail-safe) this wrapper:

          1. advances `safe_state_generation`.
          2. ends every open calibration run (design_new.md "What ends a
             calibration run"): a stop cuts the rails mid-crossing, so no timing
             may span one. A finished pass is a held result and is kept.
             `OutputSafety` never calls into calibration; this wrapper does.
          3. delegates to `enter_safe_state()`, the whole de-energize sequence.
          4. reconciles the recorded model fields synchronously, immediately
             after the sequence returns. That reconciliation is always the
             runtime thread's, never the sequence's, which is what lets the
             watchdog run the same sequence off-thread without racing a model
             write.

        Every caller holds `AppState.simulation_lock` or runs inside a tick.
        """
        self.safe_state_generation += 1
        self.speed_calibration.end_open_runs(
            "calibration ended: the session stopped or its outputs dropped"
        )
        try:
            self.enter_safe_state()
        finally:
            if self.power_controller is not None:
                self.power_controller.reconcile_recorded_voltages()

    def layout_emergency_cut_out(self) -> CommandResult:
        """The layout emergency cut-out, the one kill switch for the whole
        layout in every environment (design_new.md "Emergency cut-out", Layout
        emergency cut-out). Its button is labeled Emergency stop.

        Runs between ticks under `AppState.simulation_lock`. The caller accepts
        it while the session is Live or Paused; in Setup it closes the Setup
        tools instead, and while Halted it changes nothing. In order it:

          1. enters the safe state, so every block voltage goes to 0, every
             point motor de-energizes, and every open calibration run ends;
          2. forces every train's retained speed to 0, because a model
             locomotive stops as soon as its power is removed;
          3. cancels every live path with `error.origin = "eb"`, which releases
             its reservations, locks and switch claims; each train keeps its
             route assignment and its `RUNNING` run;
          4. cuts out every powered train through `eb.activate_cut_out()`; with
             no live path left, the EB effects latch no cancel, and a train
             already under EB keeps its latch;
          5. sets `layout_cut_out_active`, so the session reads Halted for
             `layout_emergency_cut_out` until Reset.

        Steps 2 and 3 do at once what Action would do, because no Action pass
        runs while the session is Halted.

        A safe state that cannot be completed does not stop the rest: every
        train is still stopped and cut out, and the failure is then latched as
        a hardware failure (design_new.md "Power", Hardware I/O failure is
        fatal to the physical session).
        """
        safe_state_failures: List[str] = []
        try:
            self.enter_output_safe_state()
        except SafeStateError as exc:
            safe_state_failures = list(exc.failures)
        for controller in self.controllers.values():
            controller.physics_sync.current_speed_kmh = 0.0
        dispatcher = self.path_dispatcher
        if dispatcher is not None:
            live_path_ids = [
                path_id
                for path_id, path in dispatcher.paths.items()
                if path.status == PathStatus.ACTIVE
            ]
            for path_id in live_path_ids:
                dispatcher.cancel_path(
                    path_id,
                    ErrorRecord("layout emergency cut-out", "eb"),
                    kind=Kind.WARNING,
                )
        cut_out = []
        for train_id in sorted(self.controllers):
            controller = self.controllers[train_id]
            if controller.powered and controller.eb.activate_cut_out().success:
                cut_out.append(train_id)
        self.layout_cut_out_active = True
        if safe_state_failures:
            self.enter_hardware_failure(
                HardwareFailure(
                    component="OutputSafety",
                    operation="layout_emergency_cut_out",
                    message="; ".join(safe_state_failures),
                )
            )
        self.notification_center.emit(
            source="SimulationController",
            operation="layout_emergency_cut_out",
            kind=Kind.WARNING,
            message=(
                f"layout emergency cut-out: every output is off and "
                f"{len(cut_out)} train(s) are cut out; Reset releases them"
            ),
            entities=[],
            event_time=time.time(),
        )
        return CommandResult(
            True, f"Layout emergency cut-out raised for {len(cut_out)} train(s)"
        )

    def reset_layout_cut_out(self) -> CommandResult:
        """Reset: the way back from Halted for `layout_emergency_cut_out`
        (design_new.md "Emergency cut-out", Reset).

        Runs between ticks under `AppState.simulation_lock`; the caller accepts
        it only in that session state. In order it:

          1. releases the EB on every powered train that holds one and is not
             ghosted — every such release passes its conditions, because the
             cut-out stopped every train and left none holding a live path. A
             ghosted train refuses `eb.release()`, so Reset leaves it alone;
          2. discards every pending emergency request through
             `phase_latches.discard_emergency_request()`, so a request the
             stopped pass latched while Halted (a locomotive pushed by hand into
             the next block) does not cut the train out on the first Live tick;
          3. clears `layout_cut_out_active`.

        Every train then reads the mode it had, and nothing moves until the
        operator acts.
        """
        if not self.layout_cut_out_active:
            return CommandResult(False, "There is no layout emergency cut-out to reset")
        released, refused = [], []
        for train_id in sorted(self.controllers):
            controller = self.controllers[train_id]
            if not controller.powered or not controller.eb.active:
                continue
            if getattr(controller, "presence_unknown", False):
                continue
            result = controller.eb.release()
            (released if result.success else refused).append(train_id)
        for controller in self.controllers.values():
            controller.route_hooks.phase_latches.discard_emergency_request()
        self.layout_cut_out_active = False
        message = f"Reset released {len(released)} train(s)"
        if refused:
            message += f"; still under EB: {', '.join(refused)}"
        self.notification_center.emit(
            source="SimulationController",
            operation="reset_layout_cut_out",
            kind=Kind.INFO if not refused else Kind.WARNING,
            message=message,
            entities=[],
            event_time=time.time(),
        )
        return CommandResult(True, message)

    def hold_all(self) -> List[str]:
        """Hold all: ATO stop on every train that reads `ATO` (design_new.md
        "Hold all").

        Runs between ticks under `AppState.simulation_lock`, in every session
        state and environment. Each train gets exactly what its own ATO stop
        does — it brakes to a stand, keeps its route and its reservation, and
        `/ato/go` resumes it — and reports the same command result. A ghosted or
        unidentified train accepts no operator command, and a train in any
        other status is left alone. Returns the IDs of the trains it stopped.
        """
        from enums import TrainStatus

        stopped: List[str] = []
        for train_id in sorted(self.controllers):
            controller = self.controllers[train_id]
            train = self.network.trains.get(train_id)
            if getattr(controller, "presence_unknown", False):
                continue
            if train is not None and getattr(train, "is_unidentified", False):
                continue
            if controller.ops.train_status != TrainStatus.ATO:
                continue
            result = controller.commands.ato_stop()
            controller.commands.report_result("ato_stop", result)
            if result.success:
                stopped.append(train_id)
        return stopped

    def consume_watchdog_trip(self) -> None:
        """Consume a loss-of-control watchdog trip once, on the runtime thread.

        The watchdog daemon dropped the physical outputs to the safe state
        directly, below the model, and recorded only the transport result (it
        may not touch model state). The runtime owns the model-side work here: it
        advances `safe_state_generation` — the runtime wrapper the daemon
        bypassed would have — reconciles every block's recorded `track_voltage`
        to `0` so the model never keeps reporting a block energized past a
        watchdog-driven drop, and, when the drop itself failed, escalates to
        `hardware_failure`. A successful drop emits one warning and does not end
        the session (design_new.md "Power", Loss-of-control watchdog).
        """
        trip_record = self._watchdog_trip_record
        if trip_record is None:
            return
        failed = trip_record.consume()
        if failed is None:
            return
        if failed:
            # The drop itself failed — the rail may still be energized. Escalate
            # to the fatal hardware failure; `enter_hardware_failure(...)` re-runs
            # the runtime safe-state wrapper, which advances
            # `safe_state_generation` and reconciles the recorded voltages, and
            # latches the halt.
            self.enter_hardware_failure(
                HardwareFailure(
                    component="output_watchdog",
                    operation="enter_safe_state",
                    message=(
                        "loss-of-control watchdog could not complete the "
                        "safe-state drop; the rail may still be energized"
                    ),
                )
            )
            return
        # A successful drop protects the layout but does not end the session.
        # The daemon dropped the outputs through the non-recording device write,
        # so the runtime advances the generation that write bypassed, reconciles
        # every block's recorded track voltage to zero, and emits one warning.
        # It also ends every open calibration run, as the wrapper would have
        # (design_new.md "What ends a calibration run").
        self.safe_state_generation += 1
        self.speed_calibration.end_open_runs(
            "calibration ended: the loss-of-control watchdog dropped the outputs"
        )
        if self.power_controller is not None:
            self.power_controller.reconcile_recorded_voltages()
        self.notification_center.emit(
            source="SimulationController",
            operation="consume_watchdog_trip",
            kind=Kind.WARNING,
            message=(
                "loss-of-control watchdog drove every output to the safe "
                "state after the control loop stopped refreshing them"
            ),
            entities=[],
            event_time=time.time(),
        )

    def try_enter_safe_state(self) -> Optional[List[str]]:
        """`enter_safe_state()` for callers that must keep going when the rig
        cannot be commanded at all.

        Returns `None` when every output was safed, or the per-chip failures —
        `OutputSafety` attempts every chip before it raises, so the list is what
        did not answer rather than where it gave up. A transport that is gone
        cannot be de-energized by the caller giving up either, so the ones that
        take the rig somewhere better (revoking the session, handing the device
        to commissioning, rebuilding the chips) report the failure and carry on.
        Callers that own the outputs and can stop instead should keep using
        `enter_safe_state()` and let it raise.
        """
        try:
            self.enter_safe_state()
        except SafeStateError as exc:
            logger.exception("safe-state command failed")
            return list(exc.failures)
        except Exception as exc:  # noqa: BLE001 - the caller decides what it means
            logger.exception("safe-state command failed")
            return [str(exc)]
        return None

    def activate_interfaces(
        self,
        environment: OperationMode,
        input_interface: Optional[Any],
        block_output_interface: Optional[Any],
        switch_output_interface: Optional[Any],
    ) -> None:
        """Atomically replace the whole environment boundary while stopped."""
        self.enter_output_safe_state()
        self.environment_active = False
        self.operation_mode = operation_environment.parse(environment)

        self._bind_interfaces(
            input_interface, block_output_interface, switch_output_interface
        )
        # Binding the interfaces marks the environment active. The session
        # flags a successful activation clears are written once the railway
        # has been started over (see `finish_activation`).
        self.activation_issues = []
        self.environment_active = True

    def finish_activation(self) -> None:
        """Clear the controller's session flags after a successful activation
        has started the railway over (design_new.md "Session state", Activate
        succeeds).

        It clears the layout emergency cut-out. For a Virtual Hardware or Real
        Hardware activation it also clears `hardware_failure`: the activation
        rebuilt every interface and verified the environment, so its success
        shows the device answers again (design_new.md "Power", Recovery is
        reactivation). Activating Simulation leaves the latch set, because
        running Simulation proves nothing about the rig.
        """
        self.layout_cut_out_active = False
        if self.hardware_failure and self.operation_mode is not OperationMode.SIMULATION:
            self.hardware_failure = False
            self.notification_center.emit(
                source="SimulationController",
                operation="finish_activation",
                kind=Kind.INFO,
                message="hardware failure cleared by a successful activation",
                event_time=time.time(),
            )

    def release_interfaces(self) -> None:
        """Drop the hardware boundary once its controller handle is released.

        Every chip of a hardware-backed session is built on one open device, so
        closing that device leaves each driver holding a handle it can no longer
        write through. The next safe state — the one the following commissioning
        `enter` issues, or the one `activate_interfaces` issues on its way in —
        would then raise from `OutputSafety` rather than safe anything, and the
        session would stay stuck there until the process restarted. Rebinding to
        the simulated drivers is what keeps a released device out of that path.

        Does no I/O of its own: the caller drives the outputs safe while the
        device is still open and closes it, and this only replaces the boundary
        afterwards. The lifecycle flags stay the caller's, so whatever reason it
        recorded for the environment being inactive survives the swap.
        """
        self.environment_active = False
        self._bind_interfaces(None, None, None)

    def _bind_interfaces(
        self,
        input_interface: Optional[Any],
        block_output_interface: Optional[Any],
        switch_output_interface: Optional[Any],
    ) -> None:
        """Wire the three interfaces and the objects that read them, falling back
        to the simulated driver for each one passed `None`.

        Pure rebinding: no device I/O and no lifecycle flags — `activate_
        interfaces` and `release_interfaces` own those, and they disagree about
        every one of them.
        """
        # Wire the loss-of-control watchdog's trip record (Real Hardware only;
        # the block and switch drivers share one watchdog). The runtime consumes
        # it each tick in `consume_watchdog_trip()`.
        self._watchdog_trip_record = None
        for iface in (block_output_interface, switch_output_interface):
            watchdog = getattr(iface, "_watchdog", None) if iface is not None else None
            trip_record = (
                getattr(watchdog, "trip_record", None) if watchdog is not None else None
            )
            if trip_record is not None:
                self._watchdog_trip_record = trip_record
                break

        self.power_controller = (
            BlockPowerController(self.network, output_interface=block_output_interface)
            if block_output_interface is not None
            else BlockPowerController(self.network)
        )
        self.power_controller._sim_controller = self
        self.network.power_controller = self.power_controller
        self.switch_output_interface = (
            switch_output_interface
            if switch_output_interface is not None
            else SimulatedSwitchDriver()
        )
        self.network.switch_output_interface = self.switch_output_interface
        self.switch_power_controller = SwitchPowerController(
            self.network,
            output_interface=self.switch_output_interface,
            sim_controller=self,
        )
        self.network.switch_power_controller = self.switch_power_controller
        self.input_interface = (
            input_interface
            if input_interface is not None
            else SimulatedOccupancyDriver(self.network, self.controllers)
        )
        self.network.input_interface = self.input_interface
        self.block_occupancy_detector = BlockOccupancyDetector(
            network=self.network,
            input_interface=self.input_interface,
            controllers=self.controllers,
            reachable_blocks=self.unidentified_trains.reachable_blocks,
            can_enter=self._train_could_be_entering,
            notify=lambda operation, message: self.notification_center.emit(
                source="BlockOccupancyDetector",
                operation=operation,
                kind=Kind.WARNING,
                message=message,
            ),
        )

    def deactivate_environment(
        self, issues: List[Dict[str, Any]], environment: Optional[OperationMode] = None
    ) -> Optional[List[str]]:
        """Leave outputs safe and expose why the selected environment is inactive.

        Returns whatever `try_enter_safe_state` reported: `None` when the outputs
        are safe, else the chips that did not answer. The bookkeeping below runs
        either way, because it matters most exactly when the transport is gone —
        an operator left with a session that refuses to operate and says nothing
        about why has been given the worst of both. What the failure means is the
        caller's call; this one only makes sure it is not lost.
        """
        failures = self.try_enter_safe_state()
        if environment is not None:
            self.operation_mode = operation_environment.parse(environment)
        self.environment_active = False
        self.activation_issues = list(issues)
        return failures

    def _detect_block_occupancy(self) -> None:
        """The global detector read at the front of Observe: read every block's
        current detector, debounce, attribute confirmed transitions to trains,
        and stash each train's entered/vacated block slice for its Observe
        commit (design_new.md
        "Block occupancy detection"). The controller drains its slice in
        on_physics_updated and the existing on_controller_progress flow commits
        Block.occupied_by.

        Also records which trains this tick's read attributed a block change to,
        in `_detected_change_train_ids`. The movement callback consumes and clears
        the pending sets themselves, so that set is the only thing left by the
        occupancy-commit step that can answer "did the detector read attribute a
        block entry or vacate to this train this tick?" — the condition under which
        a still-OFF transaction must run its Observe commit anyway (design_new.md, "Movement
        feedback runs only when movement is calculated; detection feedback always runs").
        """
        detector = self.block_occupancy_detector

        # Adopt externally-committed occupancy (place_train / restore) as already
        # confirmed so a placed-on block does not read as a fresh entry. A
        # held-clear block is excluded: the detector genuinely reads it clear and
        # only the model still holds `Block.occupied_by` for it (design_new.md
        # "Block occupancy detection", A clear read from under the train is a
        # detection fault); re-seeding it would fight that clear read and reset
        # the debounce every tick.
        detector.seed_occupied(
            block_id
            for block_id, block in self.network.blocks.items()
            if block.occupied_by is not None and block_id not in self._held_clear_blocks
        )

        try:
            entered, vacated = detector.step()
        except Exception as exc:  # noqa: BLE001 - a failed detector read is fatal
            # A failed detector transaction is a hardware I/O failure that enters
            # the fail-closed hardware-failure lifecycle; a missing or unreadable
            # source is never treated as a clear block (design_new.md "Power",
            # Hardware I/O failure, and "Hardware setup", Missing never counts as
            # clear). In Simulation the model detector never raises, so a raise
            # there is a real defect that must surface, not be masked as hardware.
            if not self.occupancy_is_real:
                raise
            self.enter_hardware_failure(
                HardwareFailure(
                    component="block_occupancy",
                    operation="read",
                    message=(
                        f"block detector read failed: {type(exc).__name__}: {exc}"
                    ),
                )
            )
            return
        entered_by_train, vacated_by_train, unmatched = detector.attribute(
            entered, vacated, self._standing_conflict_blocks(detector)
        )

        # A confirmed entry next to a block an ordinary train already holds is
        # that train standing across the boundary — one train on two blocks, not
        # a new object (design_new.md "Attribution"). This runs before the unidentified train
        # step, so an unidentified train is minted only for current that is genuinely
        # disconnected from every train. A block more than one train touches is
        # a conflict: it goes to no train, and never to an unidentified train.
        conflicts: Dict[str, List[str]] = {}
        if unmatched:
            adjacent, conflicts = self._entries_for_adjacent_trains(
                unmatched, entered_by_train
            )
            for train_id, block_ids in adjacent.items():
                entered_by_train.setdefault(train_id, set()).update(block_ids)
                unmatched -= block_ids
            unmatched -= set(conflicts)

        # What is left matches no existing train and is not next to one, so it
        # belongs to an unidentified train (design_new.md "Attribution"):
        # `UnidentifiedTrainTracker` resolves it to an unidentified train `train_id` — a new unidentified train, or
        # one continuing its run across an undetected switch tile — and from here
        # the unidentified train is an ordinary train, so its slice is written and committed
        # exactly like every other train's.
        if unmatched:
            # Observational only: record why each controllable train declined
            # these entries before they become ghosts (no state written).
            self._log_unmatched_attribution(unmatched, entered_by_train)
            for unidentified_id, block_ids in self.unidentified_trains.attribute_unmatched_entries(
                unmatched, self._unidentified_clock()
            ).items():
                entered_by_train.setdefault(unidentified_id, set()).update(block_ids)

        # A block one train has just physically entered may still be reserved
        # ahead for a different train. That train's authority runs through an
        # occupied block, so it is cut back before it can be acted on — and
        # before Observe commits the entry, which would otherwise hit the
        # `reserved_for_other_train` fatal conflict.
        self._truncate_authority_at_occupied_reservations(entered_by_train)

        # An overrun entry raises the train's emergency cut-out on this same read
        # (design_new.md "Block occupancy detection", An overrun entry or a
        # conflict raises the emergency cut-out). Runs in Observe, before any
        # train plans; the overrun block still joins the block list and the head
        # still moves to its seam below.
        self._raise_overrun_cut_outs(entered_by_train)
        # A conflict raises the cut-out for every candidate, on this read and on
        # every later read while the block still resolves as a conflict.
        self._raise_conflict_cut_outs(conflicts)

        # A confirmed clear is a real vacate only once the holder's calculated
        # tail has reached the block's exit seam; a clear read from under a train
        # is a detection fault that holds the block (design_new.md "Block
        # occupancy detection", A clear read from under the train is a detection
        # fault). This gates the raw clears into the truly-vacated set, holds the
        # rest, reports each new fault once, and re-offers held clears.
        vacated_by_train = self._gate_confirmed_clears(vacated_by_train)

        self._detected_change_train_ids = set(entered_by_train) | set(vacated_by_train)

        # Reset every train's slice, then write this tick's attributed sets.
        for controller in self.controllers.values():
            controller.physics_sync.pending_detected_entered_blocks = set()
            controller.physics_sync.pending_detected_vacated_blocks = set()
        for train_id, block_ids in entered_by_train.items():
            controller = self.controllers.get(train_id)
            if controller is not None:
                controller.physics_sync.pending_detected_entered_blocks = set(block_ids)
        for train_id, block_ids in vacated_by_train.items():
            controller = self.controllers.get(train_id)
            if controller is not None:
                controller.physics_sync.pending_detected_vacated_blocks = set(block_ids)

        # An unidentified train's tail-vacate direction is the block it moved into, known right
        # here rather than buffered from a calculated tail exit (design_new.md
        # "Unidentified trains", Direction).
        for train_id in sorted(self._detected_change_train_ids):
            if self.unidentified_trains.is_unidentified(train_id):
                self.unidentified_trains.note_vacate_directions(
                    train_id,
                    entered_by_train.get(train_id, set()),
                    vacated_by_train.get(train_id, set()),
                )

        # Physical detection outranks the calculated position: snap each affected
        # train's span forward to the block seam where the detector leads it
        # (inert under the v1 simulated sensor; active with real hardware). For a
        # unidentified train the same call writes the span its detected blocks describe.
        for train_id in sorted(self._detected_change_train_ids):
            controller = self.controllers.get(train_id)
            train = self.network.trains.get(train_id)
            if controller is None or train is None:
                continue
            self._get_or_create_physics(train).reconcile_to_detected_occupancy(
                train, controller
            )

        self._update_presence_unknown(detector)

    def _gate_confirmed_clears(
        self, vacated_by_train: Dict[str, Set[str]]
    ) -> Dict[str, Set[str]]:
        """Split this tick's confirmed clears into the truly vacated and the
        held, and drive the detection-fault lifecycle.

        A block is truly vacated for its holder only once that train's calculated
        tail has reached the block's exit seam (or the train is ghosted, or it is
        an unidentified train, whose clears commit at once). A clear read while
        the train still stands on the block is a detection fault: the block is
        held — `Block.occupied_by` and its `track voltage` stay — reported once,
        and re-offered on every later read until the tail reaches the seam or the
        block reads current again (design_new.md "Block occupancy detection",
        A clear read from under the train is a detection fault; The fault is
        reported once per block; A held clear is re-offered until the tail
        reaches the seam)."""
        confirmed = self.block_occupancy_detector.confirmed_occupied

        # This tick's transitions plus the standing held-clear set, so a held
        # clear is re-offered every read. block_id -> holding train_id.
        candidates: Dict[str, str] = {}
        for train_id, block_ids in vacated_by_train.items():
            for block_id in block_ids:
                candidates[block_id] = train_id
        for block_id, train_id in list(self._held_clear_blocks.items()):
            if block_id in confirmed:
                # Current returned under the train: the fault is over, and a
                # later clear reports it afresh.
                del self._held_clear_blocks[block_id]
                continue
            block = self.network.blocks.get(block_id)
            if getattr(block, "occupied_by", None) != train_id:
                # The block was released elsewhere (ghost teardown, reservation
                # truncation); drop the stale hold.
                del self._held_clear_blocks[block_id]
                continue
            candidates.setdefault(block_id, train_id)

        truly: Dict[str, Set[str]] = {}
        for block_id, train_id in candidates.items():
            if self._tail_reached_exit_seam(train_id, block_id) or (
                not self._train_has_confirmed_evidence(train_id, confirmed)
            ):
                # Vacate: the tail has reached the seam, or no block in the
                # train's evidence set reads occupied so the train is ghosted and
                # the ghost teardown releases the rest (design_new.md "Block
                # occupancy detection", the ghosted branch).
                truly.setdefault(train_id, set()).add(block_id)
                self._held_clear_blocks.pop(block_id, None)
            elif block_id not in self._held_clear_blocks:
                # A clear read from under the train: hold the block, report once.
                self._held_clear_blocks[block_id] = train_id
                self._emit_lost_detection_fault(train_id, block_id)
        return truly

    def _tail_reached_exit_seam(self, train_id: str, block_id: str) -> bool:
        """Whether `train_id`'s calculated tail has reached `block_id`'s exit
        seam — the gate for vacating a block the detector reads clear. An
        unidentified train integrates no motion, so its clears commit at once
        (design_new.md "Block occupancy detection", An unidentified train's
        clears commit at once)."""
        if self.unidentified_trains.is_unidentified(train_id):
            return True
        train = self.network.trains.get(train_id)
        controller = self.controllers.get(train_id)
        physics = self.physics_instances.get(train_id)
        if train is None or controller is None or physics is None:
            return True
        return physics.tail_has_reached_exit_seam(train, block_id)

    def _train_has_confirmed_evidence(
        self, train_id: str, confirmed: Set[str]
    ) -> bool:
        """Whether any block in this train's evidence set — its span blocks plus
        the blocks it holds through `Block.occupied_by` — still reads confirmed
        occupied. When none does, the train is ghosted rather than standing on a
        held-clear block (design_new.md "Block occupancy detection", the ghosted
        branch, cross-referenced from Presence unknown)."""
        controller = self.controllers.get(train_id)
        if controller is None:
            return False
        span = controller.physics_sync.current_span
        evidence = set(span.occupied_blocks or []) if span else set()
        for block_id, block in self.network.blocks.items():
            if getattr(block, "occupied_by", None) == train_id:
                evidence.add(block_id)
        return bool(evidence & confirmed)

    @staticmethod
    def _conflict_condition_key(block_id: str) -> str:
        """The `condition_key` of a standing conflict on one block
        (design_new.md "Error events", Suppression mechanism)."""
        return f"block:{block_id}:attribution_conflict"

    def _raise_conflict_cut_outs(self, conflicts: Dict[str, List[str]]) -> None:
        """Latch a cut-out request for every candidate of every block whose
        attribution result this read is a `conflict` (design_new.md "Block
        occupancy detection", An overrun entry or a conflict raises the
        emergency cut-out).

        A conflict is a block that more than one train touches while no touching
        train has it reserved; it goes to no train. It stays confirmed occupied
        and owned by no train, so it resolves as a conflict again on every later
        read and this runs again for it, until an operator moves the trains so
        that at most one still touches it. There is no automatic resolution.

        The request is latched for a candidate on every such read unless that
        train is already under an emergency brake or already holds a cut-out
        request: Action step 01f2 would apply it as a no-op there (the
        `eb.active == False` gate), and a latch write is never suppressed, so
        latching it again would only repeat the latch event every read. A
        candidate whose brake the operator released while the conflict stands
        is therefore cut out again on the next read. Unlike an overrun entry, a
        conflict carries no speed-calibration exemption.

        One warning event names the conflict, with the block and every candidate
        on its entities. It is emitted on every read the conflict stands under
        the block's condition key, so the event stream keeps one event per
        conflict and a new one when its candidates change; the key is resolved
        on the first read the block is no longer a conflict.
        """
        for block_id in sorted(self._conflict_blocks - set(conflicts)):
            self.notification_center.resolve_condition(
                self._conflict_condition_key(block_id)
            )
        self._conflict_blocks = set(conflicts)
        for block_id in sorted(conflicts):
            candidates = conflicts[block_id]
            error = ErrorRecord(
                f"conflict on block {block_id} between {', '.join(candidates)}",
                "conflict",
            )
            for train_id in candidates:
                controller = self.controllers.get(train_id)
                if controller is None:
                    continue
                latches = controller.route_hooks.phase_latches
                if controller.eb.active or latches.emergency_request == "cut_out":
                    continue
                latches.set_emergency_request("cut_out", error)
            self.notification_center.emit(
                source="SimulationController",
                operation="_raise_conflict_cut_outs",
                kind=Kind.WARNING,
                message=(
                    f"block {block_id} is touched by more than one train "
                    f"({', '.join(candidates)}); it goes to none of them and "
                    f"emergency cut-out raised for each"
                ),
                entities=[block_ref(block_id)]
                + [train_ref(train_id) for train_id in candidates],
                error=error,
                event_time=time.time(),
                condition_key=self._conflict_condition_key(block_id),
            )

    def _raise_overrun_cut_outs(self, entered_by_train: Dict[str, Set[str]]) -> None:
        """Latch a cut-out request for every train whose attribution result this
        read is an overrun entry (design_new.md "Block occupancy detection", An
        overrun entry or a conflict raises the emergency cut-out).

        An overrun entry is a block credited to a train, on its head side, that
        is not reserved for that train, while the train holds a live plan — a
        train with no authority cannot overrun one. Observe latches
        `phase_latches.emergency_request = "cut_out"` for that train; the same
        tick's Action step `01f2` applies it through
        `TrainController.eb.activate_cut_out()`, which de-energizes its blocks
        and stops it at once. One warning event names the overrun, with the
        block and train ids on the entities; it is distinct from the
        span-correction event, which reports the forward-snap magnitude, so both
        may fire for the same entry.

        A train with an open speed-calibration pass is exempt: its overrun is a
        speed-only measurement fault that the pass itself stops (design_new.md
        "Speed calibration", A leg that overruns the stretch fails the pass), so
        no cut-out is raised for it.
        """
        calibration = getattr(self, "speed_calibration", None)
        for train_id in sorted(entered_by_train):
            if self.unidentified_trains.is_unidentified(train_id):
                continue
            controller = self.controllers.get(train_id)
            if controller is None:
                continue
            # A train with no authority cannot overrun one.
            if controller.path_projection.received_path_plan is None:
                continue
            # A calibration pass drives the train at an unmeasured step into a
            # stretch whose far end is dead rail by construction; the pass ends
            # as a measurement fault on this same read and is the stop, so it
            # raises no cut-out (design_new.md "Speed calibration").
            if (
                calibration is not None
                and calibration.get_propulsion_override(train_id) is not None
            ):
                continue
            overrun_blocks = sorted(
                block_id
                for block_id in entered_by_train[train_id]
                if getattr(
                    self.network.blocks.get(block_id), "reserved_for_train", None
                )
                != train_id
            )
            if not overrun_blocks:
                continue
            # A train already cut out, or already holding a cut-out request
            # from an earlier stopped-pass read, stays that way and re-emits no
            # event.
            latches = controller.route_hooks.phase_latches
            if controller.eb.active or latches.emergency_request == "cut_out":
                continue
            block_id = overrun_blocks[0]
            latches.set_emergency_request(
                "cut_out",
                ErrorRecord(
                    f"overrun entry by {train_id} onto block {block_id}",
                    "overrun_entry",
                ),
            )
            self.notification_center.emit(
                source="SimulationController",
                operation="_detect_block_occupancy",
                kind=Kind.WARNING,
                message=(
                    f"{train_id} overran its authority onto block {block_id}; "
                    f"emergency cut-out raised"
                ),
                entities=[block_ref(block_id), train_ref(train_id)],
                error=ErrorRecord(
                    f"overrun entry by {train_id} onto block {block_id}",
                    "overrun_entry",
                ),
                event_time=time.time(),
            )

    def _emit_lost_detection_fault(self, train_id: str, block_id: str) -> None:
        """Report a block that read clear while its holding train still stands on
        it — once per fault (design_new.md "Block occupancy detection", The fault
        is reported once per block)."""
        self.notification_center.emit(
            source="SimulationController",
            operation="_detect_block_occupancy",
            kind=Kind.WARNING,
            message=(
                f"block {block_id} read clear while {train_id} still stands on "
                f"it; holding the block until the train's tail clears it"
            ),
            entities=[block_ref(block_id), train_ref(train_id)],
            error=ErrorRecord(
                f"detection lost under {train_id} on block {block_id}",
                "detection_lost_under_train",
            ),
            event_time=time.time(),
        )

    def _truncate_authority_at_occupied_reservations(
        self, entered_by_train: Dict[str, Set[str]]
    ) -> None:
        """Cut a path's reservation back when another train physically takes one
        of its reserved blocks (design_new.md "Reservation truncation", Observe
        caller).

        A reservation is a claim on track the train has not reached yet, so the
        world can take it in between: an unidentified train appears on it, or a
        train running under its own reservation reaches it first. Reservation
        that runs through a block another train is standing on is not
        reservation, and acting on it drives one train at another. Which
        resolution runs depends on whether the reserving train had LOCKED the
        taken block:

        - Reserved-but-unlocked (the taken block is beyond the reserving train's
          `Path.lock_end_block_id`): the reservation is pulled back through
          `truncate_path_before_resource(...)`, which always succeeds (it falls
          back to the lock end), so ATC brakes that train to a stop at its
          shortened reservation end and the route re-extends once the block
          clears. No cancel.
        - Locked (the taken block is within the reserving train's braking
          distance): a graceful pull-back cannot leave room to stop, so the
          reserving train is emergency-braked. Only the taken block's
          reservation and lock are released so the foreign entry commits, and
          the rest of that train's reservation and lock come down through the
          ordinary EB cancellation once it stops.

        Either way the reservation is gone before Observe commits the entry,
        which is what keeps a legitimate arrival from being reported as a
        `reserved_for_other_train` fatal conflict.
        """
        dispatcher = self.path_dispatcher
        for train_id, block_ids in sorted(entered_by_train.items()):
            for block_id in sorted(block_ids):
                block = self.network.blocks.get(block_id)
                if block is None:
                    continue
                reserved_for = getattr(block, "reserved_for_train", None)
                if reserved_for is None or reserved_for == train_id:
                    continue

                reason = (
                    f"block {block_id} is physically occupied by {train_id}"
                )
                path_id = (
                    dispatcher.active_path_id_by_train(reserved_for)
                    if dispatcher is not None
                    else None
                )
                # design_new.md "Reservation truncation", Observe caller: split
                # on whether the reserving train had LOCKED the taken block. A
                # block reserved by a path is locked only by that same path, so
                # `locked_by_path is None` means reserved-but-unlocked.
                victim_path = (
                    dispatcher.paths.get(path_id)
                    if path_id and dispatcher is not None
                    else None
                )
                taken_index = (
                    dispatcher._forward_resource_index(victim_path, "block", block_id)
                    if victim_path is not None
                    else None
                )
                if (
                    path_id
                    and getattr(block, "locked_by_path", None) is None
                    and taken_index is not None
                ):
                    # Reserved-but-unlocked: pull the reservation back gracefully
                    # to before the taken block's occurrence. The call always
                    # succeeds (falling back to the lock end), so the reserving
                    # train brakes to its shortened reservation end — no cancel.
                    dispatcher.truncate_path_before_resource(
                        path_id, taken_index, reason
                    )
                    continue

                # Locked (the foreign train is inside the reserving train's
                # braking distance): a graceful pull-back cannot leave room to
                # stop, so the reserving train is emergency-braked. Release only
                # the taken block's reservation and lock through the coordinator
                # tail-release so the foreign entry commits cleanly; the rest of
                # the reserving train's reservation and lock come down through
                # the ordinary EB cancellation once it stops (design_new.md
                # "Reservation truncation", Observe caller, Locked branch).
                reserved_by_path = getattr(block, "reserved_by_path", None)
                if reserved_by_path:
                    if self.resource_coordinator is not None:
                        self.resource_coordinator.release_reservation_tail(
                            [block_id], [], reserved_by_path
                        )
                    elif self.block_manager is not None:
                        self.block_manager.release_path_block_reservations(
                            [block_id], reserved_by_path, completed=False
                        )
                controller = self.controllers.get(reserved_for)
                if controller is not None and path_id:
                    # Latch an emergency brake request on the reserving train
                    # (design_new.md "Reservation truncation", Observe caller,
                    # Locked branch). The same tick's Action step `01f2`
                    # applies it through the canonical EB activation effects,
                    # which latch `pending_path_cancel_request` with
                    # `error.origin = "eb"` for Action to commit once the train
                    # has stopped. This is what forces the reserving train to a
                    # stand; a soft cancel alone would not, and the train is
                    # inside its braking distance of an occupied block.
                    controller.route_hooks.phase_latches.set_emergency_request(
                        "brake",
                        ErrorRecord(
                            f"{reserved_for}'s locked block {block_id} was "
                            f"entered by another train",
                            "locked_foreign_entry",
                        ),
                    )

    def _update_presence_unknown(self, detector) -> None:
        """Flag each train whose calculated position no physical reading backs
        (design_new.md "Power", Presence unknown).

        A train's evidence set is every wired block it could show current on:
        its span's blocks plus the blocks it holds (`Block.occupied_by`),
        intersected with what the session actually detects. When that set is
        non-empty and every block in it reads confirmed clear, the locomotive
        has physically left everything the model claims for it — the span
        represents nothing detected, so the train may be gone. The UI shows a
        question mark and offers removal; nothing here removes anything.

        Inert cases stay False: a session that detects nothing physical (the
        simulated driver), a train standing only on unwired blocks (no reading
        to contradict it), and an unidentified train (its span IS its confirmed blocks, and a
        unidentified train with none is retired by `UnidentifiedTrainTracker`).
        """
        detected = detector.detected_block_ids()
        confirmed = detector.confirmed_occupied if detected else set()
        held_by_train: Dict[str, Set[str]] = {}
        if detected:
            for block_id, block in self.network.blocks.items():
                holder = getattr(block, "occupied_by", None)
                if holder is not None:
                    held_by_train.setdefault(holder, set()).add(block_id)
        for train_id, controller in self.controllers.items():
            unknown = False
            train = self.network.trains.get(train_id)
            if (
                detected
                and train is not None
                and not getattr(train, "is_unidentified", False)
            ):
                span = controller.physics_sync.current_span
                evidence = set(span.occupied_blocks or []) if span else set()
                evidence |= held_by_train.get(train_id, set())
                wired = evidence & detected
                unknown = bool(wired) and not (wired & confirmed)
            became_unknown = unknown and not controller.presence_unknown
            controller.presence_unknown = unknown
            if unknown:
                # Repeat the containment before Plan on every detector read,
                # not just the rising edge.  An API call between ticks may have
                # powered the record or attached fresh authority; none of that
                # may survive long enough to plan or move while no occupancy
                # backs the identifier.
                self._release_lost_train(
                    train_id, controller, notify=became_unknown
                )

    def _release_lost_train(
        self,
        train_id: str,
        controller: TrainController,
        *,
        notify: bool = True,
    ) -> None:
        """A train the detector no longer accounts for stops being predicted and
        stops holding anything (design_new.md "Power", Presence unknown).

        The calculated position is an estimate anchored to physical detection;
        with the anchor gone there is nothing to integrate forward and no
        grounds to keep resources from other trains. Observe does the two things
        it owns directly and defers the rest to Action, per the tick's single
        teardown-committer rule (design_new.md "Observe's only cross-stage latch
        fields", "Action is the sole explicit-cancel and teardown committer"):

        - **Direct in Observe:** freeze the record inert (speed to zero, drop any
          queued command / progress reports) so it neither integrates nor plans,
          and release every block it still holds through
          `BlockManager.release_train_occupancy(train_id)` — each such block
          reads confirmed clear, which is what raised the flag, so no detector
          state is contradicted, and the release queues those blocks for the
          de-energize drain.
        - **Latched for Action:** write the `power_off` latch and, when a live
          path exists, its inclusive `path_cancel_request`, through the canonical
          `_apply_power_off_request(...)` writer. Action then commits the whole
          teardown the same way it commits any rolling-train cancellation: the
          stopped-gated cancel releases the path's future reservations and switch
          locks, the power-off consumer writes the controller `OFF` and removes
          the route run, and the ghost's own Action voltage step de-energizes its
          retained span. Freezing the speed to zero first is what lets the
          stopped-gated cancel commit on this same tick.

        The span stays as the last estimate, so the question mark has a place on
        the layout; the record itself is kept for the operator to remove.
        """
        self._freeze_lost_train_controller(controller)

        # The canonical teardown for a train with no detector observation left
        # to wait for (design_new.md "BlockManager",
        # `release_train_occupancy(train_id)`): it drops every block the train
        # holds, leaves no CAUTION behind it, and re-evaluates the protected
        # chain so nothing stays held on this train's behalf.
        if self.block_manager:
            self.block_manager.release_train_occupancy(train_id)
        else:
            for block in self.network.blocks.values():
                if getattr(block, "occupied_by", None) == train_id:
                    block.release()

        # Initiate the Action-committed teardown by writing the power-off latch
        # (and its inclusive cancel when a live path exists). The rising edge
        # always writes it, even for a train already OFF with no live path: the
        # power-off consumer runs for an OFF train too and removes its route run
        # and assignment on this same tick's Action pass (design_new.md "Power",
        # Ghosted trains). On the later passes while the flag holds, skip the
        # write once the record is settled — OFF with no live authority — so a
        # ghost that sits unknown for many ticks does not re-emit the intent
        # every tick. A record re-powered or re-authorized between ticks is not
        # settled, so its teardown is re-initiated and Action commits it.
        dispatcher = self.path_dispatcher
        has_active_path = (
            dispatcher is not None
            and dispatcher.active_path_id_by_train(train_id) is not None
        )
        already_settled = (
            not controller.powered and not has_active_path
        )
        if notify or not already_settled:
            controller.commands._apply_power_off_request(
                reason="no occupancy backs this train's position"
            )

        if notify:
            self.notification_center.emit(
                source="SimulationController",
                operation="_release_lost_train",
                kind=Kind.WARNING,
                message=(
                    f"no occupancy detected under {train_id}; it was stopped "
                    "and its route, path, and blocks were released"
                ),
                entities=[train_ref(train_id)],
                event_time=time.time(),
            )

    @staticmethod
    def _freeze_lost_train_controller(controller: TrainController) -> None:
        """Keep an unconfirmed identifier inert until detection returns.

        Observe calls this on the rising edge and again every tick while
        ``presence_unknown`` remains true, which keeps the record at a stand for
        as long as no detector backs it. Operator commands to the train are
        refused at the request handler while the flag holds
        (design_new.md "Operator-command processing slot").

        This is Observe-only containment — it neither writes `state` nor clears
        the EB / ATO run flags. Those are the operational-state machine's and
        the EB / ATO sub-modules' to write, and they are committed in Action
        through `TrainController.complete_power_off()` when the Action power-off
        consumer runs the latch Observe wrote (design_new.md Module boundaries
        rules 1/2, "Action is the sole explicit-cancel and teardown committer").
        Freezing the speed to zero here is a physics-sync write, not a state
        write, and it is what lets the stopped-gated Action cancel commit on the
        same tick the flag rose.
        """
        controller.ops.handle_position = HandlePosition.N
        controller.manual.reset_power_percent_for_safety()
        controller.physics_sync.current_speed_kmh = 0.0
        controller.physics_sync.queued_progress_reports.clear()

    # =========================================================================
    # Per-train Observe and Action stages
    # (Plan runs between them via RouteExecutor.tick_train_plan)
    # =========================================================================

    def _observe_train(self, train: Train, delta_seconds: float) -> None:
        """Observe stage for one train: integrate its movement over the elapsed
        time, then commit occupancy.

        Runs for every train as a batch before any train plans, so planning sees
        every train's committed position (design_new.md Core Runtime Loop,
        Observe). The movement calculation (`TrainPhysics.integrate_movement`)
        and the occupancy commit both live here; setting the track voltage is
        Action. Observe finishes no route task: recognizing a stop arrival is
        Plan-settlement work in `RouteExecutor.tick_train_plan(...)`, not Observe
        (design_new.md Core Runtime Loop, step 2 and the Observe scope bullet).
        """
        controller = self._get_or_create_controller(train)
        physics = self._get_or_create_physics(train)

        # A question-mark identifier has no physical occupancy backing its
        # estimate. It is a diagnostic record, not a train to keep predicting:
        # do not integrate physics while presence is unknown. Observe clears the
        # flag automatically if detection returns, at which point the ordinary
        # transaction resumes from the retained last span.
        if controller.presence_unknown:
            self._freeze_lost_train_controller(controller)
            self._sync_persistent_train_state(train, controller)
            return

        # Movement half of the physics pass — computes motion and invokes
        # TrainController.on_physics_updated(...) at least once. The voltage half
        # (TrainPhysics.apply_track_voltage) is Action.
        physics.integrate_movement(delta_seconds)
        self._commit_train_observe(controller, train)

    def _act_train(self, train: Train, delta_seconds: float) -> None:
        """Action stage for one train: drain operator commands, decide control,
        set the track voltage that drives the next tick, then consume the
        teardown latches.

        Runs after this train's Plan (design_new.md Core Runtime Loop, Action).
        Calculating movement and committing occupancy are Observe work; Action
        never moves a span or writes `Block.occupied_by`.
        """
        controller = self._get_or_create_controller(train)
        physics = self._get_or_create_physics(train)

        # A ghost (presence_unknown) is frozen inert in Observe, but it still
        # runs Action: Action is the sole teardown committer, so the power-off +
        # inclusive-cancel latches Observe wrote are consumed here (the
        # stopped-gated cancel releases the path's reservations and switch locks,
        # the power-off consumer writes `OFF` and removes the route run), and the
        # ghost's own voltage step de-energizes its retained span through the
        # ordinary emission-set difference (design_new.md "Power", Presence
        # unknown; "Action is the sole explicit-cancel and teardown committer").
        # Propulsion stays neutral throughout because the power-off latch is a
        # zero ceiling in the Propulsion cap step.

        # The speed-calibration pass no longer advances here: it advances in Plan
        # at step `c9e1` (RouteExecutor.tick_train_plan), where it arms and tears
        # down each leg's one-off route (design_new.md "Speed calibration", Where
        # it runs). Action only reads the step's power back off the run through
        # get_propulsion_override(...) during the control decision below.
        # TrainController.tick(...) applies any emergency request Observe latched
        # (step `01f2`), then — through the early_action_callback, before its
        # control decision — consumes the stopped-gated
        # `pending_path_cancel_request` (design_new.md Action step `7b40`), and only then runs the control decision on that
        # post-cancel state. TrainPhysics.apply_track_voltage() then sets the
        # block track voltage that drives the next tick's Observe movement.
        controller.tick(
            delta_seconds,
            early_action_callback=lambda: self._consume_early_action_latches(
                controller, train
            ),
        )
        physics.apply_track_voltage()
        self._log_braking_approach(train, controller, physics)

        self._commit_train_act(controller, train)

    def _consume_early_action_latches(
        self, controller: TrainController, train: Train
    ) -> None:
        """Action early slot, run inside `TrainController.tick(...,
        early_action_callback=...)` before the control decision.

        Consumes the stopped-gated `pending_path_cancel_request` (design_new.md
        Action step `7b40`) before the control decision, so the decision
        evaluates the post-cancel state — a stopped train whose authority was
        revoked this tick reaches `READY` and drops its output on this tick
        rather than one tick later.
        """
        self._consume_pending_path_cancel_request(controller, train)

    def _commit_train_observe(
        self, controller: TrainController, train: Train, observing_only: bool = False
    ) -> None:
        """Observe-stage commit for one train: drain the progress mailbox to
        write occupancy and record this tick's head-entered blocks for Plan
        settlement (step `3e9b`).

        The occupancy commit is Observe work (design_new.md Observe, Plan, and
        Action Table, Observe row); Observe finishes no route task. The
        head-advance consumption (step `640a`) and the stop-arrival check (step
        `9d8b`) both run later, in the settlement half of
        `RouteExecutor.tick_train_plan(...)`. The stopped-simulation hardware
        poll runs only this commit — it has detector observations to write but no
        planning or command execution first (see `poll_hardware_occupancy`) — and
        passes `observing_only=True` to run step `3e9b` and nothing after it.

        If the controller is OFF and the global detector read attributed no block
        entry or vacate to this train this tick, there is no movement to commit and this is a
        no-op except the persistent-state sync (design_new.md, "Movement feedback runs
        only when movement is calculated; detection feedback always runs").
        The skip is about having no movement to commit, not about power: a train
        that moves without power — pushed by hand, or an occupant the app does
        not control — still has its block entries and vacates committed, and this
        is the only path that commits them.

        A stop the train reached while the session was stopped is a statement
        about live authority, which a stopped session exercises none of
        (design_new.md "Running and stopped"; "Latch Table": "A stopped session
        consumes no latch"). A stopped session runs only this Observe commit, not
        Plan, so the stop-arrival check — now Plan-settlement work — simply does
        not run until the session resumes; every latch keeps its payload across
        the stop and is consumed by the first pass of the resumed session whose
        gate holds. `observing_only` therefore returns right after step `3e9b`.
        """
        if (
            not controller.powered
            and train.train_id not in self._detected_change_train_ids
        ):
            self._sync_persistent_train_state(train, controller)
            return

        # 3e9b. Drain queued progress reports in FIFO order → occupancy commit.
        # Reset this train's within-tick head-entry record first, then let the
        # drain re-fill it: `on_controller_progress` commits occupancy and
        # records each mapped head-entry on `entered_blocks_this_tick` for this
        # train's Plan settlement, but finishes no route task here (design_new.md
        # "Observe finishes no route task"). The head-advance consumption (step
        # `640a`) and the stop-arrival check (step `9d8b`) both run later, in the
        # settlement half of `RouteExecutor.tick_train_plan(...)`.
        controller.physics_sync.entered_blocks_this_tick = []
        if self.path_dispatcher is not None:
            while controller.physics_sync.queued_progress_reports:
                report = controller.physics_sync.queued_progress_reports.pop(0)
                self.path_dispatcher.on_controller_progress(report)

        if observing_only:
            self._sync_persistent_train_state(train, controller)
            return

        # Sync only persistent/configuration state back to Train. Live state
        # (target_speed, speed_limit_reason, controller state, current_speed)
        # lives on the controller; consumers read from the controller directly.
        self._sync_persistent_train_state(train, controller)

    def _commit_train_act(
        self, controller: TrainController, train: Train
    ) -> None:
        """Action-stage teardown for one train: consume the teardown latches in
        canonical order, then re-evaluate the open lighting-voltage run.

        The `pending_path_cancel_request` latch is consumed earlier, in the
        Action early slot before the control decision (step `7b40`, see
        `_consume_early_action_latches`); the teardown latches consumed here are
        route-abort, power-off, and route-deactivate. Calculating movement and
        committing occupancy already happened in Observe; Action only tears down.

        These steps run for every train, `OFF` included (design_new.md
        "`SimulationController._act_train(...)` flow", step 6): each is gated
        only by its own latch and, for the teardown consumers, the inclusive
        cancel having been consumed. A teardown needs no power — it releases
        route state — so a latch written against an `OFF` train (a ghost rising
        edge, a route deletion, or a power-off left by activation) finishes on
        that train's next running Action without waiting for power-on.
        """
        # Consume the teardown latches in canonical order. The inclusive
        # `pending_path_cancel_request` they share was already consumed in the
        # Action early slot, before the control decision (step `7b40`).
        self._consume_pending_route_abort(controller, train)
        self._consume_pending_power_off(controller, train)
        self._consume_pending_route_deactivate(controller, train)

        # 26f5. Re-evaluate this train's open calibration run — a speed pass or a
        # lighting-voltage run. Ordered after the power-off / route-deactivate
        # consumption above so it observes this same pass's teardown before
        # deciding whether the run has ended (design_new.md "Speed calibration",
        # Where it runs): the run ends the same tick its condition is met, so an
        # ordinary power-off that drove the controller to OFF at step `886a` ends
        # the run here on that same Action pass.
        self._reevaluate_calibration_run(train)

        # Sync only persistent/configuration state back to Train.
        self._sync_persistent_train_state(train, controller)

    # =========================================================================
    # Action latch consumers (per design_new.md latch consumption rules)
    # =========================================================================

    def _consume_pending_path_cancel_request(
        self, controller: TrainController, train: Train
    ) -> None:
        """Consume `phase_latches.path_cancel_request` when the train is
        stopped.

        While the train is rolling the latch survives across ticks; releasing
        block reservations and switch locks ahead of a moving train is unsafe.
        When the train is stopped, dispatch the cancellation through
        PathDispatcher.cancel_path(...). If no active path exists the latch
        is still cleared on this pass with a single diagnostic.
        """
        if controller.route_hooks.phase_latches.path_cancel_request is None:
            return

        if not controller.is_stopped():
            # Latch survives: reconsider next tick once the train has stopped.
            return

        if self.path_dispatcher is None:
            controller.route_hooks.phase_latches.clear("path_cancel_request")
            return

        payload = controller.route_hooks.phase_latches.path_cancel_request
        controller.route_hooks.phase_latches.clear("path_cancel_request")

        active_path_id = self.path_dispatcher.active_path_id_by_train(train.train_id)
        if active_path_id is not None:
            # Step `389a`: the latch payload carries the writer's whole
            # `ErrorRecord` and its declared `kind`; forward both into the
            # commit. `cancel_path(...)`'s own event is this latch's
            # consumption event (design_new.md "Latch consumption events"), so
            # this step emits nothing itself on this branch.
            self.path_dispatcher.cancel_path(
                active_path_id,
                error=payload.error,
                kind=payload.kind,
                deviation=payload.deviation,
            )
        else:
            # No live path to cancel. Per design_new.md "Action latched-cancel
            # consumption" this step emits the consumption event itself here,
            # with `kind = "info"` and `error = None` — the latch found nothing
            # to do, which is not a failure — and passes `latch_event=True` so
            # the repeat suppression never drops it.
            self.notification_center.emit(
                source="SimulationController",
                operation="_consume_pending_path_cancel_request",
                kind=Kind.INFO,
                message=(
                    f"latched cancel for {train.train_id} found no active path; "
                    "the latch was cleared without a dispatcher call"
                ),
                entities=[train_ref(train.train_id)],
                error=None,
                payload={
                    "latch": "path_cancel_request",
                    "origin": (
                        payload.error.origin if payload.error is not None else None
                    ),
                },
                event_time=time.time(),
                latch_event=True,
            )

    def _consume_pending_route_abort(
        self, controller: TrainController, train: Train
    ) -> None:
        """Consume `phase_latches.route_abort` once the inclusive cancel has
        committed.

        Per design_new.md Latch Table: gated by
        ``phase_latches.path_cancel_request is None`` (the inclusive cancel
        has been consumed earlier in the same pass, or was never armed
        because no live path existed). Does NOT branch on
        active-path-emptiness — that decision was already owned by the
        inclusive-cancel consumer.
        """
        if not controller.route_hooks.phase_latches.route_abort:
            return

        if controller.route_hooks.phase_latches.path_cancel_request is not None:
            # Latch survives until the inclusive cancel has been consumed.
            return

        # `remove_train_route_run(...)` is this latch's consume-side emitter
        # (design_new.md "Latch consumption events"); this step clears the
        # field once that call returns.
        self.route_executor.remove_train_route_run(train.train_id)
        controller.route_hooks.phase_latches.clear("route_abort")

    def _consume_pending_power_off(
        self, controller: TrainController, train: Train
    ) -> None:
        """Consume `phase_latches.power_off` after the inclusive cancel has
        committed.

        Per design_new.md: the inclusive `phase_latches.path_cancel_request`
        was armed by `_apply_power_off_request()` and committed earlier in the
        same Action pass (or correctly skipped when no active path existed).
        This consumer performs only its own teardown — route-run removal plus
        controller-local OFF cleanup. It does NOT call
        `PathDispatcher.cancel_path(...)`.
        """
        if not controller.route_hooks.phase_latches.power_off:
            return

        # Per design_new.md Latch Table: gated by
        # `phase_latches.path_cancel_request is None`. The inclusive cancel
        # was armed by `_apply_power_off_request()` and either committed
        # earlier in this same Action pass or was never written (no live
        # path existed). If the inclusive cancel is still pending the train
        # was not stopped this tick — defer to a later one.
        if controller.route_hooks.phase_latches.path_cancel_request is not None:
            return

        self.route_executor.remove_train_route_run(train.train_id)

        # Controller-owned OFF cleanup. `complete_power_off()` is the single
        # OFF-writing entry point besides startup (design_new.md, "Power/mode command contracts"):
        # it sets `powered = False`, resets the span head frame to end_a, and
        # clears eb.active / commands.engaged. It does not touch
        # `pending_power_off`: this step clears it once the call returns, and
        # emits the latch's consume-side event (design_new.md "Latch consumption
        # events").
        controller.complete_power_off()
        controller.route_hooks.phase_latches.clear("power_off")
        controller.notifications.emit(
            f"power off committed for {train.train_id}",
            operation="_consume_pending_power_off",
            kind="info",
            payload={"latch": "power_off"},
            latch_event=True,
        )

    def _consume_pending_route_deactivate(
        self, controller: TrainController, train: Train
    ) -> None:
        """Consume `phase_latches.route_deactivate` once the inclusive cancel
        has committed.

        Per design_new.md Latch Table: gated by
        ``phase_latches.path_cancel_request is None``. Does NOT branch on
        active-path-emptiness — that decision was already owned by the
        inclusive-cancel consumer.
        """
        if not controller.route_hooks.phase_latches.route_deactivate:
            return

        if controller.route_hooks.phase_latches.path_cancel_request is not None:
            return

        # Per design_new.md ("Action latched-cancel consumption", the
        # `pending_route_deactivate` consumer): what this step does depends on
        # whether a RUNNING run exists when it begins.
        if self.route_executor.execution_state_for(train.train_id) is not None:
            # This consumer owns the teardown: remove the run, then re-apply
            # any held structural-edit re-assignment. As with route-abort,
            # `remove_train_route_run(...)` is the consume-side emitter.
            self.route_executor.remove_train_route_run(train.train_id)
            self.route_executor.apply_pending_reassign(train.train_id)
        else:
            # The operator/admin teardown wins: an earlier abort/power-off
            # consumer, a natural finish, or activation already ended the run,
            # or the train had no run yet. Never call
            # `remove_train_route_run(...)` here, so a replacement route an
            # earlier consumer just committed stays assigned.
            self.route_executor.settle_deactivate_without_run(train.train_id)
        controller.route_hooks.phase_latches.clear("route_deactivate")

    def _reevaluate_calibration_run(self, train: Train) -> None:
        """Action step `26f5` — end an open calibration run whose conditions no
        longer hold, for either run kind.

        `SpeedCalibration.run_still_valid(train_id)` is the one validity predicate
        for both an open speed pass and an open lighting-voltage run (design_new.md
        "Speed calibration", Where it runs). When it is false the run ends this
        same tick — a speed pass through `SpeedCalibration.abandon(...)`, a
        lighting run through `SpeedCalibration.cancel_lighting(...)`. It is a
        `SimulationController`-level step because the run registry lives at
        that level.
        """
        calibration = self.speed_calibration
        train_id = train.train_id
        speed_open = calibration.runs.get(train_id) is not None
        lighting_open = calibration.lighting_runs.get(train_id) is not None
        if not speed_open and not lighting_open:
            return
        if calibration.run_still_valid(train_id):
            return
        # The two run kinds are mutually exclusive by their opening
        # preconditions; each is ended through its own teardown.
        if speed_open:
            calibration.abandon(
                train_id, reason="calibration pass ended: conditions no longer hold"
            )
        if lighting_open:
            calibration.cancel_lighting(train_id)

    # =========================================================================
    # Persistent state sync (Train <- TrainController)
    # =========================================================================

    def _sync_persistent_train_state(
        self, train: Train, controller: TrainController
    ) -> None:
        """Sync only persistent/configuration state from controller to Train.

        Live state (target speed, speed-limit reason, controller operational
        state, current speed, lookahead) lives on the controller; consumers
        read it from the controller directly. This method intentionally does
        not write live mirror fields onto Train.
        """
        # No live-state mirror writes here. All persistent/config fields
        # (id, length, max_speed, route assignment) are owned by Train and
        # are not modified per-tick.
        return

    # =========================================================================
    # Controller / physics registry accessors
    # =========================================================================

    def _get_or_create_controller(self, train: Train) -> TrainController:
        """Get or create a TrainController for the given train.

        On first creation, optionally inherit a small set of bootstrap values
        from the Train entity so that demo_main / test setup that pre-arms a
        train still produces a controller wired to the same intent.
        """
        if train.train_id not in self.controllers:
            controller = create_controller_for_train(
                train_id=train.train_id,
                network=self.network,
                path_dispatcher=self.path_dispatcher,
                route_executor=self.route_executor,
            )
            # Wire the back-reference so the controller can reach the
            # NotificationCenter and other simulation services.
            controller.simulation_controller = self
            # Per design_new.md "Ownership split", live operational state
            # (`state`, `active_cab`, `commands.engaged`, `control_mode`) lives
            # exclusively on the controller; we no longer inherit it from
            # `Train`. Tests and demo bootstrap that need pre-armed state
            # configure it on the controller directly after this call.
            self.controllers[train.train_id] = controller
        return self.controllers[train.train_id]

    def _get_or_create_physics(self, train: Train) -> TrainPhysics:
        """Get or create a TrainPhysics for the given train."""
        if train.train_id not in self.physics_instances:
            controller = self._get_or_create_controller(train)
            self.physics_instances[train.train_id] = create_physics_for_train(
                train_id=train.train_id,
                network=self.network,
                controller=controller,
            )
        return self.physics_instances[train.train_id]

    def get_controller(self, train_id: str) -> Optional[TrainController]:
        """Get controller for a train, or None if not yet created."""
        return self.controllers.get(train_id)

    def get_or_create_controller(self, train_id: str) -> TrainController:
        """Public train-id-based accessor that returns the controller for the
        named train, creating it on first call.

        Raises:
            KeyError: If no train with that id exists in the network.
        """
        train = self.network.trains.get(train_id)
        if train is None:
            raise KeyError(f"Unknown train: {train_id}")
        return self._get_or_create_controller(train)

    def get_or_create_physics(self, train_id: str) -> TrainPhysics:
        """Public counterpart of :meth:`get_or_create_controller` for the
        TrainPhysics instance. Mirrors the same lookup semantics."""
        train = self.network.trains.get(train_id)
        if train is None:
            raise KeyError(f"Unknown train: {train_id}")
        return self._get_or_create_physics(train)

    # =========================================================================
    # Train lifecycle
    # =========================================================================

    def reposition_train(
        self, train: "Train", x: int, y: int, direction: "Direction"
    ) -> "TrainSpan":
        """Re-place an already-added `train` at tile (x, y) and reseed its span.

        Clears the train's currently-occupied blocks, calls
        `network.place_train(...)`, and writes the resulting span onto the
        train's controller. This is the canonical re-placement writer of
        `TrainController.physics_sync.current_span`; callers (e.g. the train
        route handlers) go through here rather than assigning the span
        directly. `add_train` owns the initial seed; this owns subsequent
        re-placement. Raises `ValueError`/`KeyError` from `place_train` for a
        tile that cannot host the train.
        """
        ctrl = self.get_controller(train.train_id)
        span = ctrl.get_current_span() if ctrl is not None else None
        if self.network.block_manager and span is not None:
            for block_id in span.occupied_blocks:
                self.network.block_manager.clear_block(block_id)
        new_span = self.network.place_train(train, x, y, direction)
        if ctrl is not None:
            ctrl.physics_sync.current_span = new_span
        return new_span

    def add_train(
        self,
        train_id: str,
        starting_block_id: str,
        initial_direction: str,
        max_speed_kmh: float,
        length_cm: float,
        train_type: Optional[str] = None,
        name: Optional[str] = None,
        initial_cab: Optional[Literal["A", "B"]] = None,
        start_tile: Optional[Tuple[int, int]] = None,
        roster_entry_id: Optional[str] = None,
    ) -> Train:
        """Sole entry point for adding a train at runtime.

        Synchronous all-or-nothing. Constructs the Train, places it on a tile
        of ``starting_block_id`` facing ``initial_direction``, registers the
        controller and physics instances, wires back-references, and emits a
        ``train_placed`` event-log notification (design_new.md "Event log").

        Args:
            train_id: Identifier for the new train.
            starting_block_id: Block id whose tiles host the placement.
            initial_direction: Initial heading: "NORTH", "EAST", "SOUTH", or
                "WEST" (case-insensitive).
            max_speed_kmh: Operational max speed in km/h.
            length_cm: Train length in centimeters.
            train_type: Optional train model / type label.
            name: Optional display name (also used for the locomotive id).
            initial_cab: Optional initial cab ("A" or "B"); set as a direct
                write before the first tick.
            start_tile: Optional explicit (x, y) within ``starting_block_id``
                to place the train head on. When None (default), uses the
                block's first canonical position. Used by demos that need
                to spawn a long train whose body extends along the rail in
                a direction that requires choosing a specific tile in a
                multi-tile platform block.

        Returns:
            The freshly created and registered ``Train``.

        Raises:
            ValueError: If a train with this id already exists, the block is
                unknown, or the train cannot be placed.
        """
        if train_id in self.network.trains:
            raise ValueError(f"Train {train_id!r} already exists")
        if starting_block_id not in self.network.blocks:
            raise ValueError(f"Unknown block: {starting_block_id!r}")

        # Roster import (design_new.md "Train Roster"): when the placement names
        # a roster entry, seed the placement defaults from it instead of blank
        # values. The entry is a template; the placed Train is a separate
        # runtime instance, so editing one after placement never changes the
        # other. The span-length and max-speed defaults are applied here; the
        # remaining config fields + the profile link are applied after the Train
        # is constructed, below.
        roster_entry = None
        roster = getattr(self.network, "train_roster", None)
        if roster_entry_id and roster is not None:
            roster_entry = roster.get_entry(roster_entry_id)
        if roster_entry is not None:
            if roster_entry.max_speed is not None:
                max_speed_kmh = float(roster_entry.max_speed)
            if roster_entry.consist_length_cm is not None:
                length_cm = float(roster_entry.consist_length_cm)

        # Resolve a starting tile inside the named block.
        start_positions = self.network.get_block_positions(starting_block_id)
        if not start_positions:
            raise ValueError(
                f"Block {starting_block_id!r} has no tiles to place a train on"
            )
        if start_tile is not None:
            if tuple(start_tile) not in {tuple(p) for p in start_positions}:
                raise ValueError(
                    f"start_tile {start_tile!r} is not a tile of block "
                    f"{starting_block_id!r}"
                )
            start_tile = tuple(start_tile)
        else:
            start_tile = start_positions[0]

        # Resolve direction enum.
        try:
            direction = Direction[initial_direction.upper()]
        except (AttributeError, KeyError):
            raise ValueError(
                f"Invalid initial_direction: {initial_direction!r}"
            )

        car = TrainCar(
            car_id=f"{train_id}_loco",
            car_type=(train_type or "LOCOMOTIVE"),
            length_cm=float(length_cm),
            max_speed=float(max_speed_kmh),
        )

        train = Train(
            train_id=train_id,
            consist=[car],
            max_speed=float(max_speed_kmh),
            model=(train_type or ""),
            car_number=(name or ""),
        )

        # Roster import: seed the remaining config fields + profile link from the
        # entry (design_new.md "Train Roster"). Setting `speed_profile_id` here
        # (when the linked profile exists) skips the default-profile creation
        # below, keeping the locomotive's measured curves tied to it across
        # placements.
        if roster_entry is not None:
            train.roster_entry_id = roster_entry.entry_id
            if roster_entry.min_radius_cm is not None:
                train.min_radius_cm = float(roster_entry.min_radius_cm)
            train.max_slope = float(roster_entry.max_slope)
            if roster_entry.speed_profile_id and hasattr(
                self.network, "speed_profiles"
            ):
                if (
                    self.network.speed_profiles.get(roster_entry.speed_profile_id)
                    is not None
                ):
                    train.speed_profile_id = roster_entry.speed_profile_id

        # Seed the train's default SpeedProfile pair so `power_percent` manual
        # mode has a defined power->speed mapping anchored to THIS train's
        # max_speed (design_new.md "SpeedProfile"): a model profile whose default
        # `power_speed_curve` runs 0% -> 0 km/h to 100% -> the train's configured
        # max_speed, plus an empty instance deriving from it, and the train names
        # the instance. Without this the train would share the module-global
        # DEFAULT_SPEED_PROFILE whose curve hardcodes 100% -> 100 km/h; naming an
        # instance also keeps calibration off any other train's record.
        if train.speed_profile_id is None and hasattr(
            self.network, "speed_profiles"
        ):
            from speed_profile import seed_default_profile_pair

            train.speed_profile_id = seed_default_profile_pair(
                self.network.speed_profiles, train_id, max_speed_kmh
            )

        # Register and place. place_train raises ValueError if the train does
        # not fit on available track.
        self.network.add_train(train)
        try:
            placed_span = self.network.place_train(
                train, start_tile[0], start_tile[1], direction
            )
        except ValueError:
            self.network.remove_train(train_id)
            raise

        # Refresh signals so the new occupancy is reflected. Signal refresh
        # is a UI projection only — emit a runtime notification and continue
        # if it fails so the train-add itself still succeeds.
        try:
            self.network.update_signals_from_blocks()
        except Exception as exc:
            try:
                self.notification_center.emit(
                    source="SimulationController",
                    operation="add_train",
                    kind=Kind.WARNING,
                    message=f"could not refresh signals after placing {train_id}",
                    entities=[train_ref(train_id)],
                    error=ErrorRecord(str(exc), "signal_refresh_failed"),
                    event_time=time.time(),
                )
            except Exception:
                logger.warning(
                    "update_signals_from_blocks() failed during add_train(%s); "
                    "NotificationCenter.emit() also failed",
                    train_id,
                    exc_info=True,
                )

        # Register controller and physics instances. add_train writes the
        # initial TrainController.physics_sync.current_span, derived directly
        # from the canonical span that place_train built, before the first tick
        # (design_new.md "Design Direction"). Subsequent re-placement of an
        # existing train goes through reposition_train, the only other writer
        # of that span; route handlers never assign it directly.
        controller = self.get_or_create_controller(train_id)
        self.get_or_create_physics(train_id)

        controller.physics_sync.current_span = placed_span

        # If an initial head end was requested, frame the span for it directly.
        # add_train runs before the first tick, so this bypasses the command
        # queue by design. There is no operator cab (design_new.md
        # "Reservation-side head derivation"); ``initial_cab`` "A"/"B" names the
        # physical end the placement span is framed head-forward for, and the
        # span carries no head label. A live authority reframes it on the first
        # driving tick; "A" is the span's default framing.
        if initial_cab == "B":
            controller._apply_head_frame("end_b")

        # An operational event (design_new.md "Event log"): `operation` names the
        # function that produced it, and it carries `kind = "info"` and no error.
        self.notification_center.emit(
            source="SimulationController",
            operation="add_train",
            kind=Kind.INFO,
            message=f"train {train_id} placed on {starting_block_id}",
            entities=[train_ref(train_id)],
            event_time=time.time(),
            payload={
                "starting_block_id": starting_block_id,
                "initial_direction": direction.name,
                "max_speed_kmh": float(max_speed_kmh),
                "length_cm": float(length_cm),
            },
        )
        return train

    def adopt_unidentified_train(
        self,
        unidentified_train_id: str,
        new_train_id: str,
        *,
        max_speed_kmh: float,
        length_cm: float,
        direction: str,
        head_tile: Optional[Tuple[int, int]] = None,
        cab: Optional[Literal["A", "B"]] = None,
        train_type: Optional[str] = None,
        name: Optional[str] = None,
        min_radius_cm: Optional[float] = None,
        max_slope: Optional[float] = None,
        speed_profile_id: Optional[str] = None,
        roster_entry_id: Optional[str] = None,
    ) -> Train:
        """Turn an unidentified train into an ordinary, driveable train.

        The operator has looked at the layout and can say what the detector
        found: which locomotive it is, how long, which way it faces. That is the
        one piece an unidentified train is missing, and supplying it is all adoption does
        (design_new.md "Unidentified trains", Adoption).

        The `Train` object itself survives — same record, same controller, same
        physics instance, same held blocks — because block occupancy is
        edge-triggered off the detector (`BlockOccupancyDetector.step`). Deleting
        the unidentified train and adding a fresh train in its place would release blocks that
        are still drawing current, and no new entry would ever fire for them, so
        they would sit `FREE` under a train that is physically standing on them.
        Adoption therefore re-keys in place instead: `train_id` changes to
        `new_train_id` across every registry that keys on it, and `is_unidentified`
        flips to `False`.

        Placement is the operator's declaration, not a guess. An unidentified train's span
        claims whole blocks because nobody knows where inside them it sits;
        `place_train` rebuilds the span from the real consist length at the
        facing given here. With no `head_tile` the consist is centered in the
        detected track (`_centered_adoption_placement`), which is what the
        detector supports: it says the train is somewhere in these blocks, not
        that it is flush against a boundary. A `head_tile` overrides that.

        Either way the body must stay inside the blocks the detector confirmed.
        A block reading no current has nothing standing on it, so a consist that
        would reach into one is refused rather than claiming it.

        Args:
            unidentified_train_id: The `train_id` of the unidentified train to adopt.
            new_train_id: The id the adopted train keeps from here on. May equal
                `unidentified_train_id` to keep the generated one.
            max_speed_kmh: Operational max speed in km/h.
            length_cm: Real consist length in centimetres.
            direction: Facing: "NORTH", "EAST", "SOUTH", or "WEST"
                (case-insensitive).
            head_tile: Optional (x, y) to put the head on, flush at that tile's
                exit edge in `direction`. Must be a tile the unidentified train currently
                claims. Omit it to center the consist in the detected track.
            cab: Optional initial cab ("A" or "B"), written directly as
                `add_train` does for a fresh train.
            train_type: Optional train model / type label.
            name: Optional display name (also used for the locomotive id).
            min_radius_cm: Optional minimum curve radius in centimetres.
            max_slope: Optional maximum climbable grade, percent.
            speed_profile_id: Optional existing `SpeedProfile` to adopt. When
                omitted, a fresh default profile is built for the new id.
            roster_entry_id: Optional **Train Roster** entry to seed config from,
                exactly as `add_train` does.

        Returns:
            The adopted `Train` (the same object the unidentified train used, re-keyed).

        Raises:
            ValueError: If the train is unknown or is not an unidentified train, if
                `new_train_id` is blank or already taken, if `direction` is not a
                compass name, if `head_tile` is not a tile the unidentified train claims, if
                the unidentified train claims no tiles at all, if the consist does not fit on
                the track behind the head, or if it would reach into a block the
                detector reports clear.
        """
        # ---- Validate everything before mutating anything. -----------------
        unidentified_train = self.network.trains.get(unidentified_train_id)
        if unidentified_train is None:
            raise ValueError(f"Unknown train: {unidentified_train_id!r}")
        if not getattr(unidentified_train, "is_unidentified", False):
            raise ValueError(
                f"Train {unidentified_train_id!r} is not an unidentified train; only an unidentified train "
                f"can be adopted"
            )
        if not self.occupancy_is_real:
            # An unidentified train is a report from a real detector about a real locomotive.
            # Where occupancy is derived from the model there is nothing
            # physical to adopt, so the operation is refused rather than
            # inventing a train (design_new.md "Unidentified trains", Adoption).
            raise ValueError(
                "Unidentified trains can only be adopted when block occupancy is read "
                "from real detectors; this session derives occupancy from the "
                "model"
            )

        new_train_id = (new_train_id or "").strip()
        if not new_train_id:
            raise ValueError("new_train_id is required")
        if new_train_id != unidentified_train_id and new_train_id in self.network.trains:
            raise ValueError(f"Train {new_train_id!r} already exists")

        try:
            resolved_direction = Direction[direction.upper()]
        except (AttributeError, KeyError):
            raise ValueError(f"Invalid direction: {direction!r}")

        controller = self.controllers.get(unidentified_train_id)
        span = controller.physics_sync.current_span if controller is not None else None
        claimed_tiles = [tuple(t) for t in (getattr(span, "occupied_tiles", None) or [])]
        if not claimed_tiles:
            # An unidentified train in the undetected gap holds nothing and claims nothing, so
            # there is no position to adopt it at (design_new.md "The gap").
            raise ValueError(
                f"Unidentified train train {unidentified_train_id!r} is not standing on any detected "
                f"block right now; wait for it to be detected again"
            )
        head_offset_cm = 0.0
        if head_tile is None:
            head_tile, head_offset_cm = self._centered_adoption_placement(
                claimed_tiles, resolved_direction, float(length_cm)
            )
        else:
            head_tile = (int(head_tile[0]), int(head_tile[1]))
            if head_tile not in claimed_tiles:
                raise ValueError(
                    f"head_tile {head_tile!r} is not a tile unidentified train "
                    f"{unidentified_train_id!r} is standing on"
                )

        # Roster import (design_new.md "Train Roster"), on the same terms as
        # `add_train`: the entry is a template, and the adopted train is a
        # separate runtime instance.
        roster_entry = None
        roster = getattr(self.network, "train_roster", None)
        if roster_entry_id and roster is not None:
            roster_entry = roster.get_entry(roster_entry_id)
        if roster_entry is not None:
            if roster_entry.max_speed is not None:
                max_speed_kmh = float(roster_entry.max_speed)
            if roster_entry.consist_length_cm is not None:
                length_cm = float(roster_entry.consist_length_cm)
            if roster_entry.min_radius_cm is not None and min_radius_cm is None:
                min_radius_cm = float(roster_entry.min_radius_cm)
            if max_slope is None:
                max_slope = float(roster_entry.max_slope)
            if speed_profile_id is None and roster_entry.speed_profile_id:
                if (
                    self.network.speed_profiles.get(roster_entry.speed_profile_id)
                    is not None
                ):
                    speed_profile_id = roster_entry.speed_profile_id

        old_profile_id = getattr(unidentified_train, "speed_profile_id", None)
        controller = self.get_or_create_controller(unidentified_train_id)

        # ---- Configure and place, still under the unidentified train's own id. -----------
        # Placement runs before the re-key so a consist that does not fit leaves
        # nothing half-adopted: the only things changed by then are plain field
        # writes on the `Train`, which the rollback below puts back. Doing it the
        # other way round would strand a train that is re-keyed and no longer a
        # unidentified train but has never been placed.
        rollback = (unidentified_train.consist, unidentified_train.max_speed, unidentified_train.model, unidentified_train.car_number)
        unidentified_train.consist = [
            TrainCar(
                car_id=f"{new_train_id}_loco",
                car_type=(train_type or "LOCOMOTIVE"),
                length_cm=float(length_cm),
                max_speed=float(max_speed_kmh),
            )
        ]
        unidentified_train.max_speed = float(max_speed_kmh)
        if train_type is not None:
            unidentified_train.model = train_type
        if name is not None:
            unidentified_train.car_number = name
        if min_radius_cm is not None:
            unidentified_train.min_radius_cm = float(min_radius_cm)
        if max_slope is not None:
            unidentified_train.max_slope = float(max_slope)
        # Flipped before placement so `place_train` -> `occupy_blocks` treats
        # this as an ordinary train: no de-energize marking, and the blocks it
        # already holds re-occupy idempotently instead of conflicting.
        unidentified_train.is_unidentified = False

        # The blocks the detector confirmed under this unidentified train. The declared
        # consist must fit inside them: a block reading no current has nothing
        # standing on it, so no train's body can reach into one.
        detected_blocks = {
            block_id
            for block_id, block in self.network.blocks.items()
            if getattr(block, "occupied_by", None) == unidentified_train_id
        }

        try:
            placed_span = self.network.place_train(
                unidentified_train,
                head_tile[0],
                head_tile[1],
                resolved_direction,
                head_offset_cm=head_offset_cm,
                confine_to_blocks=detected_blocks,
            )
        except ValueError as exc:
            unidentified_train.consist, unidentified_train.max_speed, unidentified_train.model, unidentified_train.car_number = rollback
            unidentified_train.is_unidentified = True
            raise ValueError(
                f"{exc} Give a shorter length, face it the other way, pick a "
                f"different front tile, or wait until every block it stands on "
                f"reads current."
            ) from exc

        controller.physics_sync.current_span = placed_span
        # ``cab`` "A"/"B" names the physical end the placement span is framed
        # head-forward for; there is no operator cab (design_new.md
        # "Reservation-side head derivation"). "A" is the span's default framing.
        if cab == "B":
            controller._apply_head_frame("end_b")

        # ---- Re-key onto the operator's id. --------------------------------
        # Read the held set after placement, so a block the rebuilt span reaches
        # into (the body extending past what the unidentified train held) comes along too.
        held_blocks = sorted(
            block_id
            for block_id, block in self.network.blocks.items()
            if getattr(block, "occupied_by", None) == unidentified_train_id
        )
        self._rekey_train(unidentified_train_id, new_train_id, held_blocks)
        train = self.network.trains[new_train_id]
        self.unidentified_trains.forget(unidentified_train_id)
        self.get_or_create_physics(new_train_id)

        # The unidentified train's default profile pair was anchored to a blank
        # train's max_speed and named after the generated id, so it is retired in
        # favour of a pair built for the real locomotive — unless the operator
        # named an existing instance profile (or a roster entry did) to keep
        # measured curves attached (design_new.md "Train Entity", adoption).
        profiles = getattr(self.network, "speed_profiles", None)
        if speed_profile_id and profiles is not None and profiles.get(speed_profile_id):
            train.speed_profile_id = speed_profile_id
        elif profiles is not None:
            from speed_profile import seed_default_profile_pair

            train.speed_profile_id = seed_default_profile_pair(
                profiles, new_train_id, max_speed_kmh
            )
        if (
            profiles is not None
            and old_profile_id
            and old_profile_id != train.speed_profile_id
        ):
            # Retire the unidentified train's instance and the model it was the
            # only deriver of; `remove_pair` keeps a shared model in place.
            profiles.remove_pair(old_profile_id)

        # ---- Give back what it does not actually stand on. ------------------
        span_blocks = set(getattr(placed_span, "occupied_blocks", None) or [])
        surplus = sorted(set(held_blocks) - span_blocks)
        self._hand_surplus_blocks_to_unidentified(new_train_id, surplus)

        try:
            self.network.update_signals_from_blocks()
        except Exception as exc:
            self.notification_center.emit(
                source="SimulationController",
                operation="adopt_unidentified_train",
                kind=Kind.WARNING,
                message=f"could not refresh signals after adopting {new_train_id}",
                entities=[train_ref(new_train_id)],
                error=ErrorRecord(str(exc), "signal_refresh_failed"),
                event_time=time.time(),
            )

        self.notification_center.emit(
            source="SimulationController",
            operation="adopt_unidentified_train",
            kind=Kind.INFO,
            message=(
                f"train {new_train_id} adopted from unidentified "
                f"{unidentified_train_id}"
            ),
            entities=[train_ref(new_train_id)],
            event_time=time.time(),
            payload={
                "adopted_from_unidentified_id": unidentified_train_id,
                "head_tile": [head_tile[0], head_tile[1]],
                "initial_direction": resolved_direction.name,
                "max_speed_kmh": float(max_speed_kmh),
                "length_cm": float(length_cm),
                "held_blocks": held_blocks,
            },
        )
        return train

    def _centered_adoption_placement(
        self,
        claimed_tiles: List[Tuple[int, int]],
        direction: Direction,
        length_cm: float,
    ) -> Tuple[Tuple[int, int], float]:
        """Where to put the head so the consist sits centered in what was detected.

        An unidentified train claims whole blocks because nobody knows where inside them it
        sits, and the operator declaring a length does not tell the app that
        either. Centering is the honest reading of that: the same amount of
        detected track is left unaccounted for in front of the train as behind
        it. Putting the head at the first claimed tile instead would stand every
        adopted train flush against a block boundary, which is the one position
        the detector gives no reason to believe.

        Returns `(head_tile, head_offset_cm)` for `place_train`: the tile whose
        exit leaves the detected track in the declared direction, and how far
        back from that exit edge the head sits.

        Falls back to the first claimed tile flush at its exit edge only when NO
        claimed tile can leave the detected track through this facing, which
        `place_train` then reports on its own terms. Two candidate fronts is not
        that case and must not zero the offset: see below.

        The detected length is the sum of the claimed tiles' lengths. An unidentified train
        holding two blocks joined by a switch tile is centered to within half
        that switch's length, since a switch carries no detection and so is not
        among the claimed tiles.
        """
        DIR_TO_EDGE = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}
        exit_edge = DIR_TO_EDGE.get(direction.name)
        claimed = set(claimed_tiles)

        fronts = []
        for tile in claimed_tiles:
            track = self.network.get_track(tile[0], tile[1])
            if track is None or exit_edge not in (track.edges or ()):
                continue
            if self.network._get_neighbor_coords(tile[0], tile[1], exit_edge) not in claimed:
                fronts.append(tile)
        if not fronts:
            return claimed_tiles[0], 0.0
        # More than one candidate means both ends of the run leave the detected
        # track through the same compass edge — an L- or U-shaped block, which is
        # ordinary track and most of what a real layout is made of. The facing
        # cannot say which of the two ends the nose is at, but it does not have
        # to: centering is symmetric, so measuring back from either end puts the
        # body in the SAME place and only head-versus-tail differs. So pick an end
        # of the run (`claimed_tiles` is in connectivity order) and centre anyway.
        # Bailing out to a zero offset here is what stood every train adopted on a
        # corner block flush against a block boundary.
        front = (
            fronts[0]
            if len(fronts) == 1
            else next(
                (t for t in (claimed_tiles[0], claimed_tiles[-1]) if t in fronts),
                fronts[0],
            )
        )

        detected_length_cm = sum(
            self.network._get_tile_length_cm(x, y) for (x, y) in claimed_tiles
        )
        # Negative slack means the operator declared more train than the
        # detector sees. The head stays flush at the front and `place_train`
        # decides whether the overhang lands on undetected track (a switch tile,
        # which is fine) or on a block reading no current (which it refuses).
        return front, max(0.0, (detected_length_cm - length_cm) / 2.0)

    def _hand_surplus_blocks_to_unidentified(
        self, train_id: str, surplus: List[str]
    ) -> Dict[str, Set[str]]:
        """Hand back blocks an adopted train holds but does not stand on.

        An unidentified train claims whole blocks because its real extent is unknown. Adoption
        replaces that guess with the operator's declared consist, and whatever
        the rebuilt span does not cover is not this train — it is current on
        track no train the app controls is standing on, which is the definition
        of an unidentified train. So it becomes one.

        Keeping those blocks on the adopted train instead would wall the train
        off from its own route: a block with `occupied_by` set is not an eligible
        reservation candidate for *anyone*, including its own holder (see
        `BlockManager.reserve_blocks_for_path`), and the dispatcher excludes only
        the train's span blocks from the candidate set. The train would hold
        authority-blocking track ahead of itself and never acquire a path.
        Releasing them outright is equally wrong: they are drawing current, and
        the detector reports changes rather than levels, so nothing would ever
        re-occupy them and another train could be routed straight in.

        Returns `unidentified_id -> block ids` for the blocks handed over.
        """
        manager = self.block_manager
        if manager is None or not surplus:
            return {}

        for block_id in sorted(surplus):
            manager.on_block_tail_vacated(block_id, train_id, None)

        # Ordinary Observe attribution: connected surplus blocks become one unidentified train,
        # and a group touching an unidentified train that is already there joins it.
        attribution = self.unidentified_trains.attribute_unmatched_entries(
            set(surplus), self._unidentified_clock()
        )
        for unidentified_id, block_ids in attribution.items():
            manager.occupy_blocks(sorted(block_ids), unidentified_id)
            self.unidentified_trains.write_detected_span(unidentified_id, set(block_ids), set())

        # Say so. Otherwise the operator adopts a train, finds its route refused
        # because the block ahead is occupied, and has no way to connect that to
        # the length they typed in.
        self.notification_center.emit(
            source="SimulationController",
            operation="_hand_surplus_blocks_to_unidentified",
            kind=Kind.WARNING,
            message=(
                f"{', '.join(sorted(surplus))} still read current but "
                f"{train_id} is not long enough to reach them; they are "
                f"tracked as unidentified trains. If that is really {train_id}, "
                f"adopt it again with the full consist length."
            ),
            entities=[train_ref(train_id)] + [block_ref(b) for b in sorted(surplus)],
            event_time=time.time(),
            payload={
                "blocks": sorted(surplus),
                "unidentified_train_ids": sorted(attribution),
            },
        )
        return attribution

    def _rekey_train(
        self, old_train_id: str, new_train_id: str, held_blocks: List[str]
    ) -> None:
        """Move a train's whole runtime identity from one `train_id` to another.

        Every registry that keys on `train_id` is rewritten in place — the dicts
        are shared by reference with `BlockOccupancyDetector`,
        `SimulatedOccupancyDriver`, and `RouteExecutor`, so they are mutated
        rather than rebound. Block ownership moves through the canonical
        `BlockManager` writers (release, then occupy) so no block is ever owned
        by two ids and the voltage drain still sees the release, matching how
        `UnidentifiedTrainTracker._hand_block_to` hands a block between unidentified trains.

        Only ever called for a train that holds no path, route run, or
        reservation — that is, an unidentified train being adopted. It is not a general
        rename.
        """
        if old_train_id == new_train_id:
            return

        train = self.network.trains.pop(old_train_id)
        train.train_id = new_train_id
        self.network.trains[new_train_id] = train

        controller = self.controllers.pop(old_train_id, None)
        if controller is not None:
            controller.train_id = new_train_id
            self.controllers[new_train_id] = controller

        physics = self.physics_instances.pop(old_train_id, None)
        if physics is not None:
            physics.train_id = new_train_id
            self.physics_instances[new_train_id] = physics

        manager = self.block_manager
        for block_id in held_blocks:
            block = self.network.blocks.get(block_id)
            if block is None:
                continue
            if manager is not None:
                manager.on_block_tail_vacated(block_id, old_train_id, None)
                manager.occupy_blocks([block_id], new_train_id)
            else:
                block.occupied_by = new_train_id

        # Switches carry no detection, so an unidentified train never owns one; sweep anyway
        # so the rename leaves no stale id behind if that ever changes. A switch
        # records its occupant(s) in `route_owners` (route key -> train id);
        # `occupied_by` is a read-only projection over that map, so rewrite the
        # map itself rather than assigning the property.
        for switch in getattr(self.network, "switches", {}).values():
            route_owners = getattr(switch, "route_owners", None)
            if isinstance(route_owners, dict):
                for route, owner in list(route_owners.items()):
                    if owner == old_train_id:
                        route_owners[route] = new_train_id

        if old_train_id in self._detected_change_train_ids:
            self._detected_change_train_ids.discard(old_train_id)
            self._detected_change_train_ids.add(new_train_id)

    # =========================================================================
    # Failed-runtime gate
    # =========================================================================

    @property
    def occupancy_is_real(self) -> bool:
        """True when block occupancy comes from real detectors, not the model.

        This is the "the layout is not a simulation" test, and it follows the
        occupancy interface specifically rather than the run mode as a whole —
        a real-mode session that leaves occupancy simulated still runs its
        occupancy by calculation (design_new.md "Session mode and operating environment", point 3). It is
        what gates unidentified train adoption and what the UI reads to stop calling a
        railway with live detectors a simulation.
        """
        return not isinstance(self.input_interface, SimulatedOccupancyDriver)

    @property
    def runtime_halted(self) -> bool:
        """True while either fatal latch is set.

        `failed_runtime` and `hardware_failure` have different causes and
        different ways out, but they sit on one gate: while either is set,
        `update()` refuses all further advancement (design_new.md "Power",
        Hardware I/O failure).
        """
        return self.failed_runtime or self.hardware_failure

    def enter_failed_runtime(self, payload: FatalPhysicalEntryConflict) -> None:
        """Halt further train ticks until manually released.

        Before freezing, calls :meth:`enter_output_safe_state` on every
        configured block and switch output interface, zeroing track voltage and
        switching off point motors — the safe-state write touches hardware
        outputs only, never controller/path/route/resource/occupancy state.
        It writes no other session flag: the latch alone makes the session read
        Halted for ``runtime_failure`` (design_new.md "Session state"), and
        clearing it returns the session to the state it had. Then sets
        :attr:`failed_runtime`, stores the typed fatal payload for inspection,
        and emits a FATAL notification carrying the conflict details. While the
        flag is set, :meth:`update` only advances ``simulation_time`` and skips
        Observe/Plan/Action for every train.
        """
        self.enter_output_safe_state()
        self.failed_runtime = True
        self.failed_runtime_payload: Optional[FatalPhysicalEntryConflict] = payload
        self.failed_runtime_at = self.simulation_time
        self.notification_center.emit(
            source="SimulationController",
            operation="enter_failed_runtime",
            kind=Kind.ERROR,
            message=(
                f"fatal conflict on {payload.resource_type} "
                f"{payload.resource_id}: every train is halted"
            ),
            entities=_fatal_conflict_entities(payload),
            error=ErrorRecord(
                f"{payload.incoming_train_id} entered {payload.resource_id} "
                f"held by {payload.prior_owner_train_id}",
                payload.conflict_kind.value,
            ),
            event_time=time.time(),
            payload={
                "resource_type": payload.resource_type,
                "resource_id": payload.resource_id,
                "incoming_train_id": payload.incoming_train_id,
                "prior_owner_train_id": payload.prior_owner_train_id,
                "conflict_kind": payload.conflict_kind.value,
                "incoming_path_id": payload.incoming_path_id,
                "prior_owner_path_id": payload.prior_owner_path_id,
            },
        )

    def enter_hardware_failure(self, payload: HardwareFailure) -> None:
        """Revoke output, latch the session, and reject in-place recovery.

        This method, not the reporting entity, is the only writer of
        ``hardware_failure = True`` (design_new.md "Power", Hardware I/O
        failure), mirroring :meth:`enter_failed_runtime`. ``update()`` refuses
        all further advancement while it is set, on the same gate as
        ``failed_runtime``.

        This is idempotent because one bus failure can be observed by several
        output calls in the same tick. The first failure is the canonical cause.
        The flag is latched even if a best-effort safe-state write itself fails.
        """
        if self.runtime_halted:
            return
        safe_state_error: Optional[str] = None
        try:
            self.enter_output_safe_state()
        except Exception as exc:  # noqa: BLE001 - the failure still must latch
            safe_state_error = str(exc)
            logger.exception("hardware safe-state command failed")
        finally:
            self.hardware_failure = True
            # Safe state revoked physical output permission. The environment is
            # no longer driving anything, and only activation grants that back.
            self.environment_active = False

        self.notification_center.emit(
            source="SimulationController",
            operation="enter_hardware_failure",
            kind=Kind.ERROR,
            message=(
                f"hardware failure in {payload.component}: every train is "
                "halted and the outputs are in their safe state"
            ),
            entities=(
                [_hardware_entity_ref(payload)]
                if _hardware_entity_ref(payload) is not None
                else []
            ),
            error=ErrorRecord(payload.message, payload.component),
            event_time=time.time(),
            payload={
                "component": payload.component,
                "operation": payload.operation,
                "resource_id": payload.resource_id,
                "message": payload.message,
                "safe_state_error": safe_state_error,
                "requires_new_activation": True,
            },
        )

    def start_railway_over(self) -> None:
        """Drop the railway's runtime state after a successful environment
        activation (design_new.md "Activation starts the railway over").

        Runs between ticks while operation is stopped, so it resets state
        directly rather than through latches. It keeps the layout, the Hardware
        Setup, every train with its last position, and every `Route` with its
        assignments, and drops the rest in this order:

          1. every live `Path`, cancelled with `error.origin = "revoked"`, which
             releases its block reservations, locks, and switch claims;
          2. every `RUNNING` `RouteRun`, moved to history as `DEACTIVATED` with
             the route assignment kept, so a fresh run starts once the train is
             powered on again;
          3. every train's power, through `TrainController.complete_power_off()`.
             A train still rolling stops by itself on its next running pass.

        Pending teardown latches and held assignment requests are left alone:
        the Action teardown consumers run for an `OFF` train too, so each
        finishes on the first Live tick. `complete_power_off()` does not touch
        `pending_power_off`, so a pending power-off still removes its route.

        The one latch it clears itself is the emergency request: a request
        latched on a stopped pass (for example when a Setup tool moved a
        locomotive into the next block) describes the railway before it started
        over, so every controller discards it through
        `phase_latches.discard_emergency_request()`.
        """
        dispatcher = self.path_dispatcher
        if dispatcher is not None:
            live_path_ids = [
                path_id
                for path_id, path in dispatcher.paths.items()
                if path.status == PathStatus.ACTIVE
            ]
            for path_id in live_path_ids:
                dispatcher.cancel_path(
                    path_id,
                    ErrorRecord("environment activated", "revoked"),
                    kind=Kind.INFO,
                )
        self.route_executor.end_running_runs()
        for controller in self.controllers.values():
            controller.complete_power_off()
        for controller in self.controllers.values():
            controller.route_hooks.phase_latches.discard_emergency_request()

    def clear_failed_runtime(self) -> FatalPhysicalEntryConflict:
        """Release the halt so the session can carry on, with every train off.

        A fatal conflict says two trains claim one piece of track. On a real
        layout the operator goes and sorts that out — moves the loco, fixes the
        detector, lifts the stray car off the rail — and then wants to keep
        running. Making them restart the process to do it would throw away the
        whole layout state for a problem they have already fixed, so the halt is
        a gate that reopens rather than a session-ending error.

        Reopening it does not resume mid-motion. Every train is written straight
        to `OFF` with its handle at `N` and any EB cleared, so nothing moves
        until the operator powers it back up deliberately. No speed is written:
        a train the halt froze while rolling stops by itself on its next running
        pass, whose OFF branch reports speed 0 (design_new.md step `7c41`). Each
        train keeps its live path and its route run until an ordinary cancel or
        route teardown ends it (design_new.md "Authority to run is live
        reservation"). The OFF
        transition goes through `TrainController.complete_power_off()` — the
        canonical `OFF` writer for every entry but the startup default — rather
        than a queued `power_off_cmd()`, for two reasons: the command is
        stopped-only and the frozen speed may be non-zero, and it defers its
        work to an Action pass that cannot run while the gate is shut.
        `complete_power_off()` leaves any `power_off` latch a controller was
        still holding when the halt shut the gate: its own Action consumer runs
        for the `OFF` train on the first running pass and removes the route as
        an ordinary power-off does (design_new.md "Latch Table", Clear/consume
        access). Track voltage stays at the `0` `enter_safe_state()` left it at; the next
        tick re-emits from each train's energized set, which is empty while they
        are all `OFF`.

        Returns:
            The conflict payload that caused the halt, so the caller can report
            what was cleared.

        Raises:
            ValueError: If the runtime is not halted.
        """
        if not self.failed_runtime:
            raise ValueError("The runtime is not halted; there is nothing to clear")

        payload = self.failed_runtime_payload
        # Entering the halt already ended every open calibration run through
        # `enter_output_safe_state()`, so there is none left to end here
        # (design_new.md "What ends a calibration run").
        for controller in self.controllers.values():
            controller.complete_power_off()

        self.failed_runtime = False
        self.failed_runtime_payload = None
        self.failed_runtime_at = None

        self.notification_center.emit(
            source="SimulationController",
            operation="clear_failed_runtime",
            kind=Kind.INFO,
            message=(
                "fatal conflict cleared by the operator; every train is off "
                "and the runtime is no longer halted"
            ),
            entities=(
                [train_ref(payload.incoming_train_id)]
                if payload is not None and payload.incoming_train_id
                else []
            ),
            event_time=time.time(),
            payload={
                "resource_type": payload.resource_type if payload else None,
                "resource_id": payload.resource_id if payload else None,
            },
        )
        return payload

    # =========================================================================
    # Path completion / cancellation delegation
    # =========================================================================

    def handle_path_completion(
        self,
        train_id: str,
        path_id: str,
        walk_position: int,
    ) -> None:
        """Forward a path-completion event to the RouteExecutor.

        Per design_new.md step `bc55`, the served row is the `walk_position`
        that `PathDispatcher.try_complete_active_path(...)` already resolved
        through `find_next_completion_task(...)`; `RouteExecutor` finishes that
        row through `RouteExecutor.advance_walk_position(...)`. The single
        `handle_path_completion` event is emitted by
        `RouteExecutor.handle_path_completion(...)` itself (design_new.md "Event
        log", Emitter attribution), so this wrapper does not emit a second one.
        """
        self.route_executor.handle_path_completion(
            train_id,
            path_id,
            walk_position,
        )

    def handle_path_cancellation(
        self,
        train_id: str,
        path_id: str,
        error: Optional[ErrorRecord] = None,
    ) -> None:
        """Deliver a committed path cancellation to the `RouteExecutor`.

        Per design_new.md "Stage handoff rule" this is the sanctioned
        same-transaction hop: `PathDispatcher.cancel_path(...)` calls here and
        this method delegates to `RouteExecutor.handle_path_cancellation(...)`.
        It emits nothing of its own — the `cancel_path` event the dispatcher
        already emitted *is* the `path_cancel_request` consumption event
        (design_new.md "Latch consumption events"), and a second event here
        would be a duplicate sink for the same commit. The deviation
        classification is not forwarded — it rides that `cancel_path` event.
        """
        self.route_executor.handle_path_cancellation(
            train_id,
            path_id,
            error=error,
        )


def train_runtime_entry(
    sim_controller: "SimulationController",
    train: Train,
    *,
    session_state: "SessionState",
) -> Dict[str, Any]:
    """One train's live runtime data, in the sibling-payload shape.

    The one builder for a train's live data (design_new.md "Data Flow To UI"):
    `/api/network`, `/api/trains`, and every command reply read it, so they
    cannot drift. Returns `{controller, dispatcher_progress, assigned_route_id,
    route_run, operator_status}`:

      - `controller`: `TrainController.to_dict()`
      - `dispatcher_progress`: `PathDispatcher.get_active_path_progress(...)`
        with the route-supplied `waiting_for_block_id`, or None
      - `assigned_route_id`: `RouteExecutor.assigned_route_id_for(...)`
      - `route_run`: the RouteExecutor 5-field summary of the RUNNING run, or None
      - `operator_status`: `{ato_status, pending_actions, hold_reason}`, the
        read models that need controller and route state together
    """
    from operator_status import operator_status_for

    train_id = train.train_id
    controller = sim_controller._get_or_create_controller(train)
    path_dispatcher = getattr(sim_controller, "path_dispatcher", None)
    route_executor = getattr(sim_controller, "route_executor", None)

    progress = (
        path_dispatcher.get_active_path_progress(train_id)
        if path_dispatcher is not None
        else None
    )
    # design_new.md "Data Flow To UI": `waiting_for_block_id` is composed here
    # from the route-supplied `get_waiting_for_block_id(...)` call, not inside
    # `get_active_path_progress(...)`. It needs the run's `resolved_target` plus
    # manual override, which `RouteExecutor.waiting_for_block_id_for(...)`
    # assembles.
    if progress is not None:
        progress["waiting_for_block_id"] = (
            route_executor.waiting_for_block_id_for(train_id)
            if route_executor is not None
            else None
        )

    return {
        "controller": controller.to_dict(),
        "dispatcher_progress": progress,
        "assigned_route_id": (
            route_executor.assigned_route_id_for(train_id)
            if route_executor is not None
            else None
        ),
        "route_run": (
            route_executor.route_run_summary_for(train_id)
            if route_executor is not None
            else None
        ),
        "operator_status": operator_status_for(
            sim_controller,
            train,
            session_state=session_state,
        ),
    }


def build_train_runtime_maps(
    network: RailwayNetwork,
    sim_controller: Optional["SimulationController"],
    *,
    session_state: "SessionState",
) -> Dict[str, Dict[str, Any]]:
    """The five per-train sibling maps, keyed by `train_id` (design_new.md
    "Data Flow To UI"): `controllers_by_train_id`,
    `dispatcher_progress_by_train_id`, `assigned_routes_by_train_id`,
    `route_runs_by_train_id`, and `operator_status_by_train_id`. Empty maps
    when there is no runtime."""
    maps: Dict[str, Dict[str, Any]] = {
        "controllers_by_train_id": {},
        "dispatcher_progress_by_train_id": {},
        "assigned_routes_by_train_id": {},
        "route_runs_by_train_id": {},
        "operator_status_by_train_id": {},
    }
    if not sim_controller:
        return maps
    for train_id, train in network.trains.items():
        entry = train_runtime_entry(
            sim_controller,
            train,
            session_state=session_state,
        )
        maps["controllers_by_train_id"][train_id] = entry["controller"]
        maps["dispatcher_progress_by_train_id"][train_id] = entry["dispatcher_progress"]
        maps["assigned_routes_by_train_id"][train_id] = entry["assigned_route_id"]
        maps["route_runs_by_train_id"][train_id] = entry["route_run"]
        maps["operator_status_by_train_id"][train_id] = entry["operator_status"]
    return maps


def build_network_payload(
    network: RailwayNetwork,
    sim_controller: Optional["SimulationController"],
    *,
    session_state: Optional["SessionState"] = None,
) -> Dict[str, Any]:
    """Build network payload in the canonical sibling-payload shape.

    Per design_new.md "Data Flow To UI", live runtime data is exposed as
    sibling maps alongside the persistent `trains` list, never merged into
    Train.to_dict() output (see `build_train_runtime_maps`).

    `session_state` is `AppState.session_state()`, served as `session_state`
    and read by the hold reason's first question. The HTTP snapshot passes it;
    a caller with no `AppState` (the scenario runner) is ticking the runtime
    itself, so the default reads the session as Live.
    """
    from session_state import LIVE

    session_state = session_state or LIVE
    payload = network.to_dict()
    payload.update(
        build_train_runtime_maps(
            network,
            sim_controller,
            session_state=session_state,
        )
    )
    payload["session_state"] = session_state.to_dict()

    if not sim_controller:
        payload["simulation_time"] = 0.0
        payload["failed_runtime"] = False
        payload["failed_runtime_conflict"] = None
        payload["hardware_failure"] = False
        payload["runtime_halted"] = False
        payload["operation_mode"] = OperationMode.SIMULATION.value
        payload["operation_environment"] = OperationMode.SIMULATION.value
        payload["environment_active"] = False
        payload["activation_issues"] = []
        payload["occupancy_is_real"] = False
        return payload

    payload["simulation_time"] = float(sim_controller.simulation_time)

    # Run mode (design_new.md "Session mode and operating environment"). The UI reads these to stop calling a
    # railway with real hardware on it a simulation, and to decide whether the
    # unidentified-adoption control is offered at all.
    payload["operation_mode"] = sim_controller.operation_mode.value
    payload["operation_environment"] = sim_controller.operation_mode.value
    payload["environment_active"] = bool(sim_controller.environment_active)
    payload["activation_issues"] = list(sim_controller.activation_issues)
    payload["occupancy_is_real"] = bool(sim_controller.occupancy_is_real)

    # Fatal physical-entry conflict halt (set by enter_failed_runtime). While
    # it is set, update() skips every train tick — the trains freeze at their
    # last state and only simulation_time advances. Nothing else in the polled
    # payload reflects that, so surface the flag + the conflict cause here so
    # the UI can show a halt banner and offer recovery.
    payload["failed_runtime"] = bool(sim_controller.failed_runtime)
    failed_payload = sim_controller.failed_runtime_payload
    fatal = (
        failed_payload
        if isinstance(failed_payload, FatalPhysicalEntryConflict)
        else None
    )
    payload["failed_runtime_conflict"] = (
        {
            "resource_type": fatal.resource_type,
            "resource_id": fatal.resource_id,
            "incoming_train_id": fatal.incoming_train_id,
            "prior_owner_train_id": fatal.prior_owner_train_id,
            "conflict_kind": fatal.conflict_kind.value,
            "incoming_path_id": fatal.incoming_path_id,
            "prior_owner_path_id": fatal.prior_owner_path_id,
            "halted_at_sim_time": sim_controller.failed_runtime_at,
        }
        if fatal is not None
        else None
    )
    # The hardware I/O halt is its own latch on the same gate, so the banner
    # asks "is the runtime halted" as the OR of the two and reads
    # `failed_runtime_kind` to say which one it is. The latch itself retains only
    # the bool; the diagnostic detail lives in the event stream, so the banner
    # filters that stream for the last `enter_hardware_failure` event rather than
    # reading a retained payload field (design_new.md "Error events"; "Power",
    # Hardware I/O failure).
    hw_halted = bool(sim_controller.hardware_failure)
    hw_event = (
        sim_controller.notification_center.latest_by_operation(
            "enter_hardware_failure"
        )
        if hw_halted
        else None
    )
    payload["hardware_failure"] = hw_halted
    payload["runtime_halted"] = bool(
        sim_controller.failed_runtime or sim_controller.hardware_failure
    )
    payload["failed_runtime_kind"] = (
        "hardware_failure"
        if hw_halted
        else "physical_entry_conflict" if fatal is not None else None
    )
    if not hw_halted:
        payload["failed_runtime_hardware_failure"] = None
    elif hw_event is not None:
        payload["failed_runtime_hardware_failure"] = {
            "component": hw_event.payload.get("component"),
            "operation": hw_event.payload.get("operation"),
            "resource_id": hw_event.payload.get("resource_id"),
            "message": hw_event.payload.get("message"),
            "requires_new_activation": True,
        }
    else:
        # Latched, but the event aged out of the archive: the halt is still real,
        # so keep the banner's recovery instruction even without the detail.
        payload["failed_runtime_hardware_failure"] = {
            "requires_new_activation": True,
        }

    return payload
