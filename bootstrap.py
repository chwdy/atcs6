"""
Bootstrap Module - Network initialization and demo data setup.

Definition:
This module provides functions to initialize the RailwayNetwork with either
demo data (default) or an empty state, based on configuration flags.

TODO List:
- [ ] Add support for loading from autosave on startup
- [ ] Add configuration file support
"""

from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController


def create_demo_network() -> RailwayNetwork:
    """Create a network with demo data.

    Returns:
        RailwayNetwork populated with demo tracks, blocks, signals, and trains.
    """
    import demo_main

    network = demo_main.create_comprehensive_demo()
    network = demo_main.create_track_showcase(network)
    return network


def create_empty_network() -> RailwayNetwork:
    """Create an empty network.

    Returns:
        Empty RailwayNetwork ready for user content.
    """
    return RailwayNetwork()


def setup_demo_train(network: RailwayNetwork, path_dispatcher: PathDispatcher) -> None:
    """Set up demo train on the network.

    Args:
        network: RailwayNetwork to add train to.
        path_dispatcher: PathDispatcher for path management.
    """
    import demo_main

    demo_main.setup_demo_train(network, path_dispatcher)


def bootstrap_runtime(
    demo: bool = True,
    setup_train: bool = True,
) -> tuple[RailwayNetwork, PathDispatcher, SimulationController, PersistenceManager]:
    """Bootstrap the full runtime environment.

    Args:
        demo: If True, create demo network; if False, create empty network.
        setup_train: If True and demo is True, set up demo train.

    Returns:
        Tuple of (network, path_dispatcher, sim_controller, persistence_manager).
    """
    if demo:
        network = create_demo_network()
    else:
        network = create_empty_network()

    path_dispatcher = PathDispatcher(network)

    if demo and setup_train:
        setup_demo_train(network, path_dispatcher)

    sim_controller = SimulationController(network, path_dispatcher)
    if sim_controller.power_controller:
        sim_controller.power_controller._sim_controller = sim_controller
    persistence_manager = PersistenceManager()

    return network, path_dispatcher, sim_controller, persistence_manager
