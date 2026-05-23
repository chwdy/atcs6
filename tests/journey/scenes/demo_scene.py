"""Scene that wraps `bootstrap_runtime(demo=True, setup_train=True)` to spin
up the demo network with DEMO_TRAIN at (0,0) facing EAST and a queued
`power_on` operator command. This is the "demo" scene used by
`test_demo_train_journey.py`.

Bootstrap assigns the extended ``DEMO_ROUTE_V2`` by default; this scene
re-points the train to the single-stop ``DEMO_ROUTE_V1`` so the journey
test keeps its focused "origin → East Terminal, stop forever" semantics.
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

        registry = sim_controller.route_executor.route_registry
        v2 = registry.get("DEMO_ROUTE_V2")
        if v2 is not None:
            v2.is_active = False
            v2.unassign_train(self.train_id)
        v1 = registry.get("DEMO_ROUTE_V1")
        if v1 is not None:
            v1.is_active = True
            v1.assign_train(self.train_id)

        return SceneFixture(
            network=network,
            sim_controller=sim_controller,
            train_id=self.train_id,
        )
