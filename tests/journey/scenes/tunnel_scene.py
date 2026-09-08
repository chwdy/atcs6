"""TunnelScene — exercises the tunnel feature end-to-end.

Two short east-west blocks far apart on the grid, joined by a single
tunnel between their facing dead-ends. The train starts in the west
block facing east and is given a route to the east block. The
pathfinder, lookahead, train physics, and dead-end recomputation all
have to follow the tunnel for the train to ever reach its target.

Layout:

    BLOCK_WEST                          BLOCK_EAST
    [0,0]──[1,0]──[2,0]    …tunnel…    [10,0]──[11,0]──[12,0]
       │                                                    │
    free dead-end                                    free dead-end

Tunnel: (2, 0, "E") ↔ (10, 0, "W"). After it is registered, (2, 0).E
and (10, 0).W are no longer dead-ends; only (0, 0).W and (12, 0).E are.
"""

from train_route import BlockTarget, Route, RouteMode, RouteTask
from bootstrap import bootstrap_runtime
from tests.segment_test_helpers import make_segment_block_with_tracks
from track import TRACK_EW, Tile

from .base import SceneFixture


class TunnelScene:
    train_id: str = "TUNNEL_TRAIN"
    west_block_id: str = "BLOCK_WEST"
    east_block_id: str = "BLOCK_EAST"
    route_id: str = "TUNNEL_ROUTE"

    def build(self) -> SceneFixture:
        network, _path_dispatcher, sim_controller, _persistence = bootstrap_runtime(
            demo=False, setup_train=False
        )

        for x in range(3):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))
        for x in range(10, 13):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))

        make_segment_block_with_tracks(
            network,
            block_id=self.west_block_id,
            positions=[(0, 0), (1, 0), (2, 0)],
            name="west",
            length_cm=150.0,
            speed_limit_kmh=30.0,
        )
        make_segment_block_with_tracks(
            network,
            block_id=self.east_block_id,
            positions=[(10, 0), (11, 0), (12, 0)],
            name="east",
            length_cm=150.0,
            speed_limit_kmh=30.0,
        )

        network.add_tunnel((2, 0, "E"), (10, 0, "W"))

        sim_controller.add_train(
            train_id=self.train_id,
            starting_block_id=self.west_block_id,
            initial_direction="EAST",
            max_speed_kmh=30.0,
            length_cm=20.0,
            initial_cab="A",
        )

        route = Route(
            route_id=self.route_id,
            name="Tunnel test route",
            mode=RouteMode.ONE_OFF,
            is_active=True,
        )
        route.add_task(
            RouteTask(
                task_kind="stop",
                task_location=BlockTarget(block_id=self.east_block_id),
                task_direction="forward",
            )
        )
        sim_controller.route_executor.register_route(route)
        route.assign_train(self.train_id)

        controller = sim_controller.get_or_create_controller(self.train_id)
        controller.commands.enqueue_command("power_on")
        controller.commands.enqueue_command("set_mode", {"mode": "ato"})

        return SceneFixture(
            network=network,
            sim_controller=sim_controller,
            train_id=self.train_id,
        )
