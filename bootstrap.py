"""
Bootstrap Module - Network initialization and demo data setup.

Definition:
This module provides functions to initialize the RailwayNetwork with either
demo data (default) or an empty state, based on configuration flags.

Scene selection:
The demo network is built from a *scene module* (one of `demo_main`,
`demo_3`, `demo_4`, `demo_5`). Each scene module exports a `SCENE_ID`
constant, a `create_comprehensive_demo()` factory, and a `setup_demo_train()`
helper. The active scene is the value of `CURRENT_SCENE` below; callers may
override per call by passing `scene="..."` to any of the bootstrap entry
points. Tests tied to a specific scene tag themselves with
`pytestmark = pytest.mark.scene("...")`; the tests/conftest.py hook then
activates that scene for the test (via `set_current_scene`) so it runs under
its own scene instead of being skipped.

TODO List:
- [ ] Add support for loading from autosave on startup
- [ ] Add configuration file support
"""

import importlib
import os
from types import ModuleType

import hardware_config
import operation_environment
from enums import OperationMode
from network import RailwayNetwork
from notification_center import NotificationCenter
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController


# Environment variable that overrides the default scene. `set_current_scene()`
# (called e.g. by `server.py --scene ...`) exports it so a subprocess pytest
# run inherits the same default for its *unmarked* tests. You can also export
# it directly: `ATCS_SCENE=demo_4 python -m pytest tests/`. Tests tagged with
# `@pytest.mark.scene(...)` force their own scene regardless of this default.
SCENE_ENV_VAR = "ATCS_SCENE"

# The default scene when ATCS_SCENE is unset. Edit this line to change which
# demo the server, CLI entry points, and unparameterised tests build by
# default. Per-call `scene=` arguments, the ATCS_SCENE environment variable,
# and `set_current_scene()` all take precedence.
_DEFAULT_SCENE: str = "demo7_2"

# The active scene, resolved at import time from ATCS_SCENE (falling back to
# the default). `set_current_scene()` reassigns this for the running process.
CURRENT_SCENE: str = os.environ.get(SCENE_ENV_VAR, _DEFAULT_SCENE)

# Registry of known scene IDs to their importable module names. Scene IDs may
# contain characters illegal in module names (e.g. the dot in "demo_5.1"), so
# the value is the importable module file, not a transform of the key.
_SCENE_MODULES: dict[str, str] = {
    "demo_main": "demo_main",
    "demo_3": "demo_3",
    "demo_4": "demo_4",
    "demo_5": "demo_5",
    "demo_5.1": "demo_5_1",
    "demo_6": "demo_6",
    "demo6_1": "demo6_1",
    "demo7": "demo7",
    "demo7_1": "demo7_1",
    "demo7_2": "demo7_2",
    "demo_test": "demo_test",
}


def get_current_scene_id() -> str:
    """Return the currently configured scene ID.

    Tests use this to gate themselves — a test built for `demo_main` should
    skip (with a clear reason) when the active scene is anything else.
    """
    return CURRENT_SCENE


def available_scenes() -> list[str]:
    """Return the sorted list of known scene IDs (for CLI `choices`, etc.)."""
    return sorted(_SCENE_MODULES)


def _resolve_scene_name(scene: str | None) -> str:
    """Normalise a `scene=` argument: `None` means use `CURRENT_SCENE`."""
    name = scene if scene is not None else CURRENT_SCENE
    if name not in _SCENE_MODULES:
        known = ", ".join(sorted(_SCENE_MODULES))
        raise ValueError(f"Unknown scene {name!r}; known scenes: {known}")
    return name


def set_current_scene(scene: str) -> str:
    """Switch the active scene for this process and any subprocess it spawns.

    Updates the module-level `CURRENT_SCENE` (so in-process callers such as the
    startup block-validation gate and `bootstrap_runtime` see it) and exports
    `ATCS_SCENE` into the environment (so a subprocess pytest run inherits the
    same scene and its scene-gated tests select correctly).

    Args:
        scene: Scene ID to activate. Must be a known scene.

    Returns:
        The resolved scene ID.

    Raises:
        ValueError: If `scene` is not a known scene.
    """
    global CURRENT_SCENE
    name = _resolve_scene_name(scene)
    CURRENT_SCENE = name
    os.environ[SCENE_ENV_VAR] = name
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

    Per design_new.md "Ownership split", live operational state lives on
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
    event_log_path: str | None = None,
    detector_reading_log_dir: str | os.PathLike | None = None,
    locomotive_file_path: str | os.PathLike | None = None,
) -> tuple[RailwayNetwork, PathDispatcher, SimulationController, PersistenceManager]:
    """Bootstrap the full runtime environment.

    Args:
        demo: If True, create demo network; if False, create empty network.
        setup_train: If True and demo is True, set up demo train.
        scene: Scene ID to build when `demo=True`. `None` uses
            `CURRENT_SCENE`. Ignored when `demo=False`.
        event_log_path: When set, persist the NotificationCenter event-log
            stream to this append-only JSONL file so a run's record outlives
            the session (design_new.md "Event log", Persistence). `None` (the
            default, used by unit tests) keeps the stream in memory only.
        detector_reading_log_dir: When set, write the per-pass record of what
            every block detector read into files in this directory
            (design_new.md "Event log", Detector reading log). `None` (the
            default, used by unit tests) leaves that log off.
        locomotive_file_path: When set, read the models, sets and id counters
            from this file before the scene is built, and turn on writing it
            after every state-changing request (design_new.md "SpeedProfile",
            Where models, sets and instance profiles are kept). `None` (the
            default, used by unit tests) leaves the file off.

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
    # canonical controller registry (design_new.md "Ownership split").
    notification_center = NotificationCenter()
    # design_new.md "Event log" (Persistence): persist the NotificationCenter
    # stream to disk so a run's record outlives the session. Opt-in via
    # `event_log_path` — the real server passes a path; unit tests leave it None
    # to keep the stream in memory only.
    if event_log_path:
        notification_center.enable_event_log(event_log_path)

    # design_new.md "Activation starts the railway over", Boot activates no
    # hardware environment: with Simulation selected the runtime activates
    # Simulation and the session boots Paused; a Virtual Hardware or Real
    # Hardware selection is not activated at boot, so the session boots Halted
    # for `not_activated` until the operator activates it. Boot runs no
    # readiness check of its own. The setup and the selection are still loaded,
    # because activation reads both.
    hardware_config.load_from_disk()
    operation_environment.load_from_disk()
    selected_environment = operation_environment.get_selected()
    operation_mode = selected_environment
    environment_active = selected_environment is OperationMode.SIMULATION
    activation_issues = (
        []
        if environment_active
        else [{
            "code": "not_activated",
            "message": (
                f"{selected_environment.value} is selected but not activated; "
                "activate it to go live"
            ),
        }]
    )
    input_interface = block_output_interface = switch_output_interface = None

    sim_controller = SimulationController(
        network,
        path_dispatcher,
        notification_center=notification_center,
        operation_mode=operation_mode,
        input_interface=input_interface,
        block_output_interface=block_output_interface,
        switch_output_interface=switch_output_interface,
        environment_active=environment_active,
        activation_issues=activation_issues,
    )
    # design_new.md "Event log", Detector reading log: opt-in like the event
    # log above, so only the real server writes reading files.
    if detector_reading_log_dir:
        sim_controller.detector_reading_log.enable(detector_reading_log_dir)

    # Read before the scene's trains are placed, so their instance profiles
    # take numbers the file has not given out (design_new.md "SpeedProfile",
    # Where models, sets and instance profiles are kept).
    if locomotive_file_path:
        import locomotive_file

        locomotive_file.enable(str(locomotive_file_path))
        locomotive_file.load_into(network, notification_center)

    if demo and setup_train:
        setup_demo_train(network, path_dispatcher, sim_controller, scene=scene)

    # design_new.md "Detector reading log", The run starts from what the server
    # held at boot: written on the first line, before any pass or command.
    if detector_reading_log_dir:
        from detector_reading_log import start_state_of

        sim_controller.detector_reading_log.set_start_state(start_state_of(network))

    persistence_manager = PersistenceManager()

    return network, path_dispatcher, sim_controller, persistence_manager
