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
from typing import Any, Dict, Literal, Optional, Tuple, TYPE_CHECKING

from network import RailwayNetwork
from train import Train, TrainCar
from enums import TrainState, Direction, LeadingEnd
from block_power import BlockPowerController
from notification_center import NotificationCenter, Severity
from route_executor import RouteExecutor
from speed_profile import SpeedProfileRegistry

from train_controller import TrainController, create_controller_for_train
from train_physics import TrainPhysics, create_physics_for_train
from train_span import TrainSpan
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
    ):
        self.network = network
        self.path_dispatcher = path_dispatcher
        self.last_update_time = datetime.now()

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

        # TrainController + TrainPhysics instances (shared across components)
        self.controllers: Dict[str, TrainController] = {}
        self.physics_instances: Dict[str, TrainPhysics] = {}

        # Initialize components with shared controllers
        self.power_controller = BlockPowerController(network)
        network.power_controller = self.power_controller
        self.route_executor = RouteExecutor(network, path_dispatcher, self.controllers)
        self.speed_profile_registry = SpeedProfileRegistry()
        self.simulation_time: float = 0.0

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
        # train ticks (Phase 1/2/3 skipped) until the operator manually
        # cleans up and calls release_failed_runtime_after_manual_cleanup().
        if self.failed_runtime:
            return

        # Refresh route-managed scheduling state (time advancement only).
        self.route_executor.update(delta_seconds)

        # Build candidate train list and let RouteExecutor decide ordering.
        candidate_train_ids = list(self.network.trains.keys())
        ordered_train_ids = self.route_executor.order_runnable_trains(
            candidate_train_ids
        )

        for train_id in ordered_train_ids:
            train = self.network.trains.get(train_id)
            if train is None:
                continue

            # Phase 1 - Setup. Run before the per-train transaction so the
            # planning kernel sits in the outer loop. Skip while the
            # controller is OFF; the Phase-2 command drain must run first so
            # that power_on_cmd() can be accepted.
            controller = self._get_or_create_controller(train)
            if controller.state != TrainState.OFF:
                self.route_executor.tick_train_phase1(train_id, delta_seconds)

            self._update_train(train, delta_seconds)

        # After all trains, refresh signals so they reflect every block state
        # change (including path completions) committed during the tick.
        self.network.update_signals_from_blocks()

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
        if controller.state == TrainState.OFF:
            self._sync_persistent_train_state(train, controller)
            return

        # 3a. Drain queued progress reports in FIFO order.
        if self.path_dispatcher is not None:
            while controller.queued_progress_reports:
                report = controller.queued_progress_reports.pop(0)
                self.path_dispatcher.on_controller_progress(report)

        # 3b. Post-physics usable-authority stop check + path completion.
        if self.path_dispatcher is not None:
            if self.path_dispatcher.active_path_id_by_train(train.train_id) is not None:
                if controller.is_stopped_at_authority_end():
                    event_time = time.time()
                    self.path_dispatcher.try_complete_active_path(
                        train.train_id, event_time
                    )

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
        if controller.phase_latches.path_cancel_request is None:
            return

        if not controller.is_stopped():
            # Latch survives: reconsider next tick once the train has stopped.
            return

        if self.path_dispatcher is None:
            controller.phase_latches.consume_path_cancel_request()
            return

        payload = controller.phase_latches.consume_path_cancel_request()

        active_path_id = self.path_dispatcher.active_path_id_by_train(train.train_id)
        if active_path_id is not None:
            self.path_dispatcher.cancel_path(
                active_path_id,
                initiator=payload.initiator,
                reason=payload.reason,
            )
        else:
            logger.debug(
                "latched cancel for %s initiator=%s skipped: no active path",
                train.train_id,
                payload.initiator,
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
        if not controller.phase_latches.route_abort:
            return

        if controller.phase_latches.path_cancel_request is not None:
            # Latch survives until the inclusive cancel has been consumed.
            return

        self.route_executor.remove_train_route_run(train.train_id)
        controller.phase_latches.consume_route_abort()

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
        if not controller.phase_latches.power_off:
            return

        # Per design_new.md Latch Table: gated by
        # `phase_latches.path_cancel_request is None`. The inclusive cancel
        # was armed by `_apply_power_off_request()` and either committed
        # earlier in this same Phase-3 pass or was never written (no live
        # path existed). If the inclusive cancel is still pending the train
        # was not stopped this tick — defer to a later one.
        if controller.phase_latches.path_cancel_request is not None:
            return

        self.route_executor.remove_train_route_run(train.train_id)

        # Controller-local OFF cleanup.
        controller.state = TrainState.OFF
        controller.cab = None
        controller.eb_active = False
        controller.ato_running = False
        controller.power_on = False
        controller.phase_latches.consume_power_off()

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
        if not controller.phase_latches.route_deactivate:
            return

        if controller.phase_latches.path_cancel_request is not None:
            return

        self.route_executor.remove_train_route_run(train.train_id)
        controller.phase_latches.consume_route_deactivate()

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

    def get_physics(self, train_id: str) -> Optional[TrainPhysics]:
        """Get physics instance for a train, or None if not yet created."""
        return self.physics_instances.get(train_id)

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

    def remove_train_controller(self, train_id: str) -> None:
        """Remove controller instance for a train (cleanup on train removal)."""
        if train_id in self.controllers:
            del self.controllers[train_id]

    def remove_train_physics(self, train_id: str) -> None:
        """Remove physics instance for a train (cleanup on train removal)."""
        if train_id in self.physics_instances:
            del self.physics_instances[train_id]

    def remove_train_instances(self, train_id: str) -> None:
        """Remove all simulation instances for a train."""
        self.remove_train_controller(train_id)
        self.remove_train_physics(train_id)

    # =========================================================================
    # Train lifecycle
    # =========================================================================

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
    ) -> Train:
        """Sole entry point for adding a train at runtime.

        Synchronous all-or-nothing. Constructs the Train, places it on a tile
        of ``starting_block_id`` facing ``initial_direction``, registers the
        controller and physics instances, wires back-references, and emits a
        ``train_added`` notification.

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

        # Resolve a starting tile inside the named block.
        start_positions = self.network.get_block_positions(starting_block_id)
        if not start_positions:
            raise ValueError(
                f"Block {starting_block_id!r} has no tiles to place a train on"
            )
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

        # Register and place. place_train raises ValueError if the train does
        # not fit on available track.
        self.network.add_train(train)
        try:
            self.network.place_train(train, start_tile[0], start_tile[1], direction)
        except ValueError:
            self.network.remove_train(train_id)
            raise

        # Refresh signals so the new occupancy is reflected. Signal refresh
        # is a UI projection only — log and continue if it fails so the
        # train-add itself still succeeds.
        try:
            self.network.update_signals_from_blocks()
        except Exception:
            logger.warning(
                "update_signals_from_blocks() failed during add_train(%s)",
                train_id,
                exc_info=True,
            )

        # Register controller and physics instances. add_train is the sole
        # entry point that writes the initial TrainController.current_span,
        # so we derive it from the placed TrainPosition before the first tick.
        controller = self.get_or_create_controller(train_id)
        self.get_or_create_physics(train_id)

        if train.position is not None:
            try:
                controller.current_span = TrainSpan.from_train_position(train.position)
            except Exception:
                self.network.remove_train(train_id)
                raise

        # If an initial cab was requested, write it directly. add_train runs
        # before the first tick, so this bypasses the command queue by design.
        if initial_cab is not None:
            controller.cab = initial_cab
            if controller.current_span is not None:
                controller.current_span.leading_end = (
                    LeadingEnd.A if initial_cab == "A" else LeadingEnd.B
                )

        # Emit notification.
        self.notification_center.emit(
            source="SimulationController",
            kind="train_added",
            severity=Severity.INFO,
            event_time=self.simulation_time,
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

        Sets :attr:`failed_runtime`, stores the typed fatal payload for
        inspection, and emits a FATAL notification carrying the conflict
        details. While the flag is set, :meth:`update` only advances
        ``simulation_time`` and skips Phase 1/2/3 for every train.
        """
        self.failed_runtime = True
        self.failed_runtime_payload: Optional[FatalPhysicalEntryConflict] = payload
        self.notification_center.emit(
            source="SimulationController",
            kind="fatal_runtime",
            severity=Severity.FATAL,
            event_time=self.simulation_time,
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

    def release_failed_runtime_after_manual_cleanup(self) -> bool:
        """Clear the :attr:`failed_runtime` flag after verifying that the
        conflicting condition reported in :attr:`failed_runtime_payload`
        has actually been cleaned up.

        Per design_new.md ~lines 264-265, this release helper must verify
        that the fatal resource no longer has the conflicting physical
        owner / disabled-entry condition recorded in the stored fatal
        payload before clearing the flag and emitting the resume
        notification. If verification fails, leave the runtime failed and
        return ``False``; the operator must repair the underlying state
        first.

        Returns ``True`` when the flag was cleared, ``False`` when
        verification failed.
        """
        if not self.failed_runtime:
            return True
        payload = self.failed_runtime_payload
        if payload is None:
            # Defensive: no stored payload means we cannot verify. Refuse
            # to release rather than silently clear.
            self.notification_center.emit(
                source="SimulationController",
                kind="failed_runtime_release_rejected",
                severity=Severity.WARNING,
                event_time=self.simulation_time,
                payload={"reason": "missing_fatal_payload"},
            )
            return False

        ok, reason = self._verify_fatal_conflict_cleared(payload)
        if not ok:
            self.notification_center.emit(
                source="SimulationController",
                kind="failed_runtime_release_rejected",
                severity=Severity.WARNING,
                event_time=self.simulation_time,
                train_id=payload.incoming_train_id,
                payload={
                    "resource_type": payload.resource_type,
                    "resource_id": payload.resource_id,
                    "conflict_kind": payload.conflict_kind.value,
                    "reason": reason,
                },
            )
            return False

        self.failed_runtime = False
        self.failed_runtime_payload = None
        self.notification_center.emit(
            source="SimulationController",
            kind="failed_runtime_released",
            severity=Severity.INFO,
            event_time=self.simulation_time,
        )
        return True

    def _verify_fatal_conflict_cleared(
        self, payload: FatalPhysicalEntryConflict
    ) -> Tuple[bool, str]:
        """Inspect live network state and verify the conflict described
        by ``payload`` is no longer present. Returns ``(True, "")`` when
        the resource is clean; ``(False, reason)`` otherwise.
        """
        kind = payload.conflict_kind
        prior_owner = payload.prior_owner_train_id
        if payload.resource_type == "block":
            block = self.network.blocks.get(payload.resource_id)
            if block is None:
                return (False, "resource_missing")
            if kind == FatalConflictKind.DISABLED_RESOURCE_ENTRY:
                if block.disabled:
                    return (False, "resource_still_disabled")
                return (True, "")
            if kind == FatalConflictKind.OCCUPIED_BY_OTHER_TRAIN:
                if block.occupied_by == prior_owner:
                    return (False, "block_still_occupied_by_prior_owner")
                return (True, "")
            if kind == FatalConflictKind.RESERVED_FOR_OTHER_TRAIN:
                if block.reserved_for_train == prior_owner:
                    return (False, "block_still_reserved_for_prior_owner")
                return (True, "")
            return (False, "unsupported_block_conflict_kind")
        if payload.resource_type == "switch":
            switches = getattr(self.network, "switches", None)
            switch = switches.get(payload.resource_id) if switches else None
            if switch is None:
                return (False, "resource_missing")
            if kind == FatalConflictKind.DISABLED_RESOURCE_ENTRY:
                if getattr(switch, "disabled", False):
                    return (False, "resource_still_disabled")
                return (True, "")
            if kind == FatalConflictKind.SWITCH_OCCUPIED_BY_OTHER_TRAIN:
                if getattr(switch, "occupied_by", None) == prior_owner:
                    return (False, "switch_still_occupied_by_prior_owner")
                return (True, "")
            return (False, "unsupported_switch_conflict_kind")
        return (False, "unknown_resource_type")

    # =========================================================================
    # Path completion / cancellation delegation
    # =========================================================================

    def handle_path_completion(
        self,
        train_id: str,
        path_id: str,
        authority_end_block_id: str,
        requested_destination_direction: str,
        event_time: float,
    ) -> None:
        """Forward a path-completion event to the RouteExecutor and emit a
        notification.
        """
        self.route_executor.handle_path_completion(
            train_id,
            path_id,
            authority_end_block_id,
            requested_destination_direction,
            event_time,
        )
        self.notification_center.emit(
            source="SimulationController",
            kind="path_completion",
            severity=Severity.INFO,
            event_time=event_time,
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
        self.route_executor.handle_path_cancellation(
            train_id, path_id, initiator, reason
        )
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

    Per design_new.md "Data Flow To UI" (~line 2065), live runtime data
    is exposed as three sibling maps alongside the persistent `trains`
    list, never merged into Train.to_dict() output:

      - `controllers_by_train_id`: TrainController.to_dict() per train
      - `dispatcher_progress_by_train_id`: PathDispatcher progress per train
      - `assigned_routes_by_train_id`: RouteExecutor-derived route id per train
    """
    payload = network.to_dict()

    if not sim_controller:
        payload["controllers_by_train_id"] = {}
        payload["dispatcher_progress_by_train_id"] = {}
        payload["assigned_routes_by_train_id"] = {}
        return payload

    path_dispatcher = getattr(sim_controller, "path_dispatcher", None)
    route_executor = getattr(sim_controller, "route_executor", None)

    controllers_by_train_id: Dict[str, Any] = {}
    dispatcher_progress_by_train_id: Dict[str, Any] = {}
    assigned_routes_by_train_id: Dict[str, Any] = {}

    for train_id, train in network.trains.items():
        controller = sim_controller._get_or_create_controller(train)
        controllers_by_train_id[train_id] = controller.to_dict()

        if path_dispatcher is not None:
            try:
                dispatcher_progress_by_train_id[train_id] = (
                    path_dispatcher.get_active_path_progress(train_id)
                )
            except Exception as e:
                logger.debug(
                    "Failed to read dispatcher progress for train %s: %s",
                    train_id,
                    e,
                )
                dispatcher_progress_by_train_id[train_id] = None
        else:
            dispatcher_progress_by_train_id[train_id] = None

        if route_executor is not None:
            try:
                assigned_routes_by_train_id[train_id] = (
                    route_executor.assigned_route_id_for(train_id)
                )
            except Exception as e:
                logger.debug(
                    "Failed to resolve assigned route for train %s: %s",
                    train_id,
                    e,
                )
                assigned_routes_by_train_id[train_id] = None
        else:
            assigned_routes_by_train_id[train_id] = None

    payload["controllers_by_train_id"] = controllers_by_train_id
    payload["dispatcher_progress_by_train_id"] = dispatcher_progress_by_train_id
    payload["assigned_routes_by_train_id"] = assigned_routes_by_train_id

    return payload
