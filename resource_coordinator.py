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

from switch_manager import (
    LockAcquireResult,
    SwitchClaimKey,
    SwitchLockRequest,
    SwitchManager,
)

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
    # Exact switch claim keys this call newly inserted (design_new.md
    # "Resource-coordinator model", Return contract). On a failed return this is
    # empty: the coordinator has already released its own journal.
    inserted_switch_claim_keys: List[SwitchClaimKey] = field(default_factory=list)


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
        manual_override: bool = False,
    ) -> SegmentReserveResult:
        """Atomically reserve a segment's blocks AND switches.

        Fix #11 (design_new.md line 938): order is switches first, then blocks,
        so switch conflicts are discovered before any blocks are reserved. On
        block failure, release the just-acquired switch locks before returning.

        `manual_override` (default False) is propagated from the calling
        `PathDispatcher.create_path(...)` / `extend_path(...)` per the
        `disabled` and `manual` availability flags canonical home in
        design_new.md (under TrackResource). It is forwarded to
        `SwitchManager.acquire_locks_for_segment(...)` and
        `BlockManager.reserve_blocks_for_path(...)` so manual resources are
        reachable only when the operator explicitly chose to override.
        """
        # Phase 1: acquire switch locks first.
        sr: LockAcquireResult = self.switch_manager.acquire_locks_for_segment(
            switch_requests, path_id, train_id, manual_override=manual_override
        )
        if not sr.success:
            return SegmentReserveResult(
                success=False,
                failed_resource_type="switch",
                failed_resource_id=sr.failed_switch_id,
                reason=sr.reason or "switch_locked",
            )

        # Phase 2: reserve blocks only after switches succeeded.
        if future_block_ids:
            try:
                br = self.block_manager.reserve_blocks_for_path(
                    future_block_ids,
                    path_id,
                    train_id,
                    block_traversals,
                    manual_override=manual_override,
                )
            except Exception as exc:
                # Roll back only the switch claims THIS call newly inserted, so
                # a re-touch of a lock the path already held is never dropped.
                self.switch_manager.rollback_inserted_claims(
                    path_id, list(sr.inserted_claim_keys)
                )
                return SegmentReserveResult(
                    success=False,
                    failed_resource_type="block",
                    reason=str(exc),
                )
            if not br.success:
                # Roll back only the switch claims THIS call newly inserted.
                self.switch_manager.rollback_inserted_claims(
                    path_id, list(sr.inserted_claim_keys)
                )
                return SegmentReserveResult(
                    success=False,
                    failed_resource_type="block",
                    failed_resource_id=br.failed_block_id,
                    reason=br.reason or "reserve_failed",
                )

        return SegmentReserveResult(
            success=True,
            reserved_block_ids=list(future_block_ids),
            inserted_switch_claim_keys=list(sr.inserted_claim_keys),
        )

    def rollback_probe(
        self,
        block_ids: List[str],
        inserted_switch_claim_keys: List[SwitchClaimKey],
        path_id: str,
    ) -> None:
        """Release only what a failed multi-segment acquisition probe newly
        acquired (design_new.md "Resource-coordinator model").

        `inserted_switch_claim_keys` are the exact claim keys this probe
        inserted (from the `reserve_segment(...)` / `acquire_locks_for_segment`
        return). The switch release goes through
        `SwitchManager.rollback_inserted_claims(...)`, which drops only those
        keys — so a switch the probe merely re-touched over crossover topology,
        one the path legitimately still holds, is left intact. Block and switch
        holds are released in reverse acquisition order (switches are acquired
        first, so they release last).

        This is deliberately NOT `release_authority_tail(...)`: a failed probe
        removes only its own new holds, never a whole switch family.
        """
        if block_ids:
            self.block_manager.release_path_block_reservations(
                list(block_ids), path_id, completed=False
            )
        if inserted_switch_claim_keys:
            self.switch_manager.rollback_inserted_claims(
                path_id, list(reversed(list(inserted_switch_claim_keys)))
            )

    def release_authority_tail(
        self,
        block_ids: List[str],
        main_switch_ids: List[str],
        path_id: str,
    ) -> None:
        """Release a dropped tail when live authority is deliberately shortened
        (design_new.md "Resource-coordinator model").

        `main_switch_ids` are the dropped primary (main-usage) switches; the
        switch release also drops the flank locks those primaries placed on
        neighbours, keyed by `(path_id, primary_id)`, via
        `SwitchManager.release_segment_locks(...)`. Whole-family release is
        correct here: the truncation removes route points that have left the
        authority, so every claim they anchor is no longer valid. A coarse
        path-wide release would instead miss the flank neighbours (they are not
        in `main_switch_ids`).

        Separating this from `rollback_probe(...)` is required: truncation
        removes whole no-longer-valid families, while a failed acquisition
        removes only the keys that probe inserted.
        """
        if block_ids:
            self.block_manager.release_path_block_reservations(
                list(block_ids), path_id, completed=False
            )
        if main_switch_ids:
            self.switch_manager.release_segment_locks(
                path_id, list(main_switch_ids)
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
            # design_new.md: filter to the blocks actually reserved for this path
            # before releasing, so an expected already-released block (e.g. an
            # ahead reservation Observe authority truncation already dropped via
            # release_authority_tail) does not emit a spurious mismatch warning.
            # The released set is exactly that matching subset.
            matching = self.block_manager.matching_path_reservation_ids(
                list(block_ids), path_id
            )
            if matching:
                self.block_manager.release_path_block_reservations(
                    matching, path_id, completed=completed
                )
            released_blocks = matching
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
        route: Optional[frozenset] = None,
    ) -> None:
        """Route a tail-vacate event to the correct manager.

        ``route`` is forwarded only to the switch manager (route-keyed
        occupancy); block tail-vacate uses ``direction`` instead.
        """
        if resource_type == "block":
            handler = getattr(self.block_manager, "on_block_tail_vacated", None)
            if not callable(handler):
                return
            handler(resource_id, train_id, direction)
            return
        if resource_type == "switch":
            self.switch_manager.on_switch_tail_vacated(
                resource_id, train_id, route=route
            )
            return

    def on_physical_entry(
        self,
        resource_type: str,
        resource_id: str,
        train_id: str,
        path_id: Optional[str],
        route: Optional[frozenset] = None,
    ) -> bool:
        """Route a clean-physical-entry event to the correct manager.

        ``route`` is forwarded only to the switch manager (route-keyed
        occupancy on the entered switch tile).
        """
        if resource_type == "block":
            # Fix #30 (design_new.md): `occupy_blocks` (plural) is the
            # design's sole physical-entry writer. Route runtime through it.
            result = self.block_manager.occupy_blocks([resource_id], train_id)
            return bool(result)
        if resource_type == "switch":
            return self.switch_manager.on_physical_entry(
                resource_id, train_id, path_id, route=route
            )
        return False
