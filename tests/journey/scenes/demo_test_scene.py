"""Scene for the two-branch curve-radius routing test set (``demo_test``).

The ``demo_test`` layout joins p1 (south-east) and p2 (north-west) with two
branches that share the throat blocks at each switch:

  * SHORT branch -- ``BLK_2FEE7D`` -> ``BLK_2FDE9B`` (43 tiles, 4200cm)
  * LONG branch  -- seven blocks ``BLK_2FD0FF`` .. ``BLK_2F9133`` (63 tiles, 6200cm)

A 50cm train sits in p1 with a one-off route to p2 (both installed by
``demo_test.setup_demo_train``). The train's minimum curve radius is 315mm.

With no per-block curve limit the pathfinder picks the SHORT branch on
tile-count then length. Passing ``short_block_radius_cm`` below the train's
315mm minimum makes the SHORT branch's curves too tight for the train, so the
pathfinder rejects every candidate through it and the train takes the LONG
branch instead.
"""

import contextlib
import io

from bootstrap import bootstrap_runtime

from .base import SceneFixture

# Blocks unique to each branch (the throat blocks BLK_2FFFE0 / BLK_2F8674 are
# shared and so are not listed here).
SHORT_BRANCH_BLOCKS = ("BLK_2FEE7D", "BLK_2FDE9B")
LONG_BRANCH_BLOCKS = (
    "BLK_2FD0FF",
    "BLK_2FC44E",
    "BLK_2FB8FA",
    "BLK_2FB0B5",
    "BLK_2FA65B",
    "BLK_2F9DB3",
    "BLK_2F9133",
)
P2_BLOCK = "PLAT_513670_BLOCK"

# When the two branches are equalised, each branch's divergent blocks are set
# to sum to this total length (centimetres) so the only remaining difference
# between the branches is the posted speed limit.
EQUAL_BRANCH_DIVERGENT_CM = 280.0


class DemoTestScene:
    """Scene factory for the curve-radius and speed-limit routing test set.

    All mutations are applied before the first tick (before the route's path is
    acquired), so the pathfinder plans against them.

    Args:
        short_block_radius_cm: When set, the curve radius (centimetres) written
            onto every SHORT-branch block. Below the train's 315mm (31.5cm)
            minimum this forces the LONG branch; None (default) leaves the
            SHORT branch unconstrained so the train takes it.
        equalize_branch_lengths: When True, both branches' divergent blocks are
            rewritten so each branch sums to ``EQUAL_BRANCH_DIVERGENT_CM``. With
            equal length the routing choice turns purely on the posted speed
            limits below.
        short_branch_speed_kmh / long_branch_speed_kmh: When set, the posted
            ``speed_limit_kmh`` for every block on that branch. The pathfinder
            ranks candidates by estimated travel time, so the faster branch
            wins even when it has more tiles.
        short_branch_grade_percent: When set, the signed ``grade_percent`` on
            every SHORT-branch block. ``grade_percent`` is + uphill in the
            block's forward direction; the SHORT branch is traversed in reverse
            for the p1->p2 route, so a *negative* value is the train's uphill
            climb and a positive one is a descent. A climb steeper than the
            train's 15% ``max_slope`` forces the LONG branch; a descent never
            does (the train only stalls going up).
    """

    train_id: str = "TEST_TRAIN"

    def __init__(
        self,
        short_block_radius_cm: float | None = None,
        equalize_branch_lengths: bool = False,
        short_branch_speed_kmh: float | None = None,
        long_branch_speed_kmh: float | None = None,
        short_branch_grade_percent: float | None = None,
    ):
        self.short_block_radius_cm = short_block_radius_cm
        self.equalize_branch_lengths = equalize_branch_lengths
        self.short_branch_speed_kmh = short_branch_speed_kmh
        self.long_branch_speed_kmh = long_branch_speed_kmh
        self.short_branch_grade_percent = short_branch_grade_percent

    def build(self) -> SceneFixture:
        with contextlib.redirect_stdout(io.StringIO()):
            network, _path_dispatcher, sim_controller, _persistence = (
                bootstrap_runtime(demo=True, scene="demo_test", setup_train=True)
            )

        if self.equalize_branch_lengths:
            for branch in (SHORT_BRANCH_BLOCKS, LONG_BRANCH_BLOCKS):
                per_block = EQUAL_BRANCH_DIVERGENT_CM / len(branch)
                for block_id in branch:
                    network.blocks[block_id].length_cm = per_block

        if self.short_branch_speed_kmh is not None:
            for block_id in SHORT_BRANCH_BLOCKS:
                network.blocks[block_id].speed_limit_kmh = self.short_branch_speed_kmh
        if self.long_branch_speed_kmh is not None:
            for block_id in LONG_BRANCH_BLOCKS:
                network.blocks[block_id].speed_limit_kmh = self.long_branch_speed_kmh

        if self.short_block_radius_cm is not None:
            for block_id in SHORT_BRANCH_BLOCKS:
                network.blocks[block_id].min_radius_cm = self.short_block_radius_cm

        if self.short_branch_grade_percent is not None:
            for block_id in SHORT_BRANCH_BLOCKS:
                network.blocks[block_id].grade_percent = self.short_branch_grade_percent

        return SceneFixture(
            network=network,
            sim_controller=sim_controller,
            train_id=self.train_id,
        )
