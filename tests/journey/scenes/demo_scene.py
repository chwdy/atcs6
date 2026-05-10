"""Scene that wraps `bootstrap_runtime(demo=True, setup_train=True)` to spin
up the demo network with DEMO_TRAIN at (0,0) facing EAST and a queued
`power_on` operator command. This is the "demo" scene used by
`test_demo_train_journey.py`.
"""

import contextlib
import io

from bootstrap import bootstrap_runtime

from .base import SceneFixture


class DemoScene:
    """Scene factory: returns a fixture for the demo train journey."""

    train_id: str = "DEMO_TRAIN"

    def build(self) -> SceneFixture:
        # Suppress demo_main's print spam so test output stays clean.
        with contextlib.redirect_stdout(io.StringIO()):
            network, _path_dispatcher, sim_controller, _persistence = (
                bootstrap_runtime(demo=True, setup_train=True)
            )
        return SceneFixture(
            network=network,
            sim_controller=sim_controller,
            train_id=self.train_id,
        )
