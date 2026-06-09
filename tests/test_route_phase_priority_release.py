"""Phase-based scheduling priority + best-effort idle-hold release.

Pins the two mechanisms specced in design_new.md "Per-train tick transaction"
(runnable-train ordering tier) and "Idle-hold maintenance and release"
(Planning Kernel step 66aa):

  1. `RouteRun.is_starting` — True until the run completes its first real
     movement target, then False forever. MUST stay monotonic across
     CIRCULAR / ROUND_TRIP wrap (the regression this guards: an earlier
     definition used `current_waypoint_index`, which is `task_index` space and
     cycles on wrap, so is_starting would falsely flip back to True).
  2. `order_runnable_trains` orders running-phase runs ahead of starting-phase
     runs, then by effective priority within each tier.
  3. A stalled truncated best-effort hold releases after IDLE_HOLD_RELEASE_TICKS
     and cools down; a full authority is never released.
"""
from bootstrap import bootstrap_runtime
from constants import IDLE_HOLD_RELEASE_TICKS, IDLE_HOLD_COOLDOWN_TICKS
from route_executor import RouteExecutionState
from train_route import RouteTaskRun


def _state_with_walk(n, finished_prefix, start_walk_position):
    """A RouteExecutionState with `n` walk-positions, the first
    `finished_prefix` finished, seeded at `start_walk_position`."""
    rs = RouteExecutionState(train_id="T", route_id="R", started_at=0.0)
    rs.task_runs = [RouteTaskRun(task_index=i) for i in range(n)]
    for i in range(finished_prefix):
        rs.task_runs[i].finished_at = 0.0
    rs.start_walk_position = start_walk_position
    return rs


class TestIsStarting:
    def test_first_mode(self):
        # FIRST: start_walk_position 0, nothing finished -> starting.
        rs = _state_with_walk(n=4, finished_prefix=0, start_walk_position=0)
        assert rs.is_starting is True
        # Completing walk-position 0 (the first task) flips it off.
        rs.task_runs[0].finished_at = 1.0
        assert rs.is_starting is False

    def test_nearest_seeded(self):
        # NEAREST seeded to walk-position 2: prefix [0,2) pre-finished.
        rs = _state_with_walk(n=5, finished_prefix=2, start_walk_position=2)
        assert rs.next_planning_walk_position == 2
        assert rs.is_starting is True  # 2 <= 2
        # Completing the seeded first target (walk-position 2) flips it off.
        rs.task_runs[2].finished_at = 1.0
        assert rs.next_planning_walk_position == 3
        assert rs.is_starting is False  # 3 > 2

    def test_monotonic_across_circular_wrap(self):
        """The regression guard: a CIRCULAR run that laps past its seed and
        finishes a LOW task_index must NOT read is_starting True again."""
        rs = _state_with_walk(n=5, finished_prefix=2, start_walk_position=2)
        # Complete the forward leg: walk-positions 2,3,4.
        for i in (2, 3, 4):
            rs.task_runs[i].finished_at = 1.0
        assert rs.is_starting is False
        # CIRCULAR wrap: append fresh walk-positions whose task_index cycles
        # back to 0/1 (below start_walk_position), and finish them.
        rs.task_runs.append(RouteTaskRun(task_index=0, finished_at=2.0))  # wp 5
        rs.task_runs.append(RouteTaskRun(task_index=1, finished_at=3.0))  # wp 6
        # current_waypoint_index (task_index space) is now 1 (< start 2) —
        # the trap. is_starting must stay False because next_planning_walk_position
        # (walk-position space) only advances.
        assert rs.current_waypoint_index == 1
        assert rs.is_starting is False


class TestTwoTierOrdering:
    def test_running_before_starting_regardless_of_priority(self):
        _net, _pd, sim, _pm = bootstrap_runtime(scene="demo_6")
        re = sim.route_executor
        for _ in range(6):  # power-on drains in Phase 2, runs allocate next Phase 1
            sim.update(0.1)
        with_runs = [t for t in _net.trains if re.execution_state_for(t) is not None]
        assert len(with_runs) >= 2, "need two allocated runs to test ordering"
        a, b = with_runs[0], with_runs[1]
        run_a = re.execution_state_for(a)
        run_b = re.execution_state_for(b)
        assert run_a is not None and run_b is not None

        # Force A into the running tier (is_starting False), B into starting.
        run_a.start_walk_position = -1  # next_planning_walk_position (>=0) > -1
        run_b.start_walk_position = 10_000  # always <= -> is_starting True
        assert run_a.is_starting is False
        assert run_b.is_starting is True
        # Even with A given LOWER priority than B, the running tier wins.
        run_a.overrides["priority"] = 0
        run_b.overrides["priority"] = 9

        order = re.order_runnable_trains([b, a])
        assert order.index(a) < order.index(b), (
            "running-phase run must order ahead of a starting-phase run "
            "regardless of priority"
        )


class TestIdleHoldRelease:
    def test_stalled_hold_releases_and_cools_down(self):
        """T_YB_4 is throat-limited in demo_6: it holds a truncated best-effort
        authority it cannot grow. The idle-hold guard must release it and cool
        down, never letting idle_hold_ticks exceed the budget, and never
        releasing a full (non-truncated) authority."""
        net, pd, sim, _pm = bootstrap_runtime(scene="demo_6")
        re = sim.route_executor
        TID = "T_YB_4"
        train = net.trains[TID]

        saw_release = False
        max_idle = 0
        for _ in range(200):
            sim.update(0.1)
            rs = re.execution_state_for(TID)
            if rs is None:
                continue
            max_idle = max(max_idle, rs.idle_hold_ticks)
            if rs.release_cooldown_ticks > 0:
                saw_release = True
                # During cooldown the held authority is released (freed).
                assert pd.get_active_path(TID) is None

            # Invariant: a full authority is NEVER counted as idle / released.
            for other in net.trains:
                orun = re.execution_state_for(other)
                ap = pd.get_active_path(other)
                if (
                    orun is not None
                    and ap is not None
                    and ap.get_authority_end_block_id()
                    == ap.requested_destination_block_id
                ):
                    assert orun.idle_hold_ticks == 0, (
                        f"{other} holds a FULL authority but idle_hold_ticks="
                        f"{orun.idle_hold_ticks} (full paths must never accrue)"
                    )

        assert saw_release, "expected the throat-limited T_YB_4 to hit an idle-hold release"
        assert max_idle <= IDLE_HOLD_RELEASE_TICKS, (
            f"idle_hold_ticks reached {max_idle}, over budget {IDLE_HOLD_RELEASE_TICKS}"
        )
        assert IDLE_HOLD_COOLDOWN_TICKS > 0
