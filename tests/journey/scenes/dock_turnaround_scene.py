"""Scene that runs DEMO_TRAIN through a three-leg route requiring a
turn-around at a no-platform dock.

The route is:
  task 0: stop at the block containing (5,0)   — non-platform main-east block
  task 1: stop at the block containing (0,0)   — no-platform dock at the
                                                  west dead-end of the main line
  task 2: stop at the North Station platform   — up the (3,y) branch

The train starts at (0,0) facing EAST, so the run forces two reversals at
non-platform blocks: one at the east main block to come back, and one at
the (0,0) dock to head out to North Station via switch (3,0).

Pins design_new.md "`CAUTION` vs reservation independence" (CAUTION is a speed
cap, not an obstruction) and design_new.md "Canonical Next-Target Selection"
(`task_direction == "either"` on a BlockTarget picks whichever block-relative
direction is reachable from the current span).
"""

from train_route import (
    BlockTarget,
    PlatformTarget,
    Route,
    RouteMode,
    RouteTask,
)

from .base import SceneFixture
from .demo_scene import DemoScene


class DockTurnaroundScene:
    """Scene factory: DemoScene + a three-task turnaround route."""

    train_id: str = "DEMO_TRAIN"
    route_id: str = "TURNAROUND_ROUTE"

    def build(self) -> SceneFixture:
        fixture = DemoScene().build()
        sim = fixture.sim_controller
        network = fixture.network
        train_id = fixture.train_id

        for default_route_id in ("DEMO_ROUTE_V1", "DEMO_ROUTE_V2"):
            default_route = sim.route_executor.route_registry.get(default_route_id)
            if default_route is not None:
                default_route.is_active = False
                default_route.unassign_train(train_id)

        east_block_id = network.get_track(5, 0).block_id
        west_block_id = network.get_track(0, 0).block_id
        north_platform = next(
            station
            for station in network.stations.values()
            if station.name == "North Station"
        )

        route = Route(
            route_id=self.route_id,
            name="Dock turnaround: east block -> (0,0) dock -> North Station",
            mode=RouteMode.ONE_OFF,
            is_active=True,
        )
        route.add_task(
            RouteTask(
                task_kind="stop",
                task_location=BlockTarget(block_id=east_block_id),
                task_direction="either",
            )
        )
        route.add_task(
            RouteTask(
                task_kind="stop",
                task_location=BlockTarget(block_id=west_block_id),
                task_direction="either",
            )
        )
        route.add_task(
            RouteTask(
                task_kind="stop",
                task_location=PlatformTarget(
                    platform_id=north_platform.platform_id
                ),
                task_direction="either",
            )
        )
        sim.route_executor.register_route(route)
        route.assign_train(train_id)

        return fixture
