"""Only the next reserved block is pre-energized (design_new.md "Power").

A train's emission set is the blocks it stands on, its `pending-entry block`,
and the single next block of its reserved corridor — never the whole corridor.
One block ahead is what it takes to cross an insulated boundary without a power
gap; every further block of live rail is track a train running faster than its
speed profile keeps accelerating over while the app still believes it is a block
back.

These tests reuse the straight-line harness from the seam-cap suite: six 100cm
blocks BLK_0..BLK_5 on one E-W run.
"""

from block_manager import ReservedBlockTraversal
from enums import Direction

from tests.test_block_occupancy_cap import arm, build_line, place


def _fwd(exit_edge="E"):
    return ReservedBlockTraversal(exit_edge=exit_edge, block_direction="forward")


def reserve(network, block_ids, train_id="T", path_id="P1"):
    result = network.block_manager.reserve_blocks_for_path(
        list(block_ids),
        path_id,
        train_id,
        directions={bid: _fwd() for bid in block_ids},
    )
    assert result.success is True, result.reason
    return result


# ---------------------------------------------------------------------------
# The emission set
# ---------------------------------------------------------------------------


def test_only_the_first_block_of_the_corridor_is_energized():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=0, length_cm=40.0)
    reserve(network, ["BLK_1", "BLK_2", "BLK_3"])
    arm(physics, controller)

    energized = physics._energized_blocks_for_train(train)

    assert "BLK_0" in energized, "the block the locomotive stands on"
    assert "BLK_1" in energized, "the boundary it is about to cross"
    assert "BLK_2" not in energized
    assert "BLK_3" not in energized
    assert physics.next_reserved_block == "BLK_1"


def test_the_energized_block_advances_with_the_train():
    """Entering the pre-energized block hands the pre-energizing on to the next
    one, so the loco never meets dead rail at a seam it is authorized to cross."""
    network = build_line()
    sim, train, controller, physics = place(network, head_x=0, length_cm=40.0)
    reserve(network, ["BLK_1", "BLK_2", "BLK_3"])
    arm(physics, controller)
    assert physics._energized_blocks_for_train(train) == {"BLK_0", "BLK_1"}

    # The head reaches BLK_1 and its detector confirms it.
    network.block_manager.occupy_block("BLK_1", "T")
    controller.physics_sync.current_span = network.place_train(
        train, 1, 0, Direction.EAST
    )
    arm(physics, controller)

    energized = physics._energized_blocks_for_train(train)
    assert "BLK_1" in energized
    assert "BLK_2" in energized, "the next boundary is pre-energized in turn"
    assert "BLK_3" not in energized
    assert physics.next_reserved_block == "BLK_2"


def test_a_train_past_its_authority_has_dead_rail_in_front_of_it():
    """Nothing reserved ahead means nothing energized ahead — the layout itself
    enforces the authority end."""
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    arm(physics, controller)

    energized = physics._energized_blocks_for_train(train)

    assert energized == {"BLK_2"}
    assert physics.next_reserved_block is None


def test_the_walk_skips_blocks_the_consist_is_still_standing_on():
    """A long consist straddles blocks; the next reserved block is the first one
    ahead it is *not* standing on, not the first one the walk meets."""
    network = build_line()
    # 140cm of train, head on tile 2's exit seam: all of BLK_2 and 40cm of BLK_1.
    sim, train, controller, physics = place(network, head_x=2, length_cm=140.0)
    reserve(network, ["BLK_3", "BLK_4"])
    arm(physics, controller)

    energized = physics._energized_blocks_for_train(train)

    assert {"BLK_1", "BLK_2"} <= energized, "the whole consist"
    assert "BLK_3" in energized
    assert "BLK_4" not in energized


def test_an_unidentified_train_energizes_nothing_and_resolves_no_next_block():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=0, length_cm=40.0)
    reserve(network, ["BLK_1"])
    arm(physics, controller)
    physics._energized_blocks_for_train(train)
    assert physics.next_reserved_block == "BLK_1"

    train.is_unidentified = True

    assert physics._energized_blocks_for_train(train) == set()
    assert physics.next_reserved_block is None


# ---------------------------------------------------------------------------
# De-energizing against the same source
# ---------------------------------------------------------------------------


def test_the_drain_holds_the_next_reserved_block_and_nothing_beyond():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=0, length_cm=40.0)
    reserve(network, ["BLK_1", "BLK_2"])
    arm(physics, controller)
    physics._energized_blocks_for_train(train)

    held = sim._calculated_hold_blocks()

    assert "BLK_0" in held
    assert "BLK_1" in held, (
        "the emission side drives it, so the drain must not zero it on the "
        "same tick"
    )
    assert "BLK_2" not in held


def test_a_block_that_stops_being_next_is_reported_for_the_drain():
    """`BlockManager` counts a still-reserved block as held, so nothing would
    queue one the train stopped energizing. The emission side reports it."""
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    reserve(network, ["BLK_3"])
    arm(physics, controller)
    physics._set_voltage_for_blocks(train, 9.0)
    assert sim.power_controller.get_block_voltage("BLK_3") == 9.0
    assert physics.take_blocks_dropped_from_energized() == set()

    # The corridor is replanned the other way: BLK_3 stays reserved for this
    # train but is no longer the block in front of it.
    network.block_manager.release_block("BLK_3")
    reserve(network, ["BLK_1"], path_id="P2")
    controller.physics_sync.current_span = network.place_train(
        train, 2, 0, Direction.WEST
    )
    arm(physics, controller)
    physics._set_voltage_for_blocks(train, 9.0)

    assert physics.next_reserved_block == "BLK_1"
    assert "BLK_3" in physics._blocks_dropped_from_energized


def test_the_settle_drain_zeroes_a_block_that_left_the_energized_set():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    reserve(network, ["BLK_3"])
    arm(physics, controller)
    physics._set_voltage_for_blocks(train, 9.0)
    assert sim.power_controller.get_block_voltage("BLK_3") == 9.0

    network.block_manager.release_block("BLK_3")
    reserve(network, ["BLK_1"], path_id="P2")
    controller.physics_sync.current_span = network.place_train(
        train, 2, 0, Direction.WEST
    )
    arm(physics, controller)
    physics._set_voltage_for_blocks(train, 9.0)
    sim._settle_after_commits()

    assert sim.power_controller.get_block_voltage("BLK_3") == 0.0
    assert sim.power_controller.get_block_voltage("BLK_2") == 9.0


def test_the_drain_leaves_a_block_another_train_picked_up_alone():
    """One block is one feed, so only the drain can decide: this train dropping
    a block says nothing about whether another train just took it."""
    from train import Train, TrainCar

    network = build_line()
    sim, train, controller, physics = place(network, head_x=0, length_cm=40.0)
    reserve(network, ["BLK_1"])
    arm(physics, controller)
    physics._set_voltage_for_blocks(train, 9.0)
    assert sim.power_controller.get_block_voltage("BLK_1") == 9.0

    # BLK_1 leaves this train's set, but a second train is standing on it.
    network.block_manager.release_block("BLK_1")
    other = Train(
        train_id="T2",
        consist=[TrainCar(car_id="c1", car_type="LOCOMOTIVE", length_cm=40.0)],
        max_speed=80.0,
    )
    network.add_train(other)
    other_controller = sim._get_or_create_controller(other)
    other_controller.physics_sync.current_span = network.place_train(
        other, 1, 0, Direction.EAST
    )
    other_physics = sim._get_or_create_physics(other)

    arm(physics, controller)
    physics._set_voltage_for_blocks(train, 9.0)
    arm(other_physics, other_controller)
    other_physics._set_voltage_for_blocks(other, 6.0)
    sim._settle_after_commits()

    assert sim.power_controller.get_block_voltage("BLK_1") == 6.0
