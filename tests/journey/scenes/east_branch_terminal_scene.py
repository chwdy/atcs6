"""Scene that drives the demo train origin → East Terminal → east branch
Station via a single multi-stop route with NO dwell.

Skipping the dwell avoids the wall-clock dwell timer in route_executor
(which uses ``time.time()`` and so doesn't track sim ticks). With two
back-to-back stop tasks, the next path is dispatched immediately after the
previous stop completes.

Used by tests/test_east_branch_caution_journey.py to pin the directional
CAUTION behavior for BLOCK_EAST_BRANCH_2 when the train enters the
diagonal east-branch station block.
"""

from train_route import (
    PlatformTarget,
    Route,
    RouteMode,
    RouteTask,
)

from .base import SceneFixture
from .demo_scene import DemoScene


class EastBranchTerminalScene:
    """Scene factory: train traverses origin → East Terminal → east branch
    Station with no intervening dwell."""

    train_id: str = "DEMO_TRAIN"
    route_id: str = "EAST_BRANCH_CAUTION_ROUTE"

    def build(self) -> SceneFixture:
        fixture = DemoScene().build()
        sim = fixture.sim_controller
        network = fixture.network
        train_id = fixture.train_id

        # DemoScene leaves DEMO_ROUTE_V1 active; turn off both demo routes.
        for rid in ("DEMO_ROUTE_V1", "DEMO_ROUTE_V2"):
            r = sim.route_executor.route_registry.get(rid)
            if r is not None:
                r.unassign_train(train_id)

        stations_by_name = {s.name: s for s in network.stations.values()}
        east_terminal = stations_by_name["East Terminal"]
        east_branch_station = stations_by_name["east branch Station"]

        route = Route(
            route_id=self.route_id,
            name="origin → East Terminal → east branch Station (no dwell)",
            mode=RouteMode.ONE_OFF,
        )
        route.add_task(
            RouteTask(
                task_kind="stop",
                task_location=PlatformTarget(
                    platform_id=east_terminal.platform_id
                ),
                task_direction="either",
            )
        )
        route.add_task(
            RouteTask(
                task_kind="stop",
                task_location=PlatformTarget(
                    platform_id=east_branch_station.platform_id
                ),
                task_direction="either",
            )
        )
        sim.route_executor.register_route(route)
        route.assign_train(train_id)

        return fixture
