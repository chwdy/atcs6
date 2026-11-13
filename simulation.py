"""
The runtime: `SimulationController` owns the session, the hardware
interfaces, the safe state and the runtime loop's passes (design_v2.md
"Terms", The runtime). Each pass runs the v2 runtime step through
`run_train_step`, which hands the detectors' reading to `TrainRuntime`.
"""

import logging
import time
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
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
from enums import BlockState, CommandResult, Direction, OperationMode, PathStatus
import operation_environment
from block_power import BlockPowerController
from switch_power import (
    SimulatedSwitchDriver,
    SwitchOutputOwner,
    SwitchPowerController,
)
from block_occupancy import (
    BlockOccupancyDetector,
    SimulatedOccupancyDriver,
)
from detector_reading_log import DetectorReadingLog
from hardware.safety import SafeStateError
from hardware.watchdog import WatchdogTripRecord
from notification_center import (
    EntityRef,
    Kind,
    NotificationCenter,
    block_ref,
    make_notification_emitter,
    switch_ref,
    train_ref,
)
from route_executor import RouteExecutor
from train_runtime import TrainRuntime

from train_controller import TrainController, create_controller_for_train
from train_physics import TrainPhysics, create_physics_for_train
from runtime_payloads import (
    ErrorRecord,
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
        operation_mode: Any = OperationMode.VIRTUAL_HARDWARE,
        input_interface: Optional[Any] = None,
        block_output_interface: Optional[Any] = None,
        switch_output_interface: Optional[Any] = None,
        environment_active: bool = False,
        activation_issues: Optional[List[Dict[str, Any]]] = None,
    ):
        self.network = network
        self.path_dispatcher = path_dispatcher

        # The operating environment (design_v2.md "Operating environment and
        # activation"). No environment is active until an activation builds its
        # interfaces; until then the placeholder interfaces below are bound and
        # never driven.
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

        # The trains sharing the strongest claim on each conflict block on the
        # last read, and the conflict blocks a deleted train shared the claim
        # on. Deleting a train removes a record, not a locomotive, so such a
        # block goes to an unidentified train on the next read rather than to
        # the trains left (design_new.md "Block occupancy detection", Deleting
        # a train never hands its current to another train).
        self._conflict_candidates: Dict[str, Tuple[str, ...]] = {}
        self._orphaned_conflict_blocks: Set[str] = set()

        # Wall time the current pass started: the pass's `wall_time`, which the
        # detector reading log writes and the hold time is measured on.
        self._pass_started_wall_time: Optional[float] = None

        # The per-pass record of what every block detector read (design_new.md
        # "Event log", Detector reading log). Off until `enable(...)`: the real
        # server enables it at boot; tests and scenario runs leave it off.
        self.detector_reading_log = DetectorReadingLog(
            notification_center=self.notification_center
        )

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
        )
        self._wire_power_controllers()
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
        self.unidentified_trains = UnidentifiedTrainTracker(network)
        # The v2 trains and runtime step (design_v2.md "Runtime step"). It
        # writes block voltages through whichever `power_controller` is bound
        # at the time, because activation builds a fresh one.
        self.train_runtime = TrainRuntime(
            network,
            set_block_voltage=lambda block_id, volts: self.power_controller.set_block_voltage(
                block_id, volts
            ),
            emit=self._emit_train_event,
            throw_switch=lambda switch_id, position: network.switch_manager.request_position_change(
                switch_id, position
            )[0],
            outputs_active=lambda: not self.runtime_halted,
        )
        self._train_step_was_live = False
        # Attribution needs the tracker's undetected-track walk to decide whether
        # a train could physically be standing on a block it reserved, so the
        # detector gets it as soon as the tracker exists (design_new.md "Power",
        # Attribution).
        self.block_occupancy_detector.reachable_blocks = (
            self.unidentified_trains.reachable_blocks
        )
        # Touching reads each train's block list, whether it is directional, and
        # the block on its head side, plus a ghosted train's retained span
        # (design_new.md "Block occupancy detection", Touching).
        self.block_occupancy_detector.directional = self._train_is_directional
        self.block_occupancy_detector.head_side = self._train_head_side_block
        self.block_occupancy_detector.retained_span = self._ghost_retained_span
        # A clear is vacated only on the rearmost block of its holder's block
        # list, so the clear results need each list in order (design_new.md
        # "Block occupancy detection", A newly CLEAR block).
        self.block_occupancy_detector.block_list = self._train_block_list
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

        self.speed_calibration = SpeedCalibration()
        # The clock and the environment are passed as calls that read the
        # current value, because `AppState` replaces the clock seam after
        # construction and an activation changes the environment.
        self.speed_calibration.wire_dependencies(
            controllers=self.controllers,
            operating_clock=lambda: self.operating_clock(),
            notification_center=self.notification_center,
            block_holder=lambda block_id: getattr(
                self.network.blocks.get(block_id), "occupied_by", None
            ),
            assigned_route_id=lambda train_id: self.route_executor.assigned_route_id_for(
                train_id
            ),
        )

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
        # (design_new.md "Session state"). It is v1 and goes with the v1 train
        # code.
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
            # The same named-dependency injection for RouteExecutor. The clock
            # is passed as a call that reads `self.operating_clock` each time,
            # because `AppState` replaces that seam after construction.
            self.route_executor.wire_dependencies(
                notification_center=self.notification_center,
                speed_calibration=self.speed_calibration,
                controllers=self.controllers,
                create_controller=self.get_or_create_controller,
                operating_clock=lambda: self.operating_clock(),
            )

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

        # The unidentified-train tracker creates and retires its trains'
        # controllers and physics, and hands blocks between unidentified trains
        # through the BlockManager, so it is wired once that exists. The
        # BlockManager is passed as a call that reads the current one.
        self.unidentified_trains.wire_dependencies(
            controllers=self.controllers,
            physics_instances=self.physics_instances,
            create_controller=self.get_or_create_controller,
            create_physics=self.get_or_create_physics,
            block_manager=lambda: self.block_manager,
            notification_center=self.notification_center,
        )

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

    # ------------------------------------------------------------------
    # The v2 runtime step (design_v2.md "Runtime step")
    # ------------------------------------------------------------------

    def _emit_train_event(
        self,
        operation: str,
        kind: str,
        train_id: str,
        message: str,
        payload: Dict[str, Any],
    ) -> None:
        """Put one train event on the event stream (design_v2.md "Event
        log", Train events)."""
        self.notification_center.emit(
            source="SimulationController",
            operation=operation,
            kind=Kind.WARNING if kind == "warning" else Kind.INFO,
            message=message,
            entities=[train_ref(train_id)],
            payload=payload,
        )

    def note_session_live(self, live: bool) -> None:
        """Tell the train runtime whether the session state is Live. The
        runtime loop calls this on every iteration, so the step on which the
        session leaves Live stops every train and ends every claim
        (design_v2.md "Trains", Leaving Live stops every train)."""
        if self._train_step_was_live and not live:
            self.train_runtime.leave_live(time.time())
        self._train_step_was_live = live

    def run_train_step(self, live: bool, wall_time: Optional[float] = None) -> None:
        """One runtime step: read the detectors, write the reading to the
        detector reading log, then hand the blocks that read occupied to the
        train runtime (design_v2.md "Runtime step").

        While `live` is False the step updates each train's blocks only, so
        trains still appear and disappear on the page, and nothing is claimed
        or powered.

        `wall_time` is the pass's start time. The runtime loop leaves it out,
        so the step samples the clock; a replay passes the recorded value, so
        the hold time is measured the same way it was on the rig
        (design_v2.md "Operating environment and activation", The hold time is
        measured on pass wall time).
        """
        self._pass_started_wall_time = time.time() if wall_time is None else wall_time
        if self.runtime_halted or not self.occupancy_is_real:
            return
        if live and not self.environment_active:
            return

        detector = self.block_occupancy_detector
        try:
            detector.step()
        except Exception as exc:  # noqa: BLE001 - a failed detector read is fatal
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
        self.detector_reading_log.record_pass(
            pass_kind="tick" if live else "stopped",
            wall_time=self._pass_started_wall_time,
            simulation_time=self.simulation_time,
            delta_seconds=0.0,
            operating_delta_seconds=0.0,
            detector=detector,
            environment=self.operation_mode.value if self.environment_active else None,
        )
        self.train_runtime.step(
            detector.confirmed_occupied, self._pass_started_wall_time, live
        )

    def _advance_local_operating_time(self, delta_seconds: float) -> None:
        """Advance the fallback operating clock for a controller with no
        `AppState` wired. See `operating_clock` in `__init__`."""
        self._operating_time_local += max(delta_seconds, 0.0)

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

    def _train_is_directional(self, train_id: str) -> bool:
        """Whether `train_id` holds a live reservation (`received_path_plan` is
        set) and is not stopped (design_new.md "Block occupancy detection",
        Touching). A directional train is touched on its head side only."""
        controller = self.controllers.get(train_id)
        if controller is None:
            return False
        if controller.path_projection.received_path_plan is None:
            return False
        return not controller.is_stopped()

    def _train_head_side_block(
        self, train_id: str, gained: Set[str]
    ) -> Optional[str]:
        """The block on `train_id`'s head side, given the blocks it gained on
        this read (`TrainPhysics.head_side_block`)."""
        train = self.network.trains.get(train_id)
        physics = self.physics_instances.get(train_id)
        if train is None or physics is None:
            return None
        return physics.head_side_block(gained)

    def _ghost_retained_span(self, train_id: str) -> Set[str]:
        """A ghosted train's retained span blocks, which stand in for its empty
        block list in attribution; empty for any other train (design_new.md
        "Block occupancy detection", A ghosted train's retained span stands in
        for its block list)."""
        controller = self.controllers.get(train_id)
        if controller is None or not controller.presence_unknown:
            return set()
        span = controller.physics_sync.current_span
        return set(span.occupied_blocks or []) if span is not None else set()

    def enter_safe_state(self) -> None:
        """Put all hardware outputs into their safe state — every block track
        voltage at 0 and every switch motor de-energized (design_new.md Power,
        Hardware interface boundary).

        This is the sequence itself. Points are dropped before block voltages,
        and each half is its controller's non-recording device write, so this
        call touches no recorded model field and is safe from any thread.
        It has two direct callers: the Real Hardware `OutputWatchdog` daemon,
        and process shutdown (`shutdown.py` `_apply_safe_state`), which writes
        the safe state before it closes the device (design_new.md Power,
        Runtime-thread callers reach the sequence through
        `SimulationController.enter_output_safe_state()`). Every runtime-thread
        caller goes through `enter_output_safe_state()` instead, which advances
        the safe-state generation and reconciles the recorded fields afterwards.
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

    def _wire_power_controllers(self) -> None:
        """Hand the block and switch power controllers their named
        dependencies: the event sink, the hardware-failure latch, the
        controller registry (block side), and the block power controller whose
        blocks feed the switches (switch side). Called each time this
        controller builds the pair."""
        self.power_controller.wire_dependencies(
            notification_center=self.notification_center,
            controllers=self.controllers,
            enter_hardware_failure=self.enter_hardware_failure,
        )
        self.switch_power_controller.wire_dependencies(
            notification_center=self.notification_center,
            enter_hardware_failure=self.enter_hardware_failure,
            power_controller=self.power_controller,
        )

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
        # A fresh session has thrown no coil, so the points may lie either way
        # until activation throws them (design_v2.md "Operating environment
        # and activation", Activation sets what v2 knows about each switch).
        for switch in self.network.switches.values():
            switch.position_known = False
        # Binding the interfaces marks the environment active. The session
        # flags a successful activation clears are written once the railway
        # has been started over (see `finish_activation`).
        self.activation_issues = []
        self.environment_active = True

    def finish_activation(self) -> None:
        """Clear the controller's session flags after a successful activation
        has started the railway over (design_new.md "Session state", Activate
        succeeds).

        It throws every switch once to the position the layout shows, so the
        session knows where the points lie (design_v2.md "Operating environment
        and activation", Activation throws every switch). It clears the layout
        emergency cut-out, and it clears `hardware_failure`: the activation
        rebuilt every interface and verified the environment, so its success
        shows the device answers again.
        """
        if self.switch_manager is not None:
            self.switch_manager.throw_all_to_layout()
        # An activation starts with no trains; the runtime step finds them
        # again from the detectors (design_v2.md "Trains", Trains are not
        # saved).
        self.train_runtime.clear()
        self.layout_cut_out_active = False
        if self.hardware_failure:
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
        )
        self._wire_power_controllers()
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
            directional=self._train_is_directional,
            head_side=self._train_head_side_block,
            retained_span=self._ghost_retained_span,
            block_list=self._train_block_list,
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

    def _train_block_list(self, train_id: str) -> List[str]:
        """`train_id`'s block list ordered rear to front, or empty when the
        train has no physics to order it by (design_new.md "Block occupancy
        detection", The occupancy chain runs one way)."""
        train = self.network.trains.get(train_id)
        physics = self.physics_instances.get(train_id)
        if train is None or physics is None:
            return []
        return physics.block_list_rear_to_front()

    # =========================================================================
    # Per-train Observe and Action stages
    # (Plan runs between them via RouteExecutor.tick_train_plan)
    # =========================================================================

    # =========================================================================
    # Action latch consumers (per design_new.md latch consumption rules)
    # =========================================================================

    # =========================================================================
    # Persistent state sync (Train <- TrainController)
    # =========================================================================

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
            # Hand the controller its named dependencies. The environment is
            # passed as a call that reads `self.operation_mode` each time,
            # because an activation changes it.
            controller.wire_dependencies(
                notification_center=self.notification_center,
                speed_calibration=self.speed_calibration,
                operation_mode=lambda: self.operation_mode,
            )
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
            physics = create_physics_for_train(
                train_id=train.train_id,
                network=self.network,
                controller=controller,
            )
            # The detector is passed as a call that reads
            # `self.block_occupancy_detector` each time, because an activation
            # builds a new one.
            physics.wire_dependencies(
                notification_center=self.notification_center,
                occupancy_detector=lambda: self.block_occupancy_detector,
                unidentified_trains=self.unidentified_trains,
            )
            self.physics_instances[train.train_id] = physics
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
        speed_profile_id: Optional[int] = None,
        source_profile=None,
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
            speed_profile_id: The Default profile or the set the train's
                instance profile is copied from; None copies the Default
                profile (design_new.md "SpeedProfile", Giving a train a
                profile).
            source_profile: A scene's own profile record to copy instead; only
                a scene passes it, and it is never registered (design_new.md
                "SpeedProfile", Where models, sets and instance profiles are
                kept, A scene adds no models or sets).

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

        # The profile to copy must be the Default profile or a set; refuse
        # anything else before anything is built (design_new.md "SpeedProfile",
        # Giving a train a profile).
        profiles = self.network.speed_profiles
        if source_profile is None:
            profiles.copy_source(speed_profile_id)

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

        # Every train owns exactly one instance profile: a copy of the profile
        # the placement names, or of the Default profile drawn to this train's
        # max_speed (design_new.md "SpeedProfile", Every train owns exactly one
        # instance profile; Copying a profile).
        train.speed_profile_id = profiles.create_instance(
            source_profile_id=speed_profile_id,
            source_profile=source_profile,
            max_speed_kmh=max_speed_kmh,
        ).profile_id

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

    def remove_train(self, train_id: str) -> None:
        """Delete a train the operator placed (design_new.md "Train Entity",
        Deleting a train).

        Runs between ticks under `AppState.simulation_lock`, and only on a train
        with nothing left to tear down, so it writes no latch. Raises
        `ValueError` naming the reason, and changes nothing, when:

          * no train has that id;
          * the train is unidentified: the runtime owns it, and the operator
            adopts it or it retires by itself;
          * the train is not OFF;
          * it still holds a live path or a RUNNING route run, which an OFF
            train only does after a fatal-halt clear.

        Otherwise, in order: `RouteExecutor.forget_train(...)` removes its route
        records; its blocks and its switch occupancy are released; the released
        blocks that still read current are handed to an unidentified train,
        because the locomotive may still stand on them and the detector reports
        changes rather than levels, so a released block would stay FREE under
        it; `SpeedCalibration.abandon(...)` discards a finished speed pass still
        held for it; the train, its controller (with every latch on it) and its
        physics instance leave their registries; and one `remove_train` event
        names it. Its instance profile goes with it; the models and sets stay.
        """
        from enums import TrainStatus

        train = self.network.trains.get(train_id)
        if train is None:
            raise ValueError(f"Train {train_id} not found")
        if getattr(train, "is_unidentified", False):
            raise ValueError(
                f"Train {train_id} is an unidentified train: adopt it, or it is "
                "retired once it draws no current"
            )
        controller = self.controllers.get(train_id)
        if controller is not None and controller.train_status != TrainStatus.OFF:
            raise ValueError(f"Power train {train_id} off before deleting it")
        if (
            self.path_dispatcher.get_active_path(train_id) is not None
            or self.route_executor.execution_state_for(train_id) is not None
        ):
            raise ValueError(
                f"Train {train_id} still holds a path or a running route run; "
                "unassign its route and let its teardown finish first"
            )
        held_blocks = sorted(
            block_id
            for block_id, block in self.network.blocks.items()
            if getattr(block, "occupied_by", None) == train_id
        )
        detector = self.block_occupancy_detector
        detected = detector.detected_block_ids()
        drawing = (
            sorted(set(held_blocks) & detected & detector.confirmed_occupied)
            if detected
            else []
        )

        self.route_executor.forget_train(train_id)

        if self.block_manager is not None:
            self.block_manager.release_train_occupancy(train_id)
        if self.switch_manager is not None:
            # `Switch.route_owners` is the doc's `Switch.physical_occupants`.
            for switch_id, switch in list(self.network.switches.items()):
                for route, owner in list(switch.route_owners.items()):
                    if owner == train_id:
                        self.switch_manager.on_switch_tail_vacated(
                            switch_id, train_id, route
                        )

        # Blocks that still read current go to an unidentified train: the
        # locomotive may still stand on them, and a released block would stay
        # FREE under it because the detector reports changes, not levels
        # (design_new.md "Train Entity", Deleting a train, step 3).
        handed = self._hand_blocks_to_unidentified(drawing)

        # A finished pass still held for this train goes, as on a network clear
        # or layout load, so a later train with the same id can open a run. No
        # open run reaches here: the power-off that left the train OFF ended it.
        self.speed_calibration.abandon(
            train_id, reason="the train was deleted"
        )

        self.network.remove_train(train_id)
        self.controllers.pop(train_id, None)
        self.physics_instances.pop(train_id, None)
        self._detected_change_train_ids.discard(train_id)

        # A standing conflict block this train shared the claim on goes to an
        # unidentified train on the next read, never to the trains left: the
        # locomotive may still be standing on it (design_new.md "Block
        # occupancy detection", Deleting a train never hands its current to
        # another train).
        for block_id, candidates in self._conflict_candidates.items():
            if train_id in candidates:
                self._orphaned_conflict_blocks.add(block_id)

        self.notification_center.emit(
            source="SimulationController",
            operation="remove_train",
            kind=Kind.INFO,
            message=(
                f"train {train_id} deleted; {', '.join(drawing)} still read "
                f"current and went to unidentified {', '.join(sorted(handed))}"
                if handed
                else f"train {train_id} deleted"
            ),
            entities=[train_ref(train_id)] + [train_ref(u) for u in sorted(handed)],
            event_time=time.time(),
            payload={
                "released_blocks": held_blocks,
                "unidentified_train_ids": sorted(handed),
            },
        )

    def adopt_unidentified_train(
        self,
        unidentified_train_id: str,
        new_train_id: str,
        *,
        max_speed_kmh: float,
        length_cm: float,
        direction: str,
        head_tile: Optional[Tuple[int, int]] = None,
        front_end_tile: Optional[Tuple[int, int]] = None,
        cab: Optional[Literal["A", "B"]] = None,
        train_type: Optional[str] = None,
        name: Optional[str] = None,
        min_radius_cm: Optional[float] = None,
        max_slope: Optional[float] = None,
        speed_profile_id: Optional[int] = None,
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
            front_end_tile: Optional (x, y) naming which end of the detected
                track the front is at, when no `head_tile` is given. Required
                when the facing leaves the detected track at both of its ends (an
                L- or U-shaped run); the consist is still centered
                (design_new.md "Unidentified trains", Choosing the front end).
            cab: Optional initial cab ("A" or "B"), written directly as
                `add_train` does for a fresh train.
            train_type: Optional train model / type label.
            name: Optional display name (also used for the locomotive id).
            min_radius_cm: Optional minimum curve radius in centimetres.
            max_slope: Optional maximum climbable grade, percent.
            speed_profile_id: The Default profile or the set to copy into the
                instance profile the unidentified train already owns; None
                copies the Default profile again, drawn to `max_speed_kmh`
                (design_new.md "SpeedProfile", Giving a train a profile).

        Returns:
            The adopted `Train` (the same object the unidentified train used, re-keyed).

        Raises:
            ValueError: If the train is unknown or is not an unidentified train, if
                `new_train_id` is blank or already taken, if `direction` is not a
                compass name, if `head_tile` is not a tile the unidentified train claims, if
                both `head_tile` and `front_end_tile` are given, if the facing
                matches two ends and no `front_end_tile` names one, if
                `front_end_tile` is not such an end, if
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
        if head_tile is not None and front_end_tile is not None:
            raise ValueError(
                "Name either head_tile or front_end_tile, not both: a head tile "
                "already says where the front is"
            )
        head_offset_cm = 0.0
        if head_tile is None:
            head_tile, head_offset_cm = self._centered_adoption_placement(
                claimed_tiles,
                resolved_direction,
                float(length_cm),
                front_end_tile=(
                    (int(front_end_tile[0]), int(front_end_tile[1]))
                    if front_end_tile is not None
                    else None
                ),
            )
        else:
            head_tile = (int(head_tile[0]), int(head_tile[1]))
            if head_tile not in claimed_tiles:
                raise ValueError(
                    f"head_tile {head_tile!r} is not a tile unidentified train "
                    f"{unidentified_train_id!r} is standing on"
                )

        # The profile to copy must be the Default profile or a set; refuse
        # anything else before anything changes.
        self.network.speed_profiles.copy_source(speed_profile_id)

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

        # Copy the named profile into the instance profile the unidentified
        # train already owns, keeping its number; with none named, the Default
        # profile again, now drawn to the declared max_speed (design_new.md
        # "SpeedProfile", Giving a train a profile).
        profiles = self.network.speed_profiles
        if old_profile_id is not None and profiles.get(old_profile_id) is not None:
            profiles.copy_into_instance(old_profile_id, speed_profile_id, max_speed_kmh)
            train.speed_profile_id = old_profile_id
        else:
            train.speed_profile_id = profiles.create_instance(
                source_profile_id=speed_profile_id, max_speed_kmh=max_speed_kmh
            ).profile_id

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
        *,
        front_end_tile: Optional[Tuple[int, int]] = None,
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

        Which end is the front (design_new.md "Unidentified trains", Choosing
        the front end): the candidate ends are the claimed tiles whose exit in
        `direction` leaves the detected track. A bent run, such as an L- or
        U-shaped block, can have two. The facing then does not say which one
        the front is at, and only the operator can see it, so `front_end_tile`
        must name it; without one the request is refused rather than the
        runtime picking an end. A `front_end_tile` that is not a candidate is
        refused too.

        Falls back to the first claimed tile flush at its exit edge only when NO
        claimed tile can leave the detected track through this facing, which
        `place_train` then reports on its own terms.

        The detected length is the sum of the claimed tiles' lengths. An unidentified train
        holding two blocks joined by a switch tile is centered to within half
        that switch's length, since a switch carries no detection and so is not
        among the claimed tiles.

        Raises:
            ValueError: when the facing matches two ends and `front_end_tile`
                names neither, or when `front_end_tile` is not a candidate end.
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
        if front_end_tile is not None:
            if front_end_tile not in fronts:
                raise ValueError(
                    f"front_end_tile {front_end_tile!r} is not an end of the "
                    f"detected track whose exit faces {direction.name}"
                )
            front = front_end_tile
        elif not fronts:
            return claimed_tiles[0], 0.0
        elif len(fronts) > 1:
            ends = ", ".join(f"({x}, {y})" for (x, y) in fronts)
            raise ValueError(
                f"Both ends of the detected track face {direction.name} "
                f"({ends}); name which one the front is at with front_end_tile"
            )
        else:
            front = fronts[0]

        detected_length_cm = sum(
            self.network._get_tile_length_cm(x, y) for (x, y) in claimed_tiles
        )
        # Negative slack means the operator declared more train than the
        # detector sees. The head stays flush at the front and `place_train`
        # decides whether the overhang lands on undetected track (a switch tile,
        # which is fine) or on a block reading no current (which it refuses).
        return front, max(0.0, (detected_length_cm - length_cm) / 2.0)

    def _hand_blocks_to_unidentified(self, block_ids: Iterable[str]) -> Dict[str, Set[str]]:
        """Hand free blocks that read current to unidentified trains.

        The `UnidentifiedTrainTracker` connectivity step that attribution uses
        for an unmatched block decides which: connected blocks become one
        unidentified train, and a group reachable from an unidentified train
        that is already there joins it. No other train is considered. Used for
        the blocks an adopted consist does not reach and for the blocks a
        deleted train held that still read current (design_new.md "Unidentified
        trains", Creation and identity).

        Returns `unidentified_id -> block ids`.
        """
        manager = self.block_manager
        blocks = set(block_ids)
        if manager is None or not blocks:
            return {}
        attribution = self.unidentified_trains.attribute_unmatched_entries(
            blocks, self._unidentified_clock()
        )
        for unidentified_id, held in attribution.items():
            manager.occupy_blocks(sorted(held), unidentified_id)
            self.unidentified_trains.write_detected_span(unidentified_id, set(held), set())
        return attribution

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

        attribution = self._hand_blocks_to_unidentified(surplus)

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


def add_train_runtime_fields(
    payload: Dict[str, Any], sim_controller: "SimulationController"
) -> None:
    """Add the v2 live fields to a snapshot (design_v2.md "Live fields").

    - each block gets `reads_occupied`, `train_id`, `claimed_by_train_id` and
      `voltage`;
    - each switch gets `held`;
    - `running_trains` is the map from `train_id` to a train entry. The
      document calls this map `trains`; that key still carries the v1 train
      list until v1 is removed.

    Every field is worked out here on each read from the train runtime;
    nothing stores a second copy.
    """
    runtime = sim_controller.train_runtime
    held = runtime.held_switch_ids()
    # The map still colours a block from the v1 `state` and `occupied_by`
    # fields. In a session that runs the v2 step and holds no v1 train, those
    # are written here from the v2 trains, so the map shows them.
    draws_from_trains = not sim_controller.network.trains
    for block_id, entry in (payload.get("blocks") or {}).items():
        owner = runtime.train_of_block(block_id)
        claimer = runtime.claimer_of_block(block_id)
        if draws_from_trains:
            if owner is not None or block_id in runtime.occupied:
                entry["state"] = BlockState.OCCUPIED.value
                entry["occupied_by"] = owner.train_id if owner is not None else None
            elif claimer is not None:
                entry["state"] = BlockState.RESERVED.value
            else:
                entry["state"] = BlockState.FREE.value
        entry["reads_occupied"] = block_id in runtime.occupied
        entry["train_id"] = owner.train_id if owner is not None else None
        entry["claimed_by_train_id"] = claimer.train_id if claimer is not None else None
        entry["voltage"] = float(
            sim_controller.power_controller.get_recorded_voltage(block_id)
        )
    switches = payload.get("switches") or {}
    for switch_id, entry in (switches.items() if isinstance(switches, dict) else ()):
        entry["held"] = switch_id in held
        entry["alternate_next"] = runtime.alternate_next.get(switch_id)
    payload["running_trains"] = runtime.trains_payload()


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
        payload["operation_mode"] = operation_environment.DEFAULT_ENVIRONMENT.value
        payload["operation_environment"] = operation_environment.DEFAULT_ENVIRONMENT.value
        payload["environment_active"] = False
        payload["activation_issues"] = []
        payload["occupancy_is_real"] = False
        return payload

    payload["simulation_time"] = float(sim_controller.simulation_time)
    add_train_runtime_fields(payload, sim_controller)

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
