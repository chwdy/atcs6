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
_DEFAULT_SCENE: str = "demo7_1"

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
    "demo7": "demo7",
    "demo7_1": "demo7_1",
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

    # design_new.md "Hardware setup": the hardware backend is
    # chosen once when the operation session is built. Load the persisted
    # selection from disk and resolve it to the interface overrides the session
    # wires. `None` in a slot means "use the simulated default". A real backend
    # whose driver is not present in this build raises
    # HardwareBackendUnavailable; fall back to sim mode so the server still boots
    # (the selection stays on disk, and takes effect once its driver lands).
    hardware_config.load_from_disk()
    operation_environment.load_from_disk()
    hw_cfg = hardware_config.get_hardware_config()
    selected_environment = operation_environment.get_selected()
    readiness = hardware_config.activation_report(
        hw_cfg,
        selected_environment,
        network.blocks.keys(),
        network.switches.keys(),
    )
    environment_active = bool(readiness["ready"])
    activation_issues = list(readiness["issues"])
    try:
        if not environment_active:
            raise hardware_config.HardwareBackendUnavailable(
                "; ".join(issue["message"] for issue in activation_issues)
            )
        input_interface, block_output_interface, switch_output_interface = (
            hardware_config.build_session_interfaces(
                hw_cfg,
                environment=selected_environment,
                # Read through to the live configuration, so flipping a block's
                # rail polarity in the editor lands on the next track write
                # rather than waiting for a restart.
                track_polarity_of=hardware_config.track_polarity_reversed,
            )
        )
        operation_mode = selected_environment
    except hardware_config.HardwareBackendUnavailable as exc:
        print(f"[bootstrap] {selected_environment.value} inactive: {exc}")
        input_interface = block_output_interface = switch_output_interface = None
        operation_mode = selected_environment
        environment_active = False
        if not activation_issues:
            activation_issues = [{
                "code": "backend_unavailable",
                "message": str(exc),
            }]

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
    if sim_controller.power_controller:
        sim_controller.power_controller._sim_controller = sim_controller

    if demo and setup_train:
        setup_demo_train(network, path_dispatcher, sim_controller, scene=scene)

    persistence_manager = PersistenceManager()

    return network, path_dispatcher, sim_controller, persistence_manager
