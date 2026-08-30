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
from typing import (
    Any,
    Callable,
    Optional,
    Dict,
    List,
    Mapping,
    Set,
    Tuple,
    TYPE_CHECKING,
)

from block import Block, BlockState, CautionState
from notification_center import Kind, block_ref, train_ref
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
    points at the offending block and `reason` is a short tag for
    diagnostics.
    """

    success: bool
    failed_block_id: Optional[str] = None
    reason: Optional[str] = None
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
        network: Optional["RailwayNetwork"] = None,
        *,
        notification_emitter: Optional[Callable] = None,
        fatal_conflict_reporter: Optional[Callable] = None,
    ):
        """Initialize BlockManager with existing blocks.

        Per design_new.md "Block-manager model", the manager holds the network
        and two narrow injected callbacks only; it never receives or looks up
        `SimulationController`.

        Args:
            blocks: Dictionary mapping block_id to Block instances. Used as a
                fallback registry when ``network`` is None.
            network: optional `RailwayNetwork` reference. When provided, the
                manager prefers `network.blocks` and `network.switches` over
                the local fallback dict, and uses the shared topology graph
                for directional caution reevaluation.
            notification_emitter: optional narrow callback
                `(operation, kind, message, entities, error=None, payload=None)`
                that routes a manager-owned event into `NotificationCenter`.
                The runtime wires it to `NotificationCenter.emit(...)` with the
                source fixed to "BlockManager"; it stays `None` in early
                bootstrap and isolated unit tests, where the emit is skipped.
            fatal_conflict_reporter: optional callback taking one
                `FatalPhysicalEntryConflict`, wired by the runtime to
                `SimulationController.enter_failed_runtime(...)`. Stays `None`
                in early bootstrap / isolated tests, where a conflict just
                returns False without reporting.
        """
        self.blocks = blocks
        self.network = network
        self.notification_emitter = notification_emitter
        self.fatal_conflict_reporter = fatal_conflict_reporter
        self.history: deque = deque(maxlen=100)
        # Reverse index: train_id -> set of block ids it has reserved ahead.
        # Maintained at every reservation mutation in this class so TrainPhysics
        # can read a train's reserved corridor in O(reserved) instead of
        # scanning every block each tick. Occupied blocks are NOT tracked here —
        # physics reads those from its own movement scratchpad.
        self._reserved_by_train: Dict[str, Set[str]] = {}
        # Blocks whose holder was cleared this tick (tail vacate / reservation
        # release / clear). Drained once after every train has emitted voltage;
        # each is re-checked and dropped to 0V only if still unheld. This is the
        # event-driven replacement for an all-blocks de-energize sweep.
        self._blocks_pending_deenergize: Set[str] = set()

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
    # Reserved-corridor reverse index + de-energize queue
    # ------------------------------------------------------------------

    def _index_add_reserved(self, block_id: str, train_id: Optional[str]) -> None:
        """Record that `train_id` reserved `block_id` (no-op when train None)."""
        if train_id is None:
            return
        self._reserved_by_train.setdefault(train_id, set()).add(block_id)

    def _index_drop_reserved(self, block_id: str, train_id: Optional[str]) -> None:
        """Forget a reservation of `block_id` by `train_id` (no-op when None)."""
        if train_id is None:
            return
        held = self._reserved_by_train.get(train_id)
        if held is not None:
            held.discard(block_id)
            if not held:
                del self._reserved_by_train[train_id]

    def reserved_blocks_for_train(self, train_id: str) -> Set[str]:
        """Block ids this train has reserved ahead, in O(reserved) — the
        canonical reverse of `Block.reserved_for_train`, maintained at every
        reservation mutation here. TrainPhysics unions this with the train's
        occupied span to get its held set each tick (no all-blocks scan).
        """
        return set(self._reserved_by_train.get(train_id, ()))

    def _mark_for_deenergize(self, block_id: str) -> None:
        """Queue a block whose holder was just cleared for an end-of-tick 0V
        check (drained by `take_blocks_to_deenergize`)."""
        self._blocks_pending_deenergize.add(block_id)

    def take_blocks_to_deenergize(self) -> List[str]:
        """Return and clear the block ids queued this tick that are *still*
        unheld, so the caller drives them to 0V.

        A block is one power feed, so it stays energized while it is held —
        occupied or reserved by any train — and is powered off only when no
        train holds it (design_new.md "Power"). Re-checking the live block
        fields here — not the queued snapshot — means a block vacated and then
        re-held within the same tick stays energized. This is the event-driven
        replacement for the former all-blocks de-energize sweep.
        """
        pending = self._blocks_pending_deenergize
        self._blocks_pending_deenergize = set()
        unheld: List[str] = []
        for block_id in sorted(pending):
            block = self._block_registry.get(block_id)
            if block is None:
                continue
            if not self._block_is_held(block_id):
                unheld.append(block_id)
        return unheld

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
        # Consuming a same-train reservation: drop it from the reserved index
        # (the block is now occupied, tracked by physics' span, not reserved).
        # Read `reserved_for_train` before clearing `reservation`.
        self._index_drop_reserved(block_id, block.reserved_for_train)
        block.reservation = None
        # An unidentified train holds a block but commands no voltage, so entry by an unidentified train
        # queues the feed for the end-of-tick 0V check instead of energizing it
        # (design_new.md "Unidentified trains", Never energized).
        if self._is_unidentified_train(train_id):
            self._mark_for_deenergize(block_id)
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

        Skips the call when no `fatal_conflict_reporter` is wired (early
        bootstrap / unit tests that exercise `BlockManager` in isolation).
        """
        if self.fatal_conflict_reporter is None:
            return
        payload = FatalPhysicalEntryConflict(
            resource_type="block",
            resource_id=block_id,
            incoming_train_id=train_id,
            prior_owner_train_id=prior_owner_train_id,
            conflict_kind=conflict_kind,
        )
        try:
            self.fatal_conflict_reporter(payload)
        except Exception:
            # Receiver issues must not mask the underlying conflict.
            pass

    def _block_is_held(self, block_id: str) -> bool:
        """True when `block_id` is still held — occupied or reserved by any
        train.

        A block is one power feed (design_new.md "One train per Block (power
        domain)"), so it stays energized while it is held and is powered off
        only once nothing holds it.

        An unidentified train does not count as a holder for this test: it commands no
        voltage, and a block held only by an unidentified train is driven to `0` (design_new.md
        "Unidentified trains", Never energized).
        """
        block = self._block_registry.get(block_id)
        if block is None:
            return False
        if block.reserved_for_train is not None:
            return True
        occupant = block.occupied_by
        return occupant is not None and not self._is_unidentified_train(occupant)

    def _is_unidentified_train(self, train_id: Optional[str]) -> bool:
        """True when `train_id` names an unidentified train (design_new.md "Unidentified train
        trains"). False whenever the network is not wired, which keeps the
        power tests conservative in isolated unit tests."""
        if train_id is None or self.network is None:
            return False
        train = getattr(self.network, "trains", {}).get(train_id)
        return bool(train is not None and getattr(train, "is_unidentified", False))

    def _emit_diagnostic(
        self, message: str, *, operation: str, block_id: str,
        train_id: Optional[str] = None,
    ) -> None:
        """Emit a runtime diagnostic through `NotificationCenter`.

        Per design_new.md, `NotificationCenter` is the only runtime sink;
        skipped no-op release paths surface their warnings as `warning`-level
        events naming the function that produced them, rather than
        component-local logging. Skips the emit when no `notification_emitter`
        is wired (early bootstrap / isolated unit tests); a receiver error must
        not mask the underlying operation.
        """
        if self.notification_emitter is None:
            return
        try:
            self.notification_emitter(
                operation=operation,
                kind=Kind.WARNING,
                message=message,
                entities=(
                    ([train_ref(train_id)] if train_id else [])
                    + ([block_ref(block_id)] if block_id else [])
                ),
            )
        except Exception:
            pass

    def _emit_event(
        self, operation: str, *, message: str, block_id: str,
        train_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit an operational event-log notification through
        `NotificationCenter` (design_new.md "Event log": the log records
        `block_entered` / `block_vacated` among the operational kinds).

        Best-effort, mirroring `_emit_diagnostic`: no-ops when no
        `notification_emitter` is wired (early bootstrap / isolated unit
        tests), and never raises into the caller. `block_id` always rides on
        the payload; callers may add more fields (e.g. the vacate `exit_edge`).
        """
        if self.notification_emitter is None:
            return
        body: Dict[str, Any] = {"block_id": block_id}
        if payload:
            body.update(payload)
        try:
            self.notification_emitter(
                operation=operation,
                kind=Kind.INFO,
                message=message,
                entities=(
                    ([train_ref(train_id)] if train_id else [])
                    + [block_ref(block_id)]
                ),
                payload=body,
            )
        except Exception:
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
            self._mark_for_deenergize(block_id)
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
        self._index_drop_reserved(block_id, block.reserved_for_train)
        block.reservation = None
        self._mark_for_deenergize(block_id)

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
                # design_new.md "Event log": the train entered (occupied) a
                # block. Emit once per real (FREE/RESERVED/CAUTION) -> OCCUPIED
                # transition, not on an idempotent re-occupy of a block the
                # train already holds.
                self._emit_event(
                    "occupy_blocks",
                    message=f"{train_id} entered {block_id}",
                    block_id=block_id,
                    train_id=train_id,
                )

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
                    reason="unknown_block",
                )
            if block.disabled:
                return ReserveResult(
                    success=False,
                    failed_block_id=block_id,
                    reason="disabled",
                )
            if block.manual and not manual_override:
                return ReserveResult(
                    success=False,
                    failed_block_id=block_id,
                    reason="manual_no_override",
                )
            if block.occupied_by is not None:
                return ReserveResult(
                    success=False,
                    failed_block_id=block_id,
                    reason="occupied",
                )
            if (
                block.reserved_by_path is not None
                and block.reserved_by_path != path_id
            ):
                return ReserveResult(
                    success=False,
                    failed_block_id=block_id,
                    reason="reserved_by_other_path",
                )
            traversal = directions.get(block_id) if directions else None
            if traversal is None:
                return ReserveResult(
                    success=False,
                    failed_block_id=block_id,
                    reason="missing_direction",
                )
            if (
                not manual_override
                and traversal.block_direction not in block.allowed_travel_directions
            ):
                return ReserveResult(
                    success=False,
                    failed_block_id=block_id,
                    reason="direction_not_allowed",
                )

        # Second pass: commit. By here every block is eligible.
        for block_id in block_ids:
            block = self._block_registry[block_id]
            traversal = directions[block_id]
            block.reserve_for_path(path_id, train_id, traversal.exit_edge)
            # Opposing-traversal caution clear (design_new.md "Opposing-traversal
            # caution clear"): a block is a switch-free two-ended run, so this
            # reserved exit_edge either equals block.caution.exit_edge (the
            # reserving train traverses the way the departing train went — keep
            # the margin, which the CAUTION_SPEED_LIMIT_KMH cap exists for) or is
            # the block's other end (it traverses away from the protected chain,
            # whose margin now sits behind it and restricts nothing ahead — retire
            # it, so the computed state becomes RESERVED). No _reevaluate_caution
            # cascade is needed: clearing one block's caution changes no block's
            # occupied_by, which is the only thing the reevaluator's keep test reads.
            if (
                block.caution is not None
                and traversal.exit_edge != block.caution.exit_edge
            ):
                block.caution = None
            self._index_add_reserved(block_id, train_id)
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

    def matching_path_reservation_ids(
        self, block_ids: List[str], path_id: str
    ) -> List[str]:
        """Read-only projection: the IDs in `block_ids` (in input order) that
        exist and currently satisfy `block.reserved_by_path == path_id`
        (design_new.md "BlockManager").

        `ResourceCoordinator.release_path_resources(...)` uses it to drop expected
        already-released blocks before calling `release_path_block_reservations`,
        so those never emit a mismatch warning, and to reach reservation state
        without reaching through `BlockManager` to the network or block registry.
        """
        matching: List[str] = []
        for block_id in block_ids:
            block = self._block_registry.get(block_id)
            if block is not None and block.reserved_by_path == path_id:
                matching.append(block_id)
        return matching

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
                self._emit_diagnostic(
                    f"release {block_id} but it's not reserved for {path_id} "
                    f"(current owner: {block.reserved_by_path!r})",
                    operation="release_path_blocks",
                    block_id=block_id,
                )
                continue
            reserved_train = block.reserved_for_train
            block.release_path_reservation()
            self._index_drop_reserved(block_id, reserved_train)
            self._mark_for_deenergize(block_id)
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
        direction: Optional[str],
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
        - Otherwise: clear `occupied_by`; when `direction` is an edge, set
          `block.caution = CautionState(exit_edge=direction)`; then call
          `_reevaluate_caution_blocks()` to cascade any ready-to-FREE blocks
          and returns ``True``.

        ``direction`` is the vacated block's forward `exit_edge`, or ``None``
        when the train left the detected area and no exit edge is known
        (design_new.md ":1043"). With no edge there is no forward neighbour to
        consult and no direction to protect, so no caution is written and the
        block's computed state goes straight to FREE.
        """
        block = self._block_registry.get(block_id)
        if block is None:
            return False

        if block.occupied_by != train_id:
            other = block.occupied_by
            self._emit_diagnostic(
                f"block {block_id} release for train {train_id} "
                f"skipped: currently occupied by {other!r}",
                operation="on_block_tail_vacated",
                block_id=block_id,
                train_id=train_id,
            )
            return False

        prior_state = block.state
        block.occupied_by = None
        block.caution = CautionState(exit_edge=direction) if direction else None
        self._mark_for_deenergize(block_id)
        # design_new.md "Event log": the train vacated a block (tail cleared).
        # Carries the exit edge so the log / replay can render direction.
        self._emit_event(
            "on_block_tail_vacated",
            message=f"{train_id} vacated {block_id}",
            block_id=block_id,
            train_id=train_id,
            payload={"exit_edge": direction},
        )
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

    def release_train_occupancy(self, train_id: str) -> List[str]:
        """Drop every block `train_id` occupies, without leaving CAUTION behind.

        `Block.occupied_by` is cleared by `on_block_tail_vacated()` on the
        ordinary detector-confirmed path. This is the teardown path for the two
        cases that have no detector observation to wait for: the ghosted-train
        teardown and the unidentified-train adoption re-key (design_new.md
        "BlockManager").

        It writes no `caution` — there is no train travelling on to protect
        behind — and then runs `_reevaluate_caution_blocks()`, so a chain that
        was held in CAUTION protecting this train cannot stay there forever.

        Returns the ids it released, most useful for a caller that wants to
        report what it took back.
        """
        released: List[str] = []
        for block_id, block in list(self.blocks.items()):
            if block.occupied_by != train_id:
                continue
            block.occupied_by = None
            block.caution = None
            self._mark_for_deenergize(block_id)
            released.append(block_id)
        if released:
            self._reevaluate_caution_blocks()
        return released

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
            next_block, chain_entry_edge = self._next_block_along_direction(
                block, edge
            )
            if next_block is None or chain_entry_edge is None:
                block.caution = None
                cleared.append(block.block_id)
                continue
            if next_block.state != BlockState.OCCUPIED:
                block.caution = None
                cleared.append(block.block_id)
                continue
            # An OCCUPIED forward block keeps this CAUTION: a train is still
            # standing in the protected chain. Retiring a margin the protecting
            # train has reversed away from is NOT this walk's job — that is the
            # Opposing-traversal caution clear in reserve_blocks_for_path
            # (design_new.md: the reevaluator reads no train, controller, or
            # travel_direction state; occupancy of the one forward block is the
            # whole test).

        for block_id in cleared:
            self._record_history(
                block_id,
                "caution_cleared",
                BlockState.CAUTION,
                registry[block_id].state,
                None,
            )

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

    def adjacent_blocks(self, block_id: str) -> List[str]:
        """The blocks reachable out of this block's ends, points as aligned.

        Walks each of the four compass exits through `_next_block_along_direction`,
        so it follows the switches as they are actually set rather than as any
        plan describes them, and returns the distinct blocks found. A switch-free
        run has at most two — one out each end — which is what
        `services.train_service` resolves a calibration stretch from
        (design_new.md "Speed calibration", The operator names the measured block).
        """
        block = self.blocks.get(block_id) if self.blocks else None
        if block is None:
            return []
        found: List[str] = []
        for direction in self._EDGE_OFFSETS:
            neighbour, _entry_edge = self._next_block_along_direction(block, direction)
            if neighbour is None:
                continue
            if neighbour.block_id == block_id or neighbour.block_id in found:
                continue
            found.append(neighbour.block_id)
        return found

    def _next_block_along_direction(
        self, block: Block, direction: str
    ) -> Tuple[Optional[Block], Optional[str]]:
        """Return ``(next_block, chain_entry_edge)`` when walking forward from
        ``block`` through its ``direction`` exit edge.

        ``next_block`` is the next reachable block resource. ``chain_entry_edge``
        is the edge through which this walk actually entered that block (per
        design_new.md line 842): for a straight adjacent block this is simply
        the side facing ``block``, but through switch tiles and curves it is the
        entry edge the walk used as it crossed switch legs. The caller compares
        the occupant train's ``travel_direction`` against this entry edge to
        decide CAUTION -> FREE, so the walk must hand it back rather than letting
        the caller fall back to the geometric opposite of ``direction``.

        Walks tile connectivity through any intervening switch tiles using
        the current ``Switch.position``. Returns ``(None, None)`` if the walk
        runs off the network, hits a dead end, or loops back into the same
        block.
        """
        if self.network is None:
            return (None, None)
        direction = direction.upper() if direction else None
        if direction not in self._EDGE_OFFSETS:
            return (None, None)

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
            if neighbour is None or neighbour.block_id != block.block_id:
                exit_tile = (tx, ty)
                break
        if exit_tile is None:
            return (None, None)

        # Step out into the neighbour tile and walk forward. `entry_edge`
        # tracks the edge through which the walk enters the current tile; it
        # starts as the geometric opposite of the CAUTION block's exit edge and
        # is updated as the walk crosses each switch leg, so when the walk
        # reaches the next block tile it holds that block's actual chain-entry
        # edge.
        first_neighbour = self.network.get_neighbor(
            exit_tile[0], exit_tile[1], direction
        )
        if first_neighbour is None:
            return (None, None)
        cur_x, cur_y = first_neighbour.x, first_neighbour.y
        entry_edge = self._OPPOSITE_EDGE[direction]
        visited: Set[Tuple[int, int]] = set()

        while True:
            if (cur_x, cur_y) in visited:
                return (None, None)
            visited.add((cur_x, cur_y))
            tile = self.network.get_track(cur_x, cur_y)
            if tile is None:
                return (None, None)
            # Plain block tile: this is the next reachable block.
            if tile.block_id and not tile.is_switch:
                return (self._block_registry.get(tile.block_id), entry_edge)
            if tile.block_id and tile.is_switch:
                # Block-internal switch tile: still part of a block; treat as
                # reaching that block.
                return (self._block_registry.get(tile.block_id), entry_edge)
            # Bare switch tile (boundary between blocks). Cross via the live
            # active exit determined by the current Switch.position.
            if tile.is_switch:
                exits = tile.get_exits(entry_edge, use_switch_state=True)
                if not exits:
                    return (None, None)
                exit_edge = exits[0]
                step = self.network.get_neighbor(cur_x, cur_y, exit_edge)
                if step is None:
                    return (None, None)
                cur_x, cur_y = step.x, step.y
                entry_edge = self._OPPOSITE_EDGE[exit_edge]
                continue
            # Plain track tile not assigned to any block: nothing protectable
            # ahead.
            return (None, None)
