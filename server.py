"""
Railway Server API and Runtime

Definition:
This module provides the Flask application factory setup and background thread management
for the railway management system. All route handlers have been migrated to dedicated
route modules in the routes/ directory.

Core Responsibilities:
- Startup test execution and validation
- State initialization (_build_state_and_app)
- Background thread management (simulation_loop, autosave_loop)
- Flask application factory setup

TODO List:
- [x] Move route definitions into dedicated routes modules
- [ ] Move startup tests, demo boot, and threads behind main/CLI flags
- [ ] Keep side-effectful initialization behind the app factory entrypoint
"""

from datetime import datetime
import threading

from flask import Flask

from app_factory import create_app
from app_state import AppState
from bootstrap import (
    available_scenes,
    create_demo_network,
    get_current_scene_id,
    set_current_scene,
    setup_demo_train,
    _load_scene_module,
)
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController

# Global state
network = RailwayNetwork()
# Initialize with demo data for now so it's not empty
# In a real app, we might want to start empty or load from save
app: Flask | None = None
state: AppState | None = None


def run_startup_tests():
    """Run startup tests before starting the server.

    This ensures no regressions have been introduced.
    Tests run:
    1. Block validation tests (already present)
    2. Full backend test suite (if pytest available)
    """
    import subprocess
    import sys

    print("\n" + "=" * 70)
    print("Server Startup - Running Regression Tests")
    print("=" * 70)

    # Test 1: Block validation (always run - no dependencies)
    print("\n[1/2] Running Block Validation Tests...")
    print("-" * 70)
    try:
        # Block-validation suite lives on each scene module; run it for the
        # currently active scene so the gate matches what we're about to boot.
        _load_scene_module(None).test_block_validation()
        print(f"✅ Block validation tests PASSED (scene={get_current_scene_id()})")
    except Exception as e:
        print(f"❌ Block validation tests FAILED: {e}")
        print("\n🛑 BLOCKING: Cannot start server with failing validation tests.")
        sys.exit(1)

    # Test 2: Full backend test suite (if pytest available)
    print("\n[2/2] Running Full Backend Test Suite...")
    print("-" * 70)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_backend.py", "-v", "--tb=short", "-p", "no:cacheprovider"],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            # Check if pytest is not installed
            if "No module named pytest" in result.stderr:
                print("⚠️  WARNING: pytest not installed. Skipping full test suite.")
                print("   Install with: pip install pytest")
            else:
                print("❌ Backend tests FAILED!")
                print("\n📋 Full Test Output:")
                print(result.stdout if result.stdout else "(no stdout)")
                if result.stderr:
                    print("\n📋 Stderr:")
                    print(result.stderr)
                
                print("\n🛑 BLOCKING: Cannot start server with failing tests.")
                sys.exit(1)
        else:
            print("✅ Full backend test suite PASSED")
            # Show summary line
            if result.stdout:
                lines = result.stdout.split("\n")
                for line in lines:
                    if "passed" in line.lower() or "failed" in line.lower():
                        print(f"   {line.strip()}")

    except subprocess.TimeoutExpired:
        print("❌ Backend tests TIMEOUT (exceeded 60 seconds)")
        print("\n🛑 BLOCKING: Cannot start server with timeout.")
        sys.exit(1)
    except Exception as e:
        print(f"⚠️  WARNING: Could not run full test suite: {e}")
        print("   Continuing with just block validation...")

    print("\n" + "=" * 70)
    print("✅ All Startup Tests Passed - Initializing Server")
    print("=" * 70)


def _build_state_and_app() -> tuple[AppState, Flask]:
    global app
    global state
    global network
    global path_dispatcher
    global sim_controller
    global persistence_manager
    global simulation_lock
    global simulation_running
    global simulation_speed
    global last_update_time

    if app is not None and state is not None:
        return state, app

    run_startup_tests()

    network = create_demo_network()

    path_dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, path_dispatcher)
    if sim_controller.power_controller:
        sim_controller.power_controller._sim_controller = sim_controller
    setup_demo_train(network, path_dispatcher, sim_controller)
    persistence_manager = PersistenceManager()
    simulation_lock = threading.Lock()
    simulation_running = False
    simulation_speed = 1.0
    last_update_time = datetime.now()

    state = AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=sim_controller,
        persistence_manager=persistence_manager,
        simulation_lock=simulation_lock,
        simulation_running=simulation_running,
        simulation_speed=simulation_speed,
        last_update_time=last_update_time,
    )

    app = create_app(state)

    return state, app


# Guard against Flask's dev-server reloader re-importing this module and
# starting duplicate simulation/autosave threads. When run via `python server.py`
# (__main__), main() calls _build_state_and_app() and start_all_threads()
# directly, so module-level init must be skipped. When imported by a WSGI
# server (e.g. gunicorn), __name__ != "__main__" and this block runs normally.
if __name__ != "__main__":
    from runtime import start_all_threads

    state, app = _build_state_and_app()
    start_all_threads(state)


# ============================================================================
# Helper Functions
# ============================================================================


def main() -> None:
    """Main entrypoint with CLI flag support.

    Flags:
        --no-demo: Start with empty network instead of demo data
        --skip-startup-tests: Skip running tests on startup
        --skip-frontend-build: Skip building the frontend bundle on startup
        --no-sim: Don't start simulation thread
        --no-autosave: Don't start autosave thread
        --no-reload: Disable auto-reload on file changes (default: reload enabled)
        --port: Port to run on (default: 5001)
        --scene: Scene to boot and gate startup tests to (sets ATCS_SCENE)
    """
    import argparse
    import os

    parser = argparse.ArgumentParser(description="Railway Server")
    parser.add_argument(
        "--no-demo", action="store_true", help="Start with empty network"
    )
    parser.add_argument(
        "--skip-startup-tests", action="store_true", help="Skip startup tests"
    )
    parser.add_argument(
        "--skip-frontend-build",
        action="store_true",
        help="Skip building the frontend bundle (static/dist/main.js) on startup",
    )
    parser.add_argument(
        "--no-sim", action="store_true", help="Don't start simulation thread"
    )
    parser.add_argument(
        "--no-autosave", action="store_true", help="Don't start autosave thread"
    )
    parser.add_argument(
        "--no-reload", action="store_true", help="Disable auto-reload on file changes"
    )
    parser.add_argument("--port", type=int, default=5001, help="Port to run on")
    parser.add_argument(
        "--scene",
        type=str,
        default=None,
        choices=available_scenes(),
        help=(
            f"Scene to boot and gate startup tests to (default: "
            f"{get_current_scene_id()}). Sets ATCS_SCENE so the bootstrap, "
            "the block-validation gate, and unmarked tests default to it; "
            "scene-tagged tests force their own scene via @pytest.mark.scene."
        ),
    )
    args = parser.parse_args()

    # Activate the requested scene before anything reads it. set_current_scene
    # updates the in-process CURRENT_SCENE (used by the block-validation gate
    # and bootstrap below) and exports ATCS_SCENE so the subprocess pytest run
    # in run_startup_tests() inherits the same scene.
    if args.scene is not None:
        set_current_scene(args.scene)

    # When the reloader is active, Werkzeug sets WERKZEUG_RUN_MAIN=true in the
    # child (server) process. Startup tests run only in the parent/watcher so
    # they execute once at startup, not on every file-change reload.
    is_reloader_child = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    if not args.skip_startup_tests and not is_reloader_child:
        run_startup_tests()

    # Ensure the frontend bundle is built before serving (longterm_todo.md item
    # #8). Runs once in the parent (not the reloader child), only on the real
    # `python server.py` boot — create_app() is untouched, so tests never build.
    if not is_reloader_child:
        from frontend_build import ensure_frontend_bundle

        ensure_frontend_bundle(skip=args.skip_frontend_build)

    global network, path_dispatcher, sim_controller, persistence_manager
    global simulation_lock, simulation_running, simulation_speed, last_update_time
    global state, app

    from bootstrap import bootstrap_runtime
    from runtime import start_all_threads

    network, path_dispatcher, sim_controller, persistence_manager = bootstrap_runtime(
        demo=not args.no_demo,
        setup_train=not args.no_demo,
        # design_new.md "Event log" (Persistence): the real server persists the
        # NotificationCenter stream to an append-only JSONL on disk so an
        # operator can review an earlier run's incidents later.
        event_log_path="event_log.jsonl",
    )

    # design_new.md Power, Hardware interface boundary — loss-of-control
    # fail-safe: a process shutdown must
    # drive outputs to the safe state (block voltages 0, switch motors
    # de-energized). The simulation-stop route issues the same enter_safe_state
    # call. Registered only on the real `python server.py` boot, not in tests.
    import atexit

    if sim_controller is not None:
        atexit.register(sim_controller.enter_safe_state)

    simulation_lock = threading.Lock()
    simulation_running = False
    simulation_speed = 1.0
    last_update_time = datetime.now()

    state = AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=sim_controller,
        persistence_manager=persistence_manager,
        simulation_lock=simulation_lock,
        simulation_running=simulation_running,
        simulation_speed=simulation_speed,
        last_update_time=last_update_time,
    )

    app = create_app(state)

    # Start background threads based on flags
    start_all_threads(
        state,
        simulation=not args.no_sim,
        autosave=not args.no_autosave,
    )

    app.run(debug=True, use_reloader=not args.no_reload, port=args.port)


if __name__ == "__main__":
    main()
