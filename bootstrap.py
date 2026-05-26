"""
Bootstrap Module - Network initialization and demo data setup.

Definition:
This module provides functions to initialize the RailwayNetwork with either
demo data (default) or an empty state, based on configuration flags.

Scene selection:
The demo network is built from a *scene module* (currently `demo_main` or
`demo_3`). Each scene module exports a `SCENE_ID` constant, a
`create_comprehensive_demo()` factory, and a `setup_demo_train()` helper.
The active scene is the value of `CURRENT_SCENE` below; callers may override
per call by passing `scene="..."` to any of the bootstrap entry points.
Tests that are tied to a specific scene should call `get_current_scene_id()`
and `pytest.skip(...)` (typically at module level) when the active scene
does not match.

TODO List:
- [ ] Add support for loading from autosave on startup
- [ ] Add configuration file support
"""

import importlib
from types import ModuleType

from network import RailwayNetwork
from notification_center import NotificationCenter
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController


# The active scene. Edit this single line to swap which demo the server,
# CLI entry points, and unparameterised tests build by default. Per-call
# overrides via the `scene=` argument take precedence.
CURRENT_SCENE: str = "demo_3_best_effort"

# Registry of known scene IDs to their importable module names.
_SCENE_MODULES: dict[str, str] = {
    "demo_main": "demo_main",
    "demo_3": "demo_3",
    "demo_3_best_effort": "demo_3_best_effort",
}


def get_current_scene_id() -> str:
    """Return the currently configured scene ID.

    Tests use this to gate themselves — a test built for `demo_main` should
    skip (with a clear reason) when the active scene is anything else.
    """
    return CURRENT_SCENE


def _resolve_scene_name(scene: str | None) -> str:
    """Normalise a `scene=` argument: `None` means use `CURRENT_SCENE`."""
    name = scene if scene is not None else CURRENT_SCENE
    if name not in _SCENE_MODULES:
        known = ", ".join(sorted(_SCENE_MODULES))
        raise ValueError(f"Unknown scene {name!r}; known scenes: {known}")
    return name


def _load_scene_module(scene: str | None) -> ModuleType:
    """Import and return the scene module for `scene` (or `CURRENT_SCENE`)."""
    name = _resolve_scene_name(scene)
    module = importlib.import_module(_SCENE_MODULES[name])
    # Defensive check: the scene module must self-identify with the same ID
    # we routed by, otherwise the registry is stale.
    declared = getattr(module, "SCENE_ID", None)
    if declared != name:
        raise RuntimeError(
            f"Scene module {module.__name__!r} declares SCENE_ID={declared!r} "
            f"but bootstrap routed it as {name!r}. Update _SCENE_MODULES or "
            f"the module's SCENE_ID constant."
        )
    return module


def create_demo_network(scene: str | None = None) -> RailwayNetwork:
    """Create a network with demo data from the requested scene.

    Args:
        scene: Scene ID to build. `None` (default) uses `CURRENT_SCENE`.

    Returns:
        RailwayNetwork populated with the scene's tracks, blocks, signals,
        and trains.
    """
    module = _load_scene_module(scene)
    return module.create_comprehensive_demo()


def create_empty_network() -> RailwayNetwork:
    """Create an empty network.

    Returns:
        Empty RailwayNetwork ready for user content.
    """
    return RailwayNetwork()


def setup_demo_train(
    network: RailwayNetwork,
    path_dispatcher: PathDispatcher,
    sim_controller: SimulationController,
    scene: str | None = None,
) -> None:
    """Set up the demo train(s) for the requested scene.

    Per design_new.md ~line 1292, live operational state lives on
    `TrainController`; the demo bootstrap therefore needs the
    `SimulationController` (the controller-registry owner) so it can
    configure controller state directly rather than mirroring through
    `Train`.

    Args:
        scene: Scene ID whose train layout to install. `None` (default)
            uses `CURRENT_SCENE`. Must match the scene used to build the
            network — mixing scenes here will produce an inconsistent demo.
    """
    module = _load_scene_module(scene)
    module.setup_demo_train(network, path_dispatcher, sim_controller)


def bootstrap_runtime(
    demo: bool = True,
    setup_train: bool = True,
    scene: str | None = None,
) -> tuple[RailwayNetwork, PathDispatcher, SimulationController, PersistenceManager]:
    """Bootstrap the full runtime environment.

    Args:
        demo: If True, create demo network; if False, create empty network.
        setup_train: If True and demo is True, set up demo train.
        scene: Scene ID to build when `demo=True`. `None` uses
            `CURRENT_SCENE`. Ignored when `demo=False`.

    Returns:
        Tuple of (network, path_dispatcher, sim_controller, persistence_manager).
    """
    if demo:
        network = create_demo_network(scene=scene)
    else:
        network = create_empty_network()

    path_dispatcher = PathDispatcher(network)

    # Construct SimulationController BEFORE the demo train setup so that
    # the scene module can configure controller state directly via the
    # canonical controller registry (design_new.md ~line 1292).
    notification_center = NotificationCenter()
    sim_controller = SimulationController(
        network,
        path_dispatcher,
        notification_center=notification_center,
    )
    if sim_controller.power_controller:
        sim_controller.power_controller._sim_controller = sim_controller

    if demo and setup_train:
        setup_demo_train(network, path_dispatcher, sim_controller, scene=scene)

    persistence_manager = PersistenceManager()

    return network, path_dispatcher, sim_controller, persistence_manager
