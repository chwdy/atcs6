"""
Runtime Payloads — shared dataclasses for cross-component runtime contracts.

Per design_new.md and the parallel-migration plan section II.5, these payload
schemas are the single canonical home for the data structures shared between
TrainController, TrainPhysics, PathDispatcher, RouteExecutor, and
SimulationController. Other modules import from here rather than defining
their own copies.

Schemas defined here:
- PhysicsSpanUpdate: from TrainPhysics.tick(...) into TrainController.on_physics_updated
- TileTraversal: per-tile traversal entry inside a PhysicsSpanUpdate (added/removed_tiles)
- ResourceEvent: a single entered/tail-vacated resource observation
- ControllerProgressReport: produced by TrainController, consumed by PathDispatcher.on_controller_progress
- PendingPathCancelRequest:
  the per-train controller mailbox payloads

`TrainSpan` itself is referenced as a forward type and not defined in this module
because it is the canonical span schema owned by the train/controller side. This
module imports it under TYPE_CHECKING to avoid runtime circular imports.
"""

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

if TYPE_CHECKING:
    # TrainSpan and its component schemas (SpanEndLocator) are owned by the
    # train/controller side. Until they are formally defined elsewhere, we
    # type-check against `Any` and let producers/consumers carry whichever
    # concrete shape they currently use.
    pass


# =============================================================================
# Path priority constants
# =============================================================================

DEFAULT_PATH_PRIORITY = 5
MIN_PRIORITY = 1
MAX_PRIORITY = 10


# The two event levels a latch writer may declare (design_new.md
# "Writer-helper rule": no latch writer uses `"error"`). Spelled as literals
# rather than imported from `notification_center.Kind` because that module
# imports `ErrorRecord` from here — the import may only run one way.
_KIND_INFO = "info"
_KIND_WARNING = "warning"


# =============================================================================
# Per-run / per-task error record
# =============================================================================


@dataclass
class ErrorRecord:
    """The single diagnostic-error object (design_new.md "Design Direction",
    Errors and diagnostic classification).

    It carries exactly two fields, always written whole — both present, or the
    whole record is `None`:

    - ``text``: the human-readable failure detail.
    - ``origin``: a free-form label naming what produced the error (``"eb"``,
      ``"out_of_sequence"``, ``"switch_intrusion"``, ``"operator_cancelled"``,
      ``"planning_failed"``, ``"no_viable_path"``). Diagnostic only: no runtime
      branch reads it, and writers may use labels beyond the ones the document
      names. It is the field a reader groups errors by when ``operation`` alone
      is too coarse.

    It carries no timestamp and no severity of its own: a record is never
    stored on an entity, it rides in the ``error`` field of one runtime event,
    and that event owns the time (``event_time``) and the level (``kind``).
    """

    text: str
    origin: str

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "origin": self.origin}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ErrorRecord":
        return cls(text=str(data["text"]), origin=str(data.get("origin", "")))


# =============================================================================
# Physics → Controller payload
# =============================================================================



# =============================================================================
# Controller → Dispatcher progress report
# =============================================================================



# =============================================================================
# Action mailbox payloads
# =============================================================================



# Note: per design_new.md (Latch Table), `pending_route_abort` and
# `pending_route_deactivate` are bare-bool latches on `TrainController`. The
# diagnostic detail for the originating action is recorded only on the
# `cancel_path` and `command_result` notifications (the inclusive
# `pending_path_cancel_request.error` carries it through to the cancellation
# event), not on any field on `RouteRun` or on the route-abort / deactivate
# latches themselves.



# =============================================================================
# Path completion result
# =============================================================================



# =============================================================================
# Wave 2: PathDispatcher acquisition contract
# =============================================================================



# =============================================================================
# Fatal conflict diagnostic schemas
# =============================================================================


class FatalConflictKind(Enum):
    """Diagnostic categories for fatal physical-entry conflicts surfaced by
    the runtime.

    Per design_new.md "Fatal physical-entry conflict procedure" v1 values:
    a train tried to enter a block already held by another train, a block
    reserved for another train, a switch already occupied by another train,
    a resource (block/switch) flagged disabled, a switch physical-entry
    event arrived without a route key (which the dispatcher must always
    supply for an interior-switch tile), or a train physically entered a
    switch for which it holds no compatible main lock (a switch intrusion).
    """

    OCCUPIED_BY_OTHER_TRAIN = "occupied_by_other_train"
    RESERVED_FOR_OTHER_TRAIN = "reserved_for_other_train"
    SWITCH_OCCUPIED_BY_OTHER_TRAIN = "switch_occupied_by_other_train"
    DISABLED_RESOURCE_ENTRY = "disabled_resource_entry"
    MALFORMED_ROUTE_KEY = "malformed_route_key"
    # design_new.md "Switch entity": a train physically on a switch must hold a
    # compatible main lock for the route it occupies; entering with no such lock
    # (and not a same-train already-covered seeded span) is a fatal intrusion.
    SWITCH_INTRUSION = "switch_intrusion"


@dataclass
class FatalPhysicalEntryConflict:
    """Fatal conflict report when a train would enter a resource held against
    it, or enters a disabled resource.

    Per design_new.md "Fatal physical-entry conflict procedure":
    - ``resource_type``: ``"block"`` or ``"switch"``.
    - ``resource_id``: the conflicting block or switch ID.
    - ``incoming_train_id``: the train attempting the entry.
    - ``prior_owner_train_id``: the previously recorded owner (or reservation
      holder) for conflicting train ownership; ``None`` when
      ``conflict_kind == FatalConflictKind.DISABLED_RESOURCE_ENTRY``.
    - ``conflict_kind``: the ``FatalConflictKind`` diagnostic category.
    - ``incoming_path_id`` / ``prior_owner_path_id``: active path IDs at the
      point of detection, when known.
    """

    resource_type: str  # "block" | "switch"
    resource_id: str
    incoming_train_id: str
    prior_owner_train_id: Optional[str]
    conflict_kind: FatalConflictKind
    incoming_path_id: Optional[str] = None
    prior_owner_path_id: Optional[str] = None


@dataclass(frozen=True)
class HardwareFailure:
    """A hardware-backed session lost the device contract after activation.

    Unlike a physical-entry conflict, this halt cannot be cleared in place. The
    operator must make the outputs safe, repair or replace the failed hardware,
    and activate a newly validated session.
    """

    component: str
    operation: str
    message: str
    resource_id: Optional[str] = None


# =============================================================================
# Switch lock claim payload
# =============================================================================


@dataclass
class OccupyResult:
    """Result of `BlockManager.occupy_blocks(...)`.

    Per design_new.md "BlockManager" — `outcome` is one of:
      - ``"clean_entry"``: at least one block transitioned to OCCUPIED.
      - ``"idempotent_noop"``: every requested block was already OCCUPIED
        by the same train (no state change).
      - ``"fatal_conflict"``: a fatal physical-entry conflict was detected;
        ``BlockManager`` already invoked
        ``SimulationController.enter_failed_runtime(...)``.

    On ``"fatal_conflict"`` the caller must stop processing the current
    physical-entry batch.
    """

    outcome: str
    reason: Optional[str] = None

    def __bool__(self) -> bool:
        """Truthy iff the call did not produce a fatal conflict.

        Existing callers that do `if not occupy_blocks(...)` keep working.
        """
        return self.outcome != "fatal_conflict"



# =============================================================================
# Resolved-target schema (moved from route_executor.py)
# =============================================================================



# =============================================================================
# BFS pathfinding intermediate types
#
# These types describe the dispatcher's internal pathfinding pipeline:
# `PathDispatcher.find_paths(...)` returns `PathSegment` candidates; the
# `_validate_path_geometry(...)` step builds a `ValidationResult` carrying
# `ValidationError` records keyed by `ValidationErrorType`. The v1 public
# API (`PathAcquisitionResult`, `CancellationResult`) is built on top of
# these intermediates inside `create_path` / `extend_path` / `cancel_path`.
# =============================================================================


class ValidationErrorType(Enum):
    """Classification for path validation failures."""

    UNBLOCKED_TILE = "unblocked_tile"
    BLOCK_OCCUPIED = "block_occupied"
    BLOCK_RESERVED = "block_reserved"
    BLOCK_DISABLED = "block_disabled"
    BLOCK_MANUAL = "block_manual"  # block.manual==True and no manual_override
    SWITCH_LOCKED = "switch_locked"
    SWITCH_MANUAL = "switch_manual"  # switch.manual==True and no manual_override
    SWITCH_DISABLED = "switch_disabled"
    FLANK_SWITCH_CONFLICT = "flank_switch_conflict"
    # A free resource on a way the current meet assignment keeps for another
    # train (design_new.md "Resource acquisition", A meet keeps a waiting
    # train's way). `ValidationError.kept_for_train` names that train.
    MEET_WAY_KEPT = "meet_way_kept"


@dataclass
class ValidationError:
    """Single validation failure record."""

    type: ValidationErrorType
    message: str = ""
    tile: Optional[Tuple[int, int]] = None
    block_id: Optional[str] = None
    position: Optional[Tuple[int, int]] = None
    occupied_by: Optional[str] = None
    locked_by_path: Optional[str] = None
    kept_for_train: Optional[str] = None



# =============================================================================
# PathDispatcher <-> RouteExecutor seam contracts
# =============================================================================
#
# These two Protocols name the surface each side of the PathDispatcher /
# RouteExecutor seam exposes to the other. They replace runtime `getattr`
# feature-detection — which fails silently when a method is renamed — with a
# declared, structurally-typed contract a type checker can verify. They live
# here in runtime_payloads, the leaf module both sides already import, so
# neither class has to import the other just to name the contract.



