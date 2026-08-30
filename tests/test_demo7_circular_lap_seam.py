"""A CIRCULAR route must be able to close its lap.

demo7 is a four-block oval with one loco and no switches. In `lap_stop` mode
the route is three waypoints plus a stop and a dwell on the starting block, so
one pass of the route walk is exactly one lap of the layout.

The lap seam is the moment the route walk wraps and asks for authority into the
block the run started from. That block is in the live path's `resource_sequence`
as history, but the train released it long ago (head entry consumed the
reservation, tail vacate cleared `occupied_by`), so authorizing it again is an
ordinary acquisition rather than a second concurrent claim. Per design_new.md
"Path invariants" the repeated-resource rule bars holding one resource twice at
the same time; it is not a bar on ever revisiting one.

Because the lap ends on a `stop`, the path completes there and the next lap
begins with a fresh `create_path`. That is what keeps the pattern steady: one
create and one completion per lap, no cancellations, and a `resource_sequence`
that never accumulates.
"""

import contextlib
import io

import pytest

from bootstrap import bootstrap_runtime


LOOP_ORDER = ["BLK_AA62D2", "BLK_AA866E", "BLK_AA7B7E", "BLK_AA6FB5"]
START_BLOCK = LOOP_ORDER[0]
TRAIN_ID = "T_LOOP"

# Long enough for three laps at every dt in the sweep (one lap is ~175 s of
# simulated time), with room to spare.
SIM_SECONDS = 900.0


@pytest.fixture
def demo7_runtime(monkeypatch):
    """demo7 in `lap_stop` mode with a negligible dwell.

    `RouteExecutor` measures dwell against wall-clock system time (design_new.md
    "Task time semantics"), which is right for a layout running at 1x but never
    elapses in a test that advances simulated time thousands of times faster
    than real time. The seam is what this test is about, so the dwell is set
    near zero and departs immediately; `test_dwell_holds_for_its_configured_time`
    covers the hold itself.
    """
    import demo7

    monkeypatch.setattr(demo7, "HOLD_SECONDS", 0.001)
    with contextlib.redirect_stdout(io.StringIO()):
        network, dispatcher, sim, _persistence = bootstrap_runtime(
            demo=True, setup_train=True, scene="demo7"
        )
    return network, dispatcher, sim


def _run_laps(sim, dispatcher, dt):
    """Tick the scene, recording one entry per create / completion / block."""
    events = []

    original_create = dispatcher.create_path
    original_complete = sim.route_executor.handle_path_completion

    def traced_create(**kwargs):
        result = original_create(**kwargs)
        if result.success:
            events.append(("create", kwargs["requested_destination_block_id"]))
        return result

    def traced_complete(train_id, path_id, walk_position):
        # The callback no longer receives the authority-end block (design_new.md
        # step `bc55` hands it the served walk-position). Trace the served block
        # from the run's own copy of the target
        # (`task_runs[walk_position].resolved_target`).
        run = sim.route_executor.execution_state_for(train_id)
        target = (
            run.task_runs[walk_position].resolved_target
            if run is not None and 0 <= walk_position < len(run.task_runs)
            else None
        )
        block = target.requested_destination_block_id if target is not None else None
        events.append(("complete", block))
        return original_complete(train_id, path_id, walk_position)

    dispatcher.create_path = traced_create
    sim.route_executor.handle_path_completion = traced_complete

    controller = sim.get_or_create_controller(TRAIN_ID)
    max_sequence_len = 0
    last_block = None
    for _ in range(int(SIM_SECONDS / dt)):
        sim.update(dt)
        assert not sim.failed_runtime, (
            f"entered failed runtime at t={sim.simulation_time:.1f}: "
            f"{sim.failed_runtime_payload}"
        )
        block_id = controller._get_current_block_id()
        if block_id != last_block:
            if block_id is not None:
                events.append(("block", block_id))
            last_block = block_id
        path_id = dispatcher.active_path_id_by_train(TRAIN_ID)
        if path_id is not None:
            max_sequence_len = max(
                max_sequence_len,
                len(dispatcher.paths_by_id[path_id].resource_sequence),
            )
    return events, max_sequence_len


@pytest.mark.parametrize("dt", [0.1, 0.2, 0.3], ids=["1x", "2x", "3x"])
def test_circular_route_laps_repeatedly(demo7_runtime, dt):
    """The loco keeps lapping in loop order, without ever failing the runtime."""
    _network, dispatcher, sim = demo7_runtime
    events, _ = _run_laps(sim, dispatcher, dt)

    visited = [block_id for kind, block_id in events if kind == "block"]
    assert len(visited) >= 9, (
        f"expected at least two full laps of block entries, saw {visited}"
    )

    # Every entry follows the previous one in loop order, across the seam.
    for previous, current in zip(visited, visited[1:]):
        expected = LOOP_ORDER[(LOOP_ORDER.index(previous) + 1) % len(LOOP_ORDER)]
        assert current == expected, (
            f"loop order broken: {previous} -> {current}, expected {expected}. "
            f"Full sequence: {visited}"
        )


@pytest.mark.parametrize("dt", [0.1, 0.2, 0.3], ids=["1x", "2x", "3x"])
def test_each_lap_is_one_path_completed_at_the_stop(demo7_runtime, dt):
    """One create and one completion per lap, and completion lands on the stop.

    A path that could not cross the seam would instead be cancelled there and
    replaced, which shows up as a create with no completion before it.
    """
    _network, dispatcher, sim = demo7_runtime
    events, _ = _run_laps(sim, dispatcher, dt)

    lifecycle = [(kind, block_id) for kind, block_id in events if kind != "block"]
    completions = [b for kind, b in lifecycle if kind == "complete"]

    assert len(completions) >= 2, (
        f"expected at least two lap completions, saw {lifecycle}"
    )
    assert all(block_id == START_BLOCK for block_id in completions), (
        f"every lap must complete on the stop block {START_BLOCK}: {completions}"
    )

    # After the first create, the pattern is strictly alternating: a lap
    # completes, then the next lap is created.
    for previous, current in zip(lifecycle, lifecycle[1:]):
        assert previous[0] != current[0], (
            f"two {current[0]} events in a row — a lap was cancelled and "
            f"re-created rather than completing at the stop: {lifecycle}"
        )


@pytest.mark.parametrize("dt", [0.1, 0.2, 0.3], ids=["1x", "2x", "3x"])
def test_path_does_not_accumulate_across_laps(demo7_runtime, dt):
    """`resource_sequence` stays lap-sized however long the loop runs.

    The path completing at each lap's stop is what bounds it. Without that,
    authority would grow by a lap every lap and an all-day display would end up
    dragging a sequence thousands of entries long through every tick.
    """
    _network, dispatcher, sim = demo7_runtime
    _events, max_sequence_len = _run_laps(sim, dispatcher, dt)

    # One lap is four blocks; the seed span plus the terminal frame leaves room
    # for a couple more entries, but nothing near a second lap's worth.
    assert max_sequence_len <= 8, (
        f"resource_sequence peaked at {max_sequence_len} entries — authority is "
        f"accumulating across laps instead of resetting at each stop"
    )


def test_dwell_holds_for_its_configured_time(monkeypatch):
    """The stop's dwell holds for `HOLD_SECONDS` before the next lap starts.

    A dwell holding for `task_time` measures against the route's timing basis,
    which defaults to the operating clock (design_new.md "Task time
    semantics"). That clock is seconds of *operation*: it advances with the
    tick, is scaled by playback, and stops when the operator stops. On a
    layout running at 1x with nobody touching Stop it coincides with wall
    time, which is what makes `HOLD_SECONDS = 5.0` a five-second stand in
    front of an audience — and it stays a five-second stand at 4x playback of
    a four-times-faster world.
    """
    import demo7

    hold_seconds = 0.75
    monkeypatch.setattr(demo7, "HOLD_SECONDS", hold_seconds)
    with contextlib.redirect_stdout(io.StringIO()):
        _network, dispatcher, sim, _persistence = bootstrap_runtime(
            demo=True, setup_train=True, scene="demo7"
        )

    marks = []
    original_create = dispatcher.create_path
    original_complete = sim.route_executor.handle_path_completion

    def traced_create(**kwargs):
        result = original_create(**kwargs)
        if result.success:
            marks.append(("create", sim.operating_clock()))
        return result

    def traced_complete(*args, **kwargs):
        marks.append(("complete", sim.operating_clock()))
        return original_complete(*args, **kwargs)

    dispatcher.create_path = traced_create
    sim.route_executor.handle_path_completion = traced_complete

    for _ in range(200_000):
        sim.update(0.3)
        assert not sim.failed_runtime
        if sum(1 for kind, _ in marks if kind == "create") >= 2:
            break

    holds = [
        later[1] - earlier[1]
        for earlier, later in zip(marks, marks[1:])
        if earlier[0] == "complete" and later[0] == "create"
    ]
    assert holds, f"never saw a completion followed by the next lap's create: {marks}"
    assert holds[0] >= hold_seconds, (
        f"departed after {holds[0]:.2f} s of operation, before the "
        f"{hold_seconds} s dwell elapsed"
    )
