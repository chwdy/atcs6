"""Span-aware reachability for the Quick Route tool.

`RailwayPathfinder.get_reachable_endpoints_for_span(...)` must answer where a
PLACED train can actually go — forward out of its head or in reverse out of its
tail — never routing back through its own body or through a switch its body is
sitting on. This contrasts with the topological `get_reachable_endpoints(...)`,
which fans out from every edge of a block ignoring the train entirely.

Layout (N = +y), one horizontal depot ladder:

    SIDE1(1,2)   SIDE2(2,2)        <- sidings, reachable only by diverging N
       |            |
  LEAD-S1(1,1)-----S2(2,1)----MAIN(3,1)
  (0,1)  switch     switch

S1/S2 track_bits = E-W | N-W  -> entering from W exits {E (straight), N (diverge)}.
A train whose body occupies LEAD(0,1), S1(1,1), S2(2,1) with its head at S2
facing E can only reach MAIN; SIDE1/SIDE2 require throwing a switch the body
is sitting on.
"""

from network import RailwayNetwork
from railway_pathfinder import RailwayPathfinder
from track import TrackSegment

EW, NW = 2, 8
NS, SE = 1, 16


def _depot_ladder() -> RailwayNetwork:
    net = RailwayNetwork()
    net.add_track(TrackSegment(x=0, y=1, track_bits=EW))        # LEAD
    net.add_track(TrackSegment(x=1, y=1, track_bits=EW | NW))   # S1 switch
    net.add_track(TrackSegment(x=2, y=1, track_bits=EW | NW))   # S2 switch
    net.add_track(TrackSegment(x=3, y=1, track_bits=EW))        # MAIN
    net.add_track(TrackSegment(x=1, y=2, track_bits=1))         # SIDE1 (N-S stub)
    net.add_track(TrackSegment(x=2, y=2, track_bits=1))         # SIDE2 (N-S stub)
    net.create_block_with_tracks(block_id="LEAD", positions=[(0, 1)])
    net.create_block_with_tracks(block_id="MAIN", positions=[(3, 1)])
    net.create_block_with_tracks(block_id="SIDE1", positions=[(1, 2)])
    net.create_block_with_tracks(block_id="SIDE2", positions=[(2, 2)])
    return net


def test_topological_reachable_offers_impossible_sidings():
    """Baseline: the block-keyed topological query DOES list the sidings,
    because it walks through the switch tiles ignoring the train. This is the
    behavior the Quick Route tool must NOT use for a placed train."""
    pf = RailwayPathfinder(_depot_ladder())
    got = set(pf.get_reachable_endpoints("LEAD", include_stations=False))
    assert got == {"MAIN", "SIDE1", "SIDE2"}


def test_span_aware_reachable_excludes_body_spanned_switch_sidings():
    """Span-aware query from the head (S2 facing E) + tail (LEAD facing W),
    body excluded, reaches only MAIN."""
    pf = RailwayPathfinder(_depot_ladder())
    head_seeds = [
        ((2, 1), "E"),  # head at S2, faces east (out the depot toward MAIN)
        ((0, 1), "W"),  # tail at LEAD, faces west (dead end)
    ]
    occupied_tiles = [(0, 1), (1, 1), (2, 1)]
    occupied_blocks = ["LEAD"]
    got = set(
        pf.get_reachable_endpoints_for_span(
            head_seeds=head_seeds,
            occupied_tiles=occupied_tiles,
            occupied_blocks=occupied_blocks,
            include_stations=False,
        )
    )
    assert got == {"MAIN"}, f"expected only MAIN, got {sorted(got)}"


def test_span_aware_reverse_end_reaches_backward():
    """A train sitting wholly on MAIN with its head facing W (back down the
    ladder) reaches the straight-through blocks but still never the sidings,
    since reaching a siding needs a diverging switch throw the topological-
    only walk would allow but the train cannot drive into from this approach."""
    pf = RailwayPathfinder(_depot_ladder())
    # Head at MAIN(3,1) facing W; single-tile body, tail faces E (dead end).
    head_seeds = [((3, 1), "W"), ((3, 1), "E")]
    got = set(
        pf.get_reachable_endpoints_for_span(
            head_seeds=head_seeds,
            occupied_tiles=[(3, 1)],
            occupied_blocks=["MAIN"],
            include_stations=False,
        )
    )
    # Driving W from MAIN enters S2 from E. S2's segments are E-W and N-W;
    # entering from E only pairs to W (straight), so it continues to S1 and
    # LEAD. The N (diverge) arm pairs with W, not E, so the sidings stay
    # unreachable from this approach.
    assert got == {"LEAD"}, f"expected only LEAD, got {sorted(got)}"


def test_dispatcher_reachable_for_train_end_to_end():
    """Full path: a placed train whose body spans LEAD + both switches, head
    at S2 facing east. The dispatcher resolves the train's span and its
    end-outward edges through the controller, so the reachable set is exactly
    {MAIN} — the impossible sidings are never offered.

    This exercises `TrainController.get_span_end_exits()` (the bend-safe
    end-orientation derivation) feeding
    `PathDispatcher.get_reachable_endpoints_for_train(...)`.
    """
    from path_dispatcher import PathDispatcher
    from simulation import SimulationController
    from train import Train, TrainCar
    from train_span import SpanEndLocator, TrainSpan

    network = _depot_ladder()
    car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="DEPOT_TRAIN", consist=[car], max_speed=80.0)
    network.add_train(train)

    sim = SimulationController(network, PathDispatcher(network))
    controller = sim._get_or_create_controller(train)
    # Body spans LEAD(0,1) -> S1(1,1) -> S2(2,1); end_b (head) at S2 faces E.
    controller.physics_sync.current_span = TrainSpan(
        end_a=SpanEndLocator(tile=(0, 1), distance_cm=0.0, entry_edge="E"),
        end_b=SpanEndLocator(tile=(2, 1), distance_cm=20.0, entry_edge="W"),
        occupied_tiles=[(0, 1), (1, 1), (2, 1)],
        occupied_blocks=["LEAD"],
    )

    # Each physical end resolves to its outward edge (bend-safe, via the
    # adjacent body tile): LEAD end faces W (dead end), S2 end faces E.
    seeds = {tile: edge for tile, edge in controller.get_span_end_exits()}
    assert seeds == {(0, 1): "W", (2, 1): "E"}, f"unexpected seeds: {seeds}"

    reachable = set(
        sim.path_dispatcher.get_reachable_endpoints_for_train(
            "DEPOT_TRAIN", include_stations=False
        )
    )
    assert reachable == {"MAIN"}, f"expected only MAIN, got {sorted(reachable)}"


def _curved_platform() -> RailwayNetwork:
    """A bent (L-shaped) platform whose two physical END tiles are CURVES.

        NORTH(2,2)             top exit (N-S straight)
          |
        (2,1) NW-curve --- (1,1) SE-curve
                                  |
                           (1,0) NW-curve --- WEST(0,0)   west exit (E-W straight)

    The body occupies the three curve tiles. The east-end tile (2,1) heads
    'straight' east off the rail (it is an N-W curve with no E edge); the rail
    actually bends north to NORTH. The south-end tile (1,0) heads 'straight'
    south off the rail (an N-W curve with no S edge); the rail bends west to
    WEST. This is the demo_5 platform shape that broke the Quick Route tool.
    """
    net = RailwayNetwork()
    net.add_track(TrackSegment(x=2, y=1, track_bits=NW))   # east end, edges {N,W}
    net.add_track(TrackSegment(x=1, y=1, track_bits=SE))   # mid,      edges {S,E}
    net.add_track(TrackSegment(x=1, y=0, track_bits=NW))   # south end, edges {N,W}
    net.add_track(TrackSegment(x=2, y=2, track_bits=NS))   # NORTH exit
    net.add_track(TrackSegment(x=0, y=0, track_bits=EW))   # WEST exit
    net.create_block_with_tracks(block_id="PLATFORM", positions=[(2, 1), (1, 1), (1, 0)])
    net.create_block_with_tracks(block_id="NORTH", positions=[(2, 2)])
    net.create_block_with_tracks(block_id="WEST", positions=[(0, 0)])
    return net


def test_span_end_exits_follow_curved_end_tile():
    """Regression: a train on a CURVED platform must seed the Quick Route walk
    with the edge the rail bends to, not the straight-line body heading.

    The straight-line continuation of the body points east out of (2,1) and
    south out of (1,0) — both off the rail (those end tiles are curves with no
    E/S edge), so a heading-derived seed dead-ends and reaches nothing. Reading
    the outward edge from the end tile's real track edges seeds N out of (2,1)
    and W out of (1,0), so the train reaches NORTH and WEST. This is the demo_5
    'train on a platform shows no viable path' bug.
    """
    from path_dispatcher import PathDispatcher
    from simulation import SimulationController
    from train import Train, TrainCar
    from train_span import SpanEndLocator, TrainSpan

    network = _curved_platform()
    car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="CURVE_TRAIN", consist=[car], max_speed=80.0)
    network.add_train(train)

    sim = SimulationController(network, PathDispatcher(network))
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = TrainSpan(
        end_a=SpanEndLocator(tile=(2, 1), distance_cm=20.0, entry_edge="W"),
        end_b=SpanEndLocator(tile=(1, 0), distance_cm=10.0, entry_edge="N"),
        occupied_tiles=[(2, 1), (1, 1), (1, 0)],
        occupied_blocks=["PLATFORM"],
    )

    # Each end follows its curve outward, never the off-rail straight heading.
    seeds = {tile: edge for tile, edge in controller.get_span_end_exits()}
    assert seeds == {(2, 1): "N", (1, 0): "W"}, f"unexpected seeds: {seeds}"

    reachable = set(
        sim.path_dispatcher.get_reachable_endpoints_for_train(
            "CURVE_TRAIN", include_stations=False
        )
    )
    assert reachable == {"NORTH", "WEST"}, f"expected NORTH+WEST, got {sorted(reachable)}"
