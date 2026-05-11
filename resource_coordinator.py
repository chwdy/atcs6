"""
ResourceCoordinator - unified facade for path-level reserve / release / event
dispatch across Block and Switch resources.

`PathDispatcher` calls one segment-level API on this coordinator instead of
branching on `resource_type` itself. The coordinator owns no state and is a
pure composition layer over `BlockManager` and `SwitchManager`. The single
remaining `resource_type` string branch lives in
`release_tail_vacated(...)` / `on_physical_entry(...)` because the originating
events arrive typed by that string from the controller progress report.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from switch_manager import LockAcquireResult, SwitchLockRequest, SwitchManager

if TYPE_CHECKING:
    from block_manager import BlockManager, ReservedBlockTraversal


@dataclass
class SegmentReserveResult:
    """Combined result of a `reserve_segment(...)` call.

    `success` is True iff both the block-reservation and switch-lock phases
    committed. On switch failure the block phase is rolled back inside the
    coordinator before the result is returned.
    """

    success: bool
    failed_resource_type: Optional[str] = None  # "block" | "switch" | None
    failed_resource_id: Optional[str] = None
    reason: Optional[str] = None
    reserved_block_ids: List[str] = field(default_factory=list)
    locked_switch_ids: List[str] = field(default_factory=list)


class ResourceCoordinator:
    """Composition facade over BlockManager + SwitchManager.

    Owns no state. Threads through `path_id` / `train_id` / per-resource
    metadata to the right manager, and rolls back the first phase if the
    second fails so the dispatcher sees a single atomic segment-level call.
    """

    def __init__(
        self,
        block_manager: "BlockManager",
        switch_manager: SwitchManager,
    ):
        self.block_manager = block_manager
        self.switch_manager = switch_manager

    # ------------------------------------------------------------------
    # Reserve / release
    # ------------------------------------------------------------------

    def reserve_segment(
        self,
        future_block_ids: List[str],
        block_traversals: Dict[str, "ReservedBlockTraversal"],
        switch_requests: List[SwitchLockRequest],
        path_id: str,
        train_id: str,
    ) -> SegmentReserveResult:
        """Atomically reserve a segment's blocks AND switches.

        Order: blocks first (BlockManager's own all-or-nothing applies), then
        switches (SwitchManager's own all-or-nothing applies). On switch
        failure, release the just-reserved blocks before returning.
        """
        if future_block_ids:
            try:
                br = self.block_manager.reserve_blocks_for_path(
                    future_block_ids, path_id, train_id, block_traversals
                )
            except Exception as exc:
                return SegmentReserveResult(
                    success=False,
                    failed_resource_type="block",
                    reason=str(exc),
                )
            if not br.success:
                return SegmentReserveResult(
                    success=False,
                    failed_resource_type="block",
                    failed_resource_id=br.failed_block_id,
                    reason=br.failure_reason or "reserve_failed",
                )

        sr: LockAcquireResult = self.switch_manager.acquire_locks_for_segment(
            switch_requests, path_id, train_id
        )
        if not sr.success:
            if future_block_ids:
                self.block_manager.release_path_block_reservations(
                    future_block_ids, path_id, completed=False
                )
            return SegmentReserveResult(
                success=False,
                failed_resource_type="switch",
                failed_resource_id=sr.failed_switch_id,
                reason=sr.reason or "switch_locked",
            )

        return SegmentReserveResult(
            success=True,
            reserved_block_ids=list(future_block_ids),
            locked_switch_ids=list(sr.locked_switch_ids),
        )

    def rollback_segment(
        self,
        block_ids: List[str],
        switch_ids: List[str],
        path_id: str,
    ) -> None:
        """Release any partial holds placed on a segment that failed mid-flight."""
        if block_ids:
            self.block_manager.release_path_block_reservations(
                list(block_ids), path_id, completed=False
            )
        if switch_ids:
            self.switch_manager.release_path_locks_for_switches(
                list(switch_ids), path_id
            )

    def release_path_resources(
        self,
        path_id: str,
        block_ids: List[str],
        completed: bool = False,
    ) -> Tuple[List[str], List[str]]:
        """Release every block reservation and switch lock for `path_id`.

        Returns (released_block_ids, released_switch_ids).
        """
        released_blocks: List[str] = []
        if block_ids:
            self.block_manager.release_path_block_reservations(
                list(block_ids), path_id, completed=completed
            )
            released_blocks = list(block_ids)
        released_switches = self.switch_manager.release_path_locks(path_id)
        return (released_blocks, released_switches)

    # ------------------------------------------------------------------
    # Tail-vacate / physical-entry dispatch (the single remaining
    # resource_type string branch lives here)
    # ------------------------------------------------------------------

    def release_tail_vacated(
        self,
        resource_type: str,
        resource_id: str,
        train_id: str,
        direction: Optional[str],
    ) -> None:
        """Route a tail-vacate event to the correct manager."""
        if resource_type == "block":
            handler = getattr(self.block_manager, "on_block_tail_vacated", None)
            if not callable(handler):
                return
            try:
                handler(resource_id, train_id, direction)
            except TypeError:
                handler(resource_id, train_id)
            return
        if resource_type == "switch":
            self.switch_manager.on_switch_tail_vacated(resource_id, train_id)
            return

    def on_physical_entry(
        self,
        resource_type: str,
        resource_id: str,
        train_id: str,
        path_id: Optional[str],
    ) -> bool:
        """Route a clean-physical-entry event to the correct manager."""
        if resource_type == "block":
            return self.block_manager.occupy_block(resource_id, train_id)
        if resource_type == "switch":
            return self.switch_manager.on_physical_entry(
                resource_id, train_id, path_id
            )
        return False
