"""Regression test: a movement-derived deviation is LATCHED, not committed inline.

Contract (design_new.md lines 277-279 + the EB workflow / Latch Table):

When a train's head leaves its authorized ``resource_sequence`` while ROLLING
(a "deviation"):

1. The live path stays ``ACTIVE`` and KEEPS its block reservations + switch
   locks. Resources are NOT released while the train is still rolling, because
   releasing future authority ahead of a moving train is unsafe.
2. The cancel is LATCHED:
   ``controller.route_hooks.phase_latches.path_cancel_request`` is written with
   ``deviation=True`` (carrying its ``ErrorRecord``) by
   ``PathDispatcher.on_controller_progress``. It is NOT committed inline.
3. The canonical lookahead reports ``atc_limit == 0`` at distance 0 with
   ``atc_limit_reason_code == AtcLimitReasonCode.DEVIATION`` (NOT
   ``NO_AUTHORITY``). ATC brakes; an overspeed escalates to EB.
4. Only once the train is stopped does the gated Phase-3 explicit-cancel step
   commit ``cancel_path`` (reservations + switch locks released, latch cleared,
   path ``CANCELLED``).

Why this discriminates latched-vs-inline behaviour
--------------------------------------------------
The *same* head-off-path geometry reports ``NO_AUTHORITY`` when no deviation
latch is pending, and ``DEVIATION`` only when the deviation latch is set (the
lookahead branch in ``train_controller/__init__.py`` keys on a pending
``deviation=True`` latch). Under the OLD inline-cancel behaviour,
``on_controller_progress`` would have called ``cancel_path(...)`` immediately
on the deviating tick while the train was still rolling, so:

  * ``active_path_id_by_train(train_id)`` would already be ``None`` (path
    cancelled), not the path id this test asserts is still ACTIVE;
  * the lookahead would report ``no_authority`` (no live plan), not
    ``deviation``;
  * the block reservations and the switch lock would already be released while
    the train was still rolling.

This test asserts the opposite of all three on the deviating (rolling) tick, so
it fails closed against any regression to inline cancel. ``test_no_latch_head_
off_path_reports_no_authority`` pins the contrast directly: identical geometry,
no deviation latch -> ``NO_AUTHORITY``.

The test drives the real tick pipeline -- ``TrainController.on_physics_updated``
(deviation detection), ``PathDispatcher.on_controller_progress`` (latch write),
``TrainController.get_lookahead`` / ``_distance_zero_atc_anchor`` (DEVIATION
emission), and ``SimulationController._consume_pending_path_cancel_request``
(stopped-gated Phase-3 commit) -- with a deterministic, fixed off-sequence span
update (no wall-clock, no randomness). A ``no-boundary`` span update whose
``new_span`` places the head on a tile the path does not enumerate is exactly
what physics produces when a train rolls onto track the route never authorized
(e.g. a switch diverts it), so this is faithful to the live path.
"""

from typing import Tuple

import pytest

from enums import AtcLimitReasonCode, Direction, PathStatus, TrainState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from runtime_payloads import PhysicsSpanUpdate
from simulation import SimulationController
from switch import Switch
from track import TRACK_EW, TRACK_NW, Tile
from train import Train, TrainCar

from tests.train_test_helpers import make_train_span
from tests.segment_test_helpers import make_segment_block_with_tracks


# Fixed event time: the contract is independent of wall-clock, so pin it so the
# test is fully deterministic.
FIXED_EVENT_TIME = 1000.0

# Off-sequence tile the deviated head lands on. (5, 0) lives in BLK_D, which is
# not part of the A -> SW_X -> C authority the train was granted, so the head's
# tile is absent from the path's resource_sequence -> deviation
# "out_of_sequence".
OFF_SEQUENCE_TILE: Tuple[int, int] = (5, 0)


def _build_switch_network() -> Tuple[RailwayNetwork, Switch]:
    """A straight EW line with a real Switch entity in the middle:

        [BLK_A: (0,0)(1,0)]--[SW_X @ (2,0)]--[BLK_C: (3,0)(4,0)]   [BLK_D: (5,0)(6,0)]

    A path from BLK_A to BLK_C traverses SW_X, so create_path both reserves
    BLK_C and locks SW_X -- giving us a block reservation AND a switch lock to
    assert are held during the deviation and released after the Phase-3 commit.
    BLK_D is reachable track that the A->C authority never enumerates; the
    deviated head is placed there.
    """
    network = RailwayNetwork()

    network.add_track(Tile(0, 0, track_bits=TRACK_EW))
    network.add_track(Tile(1, 0, track_bits=TRACK_EW))
    make_segment_block_with_tracks(network, "BLK_A", positions=[(0, 0), (1, 0)], name="A", length_cm=200.0
    )

    # Switch tile at (2, 0): EW (straight, main) + NW (diverge). Must be a real
    # Switch entity so create_path can lock it.
    switch_track = Tile(2, 0, track_bits=TRACK_EW | TRACK_NW)
    network.add_track(switch_track)
    switch = Switch(switch_id="SW_X", x=2, y=0)
    network.add_switch(switch)
    switch_track.switch_id = switch.switch_id

    for x in (3, 4, 5, 6):
        network.add_track(Tile(x, 0, track_bits=TRACK_EW))
    make_segment_block_with_tracks(network, "BLK_C", positions=[(3, 0), (4, 0)], name="C", length_cm=200.0
    )
    make_segment_block_with_tracks(network, "BLK_D", positions=[(5, 0), (6, 0)], name="D", length_cm=200.0
    )

    return network, switch


def _seed_rolling_train_on_live_path(
    network: RailwayNetwork,
) -> Tuple[SimulationController, PathDispatcher, object, object, str]:
    """Place a train at (1, 0) in BLK_A facing EAST, give it an ACTIVE path to
    BLK_C, and bring it under manual control (cab A, RUNNING_MOVING) with real
    forward authority. Returns (sim, dispatcher, controller, train, path_id)."""
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T1", consist=[car], max_speed=60.0)
    network.add_train(train)
    span = network.place_train(train, 1, 0, direction=Direction.EAST)

    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    controller = sim._get_or_create_controller(train)
    # v1: the live position lives on the controller span; seed it so the
    # dispatcher can resolve the train's current block.
    controller.physics_sync.current_span = span

    result = dispatcher.create_path(
        train_id=train.train_id, requested_destination_block_id="BLK_C"
    )
    assert result.success, f"create_path failed: {getattr(result, 'reason', None)}"

    # Bring the train under control with real forward authority: manual mode,
    # cab A, moving EAST. Manual trains have no depart handshake; put the train
    # in the moving state directly.
    controller.ops.state = TrainState.READY
    controller.commands.control_mode = "manual"
    controller._apply_head_frame("end_a")
    controller.ops.enter_running_moving()
    assert controller.ops.state == TrainState.RUNNING_MOVING

    return sim, dispatcher, controller, train, result.path_id


def _apply_span_update(controller, head_tile, speed_kmh) -> None:
    """Drive the real on_physics_updated path with a deterministic no-boundary
    span update that relocates the head to ``head_tile`` at ``speed_kmh``.

    ``added_tiles``/``removed_tiles`` are empty (the no-boundary case physics
    emits once per tick); the deviation computation keys off the new head tile
    vs. the live plan, which is exactly what this exercises.
    """
    occupied_block = "BLK_D" if head_tile == OFF_SEQUENCE_TILE else None
    new_span = make_train_span(
        head_tile,
        facing_direction=Direction.EAST,
        occupied_blocks=[occupied_block] if occupied_block else [],
    )
    controller.on_physics_updated(
        event_time=FIXED_EVENT_TIME,
        span_update=PhysicsSpanUpdate(
            new_span=new_span,
            current_speed_kmh=speed_kmh,
            added_tiles=[],
            removed_tiles=[],
        ),
    )


def _drain_progress_reports(dispatcher, controller) -> None:
    """Phase 3a: drain queued progress reports through on_controller_progress
    (the canonical site that writes the deviation cancel latch)."""
    while controller.physics_sync.queued_progress_reports:
        report = controller.physics_sync.queued_progress_reports.pop(0)
        dispatcher.on_controller_progress(report)


def _anchor_reason_code(controller) -> AtcLimitReasonCode:
    """The published distance-0 ATC reason code from the canonical lookahead."""
    _limit, code = controller._distance_zero_atc_anchor()
    return code


def test_deviation_while_rolling_latches_and_holds_resources():
    """The whole contract in one flow:

    rolling deviation -> latch set (deviation=True), path stays ACTIVE,
    block reservation + switch lock STILL held, lookahead reports DEVIATION at
    atc_limit 0; Phase-3 does NOT commit while rolling; once stopped, Phase-3
    commits cancel_path (resources released, latch cleared, path CANCELLED).
    """
    network, switch = _build_switch_network()
    sim, dispatcher, controller, train, path_id = _seed_rolling_train_on_live_path(
        network
    )

    # --- Baseline: healthy authority before the deviation. ---
    assert dispatcher.active_path_id_by_train("T1") == path_id
    assert controller.route_hooks.phase_latches.path_cancel_request is None
    # The switch is locked and BLK_C is reserved to this path/train at create.
    assert switch.is_locked(), "switch should be locked by the active path"
    assert network.blocks["BLK_C"].reserved_for_train == "T1"
    assert network.blocks["BLK_C"].reserved_by_path == path_id
    # Distance-0 anchor is NOT a stop reason yet (real forward authority).
    pre_limit, pre_code = controller._distance_zero_atc_anchor()
    assert pre_code != AtcLimitReasonCode.DEVIATION
    assert pre_code != AtcLimitReasonCode.NO_AUTHORITY
    assert pre_limit > 0.0

    # --- Deviate: head rolls onto an off-sequence tile (BLK_D) at 20 km/h. ---
    _apply_span_update(controller, OFF_SEQUENCE_TILE, speed_kmh=20.0)
    _drain_progress_reports(dispatcher, controller)

    # (2) The cancel is LATCHED with deviation=True + its ErrorRecord.
    latch = controller.route_hooks.phase_latches.path_cancel_request
    assert latch is not None, "deviation must LATCH a cancel request"
    assert latch.deviation is True, (
        "expected the movement-derived deviation classification on the latch"
    )
    assert latch.deviation is True
    assert latch.error is not None
    assert latch.error.origin == "out_of_sequence"

    # The train is genuinely still rolling at this point.
    assert not controller.is_stopped()

    # (1) The live path is STILL ACTIVE and KEEPS its resources while rolling.
    assert dispatcher.active_path_id_by_train("T1") == path_id, (
        "path must stay ACTIVE while the deviated train is rolling "
        "(inline cancel would have set this to None)"
    )
    assert switch.is_locked(), "switch lock must be HELD while rolling"
    assert (path_id, None) in switch.locks
    assert switch.locks[(path_id, None)] == ("T1", 0)
    assert network.blocks["BLK_C"].reserved_for_train == "T1", (
        "block reservation must be HELD while rolling"
    )
    assert network.blocks["BLK_C"].reserved_by_path == path_id

    # (3) Canonical lookahead reports DEVIATION (NOT NO_AUTHORITY) at limit 0.
    dev_limit, dev_code = controller._distance_zero_atc_anchor()
    assert dev_code == AtcLimitReasonCode.DEVIATION, (
        f"expected DEVIATION reason, got {dev_code!r} "
        "(NO_AUTHORITY here would mean the deviation latch did not fire / the "
        "path was cancelled inline)"
    )
    assert dev_limit == 0.0
    # Same code surfaces on the full lookahead payload's distance-0 curve.
    payload = controller.get_lookahead()
    curves = (payload.get("lookahead") or {}).get("speed_curves") or []
    assert curves, "lookahead should carry a distance-0 anchor curve"
    assert curves[0]["atc_limit_reason_code"] == AtcLimitReasonCode.DEVIATION.value
    assert curves[0]["atc_limit"] == 0.0

    # (4a) Phase-3 must NOT commit while the train is still rolling: the latch
    # survives and the path stays ACTIVE.
    sim._consume_pending_path_cancel_request(controller, train)
    assert controller.route_hooks.phase_latches.path_cancel_request is not None, (
        "Phase-3 must preserve the latch while the train is rolling"
    )
    assert dispatcher.active_path_id_by_train("T1") == path_id
    assert switch.is_locked()
    assert network.blocks["BLK_C"].reserved_for_train == "T1"

    # --- Drive the train to a full stop (head still off-sequence). ---
    _apply_span_update(controller, OFF_SEQUENCE_TILE, speed_kmh=0.0)
    _drain_progress_reports(dispatcher, controller)
    assert controller.is_stopped()

    # (4b) Now the gated Phase-3 explicit-cancel step commits the latch.
    sim._consume_pending_path_cancel_request(controller, train)

    # Path is no longer active for the train; the retained row is CANCELLED.
    assert dispatcher.active_path_id_by_train("T1") is None
    assert dispatcher.get_path(path_id).status == PathStatus.CANCELLED
    # The latch is cleared on the committing pass.
    assert controller.route_hooks.phase_latches.path_cancel_request is None
    # Previously-held resources are released.
    assert not switch.is_locked(), "switch lock must be released after commit"
    assert network.blocks["BLK_C"].reserved_for_train is None
    assert network.blocks["BLK_C"].reserved_by_path is None
    # The controller's live authority projection drops to None.
    assert controller.path_projection.received_path_plan is None


def test_head_on_path_resource_but_unenumerated_tile_is_not_a_deviation():
    """The predicate is the head's RESOURCE, not its tile
    (design_new.md "Controller-mediated progress").

    A path's stored ``path_tiles`` for a block hold only the tiles the train
    will traverse, not every tile of the block, so a head can sit on its own
    authorized block and still miss the tile map. Tile-matching alone called
    that a deviation.

    This is the reverse departure: when the path leaves out the train's other
    end, ``create_path`` seeds the sequence at the exit tile of the block the
    train is standing on — a tile the train is not on. The path was cancelled
    on the first tick after it was assigned, before the operator could press
    GO, leaving no authority, an ATC limit of 0, and a dead rail under a train
    the app was showing as ready to run.
    """
    network, _switch = _build_switch_network()
    _sim, dispatcher, controller, _train, path_id = _seed_rolling_train_on_live_path(
        network
    )

    plan = controller.path_projection.received_path_plan
    start = plan.resource_sequence[0]
    assert start.resource_id == "BLK_A"
    enumerated = {(t.x, t.y) for t in start.path_tiles}
    unenumerated = (0, 0)
    assert unenumerated not in enumerated, (
        "fixture no longer exercises the case: the path enumerates every tile "
        "of its own start block"
    )

    # Put the head on that unenumerated tile of BLK_A — still the path's own
    # sequence-0 resource.
    new_span = make_train_span(
        unenumerated,
        facing_direction=Direction.EAST,
        occupied_blocks=["BLK_A"],
    )
    controller.on_physics_updated(
        event_time=FIXED_EVENT_TIME,
        span_update=PhysicsSpanUpdate(
            new_span=new_span,
            current_speed_kmh=20.0,
            added_tiles=[],
            removed_tiles=[],
        ),
    )

    reports = list(controller.physics_sync.queued_progress_reports)
    assert reports, "no progress report was enqueued"
    assert not any(r.deviation for r in reports), (
        "head on its own authorized block was reported as a deviation"
    )
    assert reports[-1].current_head_sequence_index == start.sequence_index

    _drain_progress_reports(dispatcher, controller)
    assert controller.route_hooks.phase_latches.path_cancel_request is None
    assert dispatcher.active_path_id_by_train("T1") == path_id
    assert dispatcher.get_path(path_id).status == PathStatus.ACTIVE


def test_no_latch_head_off_path_reports_no_authority():
    """Discrimination guard: the SAME head-off-path geometry, but with NO
    deviation latch pending, reports NO_AUTHORITY -- not DEVIATION.

    This is the exact state the old inline-cancel behaviour would leave behind
    on the deviating tick (path already cancelled, no plan, no deviation
    latch). It proves the DEVIATION reason in the test above is produced by the
    pending deviation latch, not by head-off-path geometry alone.
    """
    network, _switch = _build_switch_network()
    _sim, _dispatcher, controller, _train, _path_id = (
        _seed_rolling_train_on_live_path(network)
    )

    # Relocate the head off-sequence and keep it rolling, WITHOUT writing the
    # deviation latch (no on_controller_progress drain).
    new_span = make_train_span(
        OFF_SEQUENCE_TILE,
        facing_direction=Direction.EAST,
        occupied_blocks=["BLK_D"],
    )
    controller.physics_sync.current_span = new_span
    controller.physics_sync.current_speed_kmh = 20.0

    assert controller.route_hooks.phase_latches.path_cancel_request is None
    limit, code = controller._distance_zero_atc_anchor()
    assert code == AtcLimitReasonCode.NO_AUTHORITY, (
        f"head-off-path with no deviation latch must report NO_AUTHORITY, "
        f"got {code!r}"
    )
    assert limit == 0.0
