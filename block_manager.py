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
from typing import Callable, Optional, Dict, Set, Tuple, TYPE_CHECKING

from block import Block

if TYPE_CHECKING:
    from network import RailwayNetwork


# ============================================================================
# Canonical value types
# ============================================================================




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
        active_path_getter: Optional[Callable[[str], object]] = None,
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
            active_path_getter: optional callback taking a `train_id` and
                returning that train's active `Path` (or `None`) — the same one
                `SwitchManager` receives. Used only to fill a fatal
                block-conflict payload's `incoming_path_id` (always) and its
                `prior_owner_path_id` for the physical-owner conflict; the
                `reserved_for_other_train` conflict reads `prior_owner_path_id`
                from `block.reservation.path_id` instead (design_new.md
                "Block-manager model"). Stays `None` in setups that wired no
                path lookup.
        """
        self.blocks = blocks
        self.network = network
        self.notification_emitter = notification_emitter
        self.fatal_conflict_reporter = fatal_conflict_reporter
        self.active_path_getter = active_path_getter
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


    # ------------------------------------------------------------------
    # Reserved-corridor reverse index + de-energize queue
    # ------------------------------------------------------------------




    # ------------------------------------------------------------------
    # Existing primitive operations (kept; called by the new methods)
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Fatal-conflict reporting helper
    # ------------------------------------------------------------------








    # ------------------------------------------------------------------
    # New v1 public API
    # ------------------------------------------------------------------







    # ------------------------------------------------------------------
    # Private: caution reevaluation
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Topology walk helper for directional caution reevaluation
    # ------------------------------------------------------------------

    _EDGE_OFFSETS: Dict[str, Tuple[int, int]] = {
        "N": (0, 1),
        "E": (1, 0),
        "S": (0, -1),
        "W": (-1, 0),
    }









