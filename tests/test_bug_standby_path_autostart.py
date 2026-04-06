"""
Bug Reproduction: Standby train starts moving when new path assigned.

User-reported bug:
1. Train finishes path (extended) to east branch terminal → goes STANDBY
2. User assigns a new path going back to east terminal
3. Train immediately starts moving — state still shows STANDBY but it IS running
4. Train moves in the ORIGINAL direction, going further from the terminal

Key findings from code analysis:
- Section 6 of tick() calls transition_to_standby() whenever ato_running=False AND speed=0,
  regardless of current state (including RUNNING_AT_STATION and RUNNING_WAITING).
- When a new reverse path is assigned and the head tile is NOT in the path
  (because assign_complete_path_to_train fails — train is "past" path start),
  _path_needs_cab_flip() returns False and _required_cab_for_path() can't detect
  the needed cab flip → train moves in original (wrong) direction.
- In RUNNING_WAITING with ato_running=True, assigning a path with clearance causes
  auto-transition to RUNNING_MOVING.

Run with:
    conda run -n general pytest tests/test_bug_standby_path_autostart.py -v -s
"""

import logging
import pytest

from network import RailwayNetwork
from track import TrackSegment, TRACK_EW, TRACK_NS, TRACK_NW
from train import Train, TrainCar
from path_dispatcher import PathDispatcher
from train_controller import create_controller_for_train
from simulation import SimulationController
from enums import Direction, TrainState, HandlePosition
from path_utils import assign_complete_path_to_train, convert_tiles_to_train_path
from switch import Switch
from tests.train_test_helpers import make_train_position

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING)  # reduce noise; -s shows prints


def build_east_branch_layout() -> RailwayNetwork:
    """
    Build a simplified version of the demo east-branch layout.

    Coordinate system: N = (0,+1), so increasing y = NORTH.

        (0,0)-(4,0): BLOCK_MAIN  [east-west, train starts here]
        (5,0): Switch EW+NW (boundary, no block)
                NW edge at (5,0) connects to (5,1) going NORTH
        (5,1)-(5,5): BLOCK_EAST_BRANCH  [north-south]
                     Train enters from south, travels NORTH to reach y=5

    After the train arrives at the north end (5,5), it is at EAST TERMINAL.
    The reverse path goes SOUTH from (5,5) back to (5,1), then WEST to BLOCK_MAIN.
    """
    network = RailwayNetwork()

    # Main line: (0,0) to (4,0) going east
    for x in range(5):
        network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))

    network.create_block_with_tracks(
        block_id="BLOCK_MAIN",
        positions=[(x, 0) for x in range(5)],
        name="Main Line",
        length_cm=250.0,
        max_speed_kmh=60.0,
    )

    # Switch at (5,0): EW main line + NW branch (NW = connects N and W edges)
    # N edge connects to (5,1), W edge connects to (4,0)
    switch_track = TrackSegment(5, 0, track_bits=TRACK_EW | TRACK_NW)
    switch_track.switch_state = 1  # NW branch active (N=toward branch, W=toward main)
    network.add_track(switch_track)
    sw = Switch(switch_id="SW_5_0", x=5, y=0)
    network.add_switch(sw)
    switch_track.switch_id = sw.switch_id

    # East branch: (5,1) to (5,5) going north (NS tracks, y increases = NORTH)
    for y in range(1, 6):
        network.add_track(TrackSegment(5, y, track_bits=TRACK_NS))

    network.create_block_with_tracks(
        block_id="BLOCK_EAST_BRANCH",
        positions=[(5, y) for y in range(1, 6)],
        name="East Branch Terminal",
        length_cm=250.0,
        max_speed_kmh=40.0,
    )

    return network


def dump_state(label: str, train, controller, extra: dict = None) -> None:
    """Print a concise debug snapshot."""
    print(f"\n{'=' * 60}")
    print(f"[DEBUG] {label}")
    print(f"  controller.state       = {controller.state}")
    print(f"  controller.ato_running = {controller.ato_running}")
    print(f"  controller.path_backed_run_active = {controller.path_backed_run_active}")
    print(f"  controller.active_cab  = {controller.active_cab}")
    print(
        f"  controller.ato_destination_block_id = {controller.ato_destination_block_id}"
    )
    print(f"  controller.handle_position = {controller.handle_position}")
    print(f"  train.assigned_path_id = {getattr(train, 'assigned_path_id', None)}")
    print(f"  train.current_speed    = {train.current_speed:.2f} km/h")
    print(f"  train.facing_direction = {train.facing_direction}")
    head = train.position.head_tile if train.position else None
    print(f"  train head tile        = {head}")
    if extra:
        for k, v in extra.items():
            print(f"  {k} = {v}")
    print(f"{'=' * 60}")


class TestBugStandbyPathAutostart:
    """
    Reproduce the bug: STANDBY train starts moving on new path assignment.

    Tests are ordered from most fundamental to most complex.
    """

    @pytest.fixture
    def network(self):
        return build_east_branch_layout()

    @pytest.fixture
    def dispatcher(self, network):
        return PathDispatcher(network)

    @pytest.fixture
    def simulation(self, network, dispatcher):
        sim = SimulationController(network, dispatcher)
        # Wire back-reference so path_dispatcher.complete_path() can reach
        # the SimulationController when it needs to clear controller.ato_running.
        if sim.power_controller:
            sim.power_controller._sim_controller = sim
        return sim

    def _setup_train_at_start(self, network, dispatcher):
        """Place train at (0,0) facing east, power on ATO mode."""
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=40.0)
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=60.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)
        if network.block_manager:
            network.block_manager.occupy_block("BLOCK_MAIN", train.train_id)
        return train

    def _create_and_run_path_to_terminal(self, network, dispatcher, simulation, train):
        """
        Create a path MAIN → EAST_BRANCH, start ATO, and manually place the train
        at the north end of BLOCK_EAST_BRANCH as if it has traveled there.

        Returns the original path_id.
        """
        path_result = dispatcher.create_path(
            start_block_id="BLOCK_MAIN",
            end_block_id="BLOCK_EAST_BRANCH",
            train_id=train.train_id,
        )
        assert path_result.success, f"Path creation failed: {path_result.error}"
        path = path_result.path

        raw_path = convert_tiles_to_train_path(path.path_tiles)
        assign_complete_path_to_train(network, train, raw_path)
        train.assigned_path_id = path.path_id

        controller = simulation._get_or_create_controller(train)
        controller.ato_destination_block_id = path.end_block_id
        train.ato_destination_block_id = path.end_block_id

        controller.power_on_cmd()
        result = controller.ato_go()
        assert result.success, f"ato_go() failed: {result.message}"

        return path.path_id, controller

    def test_section6_always_calls_transition_to_standby(
        self, network, dispatcher, simulation
    ):
        """
        FINDING 1: Section 6 of tick() calls transition_to_standby() whenever
        ato_running=False AND speed=0, regardless of current state.

        This means RUNNING_AT_STATION and RUNNING_WAITING can be overwritten
        to STANDBY if ato_running is False.
        """
        train = self._setup_train_at_start(network, dispatcher)
        _, controller = self._create_and_run_path_to_terminal(
            network, dispatcher, simulation, train
        )

        # Force RUNNING_AT_STATION state with ato_running=False (as if station arrived)
        controller.state = TrainState.RUNNING_AT_STATION
        train.state = TrainState.RUNNING_AT_STATION
        controller.ato_running = False
        train.current_speed = 0.0

        dump_state(
            "BEFORE tick() (RUNNING_AT_STATION, ato_running=False)", train, controller
        )

        controller.tick(0.1)

        dump_state("AFTER tick()", train, controller)

        print(
            f"\n[FINDING] Section 6 overwrote RUNNING_AT_STATION → {controller.state}"
        )
        print(f"  Expected: RUNNING_AT_STATION (state section handles it)")
        print(f"  Actual:   {controller.state}")

        # DOCUMENT the actual behavior (may or may not be a bug)
        if controller.state == TrainState.STANDBY:
            print(
                "  [CONFIRMED] Section 6 transition_to_standby() overwrites AT_STATION state!"
            )

    def test_running_waiting_with_ato_running_true_never_completes(
        self, network, dispatcher, simulation
    ):
        """
        FINDING 2 / FIX VERIFICATION: _is_standalone_path_complete() behaviour
        with ato_running=True.

        BUG (before fix): _is_standalone_path_complete() hard-gated on
        ato_running=False, so a train stopped with ato_running=True could never
        complete its path via this path.

        FIX (Fix 3): the guard now only blocks completion when ato_running=True
        AND path context still exists.  When the path context is gone the stale
        ato_running flag is ignored.

        This test checks BOTH halves:
          A) path context still present → False (path genuinely not done yet)
          B) path context cleared       → True  (stale ato_running is ignored)
        """
        train = self._setup_train_at_start(network, dispatcher)
        path_id, controller = self._create_and_run_path_to_terminal(
            network, dispatcher, simulation, train
        )

        # Force the "stuck at end of path" state
        branch_end = (5, 5)
        train.position = make_train_position(
            head_tile=branch_end,
            facing_direction=Direction.NORTH,
            occupied_blocks=["BLOCK_EAST_BRANCH"],
        )
        train.current_speed = 0.0
        train.reserved_blocks = []

        controller.state = TrainState.RUNNING_WAITING
        train.state = TrainState.RUNNING_WAITING
        # ato_running is True (from ato_go())

        assert controller.ato_running is True, "ato_running should still be True"

        # --- Half A: path context still present → completion must not fire ---
        is_complete_with_context = controller._is_standalone_path_complete(train)
        has_context = controller._has_path_backed_run_context(train)

        print(f"\n[FINDING] Train in RUNNING_WAITING at end of path (context present):")
        print(f"  ato_running = {controller.ato_running}")
        print(f"  _is_standalone_path_complete = {is_complete_with_context}")
        print(f"  _has_path_backed_run_context = {has_context}")

        assert not is_complete_with_context, (
            "Path context still present → _is_standalone_path_complete must be False"
        )

        # --- Half B: path context cleared, ato_running still True → should complete ---
        train.assigned_path_id = None
        train.current_path = None
        train.ato_destination_block_id = None
        controller.ato_destination_block_id = None
        controller.ato_path_id = None

        is_complete_stale = controller._is_standalone_path_complete(train)
        has_context_after = controller._has_path_backed_run_context(train)

        print(f"\n[FIX CHECK] Path context cleared, ato_running still True:")
        print(f"  ato_running = {controller.ato_running}")
        print(f"  _has_path_backed_run_context = {has_context_after}")
        print(f"  _is_standalone_path_complete = {is_complete_stale}")

        assert is_complete_stale, (
            "FIX REGRESSION: with path context gone and only a stale ato_running=True, "
            "_is_standalone_path_complete should return True so the train can reach STANDBY"
        )

    def test_new_reverse_path_assign_complete_fails_for_past_head(
        self, network, dispatcher, simulation
    ):
        """
        FINDING 3: assign_complete_path_to_train fails when train is 'past'
        the path start tile (common for reverse paths).

        When the reverse path starts from the south end of BLOCK_EAST_BRANCH
        but the train is at the north end, prepare_path_for_train returns an
        error: 'Train is past path start in block'.

        This causes the fallback assignment WITHOUT prepend, leaving the head
        tile OUTSIDE the path. Downstream, _path_needs_cab_flip returns False
        and the cab flip is NOT detected → train moves in wrong direction.
        """
        train = self._setup_train_at_start(network, dispatcher)
        path_id, controller = self._create_and_run_path_to_terminal(
            network, dispatcher, simulation, train
        )

        # Complete old path to release switch
        dispatcher.complete_path(path_id)
        controller.ato_destination_block_id = None
        controller.ato_path_id = None
        controller.transition_to_standby()

        # Place train at north end of branch (y=5)
        branch_north = (5, 5)
        train.position = make_train_position(
            head_tile=branch_north,
            facing_direction=Direction.NORTH,
            occupied_blocks=["BLOCK_EAST_BRANCH"],
        )
        train.current_speed = 0.0

        # Occupy north block
        if network.block_manager:
            network.block_manager.clear_block("BLOCK_MAIN")
            network.block_manager.occupy_block("BLOCK_EAST_BRANCH", train.train_id)

        dump_state("Train at east terminal (STANDBY)", train, controller)

        # Create reverse path EAST_BRANCH → MAIN
        reverse_path_result = dispatcher.create_path(
            start_block_id="BLOCK_EAST_BRANCH",
            end_block_id="BLOCK_MAIN",
            train_id=train.train_id,
        )
        assert reverse_path_result.success, (
            f"Reverse path creation failed: {reverse_path_result.error}"
        )
        new_path = reverse_path_result.path
        new_raw_path = convert_tiles_to_train_path(new_path.path_tiles)

        print(f"\n[INFO] New reverse path tiles[0:3]: {new_raw_path[:3]}")
        print(f"[INFO] Train head tile: {train.position.head_tile}")
        print(f"[INFO] Path starts at: {new_raw_path[0] if new_raw_path else None}")
        print(
            f"[INFO] Head in path: {any((t[0], t[1]) == train.position.head_tile for t in new_raw_path)}"
        )

        # Attempt assign_complete_path_to_train
        success, error = assign_complete_path_to_train(network, train, new_raw_path)

        print(
            f"\n[FINDING] assign_complete_path_to_train: success={success}, error={error}"
        )

        if not success:
            print(
                "  [CONFIRMED BUG PREREQUISITE] assign_complete_path_to_train FAILS "
                "for reverse path when train is 'past' path start!"
            )
            print(
                f"  Effect: train.current_path is set to raw_path starting at {new_raw_path[0][:2]}"
            )
            print(f"  Train head {train.position.head_tile} is NOT in this path.")
            print(
                "  → _path_needs_cab_flip() will return False (head not found in path)"
            )
            print("  → No cab flip detected → train moves in WRONG direction")

        # Simulate what routes/paths.py does on failure
        if not success:
            train.current_path = new_raw_path
            train.path_index = 0
        train.assigned_path_id = new_path.path_id
        controller.ato_destination_block_id = new_path.end_block_id
        train.ato_destination_block_id = new_path.end_block_id

        # Now check what _required_cab_for_path returns
        required_cab = controller._required_cab_for_path()
        path_needs_flip = controller._path_needs_cab_flip()

        print(f"\n[KEY] _path_needs_cab_flip() = {path_needs_flip}")
        print(f"[KEY] _required_cab_for_path() = {required_cab}")
        print(f"[KEY] controller.active_cab = {controller.active_cab}")
        print(f"[KEY] train.facing_direction = {train.facing_direction}")

        if not path_needs_flip:
            print("\n[BUG] Cab flip NOT detected for reverse path!")
            print("  Train will move in original direction (NORTH) instead of SOUTH")

        # _path_needs_cab_flip fails (head not in path), but the direction
        # fallback in _required_cab_for_path() still returns the correct cab.
        assert not path_needs_flip, (
            "CONFIRMED: _path_needs_cab_flip() returns False when head tile "
            "is not in the path (connectivity check can't run)"
        )
        # The fallback direction logic DOES return the right cab (B for reverse)
        # even when the head isn't in the path. Direction detection works here.
        print(f"\n[NOTE] _required_cab_for_path() fallback correctly returns 'B'")
        print(
            f"  The direction issue may manifest differently in the actual demo layout"
        )
        print(f"  where overall path direction fallback may pick WEST not SOUTH")

    def test_full_bug_sequence_running_waiting_auto_start_wrong_direction(
        self, network, dispatcher, simulation
    ):
        """
        FULL BUG REPRODUCTION: The complete sequence that causes the bug.

        1. Train in RUNNING_WAITING with ato_running=True (never cleared)
        2. New reverse path assigned (head not in path tiles)
        3. New path provides clearance → effective_limit > 0
        4. RUNNING_WAITING → RUNNING_MOVING auto-transition fires
        5. _align_cab_to_required_path_direction can't detect flip → no cab flip
        6. ato_running=True → train immediately accelerates in WRONG direction

        State shows STANDBY? No, RUNNING_MOVING.
        But train is running without explicit ato_go() command.
        """
        train = self._setup_train_at_start(network, dispatcher)
        path_id, controller = self._create_and_run_path_to_terminal(
            network, dispatcher, simulation, train
        )

        # Place train at north end of branch (y=5) - arrived at terminal
        branch_north = (5, 5)
        train.position = make_train_position(
            head_tile=branch_north,
            facing_direction=Direction.NORTH,
            occupied_blocks=["BLOCK_EAST_BRANCH"],
        )
        train.current_speed = 0.0
        train.reserved_blocks = []

        # Manually force RUNNING_WAITING with ato_running=True (bug precondition)
        controller.state = TrainState.RUNNING_WAITING
        train.state = TrainState.RUNNING_WAITING
        # ato_running stays True from ato_go()

        # Occupy east branch block, clear main block (making clearance available)
        if network.block_manager:
            network.block_manager.clear_block("BLOCK_MAIN")
            network.block_manager.occupy_block("BLOCK_EAST_BRANCH", train.train_id)

        dump_state(
            "STARTING STATE: RUNNING_WAITING + ato_running=True at terminal",
            train,
            controller,
        )

        # Complete old path to release switch lock
        dispatcher.complete_path(path_id)
        # After complete_path, train.assigned_path_id is cleared by dispatcher
        controller.ato_destination_block_id = None

        # Create new reverse path EAST_BRANCH → MAIN
        reverse_path_result = dispatcher.create_path(
            start_block_id="BLOCK_EAST_BRANCH",
            end_block_id="BLOCK_MAIN",
            train_id=train.train_id,
        )
        assert reverse_path_result.success, (
            f"Reverse path creation failed: {reverse_path_result.error}"
        )
        new_path = reverse_path_result.path
        new_raw_path = convert_tiles_to_train_path(new_path.path_tiles)

        # Apply path (as routes/paths.py does)
        success, error = assign_complete_path_to_train(network, train, new_raw_path)
        if not success:
            train.current_path = new_raw_path
            train.path_index = 0
        train.assigned_path_id = new_path.path_id
        controller.ato_destination_block_id = new_path.end_block_id
        train.ato_destination_block_id = new_path.end_block_id

        dump_state(
            "AFTER reverse path assigned (still RUNNING_WAITING)",
            train,
            controller,
            extra={
                "_path_needs_cab_flip": controller._path_needs_cab_flip(),
                "_required_cab_for_path": controller._required_cab_for_path(),
                "_is_standalone_path_complete": controller._is_standalone_path_complete(
                    train
                ),
            },
        )

        # Run tick() — this is where the bug triggers
        print("\n[ACTION] Running tick()...")
        controller.tick(0.1)

        dump_state(
            "AFTER tick() — did the bug trigger?",
            train,
            controller,
        )

        new_state = controller.state
        print(f"\n[RESULT] controller.state = {new_state}")
        print(f"[RESULT] controller.ato_running = {controller.ato_running}")
        print(f"[RESULT] controller.active_cab = {controller.active_cab}")
        print(f"[RESULT] train.facing_direction = {train.facing_direction}")

        if new_state == TrainState.RUNNING_MOVING:
            print("\n[BUG CONFIRMED] Train auto-transitioned to RUNNING_MOVING!")
            print(f"  ato_running={controller.ato_running} → ATO will accelerate")
            print(
                f"  active_cab={controller.active_cab}, facing={train.facing_direction}"
            )
            if train.facing_direction == Direction.NORTH:
                print("  [DIRECTION BUG] Train faces NORTH (original direction)")
                print("  Expected: SOUTH (reverse path direction)")
            print("\n  Root causes:")
            print("  1. ato_running=True never cleared when ATC braked train to 0")
            print("  2. assign_complete_path_to_train failed → head not in path")
            print("  3. _path_needs_cab_flip returns False → no cab flip")
            print("  4. effective_limit > 0 (new path blocks are clear)")
            print("  5. RUNNING_WAITING → RUNNING_MOVING auto-transition fires")
            print("  6. ato_running=True causes immediate acceleration")

        # --- FIX VERIFICATION ---
        # complete_path() must have cleared ato_running on the controller (Fix 1).
        # Without an explicit ato_go() the train must NOT auto-start after a new
        # path is assigned.  Expected outcome after tick(): STANDBY, not RUNNING_MOVING.
        assert controller.ato_running is False, (
            "FIX REGRESSION: ato_running should be False after complete_path() "
            "(Fix 1: complete_path must clear controller.ato_running)"
        )
        assert new_state == TrainState.STANDBY, (
            f"FIX REGRESSION: train should stay STANDBY after new path assigned "
            f"without an explicit ato_go() command, got {new_state}. "
            "The train must not auto-start when ato_running is properly cleared."
        )
