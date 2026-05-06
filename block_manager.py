"""
Block State Manager - single source of truth for block occupancy.

`BlockManager` is the database-style lock manager for block resources and the
single serialization point for atomic block acquisition. It is the sole mutator
for `Block` physical state (`occupied_by`, `_is_caution`, `caution_direction`)
and for path-reservation primitives (`reserved_by_path`, `reserved_direction`,
`reserved_for_train`).

State machine (computed from primitives):
    FREE -> RESERVED -> OCCUPIED -> CAUTION -> FREE

State Descriptions:
    FREE: No reservation, no physical presence
    RESERVED: Held by a path's future reservation (interlocking locked)
    OCCUPIED: Physical train presence detected
    CAUTION: Train recently cleared but the safety margin must remain
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, List, Mapping, Set

from block import Block, BlockState


# ============================================================================
# Canonical value types
# ============================================================================


@dataclass(frozen=True)
class ReservedBlockTraversal:
    """Canonical value type for `directions` in `reserve_blocks_for_path()`.

    - `exit_edge`: compass exit edge ("N" | "E" | "S" | "W") through which the
      reservation exits this block, written into `Block.reserved_direction`.
    - `block_direction`: block-relative direction ("forward" | "reverse")
      derived canonically from `Block.ordered_tiles`. Used only for
      `Block.allowed_travel_directions` validation; not stored on the block.
    """

    exit_edge: str
    block_direction: str


@dataclass
class ReserveResult:
    """Result of an atomic `reserve_blocks_for_path()` call.

    `success == True` means every requested future block was eligible and
    has had its reservation primitives written. `success == False` means the
    call rejected before any state change was committed; `failed_block_id`
    points at the offending block and `failure_reason` is a short tag for
    diagnostics.
    """

    success: bool
    failed_block_id: Optional[str] = None
    failure_reason: Optional[str] = None
    reserved_block_ids: List[str] = field(default_factory=list)


# ============================================================================
# Manager
# ============================================================================


class BlockManager:
    """Centralized block state manager - single source of truth for block occupancy.

    All block state transitions go through this class so writes are
    serialized and the underlying `Block` data containers never get
    half-mutated by two concurrent callers.
    """

    def __init__(
        self,
        blocks: Dict[str, Block],
        simulation_controller: Optional[object] = None,
    ):
        """Initialize BlockManager with existing blocks.

        Args:
            blocks: Dictionary mapping block_id to Block instances
            simulation_controller: optional back-reference used only to report
                fatal physical-entry conflicts via
                `enter_failed_runtime(...)`. Wired by the runtime once the
                controller is constructed; stays `None` in early bootstrap.
        """
        self.blocks = blocks
        self.simulation_controller = simulation_controller
        self.history: deque = deque(maxlen=100)

    # ------------------------------------------------------------------
    # History helper
    # ------------------------------------------------------------------

    def _record_history(
        self,
        block_id: str,
        action: str,
        from_state: BlockState,
        to_state: BlockState,
        train_id: Optional[str] = None,
    ) -> None:
        """Record a state change in history."""
        self.history.append(
            {
                "timestamp": datetime.now().isoformat(),
                "block_id": block_id,
                "action": action,
                "from_state": from_state.value
                if hasattr(from_state, "value")
                else str(from_state),
                "to_state": to_state.value
                if hasattr(to_state, "value")
                else str(to_state),
                "train_id": train_id,
            }
        )

    # ------------------------------------------------------------------
    # Existing primitive operations (kept; called by the new methods)
    # ------------------------------------------------------------------

    def occupy_block(self, block_id: str, train_id: str) -> bool:
        """Mark a single block as physically occupied by a train.

        State Transitions:
            FREE      -> OCCUPIED
            RESERVED  -> OCCUPIED (only the reserved train may consume it)
            CAUTION   -> OCCUPIED (clean entry overrides stale caution)
            OCCUPIED  -> OCCUPIED (idempotent, same train only)
        """
        block = self.blocks.get(block_id)
        if not block:
            return False
        if block.disabled:
            return False

        prior_state = block.state

        if prior_state == BlockState.OCCUPIED:
            if block.occupied_by == train_id:
                return True
            return False

        if prior_state == BlockState.RESERVED:
            # Only the train the reservation was for may take it.
            if (
                block.reserved_for_train is not None
                and block.reserved_for_train != train_id
            ):
                return False
            block.occupied_by = train_id
            block._is_caution = False
            block.caution_direction = None
            # Same-train reservation is consumed by clean entry.
            block.reserved_by_path = None
            block.reserved_direction = None
            block.reserved_for_train = None
            self._record_history(
                block_id, "occupy", prior_state, BlockState.OCCUPIED, train_id
            )
            return True

        # FREE or CAUTION -> OCCUPIED. Clean entry clears stale caution.
        block.occupied_by = train_id
        block._is_caution = False
        block.caution_direction = None
        self._record_history(
            block_id, "occupy", prior_state, BlockState.OCCUPIED, train_id
        )
        return True

    def set_block_to_caution(self, block_id: str) -> bool:
        """Set a block to CAUTION (legacy single-block helper).

        Prefer `on_block_tail_vacated(...)` for the v1 lifecycle entry point.
        """
        block = self.blocks.get(block_id)
        if not block:
            return False

        prior_state = block.state
        if prior_state == BlockState.CAUTION:
            return True
        if prior_state == BlockState.OCCUPIED:
            prev_train = block.occupied_by
            block.occupied_by = None
            block._is_caution = True
            self._record_history(
                block_id,
                "set_caution",
                prior_state,
                BlockState.CAUTION,
                prev_train,
            )
            return True
        return False

    def clear_block(self, block_id: str) -> bool:
        """Clear a block back to FREE (legacy primitive).

        Used for manual overrides and path cancellation cleanup. Note: this
        clears reservation primitives and physical primitives both.
        """
        block = self.blocks.get(block_id)
        if not block:
            return False

        prior_state = block.state
        prev_train = block.occupied_by

        block.occupied_by = None
        block._is_caution = False
        block.caution_direction = None
        block.reserved_by_path = None
        block.reserved_direction = None
        block.reserved_for_train = None

        self._record_history(
            block_id, "clear", prior_state, BlockState.FREE, prev_train
        )
        return True

    def release_block(self, block_id: str, train_id: Optional[str] = None) -> bool:
        """Release a block, walking the OCCUPIED -> CAUTION -> FREE chain.

        High-level legacy helper used where the new directional vacate API
        is not yet available. Validates `train_id` against current occupant
        when provided.
        """
        block = self.blocks.get(block_id)
        if not block:
            return False

        if train_id and block.occupied_by != train_id:
            return False

        st = block.state
        if st == BlockState.OCCUPIED:
            return self.set_block_to_caution(block_id)
        if st == BlockState.CAUTION:
            return self.clear_block(block_id)
        if st == BlockState.RESERVED:
            return self.clear_block(block_id)
        return True

    def get_block_state(self, block_id: str) -> Optional[BlockState]:
        """Return the computed state for a block, or `None` if unknown."""
        block = self.blocks.get(block_id)
        return block.state if block else None

    def get_block_occupant(self, block_id: str) -> Optional[str]:
        """Return the train ID currently in a block, or `None`."""
        block = self.blocks.get(block_id)
        return block.occupied_by if block else None

    def is_block_available(
        self, block_id: str, for_train_id: Optional[str] = None
    ) -> bool:
        """Check if a block is available for occupation.

        Available if FREE, or already RESERVED/OCCUPIED by `for_train_id`
        (re-entry case).
        """
        block = self.blocks.get(block_id)
        if not block:
            return False
        if block.disabled:
            return False

        if block.state == BlockState.FREE:
            return True
        if for_train_id is not None and (
            block.occupied_by == for_train_id
            or block.reserved_for_train == for_train_id
        ):
            return True
        return False

    def occupy_blocks(self, block_ids: List[str], train_id: str) -> bool:
        """Atomically occupy multiple blocks. All-or-nothing with rollback.

        Returns True on full success. On any per-block failure, blocks
        successfully occupied earlier in this call are rolled back to FREE.
        """
        occupied: List[str] = []
        try:
            for block_id in block_ids:
                if not self.occupy_block(block_id, train_id):
                    raise ValueError(f"Failed to occupy block {block_id}")
                occupied.append(block_id)
            return True
        except Exception:
            for block_id in occupied:
                self.clear_block(block_id)
            return False

    # ------------------------------------------------------------------
    # New v1 public API
    # ------------------------------------------------------------------

    def reserve_blocks_for_path(
        self,
        block_ids: List[str],
        path_id: str,
        train_id: str,
        directions: Mapping[str, ReservedBlockTraversal],
    ) -> ReserveResult:
        """Atomically reserve a list of future blocks for a path.

        All-or-nothing. Eligibility for every requested block is checked
        before any field is written. On rejection, no state changes are
        made and the returned `ReserveResult` carries the offending block
        ID and a short failure reason.

        Eligibility per `design_new.md` (BlockManager section): a future
        block is eligible when
            block.disabled is False,
            block.occupied_by is None,
            (block.reserved_by_path is None
             or block.reserved_by_path == path_id),
            and the candidate `block_direction` is in
            block.allowed_travel_directions.

        `_is_caution == True` is NOT an eligibility failure; it affects the
        following train through the controller's caution speed cap rather
        than reservation eligibility.

        Blocks the train already physically occupies should not be in the
        candidate set (they are train-owned physical occupancy, not
        path-owned future reservation). Any such entry is rejected here.
        """
        # First pass: validate every block before writing any field.
        for block_id in block_ids:
            block = self.blocks.get(block_id)
            if block is None:
                return ReserveResult(
                    success=False,
                    failed_block_id=block_id,
                    failure_reason="unknown_block",
                )
            if block.disabled:
                return ReserveResult(
                    success=False,
                    failed_block_id=block_id,
                    failure_reason="disabled",
                )
            if block.occupied_by is not None:
                return ReserveResult(
                    success=False,
                    failed_block_id=block_id,
                    failure_reason="occupied",
                )
            if (
                block.reserved_by_path is not None
                and block.reserved_by_path != path_id
            ):
                return ReserveResult(
                    success=False,
                    failed_block_id=block_id,
                    failure_reason="reserved_by_other_path",
                )

            traversal = directions.get(block_id) if directions else None
            if traversal is None:
                return ReserveResult(
                    success=False,
                    failed_block_id=block_id,
                    failure_reason="missing_direction",
                )
            if traversal.block_direction not in block.allowed_travel_directions:
                return ReserveResult(
                    success=False,
                    failed_block_id=block_id,
                    failure_reason="direction_not_allowed",
                )

        # Second pass: commit. By here every block is eligible.
        for block_id in block_ids:
            block = self.blocks[block_id]
            traversal = directions[block_id]
            block.reserved_by_path = path_id
            block._reserve_for_path(train_id, traversal.exit_edge)
            self._record_history(
                block_id,
                "reserve",
                BlockState.FREE,
                block.state,
                train_id,
            )

        return ReserveResult(
            success=True,
            reserved_block_ids=list(block_ids),
        )

    def release_path_block_reservations(
        self,
        block_ids: List[str],
        path_id: str,
        completed: bool = False,
    ) -> None:
        """Release future-reservation ownership for a path on a set of blocks.

        Clears `reserved_by_path`, `reserved_direction`, `reserved_for_train`
        on each block where `block.reserved_by_path == path_id`. Mismatches
        emit a warning rather than raising.

        - `completed=True` means the path completed with the train fully
          stopped at the terminal; this method may additionally force-clear
          `_is_caution` and `caution_direction` on still-CAUTION matching
          owned blocks because the train is no longer moving.
        - `completed=False` means cancellation mid-flight; physical CAUTION
          state is left untouched so the safety margin survives until the
          normal directional reevaluation clears it.

        Never touches `Block.occupied_by` outside the `completed=True`
        caution-fields exception.
        """
        for block_id in block_ids:
            block = self.blocks.get(block_id)
            if block is None:
                # Quiet ignore for unknown IDs; nothing to release.
                continue
            if block.reserved_by_path != path_id:
                # Spec: emit a diagnostic, do not treat as a hard failure.
                print(
                    f"[BlockManager] release {block_id} but it's not reserved "
                    f"for {path_id} (current owner: {block.reserved_by_path!r})"
                )
                continue
            block.release_path_reservation()
            if completed and block._is_caution:
                block._is_caution = False
                block.caution_direction = None
            self._record_history(
                block_id,
                "release_reservation",
                BlockState.RESERVED,
                block.state,
                None,
            )

    def on_block_tail_vacated(
        self,
        block_id: str,
        train_id: str,
        direction: str,
    ) -> None:
        """Record a tail-vacate observation for a block.

        Called by `PathDispatcher.on_controller_progress(...)` after the
        controller-reported observation proves the train no longer occupies
        any tile in the block.

        - If the block does not exist, this is a no-op success (no diag).
        - If `block.occupied_by != train_id`, leave the block unchanged and
          emit a one-shot diagnostic.
        - Otherwise: clear `occupied_by`, set `_is_caution = True`, set
          `caution_direction = direction`, then call
          `_reevaluate_caution_blocks()` to cascade any ready-to-FREE blocks.
        """
        block = self.blocks.get(block_id)
        if block is None:
            return

        if block.occupied_by != train_id:
            other = block.occupied_by
            print(
                f"[BlockManager] block {block_id} release for train {train_id} "
                f"skipped: currently occupied by {other!r}"
            )
            return

        prior_state = block.state
        block.occupied_by = None
        block._is_caution = True
        block.caution_direction = direction
        self._record_history(
            block_id,
            "tail_vacated",
            prior_state,
            block.state,
            train_id,
        )
        self._reevaluate_caution_blocks()

    def on_switch_state_changed(self, switch_id: str) -> None:
        """Switch-side notification that connectivity may have changed.

        Called by `Switch` whenever its state mutates. Triggers a directional
        CAUTION reevaluation across all blocks. Safe to call before any
        blocks/switches have been registered (empty registry => no-op).
        """
        if not self.blocks:
            return
        self._reevaluate_caution_blocks()

    # ------------------------------------------------------------------
    # Private: caution reevaluation
    # ------------------------------------------------------------------

    def _reevaluate_caution_blocks(self) -> None:
        """Walk all CAUTION blocks and clear the ones whose forward
        protection chain no longer needs them.

        Wave 1 stub: the full algorithm (see `design_new.md`, section
        "`on_block_tail_vacated()` behaviour (directional CAUTION
        evaluation)") walks the shared `RailwayNetwork` topology graph
        through the vacated block's `caution_direction` exit edge, follows
        live tile connectivity through any intervening switch tile based on
        the current `Switch.state`, and clears `_is_caution` /
        `caution_direction` if the next reachable forward block is not
        OCCUPIED, or if no forward block is reachable at all.

        We do not yet have access to `RailwayNetwork` here (BlockManager
        construction wires only the block dict in v1 bootstrap); a follow-up
        wave will pass the network in and replace this body. For now the
        stub iterates `self.blocks.values()` and only clears caution when
        the block's `caution_direction` is missing entirely - i.e. nothing
        ahead can be protected because the direction was never recorded.
        That keeps the call side-effect free for normal state.
        """
        cleared: List[str] = []
        for block in self.blocks.values():
            if not block._is_caution:
                continue
            if block.caution_direction is None:
                # Nothing left to protect; clear caution metadata.
                block._is_caution = False
                cleared.append(block.block_id)

        for block_id in cleared:
            self._record_history(
                block_id,
                "caution_cleared",
                BlockState.CAUTION,
                self.blocks[block_id].state,
                None,
            )
