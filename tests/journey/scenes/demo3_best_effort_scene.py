"""Scene that bootstraps the `demo_3_best_effort` demo (Four-Station NE-Zigzag
Double-Track Loop, BEST_EFFORT acquisition) and exposes T1 as the primary
train for the JourneyHarness.

The harness only drives one ``train_id``, but four trains are placed by the
demo (T1..T4) — invariants inspect ``network.switches`` directly so all four
contribute to switch-state observations.

Dwell-timer note. `RouteExecutor._now()` returns `time.time()` (real
wall-clock seconds). Under the JourneyHarness's pure-tick driving, wall-clock
seconds barely advance, so a 2-second dwell would never elapse and no train
would ever depart its starting station. The scene therefore monkey-patches
`RouteExecutor._now` to return ``setup_real_time + sim.simulation_time``,
which keeps demo setup's pre-filled `finished_at` timestamps (sampled with
`time.time()` at setup) on the same monotonic clock as the post-setup dwell
checks. This is test-only wiring — it does not change behaviour in the
server.
"""

import contextlib
import io
import time

from bootstrap import bootstrap_runtime

from .base import SceneFixture


class Demo3BestEffortScene:
    """Scene factory: build the demo_3_best_effort network and expose T1."""

    train_id: str = "T1"

    def build(self) -> SceneFixture:
        # Suppress demo_3_best_effort's print spam.
        with contextlib.redirect_stdout(io.StringIO()):
            network, _path_dispatcher, sim_controller, _persistence = (
                bootstrap_runtime(
                    demo=True, setup_train=True, scene="demo_3_best_effort"
                )
            )

        # Anchor a wall-clock origin to whatever real-time stamp was used by
        # demo setup (any `task_runs[i].finished_at` was set via time.time()
        # at setup, so they're all close to NOW). After this point, the
        # executor's "now" is sim-time-driven so dwells advance under
        # tick-only playback.
        setup_real_time = time.time()
        executor = sim_controller.route_executor

        def _patched_now(_self=executor):
            return setup_real_time + _self._owning_sim.simulation_time

        # Bind the running sim_controller onto the executor so the lambda
        # can read sim.simulation_time without capturing it directly (the
        # closure must survive the route_executor's normal use).
        executor._owning_sim = sim_controller
        executor._now = _patched_now

        return SceneFixture(
            network=network,
            sim_controller=sim_controller,
            train_id=self.train_id,
        )
