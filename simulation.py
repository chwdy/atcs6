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
from typing import Any, Dict, Literal, Optional, Set, Tuple, TYPE_CHECKING

from constants import UNKNOWN_TRAIN_ID
from network import RailwayNetwork
from train import Train, TrainCar
from enums import TrainState, Direction, OperationMode
from block_power import BlockPowerController
from switch_power import SwitchPowerController
from block_occupancy import SimulatedInputInterface, BlockOccupancyDetector
from notification_center import EventKind, NotificationCenter, Severity
from route_executor import RouteExecutor

from train_controller import TrainController, create_controller_for_train
from train_physics import TrainPhysics, create_physics_for_train
from runtime_payloads import (
    FatalConflictKind,
    FatalPhysicalEntryConflict,
)

if TYPE_CHECKING:
    from path_dispatcher import PathDispatcher

logger = logging.getLogger(__name__)


class SimulationController:
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
            else OperationMode(operation_mode)
        )
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
        self.failed_runtime_payload: Optional[FatalPhysicalEntryConflict] = None
        # simulation_time at which the halt was entered, for the UI banner.
        self.failed_runtime_at: Optional[float] = None

        # TrainController + TrainPhysics instances (shared across components)
        self.controllers: Dict[str, TrainController] = {}
        self.physics_instances: Dict[str, TrainPhysics] = {}

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
        self.switch_power_controller = (
            SwitchPowerController(
                network, output_interface=self._switch_output_interface_override
            )
            if self._switch_output_interface_override is not None
            else SwitchPowerController(network)
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
            else SimulatedInputInterface(network, self.controllers)
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
        if self.failed_runtime:
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

            # After every train has emitted its held-block voltages, drop any block
            # whose holder cleared this tick (tail vacate / reservation release) to
            # 0V. BlockManager queued those blocks as their holders cleared and
            # re-checks each is still unheld, so the energized set is exactly each
            # train's occupied blocks plus its reserved-ahead corridor (design_new.md
            # "Power", Block power-controller role). The all-blocks sweep is the
            # fallback for a runtime with no BlockManager wired.
            if self.block_manager is not None:
                for block_id in self.block_manager.take_blocks_to_deenergize():
                    self.power_controller.set_block_voltage(block_id, 0.0)
            else:
                self.power_controller.deenergize_unheld_blocks()

            # After all trains, refresh signals so they reflect every block state
            # change (including path completions) committed during the tick.
            self.network.update_signals_from_blocks()

            # Maintenance counter: accrue occupied time for every block holding a
            # train this tick (see design_new.md "Maintenance counters").
            for _blk in self.network.blocks.values():
                if _blk.occupied_by is not None:
                    _blk.occupied_seconds += delta_seconds

            net.bump_lookahead_world_version()
        finally:
            net._lookahead_memo_active = False

    def enter_safe_state(self) -> None:
        """Put all hardware outputs into their safe state — every block track
        voltage at 0 and every switch motor de-energized (design_new.md Power,
        Hardware interface boundary).

        The runtime issues this when it stops driving outputs (simulation stop
        or process shutdown). It routes through the power controllers, the only
        callers of the output interfaces.
        """
        if self.power_controller is not None:
            self.power_controller.enter_safe_state()
        if self.switch_power_controller is not None:
            self.switch_power_controller.enter_safe_state()

    def _detect_block_occupancy(self) -> None:
        """Step 0 of the tick: read every block's current detector, debounce,
        attribute confirmed transitions to trains, and stash each train's
        entered/vacated block slice for its Phase-3 commit (design_new.md
        "Power -> Block occupancy detection"). The controller drains its slice in
        on_physics_updated and the existing on_controller_progress flow commits
        Block.occupied_by.
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
        entered_by_train, vacated_by_train = detector.attribute(entered, vacated)

        # An unmodeled occupant has no per-train Phase-3 transaction, so the
        # preamble commits its slice here — entry through the single writer
        # `BlockManager.occupy_blocks(...)`, which reports the fatal conflict
        # itself when the block is already held by another train (design_new.md
        # "Attribution"). Committed one block at a time so a conflict on one
        # block does not roll back a clean entry on another.
        self._commit_unknown_occupancy(
            entered_by_train.pop(UNKNOWN_TRAIN_ID, set()),
            vacated_by_train.pop(UNKNOWN_TRAIN_ID, set()),
        )

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

        # Physical detection outranks the calculated position: snap each affected
        # train's span forward to the block seam where the detector leads it
        # (inert under the v1 simulated sensor; active with real hardware).
        for train_id in sorted(set(entered_by_train) | set(vacated_by_train)):
            controller = self.controllers.get(train_id)
            train = self.network.trains.get(train_id)
            if controller is None or train is None:
                continue
            self._get_or_create_physics(train).reconcile_to_detected_occupancy(
                train, controller
            )

    def _commit_unknown_occupancy(
        self, entered: Set[str], vacated: Set[str]
    ) -> None:
        """Commit the unmodeled-occupant slice of the Step-0 detector read.

        Entry routes through `BlockManager.occupy_blocks(...)` so the
        `occupied_by` single-writer rule holds and an entry onto a block another
        train already holds raises the fatal physical-entry conflict rather than
        silently clobbering its owner. The clear is a direct field write: an
        unknown occupant carries no traversal direction, so it has no CAUTION
        transition to run through `on_block_tail_vacated(...)`.
        """
        if self.block_manager is not None:
            for block_id in sorted(entered):
                if not self.block_manager.occupy_blocks([block_id], UNKNOWN_TRAIN_ID):
                    # occupy_blocks already reported the conflict through
                    # enter_failed_runtime(...), which halts the runtime.
                    return
        for block_id in sorted(vacated):
            block = self.network.blocks.get(block_id)
            if block is not None and block.occupied_by == UNKNOWN_TRAIN_ID:
                block.occupied_by = None

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

        # Phase 2 - Execute.
        # TrainController.tick(...) begins by draining the queued operator
        # commands; TrainPhysics.tick(...) computes motion and invokes
        # TrainController.on_physics_updated(...) at least once per tick.
        controller.tick(delta_seconds)
        physics.tick(delta_seconds)

        # Phase 3 - Feedback / cleanup.
        # If the controller is still OFF after the command drain, Phase 3 is
        # skipped for this transaction even though Phase 2 physics ran.
        if controller.ops.state == TrainState.OFF:
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

    # =========================================================================
    # Failed-runtime gate
    # =========================================================================

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

    # Fatal physical-entry conflict halt (set by enter_failed_runtime). While
    # it is set, update() skips every train tick — the trains freeze at their
    # last state and only simulation_time advances. Nothing else in the polled
    # payload reflects that, so surface the flag + the conflict cause here so
    # the UI can show a halt banner and offer recovery.
    payload["failed_runtime"] = bool(sim_controller.failed_runtime)
    fatal = sim_controller.failed_runtime_payload
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

    return payload
