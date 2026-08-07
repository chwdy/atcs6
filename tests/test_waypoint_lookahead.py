"""Waypoint lookahead — arming the next target before the train has to brake.

design_new.md Planning Kernel step `66aa`. A `waypoint` is track the train
passes through, not a place it stops. Re-arming one the authority already
reaches would leave the train braking toward a terminal it is meant to run
straight past, so once the cleared track ahead of the head runs short, Phase 1
arms the *task after it* instead and the authority grows forward.

Two conditions gate it, and both matter: the served task must be a `waypoint`
the authority already reaches (nothing is owed on it), and fewer than
`WAYPOINT_LOOKAHEAD_BLOCKS` block resources may remain ahead of the head (the
train is close enough to need more).

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_waypoint_lookahead.py -v
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from constants import WAYPOINT_LOOKAHEAD_BLOCKS
from path import Path, ResourceSequenceEntry
from route_executor import RouteExecutor


def _path(block_count: int, *, head_index: int, target: str) -> Path:
    """A live authority of `block_count` blocks whose head sits at
    `head_index`, reaching `target`."""
    entries = [
        ResourceSequenceEntry(
            sequence_index=i, resource_type="block", resource_id=f"BLK_{i}"
        )
        for i in range(block_count)
    ]
    # Name the target block so `reaches_target(...)` finds it ahead of the head.
    entries[-1].resource_id = target
    return Path(
        path_id="P1",
        train_id="T1",
        resource_sequence=entries,
        current_resource_index=head_index,
    )


def _projection(task_kind: str, block: str, walk_pos: int):
    return SimpleNamespace(
        walk_position=walk_pos,
        task_kind=task_kind,
        direction="forward",
        resolved_target=SimpleNamespace(
            requested_destination_block_id=block,
            requested_destination_direction="forward",
        ),
    )


def _executor(path):
    dispatcher = MagicMock()
    dispatcher.get_active_path.return_value = path
    executor = RouteExecutor(network=MagicMock(), path_dispatcher=dispatcher)
    return executor


def _train():
    return SimpleNamespace(train_id="T1")


# ------------------------------------------------------------------ it fires


def test_it_arms_the_next_target_when_the_cleared_track_runs_short():
    # Head one block from the end: 1 block ahead, below the 3-block threshold.
    path = _path(4, head_index=2, target="BLK_3")
    executor = _executor(path)
    ahead = _projection("waypoint", "BLK_FAR", walk_pos=6)
    executor._find_next_task_target = lambda train_id, after_walk_position=None: (
        ahead if after_walk_position == 5 else None
    )

    armed = executor._apply_waypoint_lookahead(
        _train(), _projection("waypoint", "BLK_3", walk_pos=5)
    )

    assert armed is ahead
    assert armed.walk_position == 6


def test_the_armed_walk_position_is_the_lookahead_one():
    """Everything downstream keys off the returned projection's
    `walk_position` — the `resolved_target` write, the `direction` write, the
    extend retries, and the idle-hold predicate all use the lookahead index
    rather than the served waypoint's."""
    path = _path(4, head_index=2, target="BLK_3")
    executor = _executor(path)
    ahead = _projection("waypoint", "BLK_FAR", walk_pos=99)
    executor._find_next_task_target = lambda train_id, after_walk_position=None: ahead

    armed = executor._apply_waypoint_lookahead(
        _train(), _projection("waypoint", "BLK_3", walk_pos=5)
    )

    assert armed.walk_position == 99
    assert armed.resolved_target.requested_destination_block_id == "BLK_FAR"


# -------------------------------------------------------------- it holds off


def test_a_stop_target_is_never_looked_past():
    """Authority must not grow past an unserved `stop` — that would let the
    train roll through the place it was sent to (the Authority termination
    rule)."""
    path = _path(4, head_index=2, target="BLK_3")
    executor = _executor(path)
    executor._find_next_task_target = lambda *a, **k: _projection(
        "waypoint", "BLK_FAR", walk_pos=6
    )

    served = _projection("stop", "BLK_3", walk_pos=5)
    assert executor._apply_waypoint_lookahead(_train(), served) is served


def test_plenty_of_cleared_track_ahead_arms_nothing_new():
    """With the threshold's worth of blocks still ahead there is no reason to
    reach further — the train is nowhere near braking."""
    blocks_ahead = WAYPOINT_LOOKAHEAD_BLOCKS + 2
    path = _path(blocks_ahead + 1, head_index=0, target=f"BLK_{blocks_ahead}")
    executor = _executor(path)
    executor._find_next_task_target = lambda *a, **k: _projection(
        "waypoint", "BLK_FAR", walk_pos=6
    )

    served = _projection("waypoint", f"BLK_{blocks_ahead}", walk_pos=5)
    assert executor._apply_waypoint_lookahead(_train(), served) is served


def test_an_authority_short_of_the_waypoint_arms_nothing_new():
    """Nothing is owed on the current target only once the authority reaches
    it; a truncated authority still has growing to do toward the served
    waypoint itself."""
    path = _path(4, head_index=2, target="BLK_3")
    executor = _executor(path)
    executor._find_next_task_target = lambda *a, **k: _projection(
        "waypoint", "BLK_FAR", walk_pos=6
    )

    served = _projection("waypoint", "BLK_NOT_IN_PATH", walk_pos=5)
    assert executor._apply_waypoint_lookahead(_train(), served) is served


def test_no_live_authority_arms_nothing_new():
    executor = _executor(None)
    executor._find_next_task_target = lambda *a, **k: _projection(
        "waypoint", "BLK_FAR", walk_pos=6
    )

    served = _projection("waypoint", "BLK_3", walk_pos=5)
    assert executor._apply_waypoint_lookahead(_train(), served) is served


def test_a_terminal_walk_arms_nothing_new():
    """At the end of a ONE_OFF walk there is no next task to look ahead to."""
    path = _path(4, head_index=2, target="BLK_3")
    executor = _executor(path)
    executor._find_next_task_target = lambda *a, **k: None

    served = _projection("waypoint", "BLK_3", walk_pos=5)
    assert executor._apply_waypoint_lookahead(_train(), served) is served
