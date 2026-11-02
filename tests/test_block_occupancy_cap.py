"""Tests for the seam cap in TrainPhysics.cap_distance_to_detected_occupancy.

The calculated span never runs ahead of the detector (design_new.md "Power"):
the head stops at the entry seam of the next detected block until that block
confirms occupied, and the tail stops at the exit seam of the rearmost detected
block the span holds until that block confirms clear. This is the mirror of the
snap covered by `test_block_occupancy_snap.py`, which handles the detector
leading the span instead.

The cap keys off blocks the session actually detects, so these tests wire a fake
`OccupancyInput` that reports one. Under the simulated driver — which builds its
current map from the very spans the cap would hold back — nothing is detected and
the cap is inert; `test_cap_is_inert_under_the_simulated_driver` pins that.
"""

from typing import Dict, Set

from block import Block
from block_occupancy import OccupancyInput
from enums import Direction, HandlePosition
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from track import Tile, TRACK_EW
from train import Train, TrainCar
from entity_ids import segment_id_for_block


class FakeDetectorInput(OccupancyInput):
    """A detector that answers for `detected` and reports current on `live`."""

    def __init__(self, detected: Set[str], live: Set[str]) -> None:
        self.detected = set(detected)
        self.live = set(live)

    def detected_block_ids(self) -> Set[str]:
        return set(self.detected)

    def read_block_currents(self) -> Dict[str, bool]:
        return {bid: bid in self.live for bid in self.detected}


def build_line(num_tiles: int = 6) -> RailwayNetwork:
    """A straight E-W line, one 100cm block per tile (BLK_0 .. BLK_{n-1})."""
    network = RailwayNetwork()
    for x in range(num_tiles):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        track.segment_id = segment_id_for_block(f"BLK_{x}")
        network.add_track(track)
        network.blocks[f"BLK_{x}"] = Block(
            block_id=f"BLK_{x}", length_cm=100.0, speed_limit_kmh=80.0
        )
    return network


def place(network, head_x, length_cm, train_id="T"):
    sim = SimulationController(network)
    train = Train(
        train_id=train_id,
        consist=[TrainCar(car_id="c1", car_type="LOCOMOTIVE", length_cm=length_cm)],
        max_speed=80.0,
    )
    network.add_train(train)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = network.place_train(
        train, head_x, 0, Direction.EAST
    )
    physics = sim._get_or_create_physics(train)
    return sim, train, controller, physics


def wire_detector(sim, detected, confirmed):
    """Point the runtime detector at a fake input and seed its confirmed set."""
    sim.block_occupancy_detector.input_interface = FakeDetectorInput(
        detected=detected, live=confirmed
    )
    sim.block_occupancy_detector.seed_occupied(confirmed)
    return sim.block_occupancy_detector


def arm(physics, controller):
    """Build the per-tick movement scratchpad the cap reads, as tick() does."""
    from train_physics import _work_from_span

    physics._work = _work_from_span(
        controller.physics_sync.current_span,
        controller._head_end_label() == "end_a",
    )


# ---------------------------------------------------------------------------
# Head cap
# ---------------------------------------------------------------------------


def test_head_holds_at_the_seam_of_an_unconfirmed_block():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=0, length_cm=40.0)
    # place_train leaves the head on tile 0's exit seam, so BLK_1 is one step away.
    wire_detector(sim, detected={"BLK_0", "BLK_1"}, confirmed={"BLK_0"})
    arm(physics, controller)

    allowed = physics.cap_distance_to_detected_occupancy(train, 50.0)

    assert allowed == 0.0
    assert physics.pending_entry_block == "BLK_1"


def test_head_release_hands_the_cap_to_the_tail():
    """Confirming the next block frees the head, and the tail takes over.

    A train standing wholly inside one confirmed block can never advance
    further than its tail's remaining distance in that block, whatever the head
    is allowed: the block still reads occupied because the locomotive is still
    standing on it.
    """
    network = build_line()
    sim, train, controller, physics = place(network, head_x=0, length_cm=40.0)
    wire_detector(sim, detected={"BLK_0", "BLK_1"}, confirmed={"BLK_0", "BLK_1"})
    arm(physics, controller)

    allowed = physics.cap_distance_to_detected_occupancy(train, 500.0)

    # The 40cm consist sits against tile 0's exit, so its tail has 40cm of BLK_0
    # left to run — held a hair short of the seam.
    assert 39.99 < allowed < 40.0
    assert physics.pending_entry_block is None


def test_head_mid_block_is_capped_just_short_of_the_seam():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=0, length_cm=40.0)
    # Pull the whole span back so the head sits 40cm short of tile 0's exit.
    span = controller.physics_sync.current_span
    span.end_a.distance_cm = 60.0
    span.end_b.distance_cm = 20.0
    wire_detector(sim, detected={"BLK_0", "BLK_1"}, confirmed={"BLK_0"})
    arm(physics, controller)

    allowed = physics.cap_distance_to_detected_occupancy(train, 500.0)

    # 40cm to the seam, held a hair short so the head stays inside its tile.
    assert 39.99 < allowed < 40.0
    assert physics.pending_entry_block == "BLK_1"


def test_a_shorter_step_than_the_cap_is_left_alone():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=0, length_cm=40.0)
    span = controller.physics_sync.current_span
    span.end_a.distance_cm = 60.0
    span.end_b.distance_cm = 20.0
    wire_detector(sim, detected={"BLK_0", "BLK_1"}, confirmed={"BLK_0"})
    arm(physics, controller)

    # 10cm of travel does not reach the seam 40cm ahead.
    assert physics.cap_distance_to_detected_occupancy(train, 10.0) == 10.0
    assert physics.pending_entry_block is None


# ---------------------------------------------------------------------------
# Tail cap
# ---------------------------------------------------------------------------


def test_tail_holds_inside_a_block_the_detector_still_confirms():
    network = build_line()
    # 150cm train with its head on tile 1's exit seam spans BLK_1 and BLK_0.
    sim, train, controller, physics = place(network, head_x=1, length_cm=150.0)
    assert "BLK_0" in controller.physics_sync.current_span.occupied_blocks
    # BLK_2 is confirmed so the head is free; BLK_0 is still confirmed, so the
    # tail is what holds the span.
    wire_detector(
        sim,
        detected={"BLK_0", "BLK_1", "BLK_2"},
        confirmed={"BLK_0", "BLK_1", "BLK_2"},
    )
    arm(physics, controller)

    allowed = physics.cap_distance_to_detected_occupancy(train, 500.0)

    # The tail sits 50cm into tile 0, so it may run the remaining 50cm and no
    # further while BLK_0 still reads occupied.
    assert 49.99 < allowed < 50.0
    assert physics.pending_entry_block is None


def test_tail_is_released_once_its_block_confirms_clear():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=1, length_cm=150.0)
    wire_detector(
        sim, detected={"BLK_0", "BLK_1", "BLK_2"}, confirmed={"BLK_1", "BLK_2"}
    )
    arm(physics, controller)

    assert physics.cap_distance_to_detected_occupancy(train, 500.0) == 500.0


def test_the_tighter_of_the_two_caps_decides():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=1, length_cm=150.0)
    # Head held at BLK_2's seam (0cm) while the tail could still run 50cm.
    wire_detector(
        sim, detected={"BLK_0", "BLK_1", "BLK_2"}, confirmed={"BLK_0", "BLK_1"}
    )
    arm(physics, controller)

    assert physics.cap_distance_to_detected_occupancy(train, 500.0) == 0.0
    assert physics.pending_entry_block == "BLK_2"


# ---------------------------------------------------------------------------
# A held head always ends up inside a block
# ---------------------------------------------------------------------------


def build_line_with_a_gap(gap_x: int = 2, num_tiles: int = 5) -> RailwayNetwork:
    """A straight line whose tile `gap_x` belongs to no block, standing in for a
    switch tile: real track, no block, and so no detection."""
    network = RailwayNetwork()
    for x in range(num_tiles):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        if x != gap_x:
            track.segment_id = segment_id_for_block(f"BLK_{x}")
        network.add_track(track)
        if x != gap_x:
            network.blocks[f"BLK_{x}"] = Block(
                block_id=f"BLK_{x}", length_cm=100.0, speed_limit_kmh=80.0
            )
    return network


def test_head_is_held_before_undetected_track_not_on_it():
    """The head stops at its own block's exit seam rather than at the far
    block's edge, because the track between them belongs to no block.

    A head parked outside every block has no current block to resolve, so
    `PathDispatcher.create_path` can no longer plan for the train — the failure
    reads `Cannot resolve current block`.
    """
    network = build_line_with_a_gap()
    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    train = Train(
        train_id="T",
        consist=[TrainCar(car_id="c1", car_type="LOCOMOTIVE", length_cm=40.0)],
        max_speed=80.0,
    )
    network.add_train(train)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = network.place_train(
        train, 1, 0, Direction.EAST
    )
    physics = sim._get_or_create_physics(train)
    wire_detector(
        sim,
        detected={"BLK_0", "BLK_1", "BLK_3", "BLK_4"},
        confirmed={"BLK_1"},
    )
    arm(physics, controller)

    # The head sits on BLK_1's exit seam; BLK_3 is 200cm further on, past the
    # block-less tile. The hold is taken here, not at BLK_3's edge.
    assert physics.cap_distance_to_detected_occupancy(train, 500.0) == 0.0
    assert physics.pending_entry_block == "BLK_3"
    assert dispatcher._get_current_block_id_for_train(train) == "BLK_1"


def test_a_head_already_outside_every_block_is_not_pinned_there():
    """Nothing can hold a head that is already on undetected track, so it runs
    on to the next block boundary instead of being stranded."""
    network = build_line_with_a_gap()
    sim, train, controller, physics = place(network, head_x=1, length_cm=40.0)
    span = controller.physics_sync.current_span
    span.end_a.tile = (2, 0)
    span.end_a.distance_cm = 50.0
    span.end_b.tile = (2, 0)
    span.end_b.distance_cm = 10.0
    span.occupied_tiles = [(2, 0)]
    span.occupied_blocks = []
    wire_detector(
        sim, detected={"BLK_0", "BLK_1", "BLK_3", "BLK_4"}, confirmed=set()
    )
    arm(physics, controller)

    assert physics.cap_distance_to_detected_occupancy(train, 500.0) == 500.0
    assert physics.pending_entry_block is None


# ---------------------------------------------------------------------------
# Where the cap does not apply
# ---------------------------------------------------------------------------


def test_cap_is_inert_under_the_simulated_driver():
    """The simulated driver detects nothing, so no train is ever capped.

    It builds its current map from the live spans, so a head held out of a block
    would leave that block reading clear forever and the cap would never release.
    """
    network = build_line()
    sim, train, controller, physics = place(network, head_x=0, length_cm=40.0)
    assert sim.block_occupancy_detector.detected_block_ids() == set()
    arm(physics, controller)

    assert physics.cap_distance_to_detected_occupancy(train, 500.0) == 500.0
    assert physics.pending_entry_block is None


def test_an_undetected_block_never_caps():
    """Neither the block the train stands on nor the one it is entering is
    wired to a detector, so nothing holds it: a cap taken from either would
    have no physical event to release it.

    BLK_5 is detected purely so this exercises the per-block test rather than
    the whole-session short circuit; it sits 400cm away, well past the 100cm
    step under test.
    """
    network = build_line()
    sim, train, controller, physics = place(network, head_x=0, length_cm=40.0)
    wire_detector(sim, detected={"BLK_5"}, confirmed=set())
    arm(physics, controller)

    assert physics.cap_distance_to_detected_occupancy(train, 100.0) == 100.0
    assert physics.pending_entry_block is None


def test_cap_is_inert_for_an_unidentified_train():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=0, length_cm=40.0)
    train.is_unidentified = True
    wire_detector(sim, detected={"BLK_0", "BLK_1"}, confirmed={"BLK_0"})
    arm(physics, controller)

    assert physics.cap_distance_to_detected_occupancy(train, 500.0) == 500.0
    assert physics.pending_entry_block is None


def test_cap_never_returns_a_negative_distance():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=0, length_cm=40.0)
    wire_detector(sim, detected={"BLK_0", "BLK_1"}, confirmed={"BLK_0"})
    arm(physics, controller)

    assert physics.cap_distance_to_detected_occupancy(train, -5.0) == 0.0


# ---------------------------------------------------------------------------
# Power: the block a cap holds the head out of stays live
# ---------------------------------------------------------------------------


def test_pending_entry_block_is_energized_with_the_span():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=0, length_cm=40.0)
    wire_detector(sim, detected={"BLK_0", "BLK_1"}, confirmed={"BLK_0"})
    arm(physics, controller)
    physics.cap_distance_to_detected_occupancy(train, 50.0)

    energized = physics._energized_blocks_for_train(train)

    assert "BLK_0" in energized
    assert "BLK_1" in energized, (
        "the locomotive has to roll onto BLK_1 to release its own cap; "
        "de-energizing it deadlocks the train at the seam"
    )


def test_drain_leaves_a_pending_entry_block_alone():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=0, length_cm=40.0)
    wire_detector(sim, detected={"BLK_0", "BLK_1"}, confirmed={"BLK_0"})
    arm(physics, controller)
    physics.cap_distance_to_detected_occupancy(train, 50.0)

    # BLK_1 is neither occupied nor reserved, so both drain tests would zero it.
    sim.power_controller.set_block_voltage("BLK_1", 12.0)
    sim.block_manager._mark_for_deenergize("BLK_1")
    sim._settle_after_commits()

    assert sim.power_controller.get_block_voltage("BLK_1") == 12.0


def test_drain_never_cuts_the_rail_under_a_calculated_span():
    """A detector that briefly reads clear under a standing locomotive must not
    drop its rail.

    Cutting it makes the block idle, the idle detector reads the locomotive
    again, the block re-energizes, and the drive-state read flips the occupancy
    source back — the rail oscillates instead of settling.
    """
    network = build_line()
    sim, train, controller, physics = place(network, head_x=0, length_cm=40.0)
    wire_detector(sim, detected={"BLK_0", "BLK_1"}, confirmed={"BLK_0"})
    assert "BLK_0" in controller.physics_sync.current_span.occupied_blocks

    sim.power_controller.set_block_voltage("BLK_0", 6.0)
    network.blocks["BLK_0"].occupied_by = None
    sim.block_manager._mark_for_deenergize("BLK_0")
    sim._settle_after_commits()

    assert sim.power_controller.get_block_voltage("BLK_0") == 6.0


def test_drain_zeroes_a_block_no_cap_is_waiting_on():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=0, length_cm=40.0)
    wire_detector(sim, detected={"BLK_0", "BLK_1"}, confirmed={"BLK_0"})
    arm(physics, controller)
    physics.cap_distance_to_detected_occupancy(train, 50.0)

    sim.power_controller.set_block_voltage("BLK_3", 12.0)
    sim.block_manager._mark_for_deenergize("BLK_3")
    sim._settle_after_commits()

    assert sim.power_controller.get_block_voltage("BLK_3") == 0.0


# ---------------------------------------------------------------------------
# End to end through the tick
# ---------------------------------------------------------------------------


def drive_with_authority(confirmed):
    """A powered train on BLK_0 with authority to BLK_4 and the throttle open."""
    network = build_line()
    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    train = Train(
        train_id="T",
        consist=[TrainCar(car_id="c1", car_type="LOCOMOTIVE", length_cm=40.0)],
        max_speed=80.0,
    )
    network.add_train(train)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = network.place_train(
        train, 0, 0, Direction.EAST
    )
    physics = sim._get_or_create_physics(train)
    wire_detector(
        sim, detected={f"BLK_{i}" for i in range(6)}, confirmed=confirmed
    )
    assert dispatcher.create_path(
        train_id="T", requested_destination_block_id="BLK_4"
    ).success
    controller.commands.power_on_cmd()
    controller.commands.set_mode("manual")
    controller._apply_head_frame("end_a")
    controller.commands.set_handle(HandlePosition.P4)
    return sim, train, controller, physics


def test_a_driven_train_never_claims_a_block_the_detector_withholds():
    """The reported bug: the app drew the train on the next block while the
    locomotive was still standing on the previous one."""
    sim, train, controller, physics = drive_with_authority({"BLK_0"})

    for _ in range(40):
        sim.update(0.1)

    # The train really is running — it is the span that is held, not the train.
    assert controller.physics_sync.current_speed_kmh > 1.0
    span = controller.physics_sync.current_span
    assert span.occupied_blocks == ["BLK_0"], (
        f"span claimed {span.occupied_blocks} while the detector confirmed only "
        f"{sorted(sim.block_occupancy_detector.confirmed_occupied)}"
    )
    assert physics.pending_entry_block == "BLK_1"
    assert sim.network.blocks["BLK_1"].occupied_by is None

    # Once the detector sees the locomotive on BLK_1, the span follows it in.
    sim.block_occupancy_detector.input_interface.live.add("BLK_1")
    for _ in range(40):
        sim.update(0.1)

    assert "BLK_1" in controller.physics_sync.current_span.occupied_blocks
    assert physics.pending_entry_block is None


def test_a_held_span_keeps_its_pending_entry_block_live():
    """The rail the locomotive has to roll onto to release its own cap."""
    sim, train, controller, physics = drive_with_authority({"BLK_0"})

    for _ in range(40):
        sim.update(0.1)

    assert physics.pending_entry_block == "BLK_1"
    assert sim.power_controller.get_block_voltage("BLK_1") != 0.0


def test_a_held_head_energizes_no_reserved_block_past_its_pending_entry_block():
    """Only the block the train is entering may be live ahead of it.

    The reported bug: during speed calibration on the rig, the far outer block
    went live 1.5–3 s before the measured block read the locomotive. The cap
    held the head at the measured block's seam, and the next-reserved walk
    treated that pending-entry block as already stood on, so it skipped it and
    energized the reserved block beyond as well (design_new.md "Power", Only
    the next reserved block is pre-energized).
    """
    sim, train, controller, physics = drive_with_authority({"BLK_0"})

    for _ in range(40):
        sim.update(0.1)

    assert physics.pending_entry_block == "BLK_1"
    assert physics.next_reserved_block == "BLK_1"
    assert sim.network.blocks["BLK_2"].reserved_for_train == "T"
    assert sim.power_controller.get_block_voltage("BLK_2") == 0.0


# ---------------------------------------------------------------------------
# A head stopped on a switch still has a block to plan from
# ---------------------------------------------------------------------------


def test_a_head_on_a_switch_plans_from_the_block_behind_it():
    """A switch tile belongs to no block, but a train straddling one is still
    plainly on the block its body occupies — and that is the block planning
    starts from.

    Rejecting here reported an ordinary standing position as a train whose
    location could not be resolved: `Cannot resolve current block for train ...`.
    """
    network = build_line_with_a_gap()
    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    train = Train(
        train_id="T",
        consist=[TrainCar(car_id="c1", car_type="LOCOMOTIVE", length_cm=120.0)],
        max_speed=80.0,
    )
    network.add_train(train)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = network.place_train(
        train, 1, 0, Direction.EAST
    )
    # Head onto the block-less tile, body still trailing back into BLK_1.
    span = controller.physics_sync.current_span
    span.end_a.tile = (2, 0)
    span.end_a.distance_cm = 0.0
    span.occupied_tiles = [(2, 0), (1, 0)]
    span.occupied_blocks = ["BLK_1"]

    assert network.get_track(2, 0).block_id is None
    assert dispatcher._get_current_block_id_for_train(train) == "BLK_1"


# ---------------------------------------------------------------------------
# Rear pin: a held confirmed block behind the span
# ---------------------------------------------------------------------------


def test_a_held_confirmed_block_behind_the_span_pins_the_train():
    """Reversed wiring drove the loco backwards into BLK_1 while the span sits
    on BLK_2. Attribution handed BLK_1 to this train; the forward-only snap
    does not move the span for it. The tail rule still applies: the span may
    not run away from a block whose detector says the train is standing there.
    """
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    wire_detector(sim, detected={"BLK_1", "BLK_2"}, confirmed={"BLK_1", "BLK_2"})
    network.blocks["BLK_1"].occupied_by = "T"
    arm(physics, controller)

    assert physics.cap_distance_to_detected_occupancy(train, 500.0) == 0.0


def test_the_rear_pin_releases_when_the_behind_block_vacates():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    # BLK_1 confirmed clear again and released by the ordinary vacate path.
    wire_detector(sim, detected={"BLK_1", "BLK_2"}, confirmed={"BLK_2"})
    arm(physics, controller)

    # Only the ordinary tail cap remains: the 40cm consist against tile 2's
    # exit seam may run its tail's remaining 40cm of BLK_2.
    allowed = physics.cap_distance_to_detected_occupancy(train, 500.0)
    assert 39.99 < allowed < 40.0


def test_a_held_confirmed_block_ahead_does_not_pin():
    """A held confirmed block the head can reach forward is the forward snap's
    job — it must not be mistaken for a locomotive standing behind the span."""
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    wire_detector(sim, detected={"BLK_2", "BLK_3"}, confirmed={"BLK_2", "BLK_3"})
    network.blocks["BLK_3"].occupied_by = "T"
    arm(physics, controller)

    allowed = physics.cap_distance_to_detected_occupancy(train, 500.0)
    assert allowed > 0.0
