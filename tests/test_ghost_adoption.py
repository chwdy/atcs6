"""Ghost-train adoption (design_new.md "Power" -> Ghost trains, Adoption).

A ghost is what the detector found; adoption is the operator saying what it is.
The `Train` record survives the conversion — same object, same controller, same
held blocks — and only its identity, its configuration, and `is_ghost` change.

The alternative (delete the ghost, add a fresh train in its place) is what these
tests exist to rule out: block occupancy is edge-triggered off the detector
(`BlockOccupancyDetector.step` diffs the confirmed set), so releasing a block
that is still drawing current would leave it `FREE` with a locomotive standing on
it and no future entry event to correct that.

Only real hardware produces a ghost, so these tests inject a hand-driven
`OccupancyInput` in place of the simulated one, exactly as `test_ghost_trains`
does.
"""

import pytest

from block import Block
from constants import OCCUPANCY_OFF_THRESHOLD
from enums import BlockState, TrainState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from services import ValidationError, train_service
from simulation import SimulationController
from tests.test_ghost_trains import (
    FakeDetector,
    build_sim,
    ghost_ids,
    only_ghost,
    tick,
)
from track import Tile, TRACK_EW


def adopt(sim, ghost_id, new_id="T1", **kwargs):
    """Adopt with the fields every call needs, overridable per test."""
    params = dict(
        max_speed_kmh=80.0,
        length_cm=50.0,
        direction="EAST",
        cab="A",
    )
    params.update(kwargs)
    return sim.adopt_ghost_train(ghost_id, new_id, **params)


def a_ghost_on(sim, detector, block_id="BLK_1"):
    detector.current = {block_id: True}
    tick(sim)
    return only_ghost(sim.network)


# ------------------------------------------------------------------- the fix


def test_adopting_a_ghost_does_not_halt_the_runtime():
    """The bug this feature exists for: setting a detected train up as a real
    one used to run `place_train` -> `occupy_blocks` against a ghost-held block,
    which is the `occupied_by_other_train` fatal conflict."""
    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)

    adopt(sim, ghost_id)

    assert sim.failed_runtime is False
    assert sim.failed_runtime_payload is None


def test_adoption_re_keys_the_train_across_every_registry():
    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)

    train = adopt(sim, ghost_id, "BR218")

    assert train.train_id == "BR218"
    assert train.is_ghost is False
    assert sorted(network.trains) == ["BR218"]
    assert sorted(sim.controllers) == ["BR218"]
    assert sorted(sim.physics_instances) == ["BR218"]
    # The controller and physics objects carry the new id too, not just the key.
    assert sim.controllers["BR218"].train_id == "BR218"
    assert sim.physics_instances["BR218"].train_id == "BR218"
    assert network.blocks["BLK_1"].occupied_by == "BR218"


def test_the_adopted_train_keeps_standing_on_the_blocks_it_held():
    """The block must never go FREE in the handover — the locomotive is still
    on it, and the edge-triggered detector would never re-report it."""
    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)

    adopt(sim, ghost_id)

    block = network.blocks["BLK_1"]
    assert block.occupied_by == "T1"
    assert block.state == BlockState.OCCUPIED
    # And it stays that way while the current keeps flowing.
    tick(sim, times=5)
    assert network.blocks["BLK_1"].occupied_by == "T1"
    assert network.blocks["BLK_1"].state == BlockState.OCCUPIED


def test_the_adopted_train_is_not_re_detected_as_a_new_ghost():
    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)

    adopt(sim, ghost_id)
    tick(sim, times=10)

    assert ghost_ids(network) == []
    assert sim.failed_runtime is False


def test_blocks_the_consist_does_not_reach_go_back_to_being_ghosts():
    """A ghost claims whole blocks because its extent is unknown. Adoption
    replaces that with the operator's declared consist, and whatever the rebuilt
    span does not cover is not this train — it is current on track no train the
    app controls stands on, which is a ghost.

    Keeping it on the adopted train walls that train off from its own route: a
    block with `occupied_by` set is not an eligible reservation candidate for
    anyone, its own holder included."""
    network, sim, detector = build_sim()
    detector.current = {"BLK_0": True, "BLK_1": True}
    tick(sim)
    ghost_id = only_ghost(network)

    # A 50cm consist covers one 100cm block, not two.
    adopt(sim, ghost_id, length_cm=50.0, head_tile=(0, 0))

    assert network.blocks["BLK_0"].occupied_by == "T1"
    surplus_owner = network.blocks["BLK_1"].occupied_by
    assert surplus_owner != "T1"
    assert sim.ghost_trains.is_ghost(surplus_owner)
    # Still OCCUPIED, so nothing is routed into it — the current is real.
    assert network.blocks["BLK_1"].state == BlockState.OCCUPIED


def test_an_adopted_train_never_holds_a_block_outside_its_own_span():
    """The invariant behind the routing fix, stated directly."""
    network, sim, detector = build_sim(num_blocks=4)
    detector.current = {"BLK_0": True, "BLK_1": True, "BLK_2": True}
    tick(sim)
    ghost_id = only_ghost(network)

    adopt(sim, ghost_id, length_cm=50.0, head_tile=(0, 0))

    span = set(sim.controllers["T1"].physics_sync.current_span.occupied_blocks)
    owned = {b for b, blk in network.blocks.items() if blk.occupied_by == "T1"}
    assert owned == span


def test_the_operator_is_told_which_blocks_were_handed_back():
    """Otherwise a refused route ('block ahead is occupied') has no visible
    connection to the length they typed in."""
    network, sim, detector = build_sim()
    detector.current = {"BLK_0": True, "BLK_1": True}
    tick(sim)
    ghost_id = only_ghost(network)

    adopt(sim, ghost_id, length_cm=50.0, head_tile=(0, 0))

    recent = sim.notification_center.peek_recent(50)
    note = next(n for n in recent if n.kind == "adoption_surplus_blocks")
    assert note.payload["blocks"] == ["BLK_1"]
    assert "T1" in note.payload["message"]


def test_a_consist_that_reaches_both_blocks_keeps_both():
    """No surplus when the declared length actually covers what was detected."""
    network, sim, detector = build_sim()
    detector.current = {"BLK_0": True, "BLK_1": True}
    tick(sim)
    ghost_id = only_ghost(network)

    # Head at the far end of BLK_1 with a 150cm consist reaches back into BLK_0.
    adopt(sim, ghost_id, length_cm=150.0, head_tile=(1, 0))

    owned = {b for b, blk in network.blocks.items() if blk.occupied_by == "T1"}
    assert owned == {"BLK_0", "BLK_1"}
    assert sim.ghost_trains.ghost_ids == []


# ----------------------------------------------------------------- placement


def build_wide_sim(tiles_per_block: int = 3, block_len: float = 300.0):
    """Three blocks of several tiles each, so "where inside the block" is a real
    question. `build_sim` gives one tile per block, where every position is a
    boundary."""
    network = RailwayNetwork()
    for b in range(3):
        block = Block(block_id=f"BLK_{b}", length_cm=block_len)
        entries = []
        for i in range(tiles_per_block):
            x = b * tiles_per_block + i
            track = Tile(x=x, y=0, track_bits=TRACK_EW)
            track.segment_id = f"BLK_{b}"
            network.add_track(track)
            entries.append((x, 0, "W"))
        # `ordered_tiles` reads through to the block's segments, so it is
        # assigned once rather than appended to.
        block.ordered_tiles = entries
        network.blocks[f"BLK_{b}"] = block
    detector = FakeDetector()
    sim = SimulationController(
        network, PathDispatcher(network), input_interface=detector
    )
    sim.ghost_clock = lambda: sim._fake_clock
    sim._fake_clock = 0.0
    return network, sim, detector


def line_extent_cm(network, span):
    """(west end, east end) of `span` in centimetres along the whole line.

    `build_wide_sim` lays every tile out west to east, so BLK_0 is 0-300 cm,
    BLK_1 is 300-600 and BLK_2 is 600-900. A `SpanEndLocator`'s `distance_cm`
    is measured from that end's own `entry_edge`, which is `E` for a westbound
    train, so it is flipped onto the line's west-to-east axis here.
    """
    starts = {}
    run = 0.0
    for x in sorted(x for (x, _y) in network.tracks):
        starts[(x, 0)] = run
        run += network._get_tile_length_cm(x, 0)

    def position(end):
        tile = tuple(end.tile)
        length = network._get_tile_length_cm(*tile)
        along = end.distance_cm if end.entry_edge == "W" else length - end.distance_cm
        return starts[tile] + along

    return tuple(sorted((position(span.end_a), position(span.end_b))))


def test_the_adopted_train_is_centered_in_the_track_that_was_detected():
    """The detector says the train is somewhere in this block, not that it is
    flush against a boundary. Centering is the only reading that adds nothing
    the detector did not say."""
    network, sim, detector = build_wide_sim()
    detector.current = {"BLK_1": True}
    tick(sim, times=3)
    ghost_id = only_ghost(network)

    adopt(sim, ghost_id, length_cm=100.0)

    span = sim.controllers["T1"].physics_sync.current_span
    # BLK_1 is 300-600 cm along the line; a 100 cm train centred in it is 400-500.
    assert line_extent_cm(network, span) == (400.0, 500.0)
    assert span.occupied_blocks == ["BLK_1"]


def test_centering_does_not_depend_on_which_way_it_faces():
    """Facing picks which end is the front, not where the body sits."""
    network, sim, detector = build_wide_sim()
    detector.current = {"BLK_1": True}
    tick(sim, times=3)
    ghost_id = only_ghost(network)

    adopt(sim, ghost_id, length_cm=100.0, direction="WEST")

    span = sim.controllers["T1"].physics_sync.current_span
    assert line_extent_cm(network, span) == (400.0, 500.0)


def test_a_declared_head_tile_puts_the_front_at_that_tiles_exit_edge():
    """Centering is the default, not a rule: an operator who can see where the
    train actually stands says so, and that wins."""
    network, sim, detector = build_wide_sim()
    detector.current = {"BLK_1": True}
    tick(sim, times=3)
    ghost_id = only_ghost(network)

    adopt(sim, ghost_id, length_cm=100.0, head_tile=(4, 0))

    span = sim.controllers["T1"].physics_sync.current_span
    # Tile (4, 0) runs 400-500 cm along the line, so its EAST exit edge is 500
    # and a 100 cm body reaches back to 400.
    assert line_extent_cm(network, span) == (400.0, 500.0)
    assert tuple(span.end_a.tile) == (4, 0)


def test_the_body_never_reaches_a_block_that_reads_no_current():
    """The neighbour is FREE, so nothing is standing on it — a declared consist
    that would reach in is refused rather than claiming track the detector says
    is clear."""
    network, sim, detector = build_wide_sim()
    detector.current = {"BLK_1": True}
    tick(sim, times=3)
    ghost_id = only_ghost(network)

    with pytest.raises(ValueError, match="reaches into BLK_0"):
        adopt(sim, ghost_id, length_cm=420.0)

    # Refused before anything moved: still a ghost, neighbour still clear.
    assert sim.ghost_trains.ghost_ids == [ghost_id]
    assert network.blocks["BLK_1"].occupied_by == ghost_id
    assert network.blocks["BLK_0"].occupied_by is None
    assert "T1" not in network.trains


def test_a_declared_head_tile_cannot_reach_past_the_detected_track_either():
    network, sim, detector = build_wide_sim()
    detector.current = {"BLK_1": True}
    tick(sim, times=3)
    ghost_id = only_ghost(network)

    with pytest.raises(ValueError, match="reaches into BLK_0"):
        adopt(sim, ghost_id, length_cm=150.0, head_tile=(3, 0))

    assert network.blocks["BLK_0"].occupied_by is None
    assert sim.ghost_trains.ghost_ids == [ghost_id]


def test_a_consist_spanning_two_detected_blocks_is_centered_across_both():
    network, sim, detector = build_wide_sim()
    detector.current = {"BLK_0": True, "BLK_1": True}
    tick(sim, times=3)
    ghost_id = only_ghost(network)

    adopt(sim, ghost_id, length_cm=400.0)

    span = sim.controllers["T1"].physics_sync.current_span
    # 600 cm detected, 400 cm of train: 100 cm spare at each end, so the body
    # runs from 100 cm into BLK_0 to 200 cm into BLK_1.
    assert sorted(span.occupied_blocks) == ["BLK_0", "BLK_1"]
    assert line_extent_cm(network, span) == (100.0, 500.0)


# ------------------------------------------------------------------- driving


def test_an_adopted_train_accepts_operator_commands_a_ghost_refuses():
    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)
    controller = sim.controllers[ghost_id]

    # As a ghost: no cab, and the service layer refuses to drive it.
    assert controller.commands.cab is None

    adopt(sim, ghost_id, cab="A")

    controller = sim.controllers["T1"]
    assert controller.commands.cab == "A"
    assert controller.commands.power_on_cmd().success is True
    assert controller.ops.state is not TrainState.OFF


def test_adoption_rebuilds_the_span_from_the_real_consist_length():
    """A ghost's span claims whole blocks because nobody knows where inside them
    it sits; the operator's declared length and facing replace that guess."""
    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)
    ghost_span = sim.controllers[ghost_id].physics_sync.current_span
    assert ghost_span.end_b.distance_cm == pytest.approx(100.0)

    adopt(sim, ghost_id, length_cm=40.0, direction="EAST")

    span = sim.controllers["T1"].physics_sync.current_span
    assert span.occupied_blocks == ["BLK_1"]
    assert span.occupied_tiles == [(1, 0)]


def test_adoption_gives_the_train_its_own_profile_and_retires_the_ghosts():
    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)
    ghost_profile_id = network.trains[ghost_id].speed_profile_id
    assert network.speed_profiles.get(ghost_profile_id) is not None

    train = adopt(sim, ghost_id, "T1", max_speed_kmh=80.0)

    assert train.speed_profile_id == "default_T1"
    profile = network.speed_profiles.get("default_T1")
    assert profile is not None
    # Anchored to the real locomotive's max speed, not the blank ghost default.
    assert profile.power_speed_curve[-1] == (100.0, 80.0)
    assert network.speed_profiles.get(ghost_profile_id) is None


def test_an_existing_speed_profile_can_be_kept_instead():
    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)
    from speed_profile import make_default_speed_profile

    measured = make_default_speed_profile("measured_br218", 95.0)
    network.speed_profiles.register(measured)

    train = adopt(sim, ghost_id, speed_profile_id=measured.profile_id)

    assert train.speed_profile_id == measured.profile_id


def test_adoption_carries_the_operators_configuration_onto_the_train():
    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)

    train = adopt(
        sim,
        ghost_id,
        max_speed_kmh=120.0,
        length_cm=64.0,
        name="Blue Pullman",
        train_type="BR 251",
        min_radius_cm=42.0,
        max_slope=3.5,
    )

    assert train.max_speed == 120.0
    assert train.length_cm == 64.0
    assert train.car_number == "Blue Pullman"
    assert train.model == "BR 251"
    assert train.min_radius_cm == 42.0
    assert train.max_slope == 3.5
    assert train.consist[0].car_id == "T1_loco"


def test_the_new_id_may_be_the_generated_one():
    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)

    train = adopt(sim, ghost_id, ghost_id)

    assert train.train_id == ghost_id
    assert train.is_ghost is False
    assert network.blocks["BLK_1"].occupied_by == ghost_id


def test_the_head_tile_may_be_chosen_from_the_tiles_the_ghost_claims():
    network, sim, detector = build_sim()
    detector.current = {"BLK_0": True, "BLK_1": True}
    tick(sim)
    ghost_id = only_ghost(network)

    adopt(sim, ghost_id, head_tile=(1, 0), length_cm=40.0)

    span = sim.controllers["T1"].physics_sync.current_span
    assert span.occupied_tiles == [(1, 0)]


# ----------------------------------------------------------------- refusals


def test_an_ordinary_train_cannot_be_adopted():
    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)
    adopt(sim, ghost_id)

    with pytest.raises(ValueError, match="not a ghost train"):
        adopt(sim, "T1", "T2")


def test_an_unknown_train_cannot_be_adopted():
    network, sim, detector = build_sim()

    with pytest.raises(ValueError, match="Unknown train"):
        adopt(sim, "NOPE")


def test_a_taken_id_is_refused():
    network, sim, detector = build_sim()
    detector.current = {"BLK_0": True}
    tick(sim)
    first = only_ghost(network)
    adopt(sim, first, "T1")
    # A second, unconnected ghost elsewhere on the line.
    detector.current = {"BLK_0": True, "BLK_2": True}
    tick(sim)
    second = [g for g in ghost_ids(network)][0]

    with pytest.raises(ValueError, match="already exists"):
        adopt(sim, second, "T1")


def test_a_bad_direction_is_refused():
    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)

    with pytest.raises(ValueError, match="Invalid direction"):
        adopt(sim, ghost_id, direction="SIDEWAYS")


def test_a_head_tile_the_ghost_does_not_claim_is_refused():
    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)

    with pytest.raises(ValueError, match="not a tile ghost train"):
        adopt(sim, ghost_id, head_tile=(0, 0))


def test_a_ghost_in_the_undetected_gap_cannot_be_adopted():
    """It is holding nothing, so there is no position to adopt it at."""
    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)
    detector.current = {}
    tick(sim, times=OCCUPANCY_OFF_THRESHOLD)
    assert sim.controllers[ghost_id].physics_sync.current_span is None

    with pytest.raises(ValueError, match="not standing on any detected block"):
        adopt(sim, ghost_id)


def test_adoption_is_refused_when_occupancy_is_derived_from_the_model():
    """A ghost is a report from a real detector about a real locomotive. Where
    occupancy is calculated there is nothing physical to adopt."""
    from block_occupancy import SimulatedOccupancyDriver

    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)
    sim.input_interface = SimulatedOccupancyDriver(network, sim.controllers)
    assert sim.occupancy_is_real is False

    with pytest.raises(ValueError, match="real detectors"):
        adopt(sim, ghost_id)


# -------------------------------------------------------- tracker bookkeeping


def test_an_adopted_train_is_never_absorbed_by_a_neighbouring_ghost():
    """Identity reconciliation groups ghost-held blocks by connectivity. The
    adopted train's blocks must drop out of that entirely.

    The new ghost is raised two blocks away, because a block *next to* the
    adopted train is that train spanning two blocks rather than a new object
    (see `test_current_next_to_a_real_train_is_that_train`)."""
    network, sim, detector = build_sim(5)
    ghost_id = a_ghost_on(sim, detector, "BLK_1")

    adopt(sim, ghost_id)

    # A genuinely new ghost appears, disconnected from the adopted train.
    detector.current = {"BLK_1": True, "BLK_4": True}
    tick(sim)

    assert network.blocks["BLK_1"].occupied_by == "T1"
    assert network.blocks["BLK_4"].occupied_by in ghost_ids(network)
    assert "T1" not in ghost_ids(network)


def test_the_generated_id_is_not_handed_out_again_after_adoption():
    network, sim, detector = build_sim(5)
    ghost_id = a_ghost_on(sim, detector, "BLK_1")
    adopt(sim, ghost_id, ghost_id)  # keeps GHOST_1 as its real id

    detector.current = {"BLK_1": True, "BLK_4": True}
    tick(sim)

    new_ghosts = ghost_ids(network)
    assert new_ghosts and ghost_id not in new_ghosts


# ------------------------------------------------ one locomotive, one record


def test_current_next_to_a_real_train_is_that_train():
    """A locomotive standing across a block boundary draws current on both
    blocks. The far block matches no reservation and no span, but it is not a
    second object — it is this train spanning two blocks.

    Before this rule it was handed a ghost, and because a ghost is never
    energized that block was driven to 0V, stranding the very locomotive the app
    was driving on dead rail."""
    network, sim, detector = build_sim(5)
    ghost_id = a_ghost_on(sim, detector, "BLK_1")
    adopt(sim, ghost_id)

    detector.current = {"BLK_1": True, "BLK_0": True}
    tick(sim)

    assert network.blocks["BLK_0"].occupied_by == "T1"
    assert ghost_ids(network) == []


def test_current_with_empty_blocks_in_between_is_still_a_ghost():
    """Adjacency is the whole test: current that is not connected to any train
    through undetected track is a genuine ghost."""
    network, sim, detector = build_sim(5)
    ghost_id = a_ghost_on(sim, detector, "BLK_1")
    adopt(sim, ghost_id)

    detector.current = {"BLK_1": True, "BLK_3": True}
    tick(sim)

    assert network.blocks["BLK_3"].occupied_by in ghost_ids(network)
    assert network.blocks["BLK_3"].occupied_by != "T1"


# --------------------------------------------------- service-layer validation


class _State:
    """The slice of AppState the train service touches."""

    def __init__(self, network, sim):
        import threading

        self.network = network
        self.sim_controller = sim
        self.simulation_lock = threading.RLock()


def test_the_service_layer_rejects_a_bad_cab():
    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)

    with pytest.raises(ValidationError, match="Invalid cab"):
        train_service.adopt_ghost_train(
            _State(network, sim), ghost_id, new_train_id="T1", cab="C"
        )


def test_the_service_layer_rejects_a_non_positive_length():
    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)

    with pytest.raises(ValidationError, match="length_cm must be greater"):
        train_service.adopt_ghost_train(
            _State(network, sim), ghost_id, new_train_id="T1", length_cm=0
        )


def test_the_service_layer_adopts_through_the_runtime():
    network, sim, detector = build_sim()
    ghost_id = a_ghost_on(sim, detector)

    train = train_service.adopt_ghost_train(
        _State(network, sim),
        ghost_id,
        new_train_id="T1",
        length_cm=50.0,
        max_speed_kmh=80.0,
        direction="EAST",
        head_tile={"x": 1, "y": 0},
        cab="B",
        car_number="Pendolino",
    )

    assert train.train_id == "T1"
    assert train.is_ghost is False
    assert train.car_number == "Pendolino"
    assert sim.controllers["T1"].commands.cab == "B"
