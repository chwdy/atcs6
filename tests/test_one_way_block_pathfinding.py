"""One-way blocks are a pathfinding filter, not a wait-for-clear blocker.

Per design_new.md "`create_path()` flow" and "Pathfinding-home migration"
(the `query_graph_walks` contract), candidate search rejects any candidate
**whose traversal violates** a block's `Block.allowed_travel_directions`
when `manual_override == False` — every traversed block, not just the
destination. A one-way restriction is static:
a walk that crosses it against its direction can never be acquired, so per
the `no_viable_path` definition (design_new.md "`create_path()` /
`extend_path()` return contract") it must surface as a hard
pathfinding failure (or be rerouted around), never as a best-effort
truncation that parks authority just before the one-way block and retries
forever.

Regression pinned here (reported on demo_6): with BLK_ML_16 set to
forward-only, T_YA_6's mainline walk (which crosses BLK_ML_16 in reverse)
used to survive the search, fail reservation at BLK_ML_16
(`direction_not_allowed`), and get best-effort-truncated to an authority
ending at PLT_ML_9_A_BLOCK — the block immediately before the one-way block.
The train then drove there and held forever while every extend returned
a no-growth hold ("direction_not_allowed (blocked at BLK_ML_16)"), a
transient classification for a condition that can never clear.
"""

import contextlib
import io

import pytest

from runtime_payloads import (
    PathAcquisitionFailureKind,
)
from bootstrap import bootstrap_runtime

pytestmark = pytest.mark.scene("demo_6")

ONE_WAY = "BLK_ML_16"
TRAIN = "T_YA_6"
# Reachable both via the short walk through BLK_ML_16 (in reverse) and via
# the mainline loop's other side (direction-legal once ML_16 is forward-only).
TARGET_WITH_ALTERNATIVE = "PLT_ML_1_A_BLOCK"
# Only reachable by crossing BLK_ML_16 in reverse — no legal alternative.
TARGET_WITHOUT_ALTERNATIVE = "BLK_FILL_20"


def _boot():
    with contextlib.redirect_stdout(io.StringIO()):
        network, dispatcher, sim, _ = bootstrap_runtime(
            demo=True, setup_train=True, scene="demo_6"
        )
    return network, dispatcher, sim


def _sequence_ids(result):
    return [getattr(e, "resource_id", e) for e in (result.resource_sequence or [])]


def test_search_rejects_walks_crossing_one_way_block_in_reverse():
    """No surviving candidate may traverse the restricted block against its
    allowed direction; the filter is search-level, not acquisition-level."""
    network, dispatcher, _sim = _boot()
    network.blocks[ONE_WAY].allowed_travel_directions = {"forward"}

    start = dispatcher._get_current_block_id_for_train(network.trains[TRAIN])
    candidates = dispatcher._pathfinder.find_ordered_walk(
        start, TARGET_WITH_ALTERNATIVE
    )
    assert candidates, "a direction-legal walk around the loop must survive"
    for cand in candidates:
        reverse_steps = [
            s for s in (cand.ordered_walk or [])
            if s.block_id == ONE_WAY and s.block_direction == "reverse"
        ]
        assert not reverse_steps, (
            f"candidate {cand.ordered_blocks} traverses {ONE_WAY} in reverse "
            "— the allowed_travel_directions filter must reject it at search "
            "time (design_new.md \"`create_path()` flow\")"
        )


def test_create_path_reroutes_around_one_way_block():
    """With a direction-legal alternative available, create_path must use it
    instead of truncating in front of the one-way block."""
    network, dispatcher, _sim = _boot()
    network.blocks[ONE_WAY].allowed_travel_directions = {"forward"}

    result = dispatcher.create_path(
        train_id=TRAIN,
        requested_destination_block_id=TARGET_WITH_ALTERNATIVE,
        requested_destination_direction="forward",
    )

    assert result.success, (
        f"expected a reroute around {ONE_WAY}; got "
        f"failure_kind={result.failure_kind!r} reason={result.reason!r}"
    )
    assert result.reached_target is True
    assert result.reservation_end_block_id == TARGET_WITH_ALTERNATIVE
    assert ONE_WAY not in _sequence_ids(result), (
        "the granted path must not cross the one-way block against its "
        "direction"
    )


def test_create_path_fails_no_viable_path_when_no_legal_alternative():
    """The regression: a target only reachable against the one-way policy
    must be a hard NO_VIABLE_PATH failure with no path stored — never a
    a grown-but-short authority parked just before the one-way block."""
    network, dispatcher, _sim = _boot()
    network.blocks[ONE_WAY].allowed_travel_directions = {"forward"}

    result = dispatcher.create_path(
        train_id=TRAIN,
        requested_destination_block_id=TARGET_WITHOUT_ALTERNATIVE,
        requested_destination_direction="forward",
    )

    assert not result.success, (
        "a one-way violation with no alternative must fail the create call, "
        f"got reached_target={result.reached_target!r} "
        f"authority_end={result.reservation_end_block_id!r}"
    )
    assert result.failure_kind == PathAcquisitionFailureKind.NO_VIABLE_PATH
    assert dispatcher.get_active_path(TRAIN) is None, (
        "no truncated holding authority may be stored for a statically "
        "impossible target"
    )


def test_extend_path_fails_no_viable_path_not_blocked_no_growth():
    """An extend whose every suffix crosses the one-way block against its
    direction is statically impossible: NO_VIABLE_PATH per design_new.md
    :1082, not the transient no-growth retry signal."""
    network, dispatcher, _sim = _boot()

    # Legal prefix first: authority up to PLT_ML_10_A_BLOCK (before ML_16).
    # Derive the leg's terminal direction from the winning walk geometry so
    # the setup does not depend on the block's ordered-tiles orientation.
    start = dispatcher._get_current_block_id_for_train(network.trains[TRAIN])
    cands = dispatcher._pathfinder.find_ordered_walk(start, "PLT_ML_10_A_BLOCK")
    assert cands, "setup: no walk from the yard to PLT_ML_10_A_BLOCK"
    terminal_dir = next(
        (
            s.block_direction
            for s in reversed(cands[0].ordered_walk or [])
            if s.block_id == "PLT_ML_10_A_BLOCK" and s.block_direction
        ),
        "forward",
    )
    created = dispatcher.create_path(
        train_id=TRAIN,
        requested_destination_block_id="PLT_ML_10_A_BLOCK",
        requested_destination_direction=terminal_dir,
    )
    assert created.success, (
        f"setup leg failed: {created.failure_kind!r} {created.reason!r}"
    )

    network.blocks[ONE_WAY].allowed_travel_directions = {"forward"}

    extended = dispatcher.extend_path(
        path_id=created.path_id,
        requested_destination_block_id=TARGET_WITHOUT_ALTERNATIVE,
        requested_destination_direction="forward",
    )

    assert not extended.success
    assert extended.failure_kind == PathAcquisitionFailureKind.NO_VIABLE_PATH
    assert extended.appended_resource_count is None, (
        "a statically impossible suffix must fail outright, not be classified "
        "as a transient occupancy hold"
    )


def test_manual_override_still_bypasses_one_way_policy():
    """Operator manual_override=True relaxes allowed_travel_directions for the
    whole walk (design_new.md "`disabled` and `manual` availability flags"):
    the walk through the one-way block in reverse survives the search again.
    Without override it is rejected (see
    test_search_rejects_walks_crossing_one_way_block_in_reverse), so its
    reappearance here is the relaxation contract.

    The contract is that the one-way walk is a *surviving candidate*, not that
    it wins ranking: travel-time ranking may legally prefer a faster
    equal-distance alternative (here several candidates tie on tile_count and
    length, and the faster one that does not cross the one-way block wins)."""
    network, dispatcher, _sim = _boot()
    network.blocks[ONE_WAY].allowed_travel_directions = {"forward"}
    start = dispatcher._get_current_block_id_for_train(network.trains[TRAIN])

    candidates = dispatcher._pathfinder.find_ordered_walk(
        start, TARGET_WITH_ALTERNATIVE, manual_override=True
    )
    crosses_one_way = any(
        any(
            s.block_id == ONE_WAY and s.block_direction == "reverse"
            for s in (cand.ordered_walk or [])
        )
        for cand in candidates
    )
    assert crosses_one_way, (
        "with manual_override the walk through the one-way block (in reverse) "
        "must survive the search again"
    )

    # The override also lets create_path reach the target; the one-way policy
    # no longer blocks reachability.
    result = dispatcher.create_path(
        train_id=TRAIN,
        requested_destination_block_id=TARGET_WITH_ALTERNATIVE,
        requested_destination_direction="forward",
        manual_override=True,
    )
    assert result.success, (
        f"override call failed: {result.failure_kind!r} {result.reason!r}"
    )
