"""
Simulation Controller - Train Movement and Control Orchestration

Definition:
This module provides the SimulationController class which orchestrates train
simulation using the TrainController + TrainPhysics architecture.

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
        self.route_executor = RouteExecutor(network, path_dispatcher, self.controllers)
        self.speed_profile_registry = SpeedProfileRegistry()
        self.simulation_time: float = 0.0

    def update(self, delta_seconds: float) -> None:
        """Update all trains for one simulation tick."""
        self.simulation_time += delta_seconds

        self.route_executor.update(delta_seconds)

        for train in self.network.trains.values():
            self._update_train(train, delta_seconds)

    def _update_train(self, train: Train, delta_seconds: float) -> None:
        """Update a single train using TrainController + TrainPhysics."""
        controller = self._get_or_create_controller(train)
        physics = self._get_or_create_physics(train)

        # Sync power state from train to controller
        if train.state == TrainState.OFF:
            controller.power_on = False
        elif not controller.power_on and train.state != TrainState.OFF:
            controller.power_on = True

        # Sync train state TO controller only on first initialization
        if not hasattr(controller, "_initialized"):
            controller.state = train.state
            controller._initialized = True

        # Tick controller (calculates ATC limits, ATO handle)
        controller.tick(delta_seconds)

        # Tick physics (moves train, manages blocks, sets voltage)
        physics.tick(delta_seconds)

        if self.path_dispatcher and train.assigned_path_id:
            transitions = getattr(physics, "last_block_transitions", {})
            for block_id in transitions.get("entered", set()):
                self.path_dispatcher.on_train_advances(train.assigned_path_id, block_id)
            if transitions.get("vacated"):
                self.path_dispatcher.on_train_progress(train.assigned_path_id)

        # Sync controller state back to train (controller is source of truth)
        train.state = controller.state

        # Sync calculated values back to train for UI display
        train.target_speed = controller.target_speed
        train.speed_limit_reason = controller.speed_limit_reason

    def _get_or_create_controller(self, train: Train) -> TrainController:
        """Get or create a TrainController for the given train."""
        if train.train_id not in self.controllers:
            self.controllers[train.train_id] = create_controller_for_train(
                train_id=train.train_id,
                network=self.network,
            )
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

    return payload
