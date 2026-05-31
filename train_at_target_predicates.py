"""
TrainAtTargetPredicates — train-at-position predicates extracted from
`RouteExecutor`.

These are read-only predicates that ask "is the train head at this
block / platform / station?". They read the train head tile from the
controller's cab-derived span and the network's track / platform
registries; no mutation, no side effects.

`RouteExecutor` keeps the doc-canonical method names
(`_is_train_at_block`, `_is_train_at_platform`,
`_is_train_at_platform_for_task`, `_get_target_platform_tile`,
`_get_current_block_id`) as thin forwarders that delegate here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from route_executor import RouteExecutor
    from train import Train
    from train_route import RouteTask


class TrainAtTargetPredicates:
    """Train-at-target predicates for one `RouteExecutor`."""

    def __init__(self, executor: "RouteExecutor") -> None:
        self._e = executor

    # ------------------------------------------------------------------
    # Controller-derived head/facing (design_new.md: head/tail and travel
    # direction live on the controller, derived from cab + current_span).
    # ------------------------------------------------------------------

    def _controller(self, train: "Train"):
        getter = getattr(self._e, "_controller_by_id", None)
        return getter(train.train_id) if callable(getter) else None

    def _head_tile(self, train: "Train") -> Optional[Tuple[int, int]]:
        ctrl = self._controller(train)
        return ctrl._get_head_tile() if ctrl is not None else None

    def _facing(self, train: "Train"):
        ctrl = self._controller(train)
        return ctrl._facing_direction() if ctrl is not None else None

    # ------------------------------------------------------------------
    # Block / platform / station predicates
    # ------------------------------------------------------------------

    def is_train_at_block(self, train: "Train", block_id: str) -> bool:
        """True when the train's head tile sits in `block_id`."""
        head_tile = self._head_tile(train)
        if head_tile is None:
            return False
        track = self._e.network.get_track(*head_tile)
        if not track:
            return False
        return track.block_section_id == block_id

    def is_train_at_platform_for_task(
        self, train: "Train", task: "RouteTask"
    ) -> bool:
        """True when the train head sits at the platform tile that
        satisfies `task.task_location`.

        Resolves the platform id from the tagged-union task_location:
        only `PlatformTarget` carries a direct platform id. For other
        variants this predicate returns False (block / station targets
        are not "at-platform" predicates by themselves).
        """
        head_tile = self._head_tile(train)
        if head_tile is None:
            return False
        from train_route import PlatformTarget

        loc = task.task_location
        if not isinstance(loc, PlatformTarget):
            return False
        platform = self._resolver().get_platform(loc.platform_id)
        if not platform:
            return False
        target_tile = self.get_target_platform_tile(self._facing(train), platform)
        if not target_tile:
            return False
        return head_tile == target_tile

    def is_train_at_platform(self, train: "Train", platform_id: str) -> bool:
        """True when the train head sits at `platform_id`'s stopping tile."""
        head_tile = self._head_tile(train)
        if head_tile is None:
            return False
        platform = self._resolver().get_platform(platform_id)
        if not platform:
            return False
        target_tile = self.get_target_platform_tile(self._facing(train), platform)
        if not target_tile:
            return False
        return head_tile == target_tile

    # ------------------------------------------------------------------
    # Target-tile picking (direction-aware)
    # ------------------------------------------------------------------

    @staticmethod
    def get_target_platform_tile(
        facing_direction, platform: object
    ) -> Optional[Tuple[int, int]]:
        """Pick the stopping tile for `platform` based on the train's travel
        `Direction` (or None). Pure helper — callers derive the direction from
        the controller (cab + span) and pass it in."""
        positions = list(getattr(platform, "positions", []))
        if not positions:
            return None
        if len(positions) == 1:
            return positions[0]

        direction = facing_direction
        if not direction:
            return positions[0]

        def sort_key(pos: Tuple[int, int]) -> float:
            x, y = pos
            if direction.name == "NORTH":
                return -y
            if direction.name == "SOUTH":
                return y
            if direction.name == "EAST":
                return -x
            if direction.name == "WEST":
                return x
            return 0

        return sorted(positions, key=sort_key)[0]

    # ------------------------------------------------------------------
    # Current block lookup
    # ------------------------------------------------------------------

    def current_block_id(self, train: "Train") -> Optional[str]:
        """Train's current block based on the head tile, or None."""
        head_tile = self._head_tile(train)
        if head_tile is None:
            return None
        track = self._e.network.get_track(*head_tile)
        if not track:
            return None
        return track.block_section_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolver(self):
        """Resolve the executor's `RouteTargetResolver` (lazy)."""
        return self._e._get_route_target_resolver()
