"""
Bug Reproduction: Two issues reported by user.

Bug 1: Dual path assignment after ato_stop → standby
  - Train has path P1, ATO stopped via ato_stop() → STANDBY
  - New path P2 created for same train in different direction
  - P1 still exists in dispatcher → train assigned to two paths simultaneously
  - Root cause: transition_to_standby() called by ato_stop flow (tick step 6)
    does not cancel the existing path in the dispatcher

Bug 2: RUNNING_WAITING instead of STANDBY when path ends at safety margin
  - Train assigned path to terminal block
  - ATC brakes train to 0 at safety margin → tick step 8 sets RUNNING_WAITING
    (on BOTH controller.state AND train.state directly)
  - path_dispatcher.on_train_progress_from_physics() then completes the path
  - _complete_path_on_head_arrival checks train_obj.state == RUNNING_MOVING,
    which is now False → transition_to_standby() is NOT called
  - Sub-bug B: _sim_controller is never wired in the real Flask app
    (bootstrap.py / server.py), so controller.ato_running, ato_path_id, and
    ato_destination_block_id are never cleared by path completion, making
    _is_standalone_path_complete() permanently return False

Run with:
    conda run -n general pytest tests/test_bug_dual_path_and_rw_completion.py -v -s
"""

import logging
import pytest

from path import PathStatus
from path_dispatcher import PathDispatcher
from network import RailwayNetwork
from track import TrackSegment, TRACK_EW, TRACK_NS
from train import Train, TrainCar
from simulation import SimulationController
from enums import Direction, TrainState
from path_utils import assign_complete_path_to_train, convert_tiles_to_train_path
from tests.train_test_helpers import make_train_position

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING)


# ============================================================================
# Shared network layout
# ============================================================================

def build_linear_network() -> RailwayNetwork:
    """
    Simple east-west layout:

      BLOCK_A        BLOCK_B       BLOCK_END
      (0,0)-(1,0)   (2,0)-(3,0)   (4,0)-(5,0)

    Train starts in BLOCK_A. Path A→B can be created. Path B→A is the reverse.
    BLOCK_END is used as a terminal for Bug 2 (path ends there).
    """
    network = RailwayNetwork()

    for x in range(6):
        network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))

    network.create_block_with_tracks(
        block_id="BLOCK_A",
        positions=[(0, 0), (1, 0)],
        name="Block A",
        length_cm=200.0,
        max_speed_kmh=60.0,
    )
    network.create_block_with_tracks(
        block_id="BLOCK_B",
        positions=[(2, 0), (3, 0)],
        name="Block B",
        length_cm=200.0,
        max_speed_kmh=60.0,
    )
    network.create_block_with_tracks(
        block_id="BLOCK_END",
        positions=[(4, 0), (5, 0)],
        name="Block End (terminal)",
        length_cm=200.0,
        max_speed_kmh=60.0,
    )
    return network


def make_train(network: RailwayNetwork, block_id: str, start_tile, direction) -> Train:
    car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=40.0)
    train = Train(train_id="TRAIN_1", consist=[car], max_speed=60.0)
    network.add_train(train)
    network.place_train(train, start_tile[0], start_tile[1], direction)
    if network.block_manager:
        network.block_manager.occupy_block(block_id, train.train_id)
    return train


def dump_state(label, train, controller, dispatcher=None):
    paths_for_train = []
    if dispatcher:
        paths_for_train = [
            p.path_id for p in dispatcher.get_active_paths()
            if p.train_id == train.train_id
        ]
    print(f"\n{'=' * 60}")
    print(f"[DEBUG] {label}")
    print(f"  controller.state           = {controller.state}")
    print(f"  train.state                = {train.state}")
    print(f"  controller.ato_running     = {controller.ato_running}")
    print(f"  controller.path_backed_run_active = {controller.path_backed_run_active}")
    print(f"  controller.ato_path_id     = {controller.ato_path_id}")
    print(f"  controller.ato_destination_block_id = {controller.ato_destination_block_id}")
    print(f"  train.assigned_path_id     = {getattr(train, 'assigned_path_id', None)}")
    print(f"  train.current_speed        = {train.current_speed:.2f} km/h")
    print(f"  active paths for train     = {paths_for_train}")
    print(f"{'=' * 60}")


# ============================================================================
# Bug 1: Dual path assignment after ato_stop → standby
# ============================================================================

class TestBugDualPathAfterAtoStop:
    """
    Bug: train stopped by ato_stop() becomes STANDBY, but old path P1 remains
    in the dispatcher. Creating a new path P2 causes the train to be registered
    under two simultaneous paths.
    """

    @pytest.fixture
    def network(self):
        return build_linear_network()

    @pytest.fixture
    def dispatcher(self, network):
        return PathDispatcher(network)

    @pytest.fixture
    def simulation(self, network, dispatcher):
        sim = SimulationController(network, dispatcher)
        # Wire back-reference (same as test_bug_standby_path_autostart.py)
        if sim.power_controller:
            sim.power_controller._sim_controller = sim
        return sim

    def _setup_train_with_running_path(self, network, dispatcher, simulation):
        """Place train in BLOCK_A, create path A→B, start ATO."""
        train = make_train(network, "BLOCK_A", (0, 0), Direction.EAST)
        path_result = dispatcher.create_path(
            start_block_id="BLOCK_A",
            end_block_id="BLOCK_B",
            train_id=train.train_id,
        )
        assert path_result.success, f"Path A→B failed: {path_result.error}"
        path = path_result.path

        raw = convert_tiles_to_train_path(path.path_tiles)
        assign_complete_path_to_train(network, train, raw)
        train.assigned_path_id = path.path_id

        controller = simulation._get_or_create_controller(train)
        train.ato_destination_block_id = path.end_block_id

        controller.power_on_cmd()
        result = controller.ato_go()
        assert result.success, f"ato_go() failed: {result.message}"

        return train, controller, path.path_id

    def test_ato_stop_leaves_old_path_as_orphan_in_dispatcher(
        self, network, dispatcher, simulation
    ):
        """
        REPRODUCE BUG-002: after ato_stop() + transition to STANDBY, the old
        path still exists in the dispatcher (orphaned, holding block reservations).

        See docs/bugs/bug-002-ato-stop-orphaned-path-blocks-new-assignment.md
        """
        train, controller, old_path_id = self._setup_train_with_running_path(
            network, dispatcher, simulation
        )

        dump_state("BEFORE ato_stop()", train, controller, dispatcher)

        result = controller.ato_stop()
        assert result.success

        # Drive train to stopped
        train.current_speed = 0.0
        controller.tick(0.1)

        dump_state("AFTER ato_stop() + tick (STANDBY)", train, controller, dispatcher)

        assert controller.state == TrainState.STANDBY

        old_path = dispatcher.get_path(old_path_id)
        train_paths = [p for p in dispatcher.get_active_paths() if p.train_id == train.train_id]

        print(f"\n[BUG] old path still in dispatcher: {old_path is not None}")
        print(f"[BUG] train.assigned_path_id still set: {train.assigned_path_id is not None}")
        print(f"[BUG] blocks still reserved by P1: {old_path.block_sequence if old_path else []}")

        # Document the bug: orphaned path remains
        assert old_path is not None, (
            "BUG: old path P1 still in dispatcher after ato_stop → STANDBY. "
            "See bug-002 for fix approach."
        )
        assert len(train_paths) == 1, (
            f"BUG: 1 orphaned path still active for train. Got: {len(train_paths)}"
        )

    def test_orphaned_path_blocks_new_path_creation(
        self, network, dispatcher, simulation
    ):
        """
        REPRODUCE BUG (user-visible symptom): after ato_stop() → STANDBY,
        the old path P1 still holds block reservations. When the user tries
        to create a new path through the same blocks, it fails with
        "All paths blocked" — even though the train is stopped.

        The chosen product behavior is not to auto-cancel at the dispatcher
        layer. Instead, POST /api/paths returns 409 so the frontend can prompt
        the operator before cancelling the old path and retrying.

        This is the exact symptom the user reported: creating a new path in
        a different direction fails or causes unexpected behavior because the
        orphaned path is still active in the dispatcher.
        """
        train, controller, old_path_id = self._setup_train_with_running_path(
            network, dispatcher, simulation
        )

        # Stop ATO and bring to STANDBY
        controller.ato_stop()
        train.current_speed = 0.0
        controller.tick(0.1)

        assert controller.state == TrainState.STANDBY

        dump_state("STANDBY with old path still holding block reservations", train, controller, dispatcher)

        old_path = dispatcher.get_path(old_path_id)
        print(f"\n[BUG] Old path P1 still holds blocks: {old_path.block_sequence if old_path else 'None'}")

        # Direct dispatcher path creation still fails because P1 remains active.
        # The API layer handles this as an operator-visible conflict.
        new_path_result = dispatcher.create_path(
            start_block_id="BLOCK_A",
            end_block_id="BLOCK_END",
            train_id=train.train_id,
        )

        print(f"\n[RESULT] New path creation: success={new_path_result.success}, error={new_path_result.error}")

        if not new_path_result.success:
            print(
                "\n[BUG CONFIRMED] New path creation FAILED because old path P1 "
                "still holds block reservations after ato_stop → STANDBY.\n"
                "The user cannot create a new path because the dispatcher sees "
                "blocks reserved by the orphaned old path."
            )

        assert not new_path_result.success, (
            "Expected direct dispatcher path creation to FAIL because old path P1 still holds "
            "block reservations after ato_stop → STANDBY. "
            "The user-facing fix is the POST /api/paths 409 conflict flow."
        )

        print(
            "\n[BUG CONFIRMED] New path creation blocked by orphaned P1 reservations. "
            "Agreed fix approach: API returns 409 conflict → frontend prompts user to "
            "cancel old path before assigning new one. See bug-002 doc."
        )


# ============================================================================
# Bug 2: RUNNING_WAITING instead of STANDBY at end of path (safety margin)
# ============================================================================

class TestBugRunningWaitingAtPathEnd:
    """
    Bug: when the train stops at safety margin at the end of its path,
    the state is incorrectly set to RUNNING_WAITING instead of STANDBY.

    Root causes:
    1. Tick step 8 sets train.state = RUNNING_WAITING (effective_limit=0, speed=0)
    2. on_train_progress_from_physics then completes the path, but
       _complete_path_on_head_arrival only calls transition_to_standby() when
       train_obj.state == RUNNING_MOVING — which is now False.
    3. In the real Flask app, _sim_controller is not wired, so controller flags
       (ato_running, ato_path_id, ato_destination_block_id) are never cleared,
       preventing _is_standalone_path_complete from returning True in subsequent ticks.
    """

    @pytest.fixture
    def network(self):
        return build_linear_network()

    @pytest.fixture
    def dispatcher(self, network):
        return PathDispatcher(network)

    @pytest.fixture
    def simulation_wired(self, network, dispatcher):
        """Simulation with _sim_controller wired (as tests do it)."""
        sim = SimulationController(network, dispatcher)
        if sim.power_controller:
            sim.power_controller._sim_controller = sim
        return sim

    @pytest.fixture
    def simulation_unwired(self, network, dispatcher):
        """Simulation WITHOUT _sim_controller wired (mirrors real Flask app)."""
        sim = SimulationController(network, dispatcher)
        # Intentionally do NOT wire power_controller._sim_controller
        # This mirrors bootstrap.py and server.py which never set it
        return sim

    def _setup_train_on_path_to_end(self, network, dispatcher, simulation):
        """Place train in BLOCK_A with path to BLOCK_END, ATO started."""
        train = make_train(network, "BLOCK_A", (0, 0), Direction.EAST)
        path_result = dispatcher.create_path(
            start_block_id="BLOCK_A",
            end_block_id="BLOCK_END",
            train_id=train.train_id,
        )
        assert path_result.success, f"Path A→END failed: {path_result.error}"
        path = path_result.path

        raw = convert_tiles_to_train_path(path.path_tiles)
        assign_complete_path_to_train(network, train, raw)
        train.assigned_path_id = path.path_id

        controller = simulation._get_or_create_controller(train)
        train.ato_destination_block_id = path.end_block_id

        controller.power_on_cmd()
        result = controller.ato_go()
        assert result.success, f"ato_go() failed: {result.message}"

        return train, controller, path

    def _force_train_stopped_in_dest_block(self, network, dispatcher, train, controller, path):
        """
        Manually place train in destination block (BLOCK_END), stopped,
        simulating it having traveled there and stopped at safety margin.

        Also forces path into CLEARING state (entered destination block).
        """
        # Place train head in destination block's first tile
        dest_tile = (4, 0)  # First tile of BLOCK_END
        train.position = make_train_position(
            head_tile=dest_tile,
            facing_direction=Direction.EAST,
            occupied_blocks=["BLOCK_END"],
        )
        train.current_speed = 0.0

        # Force path into CLEARING state (as if train entered destination block)
        path.status = PathStatus.CLEARING

        # Update block occupancy
        if network.block_manager:
            network.block_manager.clear_block("BLOCK_A")
            network.block_manager.clear_block("BLOCK_B")
            network.block_manager.occupy_block("BLOCK_END", train.train_id)

    def test_complete_path_on_head_arrival_skips_standby_for_running_waiting(
        self, network, dispatcher, simulation_wired
    ):
        """
        REPRODUCE BUG 2 (sub-issue 1):
        _complete_path_on_head_arrival only calls transition_to_standby() when
        train.state == RUNNING_MOVING. But tick step 8 sets train.state =
        RUNNING_WAITING BEFORE on_train_progress_from_physics is called.

        So even with _sim_controller wired, the train stays RUNNING_WAITING
        (in this tick) instead of becoming STANDBY.
        """
        train, controller, path = self._setup_train_on_path_to_end(
            network, dispatcher, simulation_wired
        )
        self._force_train_stopped_in_dest_block(
            network, dispatcher, train, controller, path
        )

        # Simulate what tick step 8 does: set RUNNING_WAITING on BOTH objects
        # (because effective_limit=0 at safety margin, speed=0)
        controller.state = TrainState.RUNNING_WAITING
        train.state = TrainState.RUNNING_WAITING  # ← tick step 8 sets this directly

        dump_state(
            "State at end of tick step 8 (RUNNING_WAITING, path CLEARING)",
            train, controller, dispatcher
        )

        # Now call on_train_progress_from_physics (as simulation.py step 3 does)
        updated = dispatcher.on_train_progress_from_physics(path.path_id)

        dump_state(
            "AFTER on_train_progress_from_physics",
            train, controller, dispatcher
        )

        # Path should be completed (removed from dispatcher)
        path_still_active = dispatcher.get_path(path.path_id) is not None
        print(f"\n[RESULT] path still in dispatcher: {path_still_active}")
        print(f"[RESULT] controller.state = {controller.state}")
        print(f"[RESULT] train.state = {train.state}")
        print(f"[RESULT] controller.ato_running = {controller.ato_running}")

        # Path should be completed
        assert not path_still_active, "Path should have been completed by on_train_progress_from_physics"

        # BUG: state should be STANDBY but is RUNNING_WAITING
        # With _sim_controller wired, controller flags get cleared,
        # but transition_to_standby() is not called because train.state == RUNNING_WAITING
        # (not RUNNING_MOVING). So state stays RUNNING_WAITING.
        if controller.state == TrainState.RUNNING_WAITING:
            print(
                "\n[BUG CONFIRMED] State is RUNNING_WAITING after path completion, "
                "but should be STANDBY.\n"
                "Root cause: _complete_path_on_head_arrival only handles "
                "RUNNING_MOVING, not RUNNING_WAITING."
            )

        # FIX VERIFICATION: after fix, state should immediately be STANDBY
        assert controller.state == TrainState.STANDBY, (
            f"FIX REGRESSION: Expected STANDBY after path completes while in "
            f"RUNNING_WAITING, got {controller.state}. "
            "_complete_path_on_head_arrival must also handle RUNNING_WAITING state."
        )

    def test_running_waiting_stuck_when_sim_controller_not_wired(
        self, network, dispatcher, simulation_unwired
    ):
        """
        REPRODUCE BUG 2 (sub-issue 2, real Flask app scenario):
        _sim_controller is NOT wired on power_controller (as in real app).

        When path completes via _complete_path_on_head_arrival, controller flags
        (ato_running, ato_path_id, ato_destination_block_id) are NOT cleared.
        This makes _is_standalone_path_complete() permanently return False,
        leaving the train stuck in RUNNING_WAITING indefinitely.
        """
        train, controller, path = self._setup_train_on_path_to_end(
            network, dispatcher, simulation_unwired
        )
        self._force_train_stopped_in_dest_block(
            network, dispatcher, train, controller, path
        )

        # Simulate tick step 8: train stopped at safety margin → RUNNING_WAITING
        controller.state = TrainState.RUNNING_WAITING
        train.state = TrainState.RUNNING_WAITING

        # Record state before path completion
        ato_running_before = controller.ato_running
        ato_path_id_before = controller.ato_path_id
        ato_dest_before = controller.ato_destination_block_id

        # on_train_progress_from_physics completes the path
        dispatcher.on_train_progress_from_physics(path.path_id)

        dump_state(
            "AFTER path completion (sim_controller NOT wired)",
            train, controller, dispatcher
        )

        # With _sim_controller NOT wired, these should NOT have been cleared
        print(f"\n[RESULT] ato_running before: {ato_running_before}, after: {controller.ato_running}")
        print(f"[RESULT] ato_path_id before: {ato_path_id_before}, after: {controller.ato_path_id}")
        print(f"[RESULT] ato_destination_block_id before: {ato_dest_before}, after: {controller.ato_destination_block_id}")

        if controller.ato_running == ato_running_before and controller.ato_path_id == ato_path_id_before:
            print(
                "\n[BUG CONFIRMED] controller.ato_running and ato_path_id were NOT cleared "
                "because _sim_controller is not wired in real app.\n"
                "This prevents _is_standalone_path_complete from returning True "
                "in subsequent ticks → train stuck in RUNNING_WAITING forever."
            )

        # Now verify: even if we run a subsequent tick, does the train get to STANDBY?
        # (It shouldn't without the fix, because _has_path_backed_run_context returns True)
        has_context_after = controller._has_path_backed_run_context(train)
        is_complete_after = controller._is_standalone_path_complete(train)

        print(f"\n[RESULT] _has_path_backed_run_context = {has_context_after}")
        print(f"[RESULT] _is_standalone_path_complete = {is_complete_after}")

        # BUG CONFIRMATION: without wiring, context is NOT cleared
        assert has_context_after, (
            "Expected _has_path_backed_run_context=True when _sim_controller is NOT "
            "wired (pre-fix behavior). If this fails, controller flags are somehow "
            "being cleared without _sim_controller."
        )
        assert not is_complete_after, (
            "Expected _is_standalone_path_complete=False when _sim_controller is NOT "
            "wired (pre-fix behavior). Train is stuck in RUNNING_WAITING."
        )

        print(
            "\n[BUG CONFIRMED] Without _sim_controller wiring, controller flags "
            "(ato_running, ato_path_id, ato_destination_block_id) are NOT cleared "
            "on path completion → train stuck in RUNNING_WAITING forever.\n"
            "FIX: wire sim_controller.power_controller._sim_controller = sim_controller "
            "in bootstrap.py, server.py, and scenario_runner.py (already applied)."
        )

    def test_state_is_standby_after_path_completes_at_safety_margin(
        self, network, dispatcher, simulation_wired
    ):
        """
        FULL SEQUENCE: train stops at safety margin, path completes, state → STANDBY.

        This is the end-to-end test that verifies correct behavior after both fixes:
        1. _complete_path_on_head_arrival handles RUNNING_WAITING → STANDBY
        2. _sim_controller is wired (controller flags cleared on path completion)

        Uses simulation_wired fixture (fix 2 already applied in tests).
        Fix 1 (RUNNING_WAITING check) is what we're verifying here.
        """
        train, controller, path = self._setup_train_on_path_to_end(
            network, dispatcher, simulation_wired
        )
        self._force_train_stopped_in_dest_block(
            network, dispatcher, train, controller, path
        )

        # Simulate the exact tick scenario:
        # Step 8 fires: effective_limit=0 + speed=0 → RUNNING_WAITING
        controller.state = TrainState.RUNNING_WAITING
        train.state = TrainState.RUNNING_WAITING

        dump_state(
            "Before on_train_progress_from_physics (RUNNING_WAITING, path CLEARING)",
            train, controller, dispatcher
        )

        # Simulate the simulation.py step 3: path progress + completion
        simulation_wired.path_dispatcher.on_train_progress_from_physics(path.path_id)

        # Simulate the simulation.py step 4: sync controller state back
        train.state = controller.state

        dump_state(
            "After path completion + state sync",
            train, controller, dispatcher
        )

        print(f"\n[RESULT] Final controller.state = {controller.state}")
        print(f"[RESULT] Final train.state = {train.state}")

        # FIX VERIFICATION: state must be STANDBY, not RUNNING_WAITING
        assert controller.state == TrainState.STANDBY, (
            f"Expected STANDBY after path completes at safety margin, "
            f"got {controller.state}. State machine doc says: "
            "'standalone path completed, no remaining active route/path continuation "
            "→ STANDBY', even when train stopped before entering last tile."
        )
        assert train.state == TrainState.STANDBY, (
            f"train.state should also be STANDBY after sync, got {train.state}"
        )
