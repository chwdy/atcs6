"""
Simulation Controller - Train Movement and Control Orchestration

Definition:
This module provides the SimulationController class which orchestrates train
simulation using the TrainController + TrainPhysics architecture, following
the per-train three-phase transaction model defined in design_new.md.

Architecture:
    SimulationController (Orchestrator)
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
from typing import Any, Dict, Optional, TYPE_CHECKING

from network import RailwayNetwork
from train import Train
from enums import TrainState
from block_power import BlockPowerController
from route_executor import RouteExecutor
from speed_profile import SpeedProfileRegistry

from train_controller import TrainController, create_controller_for_train
from train_physics import TrainPhysics, create_physics_for_train
from runtime_payloads import PathCancellationKind

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
    ):
        self.network = network
        self.path_dispatcher = path_dispatcher
        self.last_update_time = datetime.now()

        # TrainController + TrainPhysics instances (shared across components)
        self.controllers: Dict[str, TrainController] = {}
        self.physics_instances: Dict[str, TrainPhysics] = {}

        # Initialize components with shared controllers
        self.power_controller = BlockPowerController(network)
        network.power_controller = self.power_controller
        self.route_executor = RouteExecutor(network, path_dispatcher, self.controllers)
        self.speed_profile_registry = SpeedProfileRegistry()
        self.simulation_time: float = 0.0

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
          4. For each ordered train, run one full three-phase transaction
             (Phase 1 routing, Phase 2 controller+physics, Phase 3 cleanup)
             via _update_train(...).
          5. After all trains have committed, refresh signals from blocks.
        """
        self.simulation_time += delta_seconds

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
            self._update_train(train, delta_seconds)

        # After all trains, refresh signals so they reflect every block state
        # change (including path completions) committed during the tick.
        self.network.update_signals_from_blocks()

    # =========================================================================
    # Per-train three-phase transaction
    # =========================================================================

    def _update_train(self, train: Train, delta_seconds: float) -> None:
        """Run one per-train three-phase transaction.

        Phase 1 - Setup: RouteExecutor.tick_train_phase1(...) (skipped while
            controller is OFF; route planning cannot run before the command
            drain has had a chance to accept power_on_cmd()).
        Phase 2 - Execute: TrainController.tick(...) drains operator commands
            then runs control logic; TrainPhysics.tick(...) integrates motion
            and calls TrainController.on_physics_updated(...) per Agent D.
        Phase 3 - Feedback / cleanup: drain queued progress reports, run the
            post-physics path-completion check, then consume the latch table
            in canonical order.
        """
        controller = self._get_or_create_controller(train)
        physics = self._get_or_create_physics(train)

        # Phase 1 - Setup. Skip while controller is OFF; the command drain in
        # Phase 2 must be allowed to run first so power_on_cmd() can be
        # accepted.
        if controller.state != TrainState.OFF:
            self.route_executor.tick_train_phase1(train.train_id, delta_seconds)

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
        self._consume_pending_route_finished(controller, train)
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
        """Consume pending_path_cancel_request when the train is stopped.

        While the train is rolling the latch survives across ticks; releasing
        block reservations and switch locks ahead of a moving train is unsafe.
        When the train is stopped, dispatch the cancellation through
        PathDispatcher.cancel_path(...). If no active path exists the latch
        is still cleared on this pass with a single diagnostic.
        """
        if controller.pending_path_cancel_request is None:
            return

        if not controller.is_stopped():
            # Latch survives: reconsider next tick once the train has stopped.
            return

        payload = controller.pending_path_cancel_request

        if self.path_dispatcher is None:
            controller.pending_path_cancel_request = None
            return

        active_path_id = self.path_dispatcher.active_path_id_by_train(train.train_id)
        if active_path_id is not None:
            self.path_dispatcher.cancel_path(
                active_path_id,
                cancellation_kind=payload.cancellation_kind,
                reason=payload.reason,
            )
        else:
            logger.debug(
                "latched cancel for %s kind=%s skipped: no active path",
                train.train_id,
                payload.cancellation_kind,
            )

        controller.pending_path_cancel_request = None

    def _consume_pending_route_abort(
        self, controller: TrainController, train: Train
    ) -> None:
        """Consume pending_route_abort once any active path has been cleared.

        The inclusive pending_path_cancel_request has already committed (or
        no path ever existed). If a live path still exists the abort is
        deferred to a later tick.
        """
        if controller.pending_route_abort is None:
            return

        if self.path_dispatcher is not None:
            if (
                self.path_dispatcher.active_path_id_by_train(train.train_id)
                is not None
            ):
                # Latch survives until the inclusive cancel has committed.
                return

        payload = controller.pending_route_abort
        self.route_executor.remove_train_route_run(train.train_id, payload.reason)
        controller.pending_route_abort = None

    def _consume_pending_power_off(
        self, controller: TrainController, train: Train
    ) -> None:
        """Consume pending_power_off: cancel any active path, remove the
        route run, then perform controller-local OFF cleanup.
        """
        if not controller.pending_power_off:
            return

        if self.path_dispatcher is not None:
            active_path_id = self.path_dispatcher.active_path_id_by_train(
                train.train_id
            )
            if active_path_id is not None:
                self.path_dispatcher.cancel_path(
                    active_path_id,
                    cancellation_kind=PathCancellationKind.POWER_OFF,
                    reason="power off",
                )

        self.route_executor.remove_train_route_run(train.train_id, "power_off")

        # Controller-local OFF cleanup.
        controller.state = TrainState.OFF
        controller.active_cab = None
        controller.eb_active = False
        controller.ato_running = False
        controller.power_on = False
        controller.pending_power_off = False

    def _consume_pending_route_finished(
        self, controller: TrainController, train: Train
    ) -> None:
        """Consume pending_route_finished: terminal route-run teardown.

        The matching live path has already been cleared by normal stop
        completion in step (b) of this same Phase 3 pass, so no dispatcher
        cancel is needed.
        """
        if not controller.pending_route_finished:
            return

        self.route_executor.remove_train_route_run(train.train_id, "route_finished")
        controller.pending_route_finished = False

    def _consume_pending_route_deactivate(
        self, controller: TrainController, train: Train
    ) -> None:
        """Consume pending_route_deactivate once any active path has been
        cleared. If a live path remains the latch is deferred.
        """
        if controller.pending_route_deactivate is None:
            return

        if self.path_dispatcher is not None:
            if (
                self.path_dispatcher.active_path_id_by_train(train.train_id)
                is not None
            ):
                return

        payload = controller.pending_route_deactivate
        self.route_executor.remove_train_route_run(train.train_id, payload.reason)
        controller.pending_route_deactivate = None

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
            )
            # Inherit ATO state from train (for demo_main workflow)
            if hasattr(train, "ato_running") and train.ato_running:
                controller.ato_running = True
            # Inherit active_cab from train (for demo_main workflow)
            if hasattr(train, "active_cab") and train.active_cab:
                controller.active_cab = train.active_cab
            # Inherit control_mode from train if set
            if hasattr(train, "control_mode") and train.control_mode:
                controller.control_mode = train.control_mode
            # Inherit controller state from train state so that pre-armed
            # trains start in the correct state (e.g. RUNNING_MOVING).
            controller.state = train.state
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


def build_network_payload(
    network: RailwayNetwork,
    sim_controller: Optional["SimulationController"],
) -> Dict[str, Any]:
    """Build network payload with onboard controller snapshots.

    Args:
        network: Railway network instance.
        sim_controller: Simulation controller for train controllers.

    Returns:
        Network dictionary with per-train onboard controller state.
    """
    payload = network.to_dict()

    if not sim_controller:
        return payload

    trains = payload.get("trains", [])
    train_lookup = {train_data.get("id"): train_data for train_data in trains}

    for train in network.trains.values():
        train_data = train_lookup.get(train.train_id)
        if not train_data:
            continue
        controller = sim_controller._get_or_create_controller(train)
        train_data["onboard"] = controller.to_dict()

        # Inject live path data (overwrite stale values from network.to_dict)
        if train.assigned_path_id:
            try:
                live_path = controller.get_live_path_data()
                if live_path:
                    path_tiles = live_path["path_tiles"]
                    path_index = live_path["path_index"]
                    train_data["path_index"] = path_index
                    train_data["path_length"] = len(path_tiles) if path_tiles else 0
                    train_data["current_path"] = [
                        {"x": x, "y": y, "dir": d.name if d else None}
                        for x, y, d in (path_tiles or [])
                    ]
            except Exception as e:
                logger.debug(
                    "Failed to get live path for train %s: %s", train.train_id, e
                )

    return payload
