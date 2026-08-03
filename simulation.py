"""
Simulation Controller - Train Movement and Control

Definition:
This module provides the SimulationController class which runs the train
simulation using the TrainController + TrainPhysics architecture, following
the per-train three-phase transaction model defined in design_new.md.

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
from enums import TrainState, Direction, HandlePosition, OperationMode
import operation_environment
from block_power import BlockPowerController
from switch_power import (
    SimulatedSwitchDriver,
    SwitchOutputOwner,
    SwitchPowerController,
)
from block_occupancy import SimulatedOccupancyDriver, BlockOccupancyDetector
from notification_center import EventKind, NotificationCenter, Severity
from route_executor import RouteExecutor

from train_controller import TrainController, create_controller_for_train
from train_physics import TrainPhysics, create_physics_for_train
from runtime_payloads import (
    FatalConflictKind,
    FatalPhysicalEntryConflict,
    HardwareFailure,
)

if TYPE_CHECKING:
    from path_dispatcher import PathDispatcher

logger = logging.getLogger(__name__)


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

        # Run mode (design_new.md "Run modes"): the hardware backend is chosen
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

        # TrainController + TrainPhysics instances (shared across components)
        self.controllers: Dict[str, TrainController] = {}
        self.physics_instances: Dict[str, TrainPhysics] = {}

        # Trains this tick's Step-0 detector read attributed a block entry or
        # vacate to. Rewritten every `_detect_block_occupancy()`; read by the
        # Phase-3 gate for a still-OFF transaction (design_new.md ":2181").
        self._detected_change_train_ids: Set[str] = set()

        # Initialize components with shared controllers. The block/switch power
        # output interfaces come from the session-build override (real backend)
        # or default to the simulated interfaces (sim mode) — design_new.md
        # "Run modes", "the backend is chosen once when the session is built".
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
        # debounce/attribution detector (design_new.md "Power -> Block occupancy
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
            notify=lambda kind, message: self.notification_center.emit(
                source="BlockOccupancyDetector",
                kind=kind,
                severity=Severity.WARNING,
                payload={"message": message},
            ),
        )
        # Unidentified trains (design_new.md "Power -> Unidentified trains"): the Step-0 owner of
        # every confirmed entry that matches no existing train. It creates and
        # retires the unidentified train `Train` records; their block commits then run through
        # the ordinary per-train chain like any other train's.
        self.unidentified_trains = UnidentifiedTrainTracker(network, self)
        # The clock the unidentified train tracker's hold windows are measured against — see
        # `_unidentified_clock` for why it is wall-clock and not `simulation_time`.
        # Replaceable so a test can age a window out without sleeping.
        self.unidentified_clock: Callable[[], float] = time.monotonic
        self.route_executor = RouteExecutor(network, path_dispatcher, self.controllers)
        self.speed_profile_registry = network.speed_profiles
        self.simulation_time: float = 0.0
        # Speed calibration (design_new.md "Speed calibration"): measures a
        # locomotive's response and writes it into its SpeedProfile. Wired with
        # this controller so progress/results surface on the command_result
        # stream owned by NotificationCenter.
        from speed_calibration import SpeedCalibration

        self.speed_calibration = SpeedCalibration(self)

        # Wire back-references so peer components can reach the notification
        # center, the route executor, and each other.
        if self.path_dispatcher is not None:
            self.path_dispatcher.simulation_controller = self
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
        if self.block_manager is not None:
            self.block_manager.simulation_controller = self
            self.block_manager.network = self.network
        if self.switch_manager is not None:
            self.switch_manager.simulation_controller = self
            self.switch_manager.network = self.network

    # =========================================================================
    # Top-level update loop (per design_new.md "Core Runtime Loop")
    # =========================================================================

    def update(self, delta_seconds: float) -> None:
        """Advance the simulation by one tick.

        Per design_new.md per-train tick transaction:
          1. Advance simulation_time.
          2. RouteExecutor.update(delta_seconds) refreshes route-managed
             scheduling state (does NOT iterate trains).
          3. RouteExecutor.order_runnable_trains(...) returns the ordered
             runnable train ids. SimulationController owns running each
             train transaction in that order.
          4. For each ordered train, run Phase 1 (RouteExecutor planning,
             skipped while controller is OFF) followed by Phase 2 + Phase 3
             via _update_train(...).
          5. After all trains have committed, refresh signals from blocks.
        """
        self.simulation_time += delta_seconds

        # When a fatal physical-entry conflict has been reported, halt all
        # train ticks (Phase 1/2/3 skipped) for the rest of this session.
        # There is no release path in v1: the operator restarts the server
        # to begin a new session.
        if not self.environment_active or self.failed_runtime:
            return

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
            # Step 0 - Global block-current read + debounce + attribution
            # (design_new.md "Power -> Block occupancy detection"). One world-wide
            # current read per tick reflecting end-of-last-tick positions. Confirmed
            # entered/vacated blocks are attributed to trains and stashed per
            # controller; each train commits its slice in Phase 3 through the normal
            # on_controller_progress flow, and the precise position is snapped to the
            # block seam where the hardware leads it.
            self._detect_block_occupancy()
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
            # train's Phase 1 (design_new.md "Contention snapshot" + "Meet
            # coordination"): both read tick-start committed reservation state
            # plus the runnable order, and Phase-1 step 66aa consumes them.
            self.route_executor.build_tick_coordination(ordered_train_ids)

            for train_id in ordered_train_ids:
                train = self.network.trains.get(train_id)
                if train is None:
                    continue

                # Phase 1 - Setup. Run before the per-train transaction so the
                # planning kernel sits in the outer loop. Skip while the
                # controller is OFF; the Phase-2 command drain must run first so
                # that power_on_cmd() can be accepted.
                controller = self._get_or_create_controller(train)
                if controller.ops.state != TrainState.OFF:
                    self.route_executor.tick_train_phase1(train_id, delta_seconds)
                # Phase 1 may have reserved blocks, locked/aligned switches, or
                # (re)built this train's path — invalidate before Phase 2 reads.
                net.bump_lookahead_world_version()

                self._update_train(train, delta_seconds)
                # Phase 2/3 moved the head and may have completed/cancelled the
                # path — invalidate before the next train (and next tick) reads.
                net.bump_lookahead_world_version()

                # A fatal physical-entry conflict during this train's Phase 3
                # freezes the runtime "at the moment of detection"
                # (design_new.md:459 `b9a2`): abort the rest of this tick — no
                # further trains, and none of the post-loop voltage drain /
                # signal refresh / maintenance-counter writes below — so all
                # state is left exactly as it was for inspection until the
                # operator manually cleans up and releases the runtime failure.
                if self.failed_runtime:
                    return

            # Unidentified train identity follows connectivity, and only the committed
            # `Block.occupied_by` says which blocks each unidentified train ended the tick
            # holding — so the unidentified train reconciliation (merge, split, gap hold,
            # retirement) runs once here, after every train's Phase 3
            # (design_new.md "Power -> Unidentified trains").
            self.unidentified_trains.reconcile(self._unidentified_clock())

            self._settle_after_commits()

            # Maintenance counter: accrue occupied time for every block holding a
            # train this tick (see design_new.md "Maintenance counters").
            for _blk in self.network.blocks.values():
                if _blk.occupied_by is not None:
                    _blk.occupied_seconds += delta_seconds

            net.bump_lookahead_world_version()
        finally:
            net._lookahead_memo_active = False

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

    def _settle_after_commits(self) -> None:
        """Drain the de-energize queue and refresh signals, after every train's
        Phase 3 has committed.

        After every train has emitted its held-block voltages, drop any block
        whose holder cleared this pass (tail vacate / reservation release) to
        0V. BlockManager queued those blocks as their holders cleared and
        re-checks each is still unheld, so the energized set is exactly each
        train's occupied blocks plus its reserved-ahead corridor (design_new.md
        "Power", Block power-controller role). The all-blocks sweep is the
        fallback for a runtime with no BlockManager wired. Signals are then
        refreshed so they reflect every block state change committed above.

        Shared by the tick and by `poll_hardware_occupancy`: a detector-driven
        commit changes block state exactly like a moving train's does, so the
        voltage and signal consequences must follow it either way.

        **The calculated span is what decides, never debounced
        `Block.occupied_by`.** `BlockManager` queues a block on the debounced
        signal, which is right for queueing but wrong for the final test: a
        detector that briefly reads clear under a stationary locomotive would
        drop the rail out from under it. Cutting the rail then makes the block
        idle, the idle detector reads the locomotive again, the block
        re-energizes, and the drive-state read flips the occupancy source back —
        a block a train is standing on oscillates between powered and dead
        instead of settling. `_calculated_hold_blocks()` is the same
        calculated-span-plus-reservations set the emission side drives, so a
        block under a span is never zeroed however its detector reads
        (design_new.md "Power", De-energize against the same source).
        """
        held = self._calculated_hold_blocks()
        if self.block_manager is not None:
            for block_id in self.block_manager.take_blocks_to_deenergize():
                if block_id in held:
                    continue
                self.power_controller.set_block_voltage(block_id, 0.0)
        else:
            self.power_controller.deenergize_unheld_blocks(skip=held)

        self.network.update_signals_from_blocks()

    def _entries_for_adjacent_trains(self, unmatched: Set[str]) -> Dict[str, Set[str]]:
        """Give each unmatched confirmed entry to an ordinary train already
        holding a block next to it (design_new.md "Power", Attribution).

        A locomotive standing across a block boundary draws current on both
        blocks. The far one matches no reservation and no span, so without this
        it would be read as a second object and handed an unidentified train — which then
        de-energizes it, because an unidentified train is never energized, stranding the very
        locomotive the app is driving. Adjacency is the same connectivity walk
        unidentified train identity uses: only the first block across undetected track counts,
        so a block with empty blocks between it and every train is still an unidentified train.
        """
        out: Dict[str, Set[str]] = {}
        for block_id in sorted(unmatched):
            train_id = self._adjacent_train_for_block(block_id)
            if train_id is not None:
                out.setdefault(train_id, set()).add(block_id)
        return out

    def _adjacent_train_for_block(self, block_id: str) -> Optional[str]:
        """The ordinary train holding a block next to `block_id`, or None.

        The calculated span is tried before `Block.occupied_by` because the span
        is where the app is driving the train and it leads confirmed occupancy on
        entry — the block a train has just been given may not have confirmed yet.
        """
        for neighbour_id in sorted(self.unidentified_trains.reachable_blocks(block_id)):
            for train_id in sorted(self.controllers):
                train = self.network.trains.get(train_id)
                if train is None or getattr(train, "is_unidentified", False):
                    continue
                span = getattr(
                    getattr(self.controllers[train_id], "physics_sync", None),
                    "current_span",
                    None,
                )
                if span is not None and neighbour_id in (span.occupied_blocks or []):
                    return train_id
            block = self.network.blocks.get(neighbour_id)
            owner = getattr(block, "occupied_by", None) if block else None
            if owner is not None and not self.unidentified_trains.is_unidentified(owner):
                return owner
        return None

    def _calculated_hold_blocks(self) -> Set[str]:
        """Every block some controllable train holds this tick, by calculation.

        The union, over every train that is not an unidentified train, of its calculated span's
        `occupied_blocks`, its `pending-entry block`, and its reserved-ahead
        corridor — exactly the emission set `TrainPhysics` drives each tick
        (design_new.md "Power", Energized set: calculated span plus
        reservations). Unidentified trains are excluded because they command no
        `track_voltage`, so a block held only by an unidentified train is driven to `0`.
        """
        held: Set[str] = set()
        for train_id, physics in self.physics_instances.items():
            train = self.network.trains.get(train_id)
            if train is None or getattr(train, "is_unidentified", False):
                continue
            controller = self.controllers.get(train_id)
            span = getattr(
                getattr(controller, "physics_sync", None), "current_span", None
            )
            if span is not None:
                held.update(span.occupied_blocks or [])
            pending = physics.pending_entry_block
            if pending is not None:
                held.add(pending)
        for block_id, block in self.network.blocks.items():
            if getattr(block, "reserved_for_train", None) is not None:
                held.add(block_id)
        return held

    def poll_hardware_occupancy(self) -> None:
        """Read the block detectors and commit what they say, with the
        simulation stopped (design_new.md "Power -> Block occupancy detection").

        Real hardware does not stop reporting when the operator presses Stop. A
        loco lifted onto the rails, a hand-pushed cut of wagons, a train left
        standing from the last session — all of it is on the track and the
        detectors see it, so the app has to show it. Without this the '165 is
        only ever scanned from inside `update()`, which the runtime loop skips
        while stopped, and every block reads FREE no matter what is standing on
        it — including at boot, which is stopped by definition.

        This is the tick's Step 0 and its commit, and nothing else: no Phase-1
        planning, no Phase-2 command drain, no physics integration. Stopped has
        to keep meaning stopped — queued operator commands still wait for the
        resume, no route is planned, no train is moved. What does run is the
        path design_new.md already defines for an occupant the app does not
        drive (":2181"): Step 0 attributes the confirmed transitions, each
        affected train enqueues a detected-blocks-only report, and its Phase 3
        commits it.

        Sim mode is left alone. Its simulated detector derives current from the
        live spans, so a poll with no motion between it and the last one can
        only re-report what is already committed — real work for a guaranteed
        no-op, on every idle pass.
        """
        if not self.environment_active or self.operation_mode is OperationMode.SIMULATION:
            return
        if self.failed_runtime:
            return

        net = self.network
        net._lookahead_memo_active = True
        net.bump_lookahead_world_version()
        try:
            self._detect_block_occupancy()
            net.bump_lookahead_world_version()

            # Only trains Step 0 attributed something to: every other train's
            # slice is empty, so its Phase 3 would have nothing to commit.
            for train_id in sorted(self._detected_change_train_ids):
                train = self.network.trains.get(train_id)
                controller = self.controllers.get(train_id)
                if train is None or controller is None:
                    continue
                # The Phase-2 physics callback would normally turn the slice
                # into a progress report; there is no Phase 2 here, so the
                # controller is asked for that report directly.
                controller.enqueue_detected_blocks_report(time.time())
                self._commit_train_phase3(controller, train)
                net.bump_lookahead_world_version()
                if self.failed_runtime:
                    return

            self.unidentified_trains.reconcile(self._unidentified_clock())
            self._settle_after_commits()
            net.bump_lookahead_world_version()
        finally:
            net._lookahead_memo_active = False

    def enter_safe_state(self) -> None:
        """Put all hardware outputs into their safe state — every block track
        voltage at 0 and every switch motor de-energized (design_new.md Power,
        Hardware interface boundary).

        The runtime issues this when it stops driving outputs (simulation stop
        or process shutdown). Track voltage routes through `BlockPowerController`
        and switch throw through `SwitchPowerController`; each controller is the
        only caller of its own output interface.
        """
        if self.power_controller is not None:
            self.power_controller.enter_safe_state()
        if self.switch_power_controller is not None:
            self.switch_power_controller.enter_safe_state()

    def activate_interfaces(
        self,
        environment: OperationMode,
        input_interface: Optional[Any],
        block_output_interface: Optional[Any],
        switch_output_interface: Optional[Any],
    ) -> None:
        """Atomically replace the whole environment boundary while stopped."""
        self.enter_safe_state()
        self.environment_active = False
        self.operation_mode = operation_environment.parse(environment)

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
            notify=lambda kind, message: self.notification_center.emit(
                source="BlockOccupancyDetector",
                kind=kind,
                severity=Severity.WARNING,
                payload={"message": message},
            ),
        )
        self.failed_runtime = False
        self.failed_runtime_payload = None
        self.failed_runtime_at = None
        self.activation_issues = []
        self.environment_active = True

    def deactivate_environment(
        self, issues: List[Dict[str, Any]], environment: Optional[OperationMode] = None
    ) -> None:
        """Leave outputs safe and expose why the selected environment is inactive."""
        self.enter_safe_state()
        if environment is not None:
            self.operation_mode = operation_environment.parse(environment)
        self.environment_active = False
        self.activation_issues = list(issues)

    def _detect_block_occupancy(self) -> None:
        """Step 0 of the tick: read every block's current detector, debounce,
        attribute confirmed transitions to trains, and stash each train's
        entered/vacated block slice for its Phase-3 commit (design_new.md
        "Power -> Block occupancy detection"). The controller drains its slice in
        on_physics_updated and the existing on_controller_progress flow commits
        Block.occupied_by.

        Also records which trains this tick's read attributed a block change to,
        in `_detected_change_train_ids`. Phase 2 consumes and clears the pending
        sets themselves, so that set is the only thing left by Phase-3 time that
        can answer "did Step 0 attribute a block entry or vacate to this train
        this tick?" — the condition under which a still-OFF transaction must run
        Phase 3 anyway (design_new.md ":2181").
        """
        detector = self.block_occupancy_detector

        # Adopt externally-committed occupancy (place_train / restore) as already
        # confirmed so a placed-on block does not read as a fresh entry.
        detector.seed_occupied(
            block_id
            for block_id, block in self.network.blocks.items()
            if block.occupied_by is not None
        )

        entered, vacated = detector.step()
        entered_by_train, vacated_by_train, unmatched = detector.attribute(
            entered, vacated
        )

        # A confirmed entry next to a block an ordinary train already holds is
        # that train standing across the boundary — one train on two blocks, not
        # a new object (design_new.md "Attribution"). This runs before the unidentified train
        # step, so an unidentified train is minted only for current that is genuinely
        # disconnected from every train.
        if unmatched:
            for train_id, block_ids in self._entries_for_adjacent_trains(
                unmatched
            ).items():
                entered_by_train.setdefault(train_id, set()).update(block_ids)
                unmatched -= block_ids

        # What is left matches no existing train and is not next to one, so it
        # belongs to an unidentified train (design_new.md "Attribution"):
        # `UnidentifiedTrainTracker` resolves it to an unidentified train `train_id` — a new unidentified train, or
        # one continuing its run across an undetected switch tile — and from here
        # the unidentified train is an ordinary train, so its slice is written and committed
        # exactly like every other train's.
        if unmatched:
            for unidentified_id, block_ids in self.unidentified_trains.attribute_unmatched_entries(
                unmatched, self._unidentified_clock()
            ).items():
                entered_by_train.setdefault(unidentified_id, set()).update(block_ids)

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
                # Repeat the containment before Phase 1 on every detector read,
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
        grounds to keep resources from other trains. So on the tick the flag
        rises: the route run is torn down, the active path is cancelled (which
        releases its reservations and switch locks), every block the train
        still holds is released — each such block reads confirmed clear, which
        is what raised the flag, so no detector state is contradicted — and the
        controller is written straight to `OFF` at zero speed. The direct write
        rather than `power_off_cmd()` for the same reasons `clear_failed_runtime`
        uses one: that command is stopped-only against a possibly non-zero
        frozen speed, and it defers to a later Phase 3. The span stays as the
        last estimate, so the question mark has a place on the layout; the
        record itself is kept for the operator to remove.
        """
        route_executor = getattr(self, "route_executor", None)
        if route_executor is not None:
            route_executor.remove_train_route_run(train_id)

        dispatcher = self.path_dispatcher
        if dispatcher is not None:
            path_id = dispatcher.active_path_id_by_train(train_id)
            if path_id:
                dispatcher.cancel_path(
                    path_id,
                    initiator="presence_unknown",
                    reason="no occupancy backs this train's position",
                )

        for block_id, block in list(self.network.blocks.items()):
            if getattr(block, "occupied_by", None) == train_id:
                if self.block_manager:
                    self.block_manager.clear_block(block_id)
                else:
                    block.release()

        self._freeze_lost_train_controller(controller)

        if notify:
            self.notification_center.emit(
                source="SimulationController",
                kind="train_presence_unknown",
                severity=Severity.WARNING,
                event_time=time.time(),
                train_id=train_id,
                payload={
                    "message": (
                        f"no occupancy detected under {train_id}; it was stopped "
                        "and its route, path, and blocks were released"
                    ),
                },
            )

    @staticmethod
    def _freeze_lost_train_controller(controller: TrainController) -> None:
        """Keep an unconfirmed identifier inert until detection returns.

        Step 0 calls this on the rising edge after releasing resources, and the
        per-train loop calls it again while ``presence_unknown`` remains true.
        Reapplying it matters: a command accepted just before the detector read
        must not revive a record that no longer represents a detected train.
        """
        controller.commands.command_inbox.clear()
        controller.ops.state = TrainState.OFF
        controller.ops.handle_position = HandlePosition.N
        controller.manual.reset_power_percent_for_safety()
        controller.physics_sync.current_speed_kmh = 0.0
        controller.physics_sync.queued_progress_reports.clear()
        controller.eb.active = False
        controller.ato.running = False

    # =========================================================================
    # Per-train three-phase transaction
    # =========================================================================

    def _update_train(self, train: Train, delta_seconds: float) -> None:
        """Run Phase 2 + Phase 3 of one per-train transaction.

        Phase 1 (`RouteExecutor.tick_train_phase1(...)`) runs in the outer
        `update()` loop immediately before this call.

        Phase 2 - Execute: TrainController.tick(...) drains operator commands
            then runs control logic; TrainPhysics.tick(...) integrates motion
            and calls TrainController.on_physics_updated(...) per Agent D.
        Phase 3 - Feedback / cleanup: drain queued progress reports, run the
            post-physics path-completion check, then consume the latch table
            in canonical order.
        """
        controller = self._get_or_create_controller(train)
        physics = self._get_or_create_physics(train)

        # A question-mark identifier has no physical occupancy backing its
        # estimate.  It is a diagnostic record, not a train to keep predicting:
        # do not drain commands or integrate physics while presence is unknown.
        # Step 0 clears the flag automatically if detection returns, at which
        # point the ordinary transaction resumes from the retained last span.
        if controller.presence_unknown:
            self._freeze_lost_train_controller(controller)
            self._sync_persistent_train_state(train, controller)
            return

        # Phase 2 - Execute.
        # TrainController.tick(...) begins by draining the queued operator
        # commands; TrainPhysics.tick(...) computes motion and invokes
        # TrainController.on_physics_updated(...) at least once per tick.
        controller.tick(delta_seconds)
        physics.tick(delta_seconds)

        self._commit_train_phase3(controller, train)

    def _commit_train_phase3(
        self, controller: TrainController, train: Train
    ) -> None:
        """Phase 3 - Feedback / cleanup for one train.

        Split out of `_update_train` because the stopped-simulation hardware
        poll runs this phase and nothing else: it has detector observations to
        commit but no Phase-1 planning and no Phase-2 execution to run first
        (see `poll_hardware_occupancy`).

        If the controller is still OFF after the command drain, Phase 3 is
        skipped for this transaction even though Phase 2 physics ran — unless
        Step 0 attributed a block entry or vacate to this train this tick
        (design_new.md ":2181", ":2641"). The skip is about having no movement
        to commit, not about power: a train that moves without power — pushed
        by hand, or an occupant the app does not control — still has its block
        entries and vacates committed, and Phase 3 is the only path that
        commits them.
        """
        if (
            controller.ops.state == TrainState.OFF
            and train.train_id not in self._detected_change_train_ids
        ):
            self._sync_persistent_train_state(train, controller)
            return

        # 3a. Drain queued progress reports in FIFO order.
        if self.path_dispatcher is not None:
            while controller.physics_sync.queued_progress_reports:
                report = controller.physics_sync.queued_progress_reports.pop(0)
                self.path_dispatcher.on_controller_progress(report)

        # 3b. Post-physics usable-authority stop check + path completion.
        # A pending path-cancel outranks completion (design_new.md step `c6f1`):
        # when `pending_path_cancel_request` is set, skip completion so step 3c
        # commits the cancellation instead of finishing the stop.
        if self.path_dispatcher is not None:
            if controller.route_hooks.phase_latches.path_cancel_request is None:
                if self.path_dispatcher.active_path_id_by_train(train.train_id) is not None:
                    if controller.is_stopped_at_authority_end():
                        self.path_dispatcher.try_complete_active_path(train.train_id)

        # 3c. Consume Phase-3 latches in canonical order.
        self._consume_pending_path_cancel_request(controller, train)
        self._consume_pending_route_abort(controller, train)
        self._consume_pending_power_off(controller, train)
        self._consume_pending_route_deactivate(controller, train)

        # Sync only persistent/configuration state back to Train. Live state
        # (target_speed, speed_limit_reason, controller state, current_speed)
        # lives on the controller; consumers read from the controller directly.
        self._sync_persistent_train_state(train, controller)

    # =========================================================================
    # Phase-3 latch consumers (per design_new.md latch consumption rules)
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
            controller.route_hooks.phase_latches.consume_path_cancel_request()
            return

        payload = controller.route_hooks.phase_latches.consume_path_cancel_request()

        active_path_id = self.path_dispatcher.active_path_id_by_train(train.train_id)
        if active_path_id is not None:
            # design_new.md line 270 + step `389a`: a movement-derived deviation
            # latch carries `deviation_kind`; forward it from the latch payload
            # into the commit so it reaches the `path_cancellation` notification.
            self.path_dispatcher.cancel_path(
                active_path_id,
                initiator=payload.initiator,
                reason=payload.reason,
                deviation_kind=payload.deviation_kind,
                route_progress_kind=payload.route_progress_kind,
            )
        else:
            # design_new.md fix #18: emit INFO notification instead of logger.debug.
            self.notification_center.emit(
                source="SimulationController",
                kind="diagnostic",
                severity=Severity.INFO,
                event_time=time.time(),
                train_id=train.train_id,
                payload={"initiator": payload.initiator},
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

        self.route_executor.remove_train_route_run(train.train_id)
        controller.route_hooks.phase_latches.consume_route_abort()

    def _consume_pending_power_off(
        self, controller: TrainController, train: Train
    ) -> None:
        """Consume `phase_latches.power_off` after the inclusive cancel has
        committed.

        Per design_new.md: the inclusive `phase_latches.path_cancel_request`
        was armed by `_apply_power_off_request()` and committed earlier in the
        same Phase-3 pass (or correctly skipped when no active path existed).
        This consumer performs only its own teardown — route-run removal plus
        controller-local OFF cleanup. It does NOT call
        `PathDispatcher.cancel_path(...)`.
        """
        if not controller.route_hooks.phase_latches.power_off:
            return

        # Per design_new.md Latch Table: gated by
        # `phase_latches.path_cancel_request is None`. The inclusive cancel
        # was armed by `_apply_power_off_request()` and either committed
        # earlier in this same Phase-3 pass or was never written (no live
        # path existed). If the inclusive cancel is still pending the train
        # was not stopped this tick — defer to a later one.
        if controller.route_hooks.phase_latches.path_cancel_request is not None:
            return

        self.route_executor.remove_train_route_run(train.train_id)

        # Controller-owned OFF cleanup. `complete_power_off()` is the single
        # OFF-writing entry point besides startup (design_new.md:282, :1808):
        # it sets `state = OFF`, clears cab / eb.active / ato.running, and
        # consumes `pending_power_off`.
        controller.complete_power_off()

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

        # Per design_new.md:1461 ("Shared-route edit teardown"): a held
        # re-assignment is applied only when THIS consumer owned the run teardown
        # — i.e. its own remove_train_route_run cleared a still-RUNNING run. If an
        # earlier abort/power-off consumer (or a natural finish) already ended the
        # run, the operator/admin teardown wins and the stash is discarded, so a
        # powered-off or aborted train is never silently re-assigned to the route.
        owned_teardown = (
            self.route_executor.execution_state_for(train.train_id) is not None
        )
        self.route_executor.remove_train_route_run(train.train_id)
        if owned_teardown:
            self.route_executor.apply_pending_reassign(train.train_id)
        else:
            self.route_executor.discard_pending_reassign(train.train_id)
        controller.route_hooks.phase_latches.consume_route_deactivate()

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
            # Per design_new.md ~line 1292, live operational state
            # (`state`, `active_cab`, `ato_running`, `control_mode`) lives
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

        # Seed the train's default SpeedProfile so `power_percent` manual mode
        # has a defined power->speed mapping anchored to THIS train's max_speed
        # (design_new.md:2235, :2257, :2259): the default `power_speed_curve` is
        # linear from 0% -> 0 km/h to 100% -> the train's configured max_speed.
        # Without this the train shares the module-global DEFAULT_SPEED_PROFILE
        # whose curve hardcodes 100% -> 100 km/h, which is wrong for any train
        # whose max_speed != 100. Register it and point the train at it.
        if train.speed_profile_id is None and hasattr(
            self.network, "speed_profiles"
        ):
            from speed_profile import make_default_speed_profile

            default_profile = make_default_speed_profile(train_id, max_speed_kmh)
            self.network.speed_profiles.register(default_profile)
            train.speed_profile_id = default_profile.profile_id

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
                    kind="signal_refresh_failed",
                    severity=Severity.WARNING,
                    event_time=time.time(),
                    train_id=train_id,
                    payload={"context": "add_train", "reason": str(exc)},
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

        # If an initial cab was requested, write it directly. add_train runs
        # before the first tick, so this bypasses the command queue by design.
        # The cab is the head indicator (design_new.md): the controller derives
        # head/tail from (cab, current_span); the span carries no head label.
        if initial_cab is not None:
            controller.commands.cab = initial_cab

        # Emit the TRAIN_PLACED event-log event (design_new.md "Event log";
        # the canonical kind is `train_placed`, the operational vocab name).
        self.notification_center.emit(
            source="SimulationController",
            kind=EventKind.TRAIN_PLACED,
            severity=Severity.INFO,
            event_time=time.time(),
            train_id=train_id,
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
        if cab is not None:
            controller.commands.cab = cab

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

        # The unidentified train's default profile was anchored to a blank train's max_speed
        # and named after the generated id, so it is retired in favour of one
        # built for the real locomotive — unless the operator named an existing
        # profile (or a roster entry did) to keep measured curves attached.
        profiles = getattr(self.network, "speed_profiles", None)
        if speed_profile_id and profiles is not None and profiles.get(speed_profile_id):
            train.speed_profile_id = speed_profile_id
        elif profiles is not None:
            from speed_profile import make_default_speed_profile

            profile = make_default_speed_profile(new_train_id, max_speed_kmh)
            profiles.register(profile)
            train.speed_profile_id = profile.profile_id
        if (
            profiles is not None
            and old_profile_id
            and old_profile_id != train.speed_profile_id
        ):
            profiles.remove(old_profile_id)

        # ---- Give back what it does not actually stand on. ------------------
        span_blocks = set(getattr(placed_span, "occupied_blocks", None) or [])
        surplus = sorted(set(held_blocks) - span_blocks)
        self._hand_surplus_blocks_to_unidentified(new_train_id, surplus)

        try:
            self.network.update_signals_from_blocks()
        except Exception as exc:
            self.notification_center.emit(
                source="SimulationController",
                kind="signal_refresh_failed",
                severity=Severity.WARNING,
                event_time=time.time(),
                train_id=new_train_id,
                payload={"context": "adopt_unidentified_train", "reason": str(exc)},
            )

        self.notification_center.emit(
            source="SimulationController",
            kind=EventKind.TRAIN_PLACED,
            severity=Severity.INFO,
            event_time=time.time(),
            train_id=new_train_id,
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

        Falls back to the first claimed tile flush at its exit edge when the
        detected track has no single front tile for this facing — a ring of
        blocks with no end, or a facing no claimed tile can exit through, which
        `place_train` then reports on its own terms.

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
        if len(fronts) != 1:
            return claimed_tiles[0], 0.0

        detected_length_cm = sum(
            self.network._get_tile_length_cm(x, y) for (x, y) in claimed_tiles
        )
        # Negative slack means the operator declared more train than the
        # detector sees. The head stays flush at the front and `place_train`
        # decides whether the overhang lands on undetected track (a switch tile,
        # which is fine) or on a block reading no current (which it refuses).
        return fronts[0], max(0.0, (detected_length_cm - length_cm) / 2.0)

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

        # Ordinary Step-0 attribution: connected surplus blocks become one unidentified train,
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
            kind="adoption_surplus_blocks",
            severity=Severity.WARNING,
            event_time=time.time(),
            train_id=train_id,
            payload={
                "message": (
                    f"{', '.join(sorted(surplus))} still read current but "
                    f"{train_id} is not long enough to reach them; they are "
                    f"tracked as unidentified trains. If that is really {train_id}, "
                    f"adopt it again with the full consist length."
                ),
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
        # so the rename leaves no stale id behind if that ever changes.
        for switch in getattr(self.network, "switches", {}).values():
            if getattr(switch, "occupied_by", None) == old_train_id:
                switch.occupied_by = new_train_id
            occupants = getattr(switch, "physical_occupants", None)
            if isinstance(occupants, dict):
                for route, owner in list(occupants.items()):
                    if owner == old_train_id:
                        occupants[route] = new_train_id

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
        occupancy by calculation (design_new.md "Run modes", point 3). It is
        what gates unidentified train adoption and what the UI reads to stop calling a
        railway with live detectors a simulation.
        """
        return not isinstance(self.input_interface, SimulatedOccupancyDriver)

    def enter_failed_runtime(self, payload: FatalPhysicalEntryConflict) -> None:
        """Halt further train ticks until manually released.

        Before freezing, calls :meth:`enter_safe_state` on every configured
        block and switch output interface, zeroing track voltage and
        switching off point motors — the safe-state write touches hardware
        outputs only, never controller/path/route/resource/occupancy state.
        Then sets :attr:`failed_runtime`, stores the typed fatal payload for
        inspection, and emits a FATAL notification carrying the conflict
        details. While the flag is set, :meth:`update` only advances
        ``simulation_time`` and skips Phase 1/2/3 for every train.
        """
        self.enter_safe_state()
        self.failed_runtime = True
        self.failed_runtime_payload: Optional[FatalPhysicalEntryConflict] = payload
        self.failed_runtime_at = self.simulation_time
        self.notification_center.emit(
            source="SimulationController",
            kind="fatal_runtime",
            severity=Severity.FATAL,
            event_time=time.time(),
            train_id=payload.incoming_train_id,
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

        This is idempotent because one bus failure can be observed by several
        output calls in the same tick. The first failure is the canonical cause.
        The flag is latched even if a best-effort safe-state write itself fails.
        """
        if self.failed_runtime:
            return
        safe_state_error: Optional[str] = None
        try:
            self.enter_safe_state()
        except Exception as exc:  # noqa: BLE001 - the failure still must latch
            safe_state_error = str(exc)
            logger.exception("hardware safe-state command failed")
        finally:
            self.failed_runtime = True
            self.failed_runtime_payload = payload
            self.failed_runtime_at = self.simulation_time

        self.notification_center.emit(
            source="SimulationController",
            kind="hardware_failure",
            severity=Severity.FATAL,
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

    def clear_failed_runtime(self) -> FatalPhysicalEntryConflict:
        """Release the halt so the session can carry on, with every train off.

        A fatal conflict says two trains claim one piece of track. On a real
        layout the operator goes and sorts that out — moves the loco, fixes the
        detector, lifts the stray car off the rail — and then wants to keep
        running. Making them restart the process to do it would throw away the
        whole layout state for a problem they have already fixed, so the halt is
        a gate that reopens rather than a session-ending error.

        Reopening it does not resume mid-motion. Every train is written straight
        to `OFF` at zero speed with its handle at `N` and any EB cleared, so
        nothing moves until the operator powers it back up deliberately. This is
        a direct write rather than a queued `power_off_cmd()`, for two reasons:
        the command is stopped-only and the frozen speed may be non-zero, and it
        defers its work to a Phase 3 that cannot run while the gate is shut.
        Track voltage stays at the `0` `enter_safe_state()` left it at; the next
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
        if isinstance(payload, HardwareFailure):
            raise ValueError(
                "A hardware failure cannot be cleared in place; repair the "
                "hardware and activate a newly validated session"
            )
        for controller in self.controllers.values():
            controller.ops.state = TrainState.OFF
            controller.ops.handle_position = HandlePosition.N
            controller.physics_sync.current_speed_kmh = 0.0
            controller.eb.active = False
            controller.ato.running = False

        self.failed_runtime = False
        self.failed_runtime_payload = None
        self.failed_runtime_at = None

        self.notification_center.emit(
            source="SimulationController",
            kind="fatal_runtime_cleared",
            severity=Severity.WARNING,
            event_time=time.time(),
            train_id=payload.incoming_train_id if payload is not None else None,
            payload={
                "message": (
                    "fatal conflict cleared by the operator; every train is off "
                    "and the runtime is running again"
                ),
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
        authority_end_block_id: str,
        requested_destination_direction: str,
    ) -> None:
        """Forward a path-completion event to the RouteExecutor and emit a
        notification.

        Carries no timestamp: per design_new.md "Time sampling" each writer
        samples wall-clock time at its own write site, so `RouteExecutor` dates
        `finished_at` itself and `emit(...)` dates the notification itself.
        """
        self.route_executor.handle_path_completion(
            train_id,
            path_id,
            authority_end_block_id,
            requested_destination_direction,
        )
        self.notification_center.emit(
            source="SimulationController",
            kind="path_completion",
            severity=Severity.INFO,
            train_id=train_id,
            path_id=path_id,
            payload={
                "authority_end_block_id": authority_end_block_id,
                "requested_destination_direction": requested_destination_direction,
            },
        )

    def handle_path_cancellation(
        self,
        train_id: str,
        path_id: str,
        initiator: str,
        reason: str,
        event_time: float,
    ) -> None:
        """Forward a path-cancellation event to the RouteExecutor and emit the
        canonical `path_cancellation` notification.

        Per design_new.md, the `initiator` field is the writer-supplied
        free-form string (``"eb"``, ``"operator_cancelled"``, ``"power_off"``,
        ``"deviation"``, ``"revoked"``, …); routing/planning layers must not
        branch on it.
        """
        # Per design_new.md line 1559: the diagnostic `initiator` does not cross
        # the route callback boundary; it is carried only on the notification
        # emitted below.
        self.route_executor.handle_path_cancellation(train_id, path_id, reason)
        self.notification_center.emit(
            source="SimulationController",
            kind="path_cancellation",
            severity=Severity.WARNING,
            event_time=event_time,
            train_id=train_id,
            path_id=path_id,
            payload={"initiator": initiator, "reason": reason},
        )


def build_network_payload(
    network: RailwayNetwork,
    sim_controller: Optional["SimulationController"],
) -> Dict[str, Any]:
    """Build network payload in the canonical sibling-payload shape.

    Per design_new.md "Data Flow To UI" (~line 2309), live runtime data
    is exposed as sibling maps alongside the persistent `trains` list,
    never merged into Train.to_dict() output:

      - `controllers_by_train_id`: TrainController.to_dict() per train
      - `dispatcher_progress_by_train_id`: PathDispatcher progress per train
      - `assigned_routes_by_train_id`: RouteExecutor-derived route id per train
      - `route_runs_by_train_id`: RouteExecutor 5-field summary
        ({route_id, run_number, state, display_state, last_error}) of the
        train's currently-RUNNING RouteRun, or None
    """
    payload = network.to_dict()

    if not sim_controller:
        payload["controllers_by_train_id"] = {}
        payload["dispatcher_progress_by_train_id"] = {}
        payload["assigned_routes_by_train_id"] = {}
        payload["route_runs_by_train_id"] = {}
        payload["simulation_time"] = 0.0
        payload["failed_runtime"] = False
        payload["failed_runtime_conflict"] = None
        payload["operation_mode"] = OperationMode.SIMULATION.value
        payload["operation_environment"] = OperationMode.SIMULATION.value
        payload["environment_active"] = False
        payload["activation_issues"] = []
        payload["occupancy_is_real"] = False
        return payload

    payload["simulation_time"] = float(sim_controller.simulation_time)

    path_dispatcher = getattr(sim_controller, "path_dispatcher", None)
    route_executor = getattr(sim_controller, "route_executor", None)

    controllers_by_train_id: Dict[str, Any] = {}
    dispatcher_progress_by_train_id: Dict[str, Any] = {}
    assigned_routes_by_train_id: Dict[str, Any] = {}
    route_runs_by_train_id: Dict[str, Any] = {}

    for train_id, train in network.trains.items():
        controller = sim_controller._get_or_create_controller(train)
        controllers_by_train_id[train_id] = controller.to_dict()

        if path_dispatcher is not None:
            try:
                dispatcher_progress_by_train_id[train_id] = (
                    path_dispatcher.get_active_path_progress(train_id)
                )
            except Exception as e:
                dispatcher_progress_by_train_id[train_id] = None
        else:
            dispatcher_progress_by_train_id[train_id] = None

        if route_executor is not None:
            try:
                assigned_routes_by_train_id[train_id] = (
                    route_executor.assigned_route_id_for(train_id)
                )
            except Exception as e:
                assigned_routes_by_train_id[train_id] = None
            try:
                route_runs_by_train_id[train_id] = (
                    route_executor.route_run_summary_for(train_id)
                )
            except Exception as e:
                route_runs_by_train_id[train_id] = None
        else:
            assigned_routes_by_train_id[train_id] = None
            route_runs_by_train_id[train_id] = None

    payload["controllers_by_train_id"] = controllers_by_train_id
    payload["dispatcher_progress_by_train_id"] = dispatcher_progress_by_train_id
    payload["assigned_routes_by_train_id"] = assigned_routes_by_train_id
    payload["route_runs_by_train_id"] = route_runs_by_train_id

    # Run mode (design_new.md "Run modes"). The UI reads these to stop calling a
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
    hardware_failure = (
        failed_payload if isinstance(failed_payload, HardwareFailure) else None
    )
    payload["failed_runtime_kind"] = (
        "hardware_failure"
        if hardware_failure is not None
        else "physical_entry_conflict" if fatal is not None else None
    )
    payload["failed_runtime_hardware_failure"] = (
        {
            "component": hardware_failure.component,
            "operation": hardware_failure.operation,
            "resource_id": hardware_failure.resource_id,
            "message": hardware_failure.message,
            "halted_at_sim_time": sim_controller.failed_runtime_at,
            "requires_new_activation": True,
        }
        if hardware_failure is not None
        else None
    )

    return payload
