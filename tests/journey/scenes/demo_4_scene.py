"""Scene for the `demo_4` 4-train double-track loop.

Builds the full demo_4 best-effort network via `bootstrap_runtime(...)`,
which seats four trains (T1..T4) on the shared CIRCULAR route with
`acquisition_mode=BEST_EFFORT`. The journey/harness drives T1; the other
three trains run in parallel and act as live-contention against the
crossover blocks. The harness only owns one Journey but every block /
switch state is reachable through `Snapshot.network`, so per-tick state
of all eight switch-adjacent blocks plus both crossovers can be sampled
from a single-train journey.

Test-only adjustment: `RouteExecutor._now()` uses wall-clock `time.time()`
rather than simulation time, but the journey harness advances *simulated*
time only (each `sim_controller.update(dt)` adds `dt` to sim_time but
microseconds to wall-clock). The demo's 2-second dwell-completion timer
therefore never elapses in a normal test run, so the four trains stay
permanently in `RUNNING_AT_STATION` and the crossovers never see traffic.
To exercise the crossovers within a test window, this scene back-dates
the bootstrap's prefilled `task_runs[*].finished_at` by 10 minutes so
every dwell timer is already elapsed at tick 0 — the trains depart on
the first eligible Phase 1.
"""

import contextlib
import io
import time

from bootstrap import bootstrap_runtime

from .base import SceneFixture


class Demo4Scene:
    """Scene factory for the `demo_4` 4-train loop, focused on T1."""

    train_id: str = "T1"

    def build(self) -> SceneFixture:
        # demo_4.setup_demo_train prints scene info; suppress it
        # so the pytest output stays clean.
        with contextlib.redirect_stdout(io.StringIO()):
            network, _path_dispatcher, sim_controller, _persistence = (
                bootstrap_runtime(
                    demo=True,
                    setup_train=True,
                    scene="demo_4",
                )
            )

        # Back-date every train's prefilled task-run stamps by 10 minutes so
        # the demo's 2-second dwell timer is already elapsed when the harness
        # starts ticking. Without this every train stays in
        # RUNNING_AT_STATION forever within the test budget.
        #
        # Both clocks are back-dated: a dwell holding for `task_time` measures
        # against the route's timing basis, which defaults to the operating
        # clock (design_new.md "Task time semantics"). That clock starts at
        # 0.0 on a fresh controller, so ten minutes before it is -600.0 — it
        # is an accumulator of elapsed operation, not an instant.
        executor = sim_controller.route_executor
        ten_minutes_ago = time.time() - 600.0
        ten_minutes_of_operation_ago = -600.0
        for exec_state in executor.execution_states.values():
            exec_state.started_at = ten_minutes_ago
            exec_state.started_at_operating = ten_minutes_of_operation_ago
            for run in exec_state.task_runs:
                if run.finished_at is not None:
                    run.finished_at = ten_minutes_ago
                    run.finished_at_operating = ten_minutes_of_operation_ago

        return SceneFixture(
            network=network,
            sim_controller=sim_controller,
            train_id=self.train_id,
        )
