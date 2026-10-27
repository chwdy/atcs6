"""Unidentified trains (design_new.md "Power" -> Unidentified trains).

Current detected on track that no train the app controls is standing on becomes
a real `Train` with `is_unidentified = True` and its own `TrainController`, so its block
entries and vacates run through the ordinary Observe attribution -> movement callback ->
occupancy-commit coordinator/manager chain (the whole chain now runs inside
Observe). The app can never drive it; keeping track of
it is what keeps every other train clear of it.

Only real hardware can produce an unidentified train — the simulated occupancy driver builds
its current map from live train spans, so every confirmed block already matches a
controllable train. These tests inject a fake `OccupancyInput` in its place and
drive real ticks through it.
"""

import threading
from types import SimpleNamespace

import pytest

from block import Block
from block_occupancy import OccupancyInput
from constants import (
    UNIDENTIFIED_GAP_HOLD_S,
    OCCUPANCY_OFF_THRESHOLD,
    OCCUPANCY_ON_THRESHOLD,
)
from enums import BlockState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from runtime_payloads import PathAcquisitionFailureKind
from services import ValidationError, train_service
from simulation import SimulationController
from track import Tile, TRACK_EW
from entity_ids import segment_id_for_block
from tests.segment_test_helpers import make_segment_block_with_tracks


class FakeDetector(OccupancyInput):
    """A hand-driven per-block current map, standing in for real detectors."""

    def __init__(self):
        self.current = {}

    def read_block_currents(self):
        return dict(self.current)


def build_sim(num_blocks: int = 3):
    """A straight E-W line, one block per tile (BLK_0 .. BLK_{n-1})."""
    network = RailwayNetwork()
    for x in range(num_blocks):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        track.segment_id = segment_id_for_block(f"BLK_{x}")
        network.add_track(track)
        block = Block(block_id=f"BLK_{x}", length_cm=100.0)
        # Eastbound canonical order, as an editor-built block carries.
        block.ordered_tiles = [(x, 0, "W")]
        network.blocks[f"BLK_{x}"] = block
    detector = FakeDetector()
    sim = SimulationController(
        network, PathDispatcher(network), input_interface=detector
    )
    # The unidentified train tracker's hold windows run on wall-clock time (see
    # SimulationController._unidentified_clock), so `tick` drives them off a fake clock
    # rather than sleeping through UNIDENTIFIED_GAP_HOLD_S for real.
    sim.unidentified_clock = lambda: sim._fake_clock
    sim._fake_clock = 0.0
    return network, sim, detector


def build_sim_facing_blocks():
    """Two 3-tile blocks meeting at (2,0)-(3,0), each drawn AWAY from the join.

    BLK_W's canonical tile order runs west from the junction and BLK_E's runs
    east from it, so both list the junction tile FIRST. That is the shape that
    makes a naive concatenation double back on itself, and it is ordinary — a
    block's tile order just records the direction it happened to be drawn in.
    """
    network = RailwayNetwork()
    for x in range(6):
        network.add_track(Tile(x=x, y=0, track_bits=TRACK_EW))
    # Geometry through the production builder so each tile's segment — and the
    # block that owns it — is really registered. Hand-assigning `segment_id` and
    # dropping a Block into the registry leaves `Tile.block_id` None, and every
    # block-topology query over those tiles then comes back empty.
    make_segment_block_with_tracks(
        network, "BLK_W", [(0, 0), (1, 0), (2, 0)], length_cm=300.0
    )
    make_segment_block_with_tracks(
        network, "BLK_E", [(3, 0), (4, 0), (5, 0)], length_cm=300.0
    )
    # Point each block's canonical tile order outward from the shared junction.
    network.blocks["BLK_W"].ordered_tiles = [(2, 0, "E"), (1, 0, "E"), (0, 0, "E")]
    network.blocks["BLK_E"].ordered_tiles = [(3, 0, "W"), (4, 0, "W"), (5, 0, "W")]

    detector = FakeDetector()
    sim = SimulationController(
        network, PathDispatcher(network), input_interface=detector
    )
    sim.unidentified_clock = lambda: sim._fake_clock
    sim._fake_clock = 0.0
    return network, sim, detector


def tick(sim, seconds: float = 0.1, times: int = OCCUPANCY_ON_THRESHOLD):
    """Advance the runtime far enough for a detector change to be believed.

    The default is the ON debounce rather than a bare 1, because almost every
    test here sets a current and then asks what the runtime made of it — which
    takes `OCCUPANCY_ON_THRESHOLD` consecutive reads, whatever that is tuned to.
    Tests that care about a specific number of reads pass `times` explicitly.
    """
    for _ in range(times):
        sim._fake_clock += seconds
        sim.update(seconds)


def unidentified_ids(network):
    return sorted(t.train_id for t in network.trains.values() if t.is_unidentified)


def only_unidentified(network):
    ids = unidentified_ids(network)
    assert len(ids) == 1, f"expected exactly one unidentified train, got {ids}"
    return ids[0]


# ------------------------------------------------------------------ creation


def test_current_on_a_block_no_train_covers_creates_an_unidentified_train():
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}

    tick(sim)

    unidentified_id = only_unidentified(network)
    unidentified_train = network.trains[unidentified_id]
    assert unidentified_train.is_unidentified is True
    # Its own controller, and a default SpeedProfile it will never use.
    assert sim.controllers.get(unidentified_id) is not None
    assert unidentified_train.speed_profile_id is not None
    assert network.speed_profiles.get(unidentified_train.speed_profile_id) is not None
    # The entry commits through the ordinary occupancy-commit chain (inside Observe), not a side channel.
    assert network.blocks["BLK_1"].occupied_by == unidentified_id
    assert network.blocks["BLK_1"].state == BlockState.OCCUPIED


def test_an_unidentified_train_is_never_powered_and_never_driven_by_the_app():
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}
    tick(sim)
    unidentified_id = only_unidentified(network)

    controller = sim.controllers[unidentified_id]
    assert controller.physics_sync.current_speed_kmh == 0.0
    assert controller._framed_head_end == "end_a"
    # No route and no path: its Plan is a no-op.
    assert sim.route_executor.assigned_route_id_for(unidentified_id) is None
    assert sim.path_dispatcher.active_path_id_by_train(unidentified_id) is None


def test_unidentified_span_is_exactly_the_blocks_it_holds():
    network, sim, detector = build_sim()
    detector.current = {"BLK_0": True, "BLK_1": True}
    tick(sim)
    unidentified_id = only_unidentified(network)

    span = sim.controllers[unidentified_id].physics_sync.current_span
    assert sorted(span.occupied_blocks) == ["BLK_0", "BLK_1"]
    # Every tile of every held block — the most track the detector allows.
    assert sorted(span.occupied_tiles) == [(0, 0), (1, 0)]
    # End locators at the outer boundaries of the end blocks.
    assert span.end_a.distance_cm == 0.0
    assert span.end_b.distance_cm == pytest.approx(100.0)


def test_span_tiles_walk_end_to_end_across_blocks_drawn_facing_each_other():
    """Each block is turned to join the one before it, not emitted as drawn.

    Two blocks that meet in the layout can each list the junction tile first
    (they were drawn outward from it). Concatenated as drawn, the span jumps
    from one block's far end to the other's, and the first and last entries —
    which become `end_a` and `end_b` — land on interior tiles instead of the
    ends of the train.

    Which END is `end_a` is not asserted: an unidentified train's orientation is
    unknown, so the span may be walked either way round. What must hold is that
    the walk is unbroken and that its two ends are the two ends of the train.
    """
    _network, sim, _detector = build_sim_facing_blocks()
    tracker = sim.unidentified_trains

    # As drawn, in either block order: both blocks lead with the junction tile,
    # so the concatenation doubles back and starts on an interior tile.
    as_drawn = [
        (e[0], e[1])
        for b in ["BLK_W", "BLK_E"]
        for e in tracker._block_tile_entries(b)
    ]
    assert as_drawn == [(2, 0), (1, 0), (0, 0), (3, 0), (4, 0), (5, 0)]

    entries = tracker._oriented_tile_entries(tracker._chain_order({"BLK_W", "BLK_E"}))
    tiles = [(e[0], e[1]) for e in entries]

    # Same tiles either way — orientation decides their order, not membership.
    assert sorted(tiles) == [(x, 0) for x in range(6)]
    # One unbroken walk: every step moves to a grid neighbour.
    steps = {
        abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in zip(tiles, tiles[1:])
    }
    assert steps == {1}, f"span doubles back: {tiles}"
    # ...and it ends at the two extremities, not at the junction.
    assert {tiles[0], tiles[-1]} == {(0, 0), (5, 0)}


def test_a_two_block_unidentified_span_is_one_unbroken_walk():
    """The same rule, through a real tick rather than the assembly helper."""
    network, sim, detector = build_sim_facing_blocks()
    detector.current = {"BLK_W": True, "BLK_E": True}
    tick(sim)

    span = sim.controllers[only_unidentified(network)].physics_sync.current_span
    tiles = list(span.occupied_tiles)
    assert sorted(tiles) == [(x, 0) for x in range(6)]
    assert {abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in zip(tiles, tiles[1:])} == {1}
    assert {span.end_a.tile, span.end_b.tile} == {(0, 0), (5, 0)}


def test_orienting_a_single_block_leaves_it_as_drawn():
    """Nothing to join to, so the block keeps its own order and its edges."""
    _network, sim, _detector = build_sim_facing_blocks()
    tracker = sim.unidentified_trains
    assert tracker._oriented_tile_entries(["BLK_W"]) == [
        (2, 0, "E"),
        (1, 0, "E"),
        (0, 0, "E"),
    ]


def test_reversing_a_block_turns_its_entry_edges_around():
    """A flipped block is entered by the edge it used to be left by.

    The recorded edge is the entry travelling the block's canonical direction;
    walked the other way the tile is entered from its exit. On this E-W line
    that is the compass opposite, but the code derives it from track geometry
    rather than assuming so — a diagonal's exit is not its opposite edge.
    """
    network, sim, detector = build_sim_facing_blocks()
    tracker = sim.unidentified_trains

    forward = tracker._block_tile_entries("BLK_E")
    assert forward == [(3, 0, "W"), (4, 0, "W"), (5, 0, "W")]
    assert tracker._reversed_entries(forward) == [
        (5, 0, "E"),
        (4, 0, "E"),
        (3, 0, "E"),
    ]


# -------------------------------------------------------------------- movement


def test_a_rolling_unidentified_train_keeps_its_id_and_leaves_caution_behind():
    network, sim, detector = build_sim()
    detector.current = {"BLK_0": True}
    tick(sim)
    unidentified_id = only_unidentified(network)

    # Rolls forward: both blocks read current, then only the new one.
    detector.current = {"BLK_0": True, "BLK_1": True}
    tick(sim)
    detector.current = {"BLK_1": True}
    tick(sim, times=OCCUPANCY_OFF_THRESHOLD)

    assert only_unidentified(network) == unidentified_id
    assert network.blocks["BLK_1"].occupied_by == unidentified_id
    left_behind = network.blocks["BLK_0"]
    assert left_behind.occupied_by is None
    # It vacated while still holding a neighbour, so the block it left takes
    # normal directional CAUTION toward that neighbour.
    assert left_behind.caution is not None
    assert left_behind.caution.exit_edge == "E"


# ------------------------------------------------------------------- the gap


def test_an_unidentified_train_that_vanishes_frees_its_last_block_immediately():
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}
    tick(sim)
    unidentified_id = only_unidentified(network)

    detector.current = {}
    tick(sim, times=OCCUPANCY_OFF_THRESHOLD)

    block = network.blocks["BLK_1"]
    assert block.occupied_by is None
    # Last block clearing: direction is None, so no caution — straight to FREE.
    assert block.caution is None
    assert block.state == BlockState.FREE
    # The unidentified train itself is held, with an empty span, for UNIDENTIFIED_GAP_HOLD_S.
    assert unidentified_ids(network) == [unidentified_id]
    assert sim.controllers[unidentified_id].physics_sync.current_span is None


def test_an_unidentified_train_reappearing_through_undetected_track_keeps_its_id():
    network, sim, detector = build_sim()
    detector.current = {"BLK_0": True}
    tick(sim)
    unidentified_id = only_unidentified(network)

    # Crossing an undetected switch tile: it holds nothing for a moment.
    detector.current = {}
    tick(sim, times=OCCUPANCY_OFF_THRESHOLD)
    assert sim.controllers[unidentified_id].physics_sync.current_span is None

    # It reappears on a block reachable from its last held block: same unidentified train.
    detector.current = {"BLK_1": True}
    tick(sim)

    assert unidentified_ids(network) == [unidentified_id]
    assert network.blocks["BLK_1"].occupied_by == unidentified_id


def test_an_unidentified_train_holding_nothing_when_the_window_ends_is_retired():
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}
    tick(sim)
    unidentified_id = only_unidentified(network)
    profile_id = network.trains[unidentified_id].speed_profile_id

    detector.current = {}
    tick(sim, times=OCCUPANCY_OFF_THRESHOLD)
    assert unidentified_ids(network) == [unidentified_id]

    tick(sim, seconds=UNIDENTIFIED_GAP_HOLD_S, times=2)

    assert unidentified_ids(network) == []
    assert unidentified_id not in sim.controllers
    assert unidentified_id not in sim.physics_instances
    assert network.speed_profiles.get(profile_id) is None


def test_a_retired_unidentified_trains_train_id_is_never_reused():
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}
    tick(sim)
    first = only_unidentified(network)

    detector.current = {}
    tick(sim, times=OCCUPANCY_OFF_THRESHOLD)
    tick(sim, seconds=UNIDENTIFIED_GAP_HOLD_S, times=2)
    assert unidentified_ids(network) == []

    detector.current = {"BLK_1": True}
    tick(sim)

    assert only_unidentified(network) != first


# ------------------------------------------------------------------- identity


def test_two_unidentified_train_groups_that_grow_together_keep_the_older_id():
    network, sim, detector = build_sim(num_blocks=3)
    # Two separate unidentified trains, one at each end of the line.
    detector.current = {"BLK_0": True}
    tick(sim)
    older = only_unidentified(network)
    detector.current = {"BLK_0": True, "BLK_2": True}
    tick(sim)
    assert len(unidentified_ids(network)) == 2
    younger = [g for g in unidentified_ids(network) if g != older][0]

    # The gap between them fills: one connected group of unidentified-held blocks.
    detector.current = {"BLK_0": True, "BLK_1": True, "BLK_2": True}
    tick(sim)

    assert unidentified_ids(network) == [older]
    assert younger not in sim.controllers
    for block_id in ("BLK_0", "BLK_1", "BLK_2"):
        assert network.blocks[block_id].occupied_by == older
    assert sorted(
        sim.controllers[older].physics_sync.current_span.occupied_blocks
    ) == ["BLK_0", "BLK_1", "BLK_2"]


def test_an_unidentified_train_group_that_splits_keeps_its_id_on_the_longest_held_part():
    network, sim, detector = build_sim(num_blocks=3)
    detector.current = {"BLK_0": True}
    tick(sim)
    original = only_unidentified(network)

    detector.current = {"BLK_0": True, "BLK_1": True, "BLK_2": True}
    tick(sim)
    assert unidentified_ids(network) == [original]

    # The middle block clears: two disconnected groups, one unidentified train id.
    detector.current = {"BLK_0": True, "BLK_2": True}
    tick(sim, times=OCCUPANCY_OFF_THRESHOLD)

    remaining = unidentified_ids(network)
    assert len(remaining) == 2
    # The part holding the block this unidentified train has held longest keeps the id.
    assert network.blocks["BLK_0"].occupied_by == original
    assert network.blocks["BLK_2"].occupied_by != original
    assert network.blocks["BLK_2"].occupied_by in remaining


# ------------------------------------------------------------ never energized


def test_a_block_held_only_by_an_unidentified_train_is_driven_to_zero():
    network, sim, detector = build_sim()
    # The feed is live before the unidentified train appears (a previous holder left it on).
    sim.power_controller.set_block_voltage("BLK_1", 12.0)
    detector.current = {"BLK_1": True}

    tick(sim)

    unidentified_id = only_unidentified(network)
    assert network.blocks["BLK_1"].occupied_by == unidentified_id
    # An unidentified train commands no track_voltage, and its span is excluded from the
    # energized set and from the de-energize test.
    assert sim.power_controller.get_block_voltage("BLK_1") == 0.0
    physics = sim.physics_instances[unidentified_id]
    assert physics._energized_blocks_for_train(network.trains[unidentified_id]) == set()


def test_the_all_blocks_deenergize_sweep_also_excludes_an_unidentified_train():
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}
    tick(sim)

    sim.power_controller.set_block_voltage("BLK_1", 12.0)
    sim.power_controller.deenergize_unheld_blocks()

    assert sim.power_controller.get_block_voltage("BLK_1") == 0.0


# ------------------------------------------------------- never driven / saved


def test_every_operator_command_is_refused_for_an_unidentified_train():
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}
    tick(sim)
    unidentified_id = only_unidentified(network)

    state = SimpleNamespace(
        network=network,
        sim_controller=sim,
        speed_profile_registry=network.speed_profiles,
        simulation_lock=threading.RLock(),
    )

    for call in (
        lambda: train_service.power_on(state, unidentified_id),
        lambda: train_service.power_off(state, unidentified_id),
        lambda: train_service.ato_go(state, unidentified_id),
        lambda: train_service.ato_stop(state, unidentified_id),
        lambda: train_service.set_handle(state, unidentified_id, position="P1"),
        lambda: train_service.release_emergency(state, unidentified_id),
        lambda: train_service.set_mode(state, unidentified_id, mode="manual"),
        lambda: train_service.set_manual_mode_type(
            state, unidentified_id, mode_type="power_percent"
        ),
        lambda: train_service.set_manual_power_percent(state, unidentified_id, percent=50),
    ):
        result = call()
        assert result.success is False
        assert "unidentified train" in result.message.lower()

    # The controller never saw any of it.
    assert sim.controllers[unidentified_id]._framed_head_end == "end_a"
    assert sim.controllers[unidentified_id].train_status.name == "OFF"


def test_an_unidentified_train_is_never_assigned_a_route_or_repositioned():
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}
    tick(sim)
    unidentified_id = only_unidentified(network)

    state = SimpleNamespace(
        network=network,
        sim_controller=sim,
        speed_profile_registry=network.speed_profiles,
        simulation_lock=threading.RLock(),
    )

    with pytest.raises(ValidationError):
        train_service.assign_route(state, unidentified_id, route_id="ROUTE_1")
    with pytest.raises(ValidationError):
        train_service.reposition_train(
            state, unidentified_id, position={"x": 2, "y": 0}, direction="EAST"
        )


def test_the_dispatcher_never_creates_a_path_for_an_unidentified_train():
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}
    tick(sim)
    unidentified_id = only_unidentified(network)

    result = sim.path_dispatcher.create_path(
        train_id=unidentified_id, requested_destination_block_id="BLK_2"
    )

    assert result.success is False
    assert result.failure_kind is PathAcquisitionFailureKind.INVALID_REQUEST
    assert sim.path_dispatcher.active_path_id_by_train(unidentified_id) is None


def test_an_unidentified_train_is_not_written_to_saved_state():
    """Runtime only: a restart re-detects whatever is still drawing current."""
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}
    tick(sim)
    unidentified_id = only_unidentified(network)

    saved_ids = {t["id"] for t in network.to_layout_dict()["trains"]}

    assert unidentified_id not in saved_ids


def test_an_unidentified_train_is_in_the_live_payload_so_it_shows_as_itself():
    """Shown as itself: an unidentified train appears in the train list and on the layout,
    which is also how the operator reaches the panel that adopts it. The save
    payload is the one that leaves unidentified trains out, not the live one."""
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}
    tick(sim)
    unidentified_id = only_unidentified(network)

    live = {t["id"]: t for t in network.to_dict()["trains"]}

    assert unidentified_id in live
    assert live[unidentified_id]["is_unidentified"] is True


def test_a_real_train_entering_a_unidentified_held_block_is_the_ordinary_conflict():
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}
    tick(sim)
    unidentified_id = only_unidentified(network)

    # No special case: physical entry onto a block another train holds is the
    # ordinary fatal `occupied_by_other_train` conflict.
    assert bool(sim.block_manager.occupy_blocks(["BLK_1"], "T_REAL")) is False
    assert sim.failed_runtime is True
    assert network.blocks["BLK_1"].occupied_by == unidentified_id


def test_reachable_blocks_cross_a_tunnel_whose_edges_are_not_opposite():
    """A W↔N tunnel enters its far tile through N, not through the opposite of
    W, so the walk must take the far edge from the tunnel. Attribution's
    adjacency reads this walk (design_new.md "Block occupancy detection",
    Touching)."""
    from tests.journey.scenes.tunnel_wn_scene import TunnelWNScene

    scene = TunnelWNScene()
    sim = scene.build().sim_controller
    tracker = sim.unidentified_trains
    assert tracker.reachable_blocks(scene.west_block_id) == {scene.north_block_id}
    assert tracker.reachable_blocks(scene.north_block_id) == {scene.west_block_id}
