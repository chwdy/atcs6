"""
Block State Manager - single source of truth for block occupancy.

`BlockManager` is the database-style lock manager for block resources and the
single serialization point for atomic block acquisition. It is the sole mutator
for `Block` physical state (`occupied_by`, `caution`) and for path-reservation
primitives (`reservation`). The convenience reads `_is_caution`,
`caution_exit_edge`, `reserved_by_path`, `reserved_direction`,
`reserved_for_train` are derived `@property` reads on `Block`.

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
from typing import Any, Optional, Dict, List, Mapping, Set, Tuple, TYPE_CHECKING

from block import Block, BlockState, CautionState
from runtime_payloads import (
    FatalPhysicalEntryConflict,
    FatalConflictKind,
    OccupyResult,
)
from track import EDGE_MASKS

if TYPE_CHECKING:
    from network import RailwayNetwork


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
        network: Optional["RailwayNetwork"] = None,
    ):
        """Initialize BlockManager with existing blocks.

        Args:
            blocks: Dictionary mapping block_id to Block instances. Used as a
                fallback registry when ``network`` is None.
            simulation_controller: optional back-reference used only to report
                fatal physical-entry conflicts via
                `enter_failed_runtime(...)`. Wired by the runtime once the
                controller is constructed; stays `None` in early bootstrap.
            network: optional `RailwayNetwork` reference. When provided, the
                manager prefers `network.blocks` and `network.switches` over
                the local fallback dict, and uses the shared topology graph
                for directional caution reevaluation.
        """
        self.blocks = blocks
        self.simulation_controller = simulation_controller
        self.network = network
        self.history: deque = deque(maxlen=100)

    # ------------------------------------------------------------------
    # Registry accessors (prefer network over fallback dict)
    # ------------------------------------------------------------------

    @property
    def _block_registry(self) -> Dict[str, Block]:
        """Authoritative block registry — `network.blocks` if wired."""
        if self.network is not None:
            return self.network.blocks
        return self.blocks

    @property
    def _switch_registry(self) -> Dict[str, object]:
        """Authoritative switch registry — `network.switches` if wired."""
        if self.network is not None:
            return self.network.switches
        return {}

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

        Conflicts (target block held against this train) are reported to
        ``SimulationController.enter_failed_runtime(...)`` via a typed
        ``FatalPhysicalEntryConflict`` payload, then return False.
        """
        block = self._block_registry.get(block_id)
        if not block:
            return False
        if block.disabled:
            self._report_fatal_conflict(
                block_id=block_id,
                train_id=train_id,
                conflict_kind=FatalConflictKind.DISABLED_RESOURCE_ENTRY,
                prior_owner_train_id=None,
            )
            return False

        prior_state = block.state

        if prior_state == BlockState.OCCUPIED:
            if block.occupied_by == train_id:
                return True
            self._report_fatal_conflict(
                block_id=block_id,
                train_id=train_id,
                conflict_kind=FatalConflictKind.OCCUPIED_BY_OTHER_TRAIN,
                prior_owner_train_id=block.occupied_by,
            )
            return False

        # Cross-train reservation conflict — checked regardless of whether
        # the computed state is RESERVED or CAUTION-with-reservation-underneath,
        # because CAUTION outranks RESERVED in `Block.state` priority so a
        # reservation can live under a caution and would otherwise slip
        # through the RESERVED-only branch.
        if (
            block.reserved_for_train is not None
            and block.reserved_for_train != train_id
        ):
            self._report_fatal_conflict(
                block_id=block_id,
                train_id=train_id,
                conflict_kind=FatalConflictKind.RESERVED_FOR_OTHER_TRAIN,
                prior_owner_train_id=block.reserved_for_train,
            )
            return False

        # Clean entry per design_new.md:717 — set physical occupancy, clear
        # stale caution, and consume any same-train reservation. The
        # reservation clear must run for every clean entry (FREE / RESERVED
        # / CAUTION) so a reservation hidden under a caution does not
        # survive the train and re-surface as RESERVED once the caution
        # clears. (`block.reservation = None` is a no-op when the field is
        # already None, so the FREE case is unaffected.)
        block.occupied_by = train_id
        block.caution = None
        block.reservation = None
        self._record_history(
            block_id, "occupy", prior_state, BlockState.OCCUPIED, train_id
        )
        return True

    # ------------------------------------------------------------------
    # Fatal-conflict reporting helper
    # ------------------------------------------------------------------

    def _report_fatal_conflict(
        self,
        *,
        block_id: str,
        train_id: str,
        conflict_kind: FatalConflictKind,
        prior_owner_train_id: Optional[str],
    ) -> None:
        """Build a `FatalPhysicalEntryConflict` and forward it to the runtime.

        Skips the call when no `simulation_controller` back-reference is
        wired (early bootstrap / unit tests that exercise `BlockManager`
        in isolation).
        """
        if self.simulation_controller is None:
            return
        payload = FatalPhysicalEntryConflict(
            resource_type="block",
            resource_id=block_id,
            incoming_train_id=train_id,
            prior_owner_train_id=prior_owner_train_id,
            conflict_kind=conflict_kind,
        )
        try:
            self.simulation_controller.enter_failed_runtime(payload)
        except Exception:
            # Receiver issues must not mask the underlying conflict.
            pass

    def set_block_to_caution(self, block_id: str) -> bool:
        """Set a block to CAUTION (legacy admin/test helper).

        Prefer `on_block_tail_vacated(...)` for the v1 runtime lifecycle entry
        point, which records a real cardinal `exit_edge`.

        Fix #35 (design_new.md): the empty-edge `CautionState(exit_edge="")`
        placeholder is acceptable here because `_reevaluate_caution_blocks`
        clears any direction-less record on the next pass. This path is
        unreachable from the runtime tick loop; it exists only for admin
        commands and legacy tests. The runtime always goes through
        `on_block_tail_vacated(block_id, train_id, direction)` with a real
        cardinal, so no direction-less caution survives across a reevaluation.
        """
        block = self._block_registry.get(block_id)
        if not block:
            return False

        prior_state = block.state
        if prior_state == BlockState.CAUTION:
            return True
        if prior_state == BlockState.OCCUPIED:
            prev_train = block.occupied_by
            block.occupied_by = None
            # Direction-less placeholder — cleared by next _reevaluate_caution_blocks.
            block.caution = CautionState(exit_edge="")
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
        block = self._block_registry.get(block_id)
        if not block:
            return False

        prior_state = block.state
        prev_train = block.occupied_by

        block.occupied_by = None
        block.caution = None
        block.reservation = None

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
        block = self._block_registry.get(block_id)
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
        block = self._block_registry.get(block_id)
        return block.state if block else None

    def direction_info(
        self, block_id: str, direction: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Resolve a block-relative `"forward"`/`"reverse"` direction to the
        concrete terminal tile and cardinal exit heading.

        Per design_new.md "Block model" — `Block.ordered_tiles` is the
        canonical traversal order; the forward terminal is the last entry
        and the reverse terminal is the first entry. The compass exit edge
        for that traversal is computed by `Block._resolve_reservation_direction`,
        which uses the adjacency between the last two (or first two) entries.

        Returns ``None`` when:
          - the block is unknown
          - the direction token is missing / unrecognized
          - the block lacks geometry to disambiguate (no `ordered_tiles`)

        Otherwise returns a dict with:
          - ``terminal_tile``: ``[x, y]`` of the terminal tile in that direction
          - ``terminal_heading``: ``"N"|"E"|"S"|"W"`` — the compass exit edge

        Used by the UI to render decorated direction labels (`F-(x,y)-E`)
        so operators can see the concrete map heading next to the
        block-relative token.
        """
        block = self._block_registry.get(block_id)
        if block is None or not block.ordered_tiles:
            return None
        if direction is None:
            return None

        token = str(direction).strip().lower()
        if token not in ("forward", "reverse"):
            return None

        heading = block._resolve_reservation_direction(token)
        if heading is None:
            return None

        terminal = block.ordered_tiles[-1] if token == "forward" else block.ordered_tiles[0]
        return {
            "terminal_tile": [int(terminal[0]), int(terminal[1])],
            "terminal_heading": str(heading).upper(),
        }

    def get_block_occupant(self, block_id: str) -> Optional[str]:
        """Return the train ID currently in a block, or `None`."""
        block = self._block_registry.get(block_id)
        return block.occupied_by if block else None

    def is_block_available(
        self, block_id: str, for_train_id: Optional[str] = None
    ) -> bool:
        """Check if a block is available for occupation.

        Available if FREE, or already RESERVED/OCCUPIED by `for_train_id`
        (re-entry case).
        """
        block = self._block_registry.get(block_id)
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

    def occupy_blocks(
        self, block_ids: List[str], train_id: str
    ) -> OccupyResult:
        """Atomically occupy multiple blocks. All-or-nothing with rollback.

        Per design_new.md "BlockManager" — returns an ``OccupyResult`` whose
        ``outcome`` is one of ``"clean_entry"`` (at least one block
        transitioned to OCCUPIED), ``"idempotent_noop"`` (every block was
        already occupied by the same ``train_id``), or ``"fatal_conflict"``
        (an entry conflict was detected and reported through
        ``SimulationController.enter_failed_runtime(...)``).

        ``OccupyResult.__bool__`` returns ``True`` when the outcome is not
        ``"fatal_conflict"``, so callers that use the result as a boolean
        continue to behave correctly.
        """
        occupied: List[str] = []
        any_transition = False
        for block_id in block_ids:
            block = self._block_registry.get(block_id)
            prior = block.state if block is not None else None
            ok = self.occupy_block(block_id, train_id)
            if not ok:
                # Rollback any clean-entry transitions performed earlier in
                # this call. The conflict has already been reported through
                # `_report_fatal_conflict` inside `occupy_block`.
                for prior_id in occupied:
                    self.clear_block(prior_id)
                return OccupyResult(
                    outcome="fatal_conflict",
                    reason=f"occupy {block_id} rejected",
                )
            occupied.append(block_id)
            if prior != BlockState.OCCUPIED:
                any_transition = True

        if not any_transition:
            return OccupyResult(outcome="idempotent_noop")
        return OccupyResult(outcome="clean_entry")

    # ------------------------------------------------------------------
    # New v1 public API
    # ------------------------------------------------------------------

    def reserve_blocks_for_path(
        self,
        block_ids: List[str],
        path_id: str,
        train_id: str,
        directions: Mapping[str, ReservedBlockTraversal],
        manual_override: bool = False,
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
            (manual_override is True
             OR the candidate `block_direction` is in
                block.allowed_travel_directions),
            and (block.manual is False OR manual_override is True).

        `_is_caution == True` is NOT an eligibility failure; it affects the
        following train through the controller's caution speed cap rather
        than reservation eligibility.

        `manual_override` is propagated from the calling
        `PathDispatcher.create_path(...)` / `extend_path(...)` per the
        `disabled` and `manual` availability flags canonical home in
        design_new.md (under TrackResource). Automatic callers omit this
        flag; only operator-initiated calls set it to True.

        Blocks the train already physically occupies should not be in the
        candidate set (they are train-owned physical occupancy, not
        path-owned future reservation). Any such entry is rejected here.
        """
        # First pass: validate every block before writing any field.
        for block_id in block_ids:
            block = self._block_registry.get(block_id)
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
            if block.manual and not manual_override:
                return ReserveResult(
                    success=False,
                    failed_block_id=block_id,
                    failure_reason="manual_no_override",
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
            if (
                not manual_override
                and traversal.block_direction not in block.allowed_travel_directions
            ):
                return ReserveResult(
                    success=False,
                    failed_block_id=block_id,
                    failure_reason="direction_not_allowed",
                )

        # Second pass: commit. By here every block is eligible.
        for block_id in block_ids:
            block = self._block_registry[block_id]
            traversal = directions[block_id]
            block.reserve_for_path(path_id, train_id, traversal.exit_edge)
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
    ) -> bool:
        """Release future-reservation ownership for a path on a set of blocks.

        Clears `reserved_by_path`, `reserved_direction`, `reserved_for_train`
        on each block where `block.reserved_by_path == path_id`. Mismatches
        emit a warning rather than raising.

        - `completed=True` means the path completed with the train fully
          stopped at the terminal; this method may additionally force-clear
          `block.caution` on still-CAUTION matching owned blocks because the
          train is no longer moving.
        - `completed=False` means cancellation mid-flight; physical CAUTION
          state is left untouched so the safety margin survives until the
          normal directional reevaluation clears it.

        Never touches `Block.occupied_by` outside the `completed=True`
        caution-fields exception.

        Per design_new.md: returns ``True`` if at least one matching
        reservation was released; ``False`` if none.
        """
        any_released = False
        for block_id in block_ids:
            block = self._block_registry.get(block_id)
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
            if completed and block.caution is not None:
                block.caution = None
            self._record_history(
                block_id,
                "release_reservation",
                BlockState.RESERVED,
                block.state,
                None,
            )
            any_released = True
        return any_released

    def on_block_tail_vacated(
        self,
        block_id: str,
        train_id: str,
        direction: str,
    ) -> bool:
        """Record a tail-vacate observation for a block.

        Called by `PathDispatcher.on_controller_progress(...)` after the
        controller-reported observation proves the train no longer occupies
        any tile in the block.

        Per design_new.md: returns ``True`` on a clean tail-vacate;
        ``False`` on the no-op cases (missing block in registry, mismatched
        owner — see below).

        - If the block does not exist, returns ``False`` (silent no-op).
        - If `block.occupied_by != train_id`, leaves the block unchanged,
          emits a one-shot diagnostic, returns ``False``.
        - Otherwise: clear `occupied_by`, set
          `block.caution = CautionState(exit_edge=direction)`, then call
          `_reevaluate_caution_blocks()` to cascade any ready-to-FREE blocks
          and returns ``True``.
        """
        block = self._block_registry.get(block_id)
        if block is None:
            return False

        if block.occupied_by != train_id:
            other = block.occupied_by
            print(
                f"[BlockManager] block {block_id} release for train {train_id} "
                f"skipped: currently occupied by {other!r}"
            )
            return False

        prior_state = block.state
        block.occupied_by = None
        block.caution = CautionState(exit_edge=direction)
        self._record_history(
            block_id,
            "tail_vacated",
            prior_state,
            block.state,
            train_id,
        )
        self._reevaluate_caution_blocks()
        return True

    def on_switch_position_changed(self, switch_id: str) -> None:
        """Switch-side notification that connectivity may have changed.

        Called by `Switch` whenever its position mutates. Triggers a directional
        CAUTION reevaluation across all blocks. Safe to call before any
        blocks/switches have been registered (empty registry => no-op).
        """
        if not self._block_registry:
            return
        self._reevaluate_caution_blocks()

    # ------------------------------------------------------------------
    # Private: caution reevaluation
    # ------------------------------------------------------------------

    def _reevaluate_caution_blocks(self) -> None:
        """Walk all CAUTION blocks and clear the ones whose forward
        protection chain no longer needs them.

        Per design_new.md "`on_block_tail_vacated()` behaviour (directional
        CAUTION evaluation)": for each block currently in CAUTION, follow
        live tile connectivity from the block's exit tile in
        ``block.caution.exit_edge`` through any intervening switch (using the
        current ``Switch.position``) until the next reachable block resource is
        found. If that next reachable block is OCCUPIED by a train whose
        current ``travel_direction`` is the same as ``block.caution.exit_edge``,
        keep caution — the train ahead is still moving away in the protect
        direction. If it is FREE/RESERVED/CAUTION, if no reachable block
        exists at all, or if the OCCUPIED forward block holds a train whose
        ``travel_direction`` is the opposite of ``block.caution.exit_edge``
        (train has reversed its cab and is no longer moving away), clear
        caution by writing ``block.caution = None``.

        When the manager has no network back-reference (early bootstrap or
        unit tests), the only safe behaviour is to clear caution entries that
        never recorded a direction — there is no topology graph to walk.
        """
        registry = self._block_registry
        cleared: List[str] = []
        for block in list(registry.values()):
            if block.caution is None:
                continue
            edge = block.caution.exit_edge
            # No direction recorded (empty edge). Fix #35 (design_new.md):
            # `set_block_to_caution` that wrote CautionState(exit_edge="") is
            # removed; this guard handles any caution records with a missing
            # direction (e.g. from old persisted state or deserialization).
            if not edge:
                block.caution = None
                cleared.append(block.block_id)
                continue
            # Without a network we cannot walk topology; bail safely.
            if self.network is None:
                continue
            next_block = self._next_block_along_direction(block, edge)
            if next_block is None:
                block.caution = None
                cleared.append(block.block_id)
                continue
            if next_block.state != BlockState.OCCUPIED:
                block.caution = None
                cleared.append(block.block_id)
                continue
            if self._forward_train_has_reversed(next_block, edge):
                block.caution = None
                cleared.append(block.block_id)

        for block_id in cleared:
            self._record_history(
                block_id,
                "caution_cleared",
                BlockState.CAUTION,
                registry[block_id].state,
                None,
            )

    def _forward_train_has_reversed(
        self, next_block: Block, caution_exit_edge: str
    ) -> bool:
        """Return True when the OCCUPIED ``next_block`` holds a train whose
        current ``travel_direction`` is the opposite of ``caution_exit_edge``.

        The CAUTION block's protection chain was built assuming the train
        ahead is still moving away in ``caution_exit_edge``. If the train
        has since flipped its cab so its travel direction now points back
        through the CAUTION block, the protection is stale.

        Returns False (preserve current keep-caution behaviour) whenever the
        train, its controller, or its travel direction cannot be resolved —
        the call must never escalate to a clear without positive evidence
        of reversal.
        """
        train_id = next_block.occupied_by
        if train_id is None:
            return False
        if self.simulation_controller is None:
            return False
        get_controller = getattr(
            self.simulation_controller, "get_controller", None
        )
        if get_controller is None:
            return False
        controller = get_controller(train_id)
        if controller is None:
            return False
        travel_direction = getattr(controller, "travel_direction", None)
        if not isinstance(travel_direction, str) or not travel_direction:
            return False
        opposite = self._OPPOSITE_EDGE.get(caution_exit_edge.upper())
        if opposite is None:
            return False
        return travel_direction.upper() == opposite

    # ------------------------------------------------------------------
    # Topology walk helper for directional caution reevaluation
    # ------------------------------------------------------------------

    _EDGE_OFFSETS: Dict[str, Tuple[int, int]] = {
        "N": (0, 1),
        "E": (1, 0),
        "S": (0, -1),
        "W": (-1, 0),
    }
    _OPPOSITE_EDGE: Dict[str, str] = {"N": "S", "S": "N", "E": "W", "W": "E"}

    def _next_block_along_direction(
        self, block: Block, direction: str
    ) -> Optional[Block]:
        """Return the next reachable block when walking forward from ``block``
        through its ``direction`` exit edge.

        Walks tile connectivity through any intervening switch tiles using
        the current ``Switch.position``. Returns None if the walk runs off the
        network, hits a dead end, or loops back into the same block.
        """
        if self.network is None:
            return None
        direction = direction.upper() if direction else None
        if direction not in self._EDGE_OFFSETS:
            return None

        # Locate the exit tile inside `block` for this direction: the tile
        # whose own track has an edge in `direction` AND whose neighbour in
        # `direction` is not part of the same block. Filtering on the track's
        # own edge mask is essential — a tile like TRACK_NS has no E edge, so
        # its eastern grid-neighbour is irrelevant even if that cell is empty.
        edge_mask = EDGE_MASKS.get(direction, 0)
        exit_tile: Optional[Tuple[int, int]] = None
        for entry in block.ordered_tiles:
            tx, ty = int(entry[0]), int(entry[1])
            tile = self.network.get_track(tx, ty)
            if tile is None or not (tile.track_bits & edge_mask):
                continue
            neighbour = self.network.get_neighbor(tx, ty, direction)
            if neighbour is None or neighbour.block_section_id != block.block_id:
                exit_tile = (tx, ty)
                break
        if exit_tile is None:
            return None

        # Step out into the neighbour tile and walk forward.
        first_neighbour = self.network.get_neighbor(
            exit_tile[0], exit_tile[1], direction
        )
        if first_neighbour is None:
            return None
        cur_x, cur_y = first_neighbour.x, first_neighbour.y
        entry_edge = self._OPPOSITE_EDGE[direction]
        visited: Set[Tuple[int, int]] = set()

        while True:
            if (cur_x, cur_y) in visited:
                return None
            visited.add((cur_x, cur_y))
            tile = self.network.get_track(cur_x, cur_y)
            if tile is None:
                return None
            # Plain block tile: this is the next reachable block.
            if tile.block_section_id and not tile.is_switch:
                return self._block_registry.get(tile.block_section_id)
            if tile.block_section_id and tile.is_switch:
                # Block-internal switch tile: still part of a block; treat as
                # reaching that block.
                return self._block_registry.get(tile.block_section_id)
            # Bare switch tile (boundary between blocks). Cross via the live
            # active exit determined by the current Switch.position.
            if tile.is_switch:
                exits = tile.get_exits(entry_edge, use_switch_state=True)
                if not exits:
                    return None
                exit_edge = exits[0]
                step = self.network.get_neighbor(cur_x, cur_y, exit_edge)
                if step is None:
                    return None
                cur_x, cur_y = step.x, step.y
                entry_edge = self._OPPOSITE_EDGE[exit_edge]
                continue
            # Plain track tile not assigned to any block: nothing protectable
            # ahead.
            return None
