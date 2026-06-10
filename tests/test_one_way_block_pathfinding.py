"""One-way blocks are a pathfinding filter, not a wait-for-clear blocker.

Per design_new.md create_path step 2 (":967"), pathfinding-home item 6
(":971"), and the `query_graph_walks` contract (":2090"), candidate search
rejects any candidate **whose traversal violates** a block's
`Block.allowed_travel_directions` when `manual_override == False` — every
traversed block, not just the destination. A one-way restriction is static:
a walk that crosses it against its direction can never be acquired, so per
the `no_viable_path` definition (":1000") it must surface as a hard
pathfinding failure (or be rerouted around), never as a best-effort
truncation that parks authority just before the one-way block and retries
forever.

Regression pinned here (reported on demo_6): with BLK_ML_16 set to
forward-only, T_YA_6's mainline walk (which crosses BLK_ML_16 in reverse)
used to survive the search, fail reservation at BLK_ML_16
(`direction_not_allowed`), and get best-effort-truncated to an authority
ending at PLT_ML_9_A_BLOCK — the block immediately before the one-way block.
The train then drove there and held forever while every extend returned
BLOCKED_NO_GROWTH ("direction_not_allowed (blocked at BLK_ML_16)"), a
transient classification for a condition that can never clear.
"""

import contextlib
import io

import pytest

from runtime_payloads import (
    AcquisitionMode,
    PathAcquisitionFailureKind,
    PathAcquisitionResultKind,
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
            "time (design_new.md :967/:971/:2090)"
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
    assert result.result_kind == PathAcquisitionResultKind.REACHED_REQUESTED_TARGET
    assert result.authority_end_block_id == TARGET_WITH_ALTERNATIVE
    assert ONE_WAY not in _sequence_ids(result), (
        "the granted path must not cross the one-way block against its "
        "direction"
    )


def test_create_path_fails_no_viable_path_when_no_legal_alternative():
    """The regression: a target only reachable against the one-way policy
    must be a hard NO_VIABLE_PATH failure with no path stored — never a
    TRUNCATED_GREW authority parked just before the one-way block."""
    network, dispatcher, _sim = _boot()
    network.blocks[ONE_WAY].allowed_travel_directions = {"forward"}

    result = dispatcher.create_path(
        train_id=TRAIN,
        requested_destination_block_id=TARGET_WITHOUT_ALTERNATIVE,
        requested_destination_direction="forward",
    )

    assert not result.success, (
        "a one-way violation with no alternative must fail the create call, "
        f"got result_kind={result.result_kind!r} "
        f"authority_end={result.authority_end_block_id!r}"
    )
    assert result.failure_kind == PathAcquisitionFailureKind.NO_VIABLE_PATH
    assert dispatcher.get_active_path(TRAIN) is None, (
        "no truncated holding authority may be stored for a statically "
        "impossible target"
    )


def test_extend_path_fails_no_viable_path_not_blocked_no_growth():
    """An extend whose every suffix crosses the one-way block against its
    direction is statically impossible: NO_VIABLE_PATH per design_new.md
    :1082, not the transient BLOCKED_NO_GROWTH retry signal."""
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
        acquisition_mode=AcquisitionMode.ALL_OR_NOTHING,
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
    assert extended.result_kind != PathAcquisitionResultKind.BLOCKED_NO_GROWTH, (
        "a statically impossible suffix must not be classified as a "
        "transient occupancy hold"
    )


def test_manual_override_still_bypasses_one_way_policy():
    """Operator manual_override=True relaxes allowed_travel_directions for
    the whole walk (design_new.md :582): the short walk through the one-way
    block is granted again."""
    network, dispatcher, _sim = _boot()
    network.blocks[ONE_WAY].allowed_travel_directions = {"forward"}

    result = dispatcher.create_path(
        train_id=TRAIN,
        requested_destination_block_id=TARGET_WITH_ALTERNATIVE,
        requested_destination_direction="forward",
        acquisition_mode=AcquisitionMode.ALL_OR_NOTHING,
        manual_override=True,
    )

    assert result.success, (
        f"override call failed: {result.failure_kind!r} {result.reason!r}"
    )
    assert ONE_WAY in _sequence_ids(result), (
        "with manual_override the shortest walk (through the one-way block) "
        "must win again"
    )
