"""
RouteTargetResolver — owns the read-only "next task target" projection
extracted from `RouteExecutor`.

Per design_new.md "Canonical Next-Target Selection" the projection is a
pure read: it never writes `task_runs[i].resolved_target`; that field is
the exclusive write of Phase 1's authority-acquisition step. This module
keeps the resolution logic structurally separate so the executor's tick
loop can stay focused on state-machine transitions.

`RouteExecutor` keeps the doc-canonical method names
(`_find_next_task_target`, `_resolve_task_target_block`,
`_resolve_task_direction`, `_compute_next_task_index`, `_get_platform`,
`_get_platform_block_id`) as thin forwarders that delegate here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Optional

from train_route import (
    BlockTarget,
    PlatformTarget,
    RouteMode,
    StationTarget,
    task_location_id,
)

if TYPE_CHECKING:
    from route_executor import (
        ResolvedNextTaskProjection,
        ResolvedRouteTarget,
        RouteExecutionState,
        RouteExecutor,
    )
    from train import Train
    from train_route import Route, RouteTask


class RouteTargetResolver:
    """Owns target / direction / next-task-index resolution for one
    `RouteExecutor`."""

    def __init__(self, executor: "RouteExecutor") -> None:
        self._e = executor

    # ------------------------------------------------------------------
    # Top-level projection
    # ------------------------------------------------------------------

    def find_next_task_target(
        self,
        train_id: str,
        exclude: Optional[Iterable["ResolvedRouteTarget"]] = None,
    ) -> Optional["ResolvedNextTaskProjection"]:
        """Read-only projection of the next non-finished task.

        Returns `None` when no next task on the directed walk exists.
        Never writes `task_runs[i].resolved_target`; that is the exclusive
        write of Phase 1's authority-acquisition step.

        Dispatches on `task_kind` from `Route.tasks[i]`:
          * `"waypoint"` / `"stop"`: resolve a concrete target block + direction.
          * `"dwell"`: no dispatcher target; `resolved_target` is `None`.
        """
        from route_executor import ResolvedNextTaskProjection, ResolvedRouteTarget

        e = self._e
        train = (
            e.network.trains.get(train_id)
            if hasattr(e.network, "trains")
            else None
        )
        if train is None:
            return None
        route_id = e.assigned_route_id_for(train_id)
        if not route_id:
            return None
        route = e.route_registry.get(route_id)
        if route is None:
            return None
        exec_state = e.execution_states.get(train_id)
        if exec_state is None:
            return None
        task_count = route.get_task_count() if hasattr(route, "get_task_count") else 0
        if task_count == 0:
            return None

        next_index = self.compute_next_task_index(route, exec_state, task_count)
        if next_index is None:
            return None

        task = route.get_task_at(next_index)
        if task is None:
            return None

        direction_str = "forward" if exec_state.signed_direction >= 0 else "reverse"

        if task.task_kind == "dwell":
            return ResolvedNextTaskProjection(
                walk_position=next_index,
                task_kind="dwell",
                direction=direction_str,
                resolved_target=None,
            )

        target_block = self.resolve_task_target_block(task)
        if target_block is None:
            return ResolvedNextTaskProjection(
                walk_position=next_index,
                task_kind=task.task_kind,
                direction=direction_str,
                resolved_target=None,
            )

        resolved_dir = self.resolve_task_direction(
            train, target_block, task.task_direction, exec_state.signed_direction
        )

        resolved = ResolvedRouteTarget(
            requested_destination_block_id=target_block,
            requested_destination_direction=resolved_dir,
        )

        if exclude:
            for ex in exclude:
                if (
                    ex.requested_destination_block_id
                    == resolved.requested_destination_block_id
                    and ex.requested_destination_direction
                    == resolved.requested_destination_direction
                ):
                    return None

        return ResolvedNextTaskProjection(
            walk_position=next_index,
            task_kind=task.task_kind,
            direction=direction_str,
            resolved_target=resolved,
        )

    # ------------------------------------------------------------------
    # Task-location → block resolution
    # ------------------------------------------------------------------

    def resolve_task_target_block(self, task: "RouteTask") -> Optional[str]:
        """Resolve `task.task_location` to a concrete `block_id`.

        Per design_new.md line 1125, `task_location` is a tagged union of
        `BlockTarget | PlatformTarget | StationTarget`; this method
        dispatches on the variant rather than probing in order.
        """
        e = self._e
        loc = getattr(task, "task_location", None)
        if loc is None:
            return None

        if isinstance(loc, BlockTarget):
            blocks = getattr(e.network, "blocks", {}) or {}
            if loc.block_id in blocks:
                return loc.block_id
            return None

        if isinstance(loc, PlatformTarget):
            return self.platform_block_id(loc.platform_id)

        if isinstance(loc, StationTarget):
            station_groups = getattr(e.network, "station_groups", {}) or {}
            station = station_groups.get(loc.station_id)
            if station is None:
                return None
            platform_ids = getattr(station, "platform_ids", []) or []
            for pid in platform_ids:
                bid = self.platform_block_id(pid)
                if bid:
                    return bid
            return None

        # Fallback: legacy string (only reachable if a caller bypassed
        # `_coerce_task_location`).
        legacy = task_location_id(loc) if loc else ""
        if not legacy:
            return None
        blocks = getattr(e.network, "blocks", {}) or {}
        if legacy in blocks:
            return legacy
        block_id = self.platform_block_id(legacy)
        if block_id:
            return block_id
        return None

    # ------------------------------------------------------------------
    # Direction resolution ("either" → forward / reverse)
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_task_direction(
        train: "Train",
        block_id: str,
        task_direction: str,
        signed_direction: int,
    ) -> str:
        """Resolve a `RouteTask.task_direction` to `"forward"` /
        `"reverse"`.

        v1 contract: the dispatcher never receives `"either"`; this resolver
        must always eliminate it before any `create_path` / `extend_path`
        call. Pure: depends only on the inputs.
        """
        if task_direction in ("forward", "reverse"):
            return task_direction
        return "forward" if signed_direction >= 0 else "reverse"

    # ------------------------------------------------------------------
    # Next-task-index computation (pure)
    # ------------------------------------------------------------------

    @staticmethod
    def compute_next_task_index(
        route: "Route",
        exec_state: "RouteExecutionState",
        task_count: int,
    ) -> Optional[int]:
        """Compute the next task index without mutating `exec_state`."""
        if task_count == 0:
            return None

        idx = exec_state.current_waypoint_index
        if 0 <= idx < task_count:
            return idx

        if route.mode == RouteMode.ONE_OFF:
            if idx >= task_count:
                return None
            return idx

        if route.mode == RouteMode.CIRCULAR:
            return idx % task_count

        if route.mode == RouteMode.ROUND_TRIP:
            if idx >= task_count:
                return task_count - 2 if task_count > 1 else 0
            if idx < 0:
                return 1 if task_count > 1 else 0
            return idx
        return None

    # ------------------------------------------------------------------
    # Platform / block lookup helpers
    # ------------------------------------------------------------------

    def get_platform(self, platform_id: str) -> Optional[object]:
        """Return the `Platform` object for `platform_id`, or None."""
        platforms = getattr(self._e.network, "platforms", None)
        if isinstance(platforms, dict) and platform_id in platforms:
            return platforms[platform_id]
        return None

    def platform_block_id(self, platform_id: str) -> Optional[str]:
        """Primary block for a platform, derived from
        `Platform.ordered_blocks` with a tile-by-tile fallback for
        platforms whose block assignments have not been populated yet."""
        platform = self.get_platform(platform_id)
        if not platform:
            return None

        ordered_blocks = getattr(platform, "ordered_blocks", None)
        if ordered_blocks:
            return ordered_blocks[0]

        network = self._e.network
        for x, y in getattr(platform, "positions", []):
            track = network.get_track(x, y)
            if track and track.block_section_id:
                return track.block_section_id
        return None
