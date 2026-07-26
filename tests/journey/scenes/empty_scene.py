"""Minimal placeholder Scene — confirms the Scene/Journey decoupling works
for non-demo setups without touching the harness.

Builds an empty network with a short stretch of EW track, wraps it in one
block, and adds a stationary train. No route is registered, so the train
never moves; the only thing this scene proves is that:

  - the harness accepts an arbitrary `Scene.build()` implementation
  - assertions/invariants from the shared modules work against any fixture
  - a Journey defined in another file plugs in without modifying the harness

This is purely a smoke check, not a real test scene.
"""

from bootstrap import bootstrap_runtime
from track import TRACK_EW, Tile

from .base import SceneFixture


class EmptyScene:
    """Empty-network scene with three tiles, one block, one stationary train."""

    train_id: str = "EMPTY_TRAIN"

    def build(self) -> SceneFixture:
        # Reuse bootstrap_runtime with demo=False to get a clean
        # network + sim_controller + path_dispatcher in one call.
        network, _path_dispatcher, sim_controller, _persistence = bootstrap_runtime(
            demo=False, setup_train=False
        )

        # Three EW tiles so the train can be placed facing east (placement
        # requires at least one connected neighbour in the facing direction).
        for x in range(3):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))
        network.create_segment_with_tracks(
            segment_id="EMPTY_BLOCK",
            positions=[(0, 0), (1, 0), (2, 0)],
            name="empty",
            length_cm=150.0,
            speed_limit_kmh=30.0,
        )

        sim_controller.add_train(
            train_id=self.train_id,
            starting_block_id="EMPTY_BLOCK",
            initial_direction="EAST",
            max_speed_kmh=30.0,
            length_cm=20.0,
        )

        return SceneFixture(
            network=network,
            sim_controller=sim_controller,
            train_id=self.train_id,
        )
