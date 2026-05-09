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
  the per-train Phase-3 mailbox payloads
- PathCancellationKind: the canonical cancellation enum

`TrainSpan` itself is referenced as a forward type and not defined in this module
because it is the canonical span schema owned by the train/controller side. This
module imports it under TYPE_CHECKING to avoid runtime circular imports.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Tuple

if TYPE_CHECKING:
    # TrainSpan and its component schemas (SpanEndLocator, SpanTile,
    # SpanResourceInterval) are owned by the train/controller side. Until they
    # are formally defined elsewhere, we type-check against `Any` and let
    # producers/consumers carry whichever concrete shape they currently use.
    pass


# =============================================================================
# Path priority constants
# =============================================================================

DEFAULT_PATH_PRIORITY = 5
MIN_PRIORITY = 1
MAX_PRIORITY = 10


# =============================================================================
# Cancellation diagnostic enum
# =============================================================================


class PathCancellationKind(Enum):
    """Reason kinds for PathDispatcher.cancel_path(...).

    Diagnostic only; routing/planning layers must not branch on this value.
    """

    EB = "eb"
    OPERATOR_CANCELLED = "operator_cancelled"
    POWER_OFF = "power_off"
    REVOKED = "revoked"
    OUT_OF_SEQUENCE = "out_of_sequence"
    AUTHORITY_OVERSHOOT = "authority_overshoot"


# =============================================================================
# Per-run / per-task error record
# =============================================================================


@dataclass
class ErrorRecord:
    """Paired error message + timestamp.

    Used by `RouteRun.last_error` and `RouteTaskRun.last_error` so the
    message and its timestamp are always either both present or both absent.
    """

    text: str
    time: float

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "time": self.time}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ErrorRecord":
        return cls(text=str(data["text"]), time=float(data["time"]))


# =============================================================================
# Physics → Controller payload
# =============================================================================


@dataclass
class TileTraversal:
    """One added/removed tile entry inside a PhysicsSpanUpdate.

    Carries traversal-frame metadata sufficient for TrainController to derive
    block tail-vacate `direction` and entered-resource sequence indices from
    the new span without re-reading network state.
    """

    tile_xy: Tuple[int, int]
    entry_edge: str  # "N" / "E" / "S" / "W"
    exit_edge: str  # "N" / "E" / "S" / "W"
    # Optional resource metadata. Producers fill these when known so that
    # consumers (TrainController.on_physics_updated) do not need to look them
    # up again.
    resource_type: Optional[Literal["block", "switch"]] = None
    resource_id: Optional[str] = None
    block_id: Optional[str] = None


@dataclass
class PhysicsSpanUpdate:
    """Payload from TrainPhysics.tick(...) into
    TrainController.on_physics_updated(event_time, span_update).

    Per design_new.md, TrainPhysics emits one update per crossed resource
    boundary and one final no-boundary callback every tick (added_tiles and
    removed_tiles both empty in the no-boundary case).
    """

    new_span: Any  # TrainSpan; left as Any here to avoid premature import binding
    current_speed_kmh: float
    added_tiles: List[TileTraversal] = field(default_factory=list)
    removed_tiles: List[TileTraversal] = field(default_factory=list)


# =============================================================================
# Controller → Dispatcher progress report
# =============================================================================


@dataclass
class ResourceEvent:
    """A single entered or tail-vacated resource observation.

    `exit_edge` is required for block tail-vacate events (the vacated block's
    forward exit edge for the traversal by which the train left the block)
    and is `None` for switch tail-vacate events and for entered-resource events.
    """

    sequence_index: Optional[int]
    resource_type: Literal["block", "switch"]
    resource_id: str
    exit_edge: Optional[str] = None  # "N" / "E" / "S" / "W"
    block_id: Optional[str] = None


@dataclass
class ControllerProgressReport:
    """Phase-3 progress report produced by
    TrainController.on_physics_updated(...) and consumed by
    PathDispatcher.on_controller_progress(...).

    Per design_new.md "Dispatcher progress/completion flow", the report does
    NOT carry path_id; the dispatcher resolves the active path from
    active_path_id_by_train(train_id).
    """

    train_id: str
    event_time: float
    entered_resources: List[ResourceEvent] = field(default_factory=list)
    tail_vacated_resources: List[ResourceEvent] = field(default_factory=list)
    current_head_sequence_index: Optional[int] = None
    deviation: bool = False
    deviation_kind: Optional[Literal["out_of_sequence", "beyond_authorized_target"]] = None
    deviation_reason: Optional[str] = None


# =============================================================================
# Phase-3 mailbox payloads
# =============================================================================


@dataclass
class PendingPathCancelRequest:
    """Phase-3 latched cancellation payload written to TrainController.

    Per design_new.md (Latch Table) the canonical fields are:
      * `initiator: str` — free-form label populated by the writer for the
        `path_cancellation` notification. Common values: ``"eb"``,
        ``"operator_cancelled"``, ``"power_off"``, ``"deviation"``,
        ``"revoked"``. Functional behavior is identical regardless of value;
        consumers must not branch on it.
      * `reason: Optional[str]` — optional plain-text diagnostic.

    Consumed by Phase 3 only when the train is fully stopped. While the train
    is still rolling, the latch survives across ticks and is reconsidered on
    the next eligible Phase 3 pass. Because v1 allows at most one active path
    per train, the payload does not need a `path_id` routing key.
    """

    initiator: str
    reason: Optional[str] = None


# Note: per design_new.md (Latch Table), `pending_route_abort` and
# `pending_route_deactivate` are bare-bool latches on `TrainController`. The
# diagnostic `reason` for the originating action is recorded only on the
# `path_cancellation` and `command_result` notifications (the inclusive
# `pending_path_cancel_request.reason` carries it through to the cancellation
# event), not on any field on `RouteRun` or on the route-abort / deactivate
# latches themselves.


@dataclass
class PhaseLatches:
    """Aggregate of the four Phase-3 latches owned by `TrainController`.

    Per design_new.md (Latch Table) the four latches share the same lifecycle
    rule: a writer sets them in Phase 2 (or in command handling) and Phase 3
    consumes-and-clears them on the same tick once the consumer's gate passes.
    Bundling the four fields plus their canonical writers / consumers into one
    data class keeps the latch discipline in a single place.

    The leaf writer is `set_path_cancel_request(...)`; the three composite
    setters (`set_route_abort`, `set_power_off`, `set_route_deactivate`) own
    their respective bool field and, when an active path exists, fan out to
    `set_path_cancel_request(...)` with the design-canonical `initiator`
    value (`"operator_cancelled"`, `"power_off"`, `"revoked"`).

    The cross-field side effect of `power_off` (clearing
    `TrainController.ato_running`) does NOT live here — that is a controller
    concern and the caller wires it together with `set_power_off(...)`.
    """

    path_cancel_request: Optional[PendingPathCancelRequest] = None
    route_abort: bool = False
    power_off: bool = False
    route_deactivate: bool = False

    # ------------------------------------------------------------------
    # Writers (canonical setters)
    # ------------------------------------------------------------------

    def set_path_cancel_request(
        self, initiator: str, reason: Optional[str] = None
    ) -> None:
        """Leaf writer for `path_cancel_request`."""
        self.path_cancel_request = PendingPathCancelRequest(
            initiator=initiator, reason=reason
        )

    def set_route_abort(
        self,
        reason: str = "operator abort",
        has_active_path: bool = False,
    ) -> None:
        """Set `route_abort`; when a live path exists, also set the inclusive
        `path_cancel_request` with `initiator="operator_cancelled"`."""
        self.route_abort = True
        if has_active_path:
            self.set_path_cancel_request("operator_cancelled", reason)

    def set_power_off(
        self,
        reason: str = "power off",
        has_active_path: bool = False,
    ) -> None:
        """Set `power_off`; when a live path exists, also set the inclusive
        `path_cancel_request` with `initiator="power_off"`."""
        self.power_off = True
        if has_active_path:
            self.set_path_cancel_request("power_off", reason)

    def set_route_deactivate(
        self,
        reason: str = "route deactivated",
        has_active_path: bool = False,
    ) -> None:
        """Set `route_deactivate`; when a live path exists, also set the
        inclusive `path_cancel_request` with `initiator="revoked"`."""
        self.route_deactivate = True
        if has_active_path:
            self.set_path_cancel_request("revoked", reason)

    # ------------------------------------------------------------------
    # Read predicates
    # ------------------------------------------------------------------

    def is_any_pending(self) -> bool:
        """True if any of the four latches is currently set."""
        return (
            self.path_cancel_request is not None
            or self.route_abort
            or self.power_off
            or self.route_deactivate
        )

    # ------------------------------------------------------------------
    # Consumers (read-and-clear)
    # ------------------------------------------------------------------

    def consume_path_cancel_request(self) -> Optional[PendingPathCancelRequest]:
        v = self.path_cancel_request
        self.path_cancel_request = None
        return v

    def consume_route_abort(self) -> bool:
        v = self.route_abort
        self.route_abort = False
        return v

    def consume_power_off(self) -> bool:
        v = self.power_off
        self.power_off = False
        return v

    def consume_route_deactivate(self) -> bool:
        v = self.route_deactivate
        self.route_deactivate = False
        return v


# =============================================================================
# Path completion result
# =============================================================================


@dataclass
class PathCompletionResult:
    """Result returned by PathDispatcher.try_complete_active_path(...).

    Per design_new.md ~line 271 (Path completion notification flow):
    - success=True only when dispatcher accepted and committed completion.
    - path_id: the active path that was completed/considered. None only when no
      active path existed.
    - new_status: PathStatus.COMPLETED on success; current unchanged status
      (e.g. ACTIVE) on a failed predicate; None when no path row exists.
    - reason: None on success; required diagnostic string on failure.
    """

    success: bool
    path_id: Optional[str] = None
    new_status: Any = None  # PathStatus from path.py; left as Any to avoid circular import
    reason: Optional[str] = None


# =============================================================================
# Wave 2: PathDispatcher acquisition contract
# =============================================================================


class AcquisitionMode(Enum):
    """How aggressively dispatcher reserves resources for a path.

    ALL_OR_NOTHING: reserve every resource end-to-end or fail.
    BEST_EFFORT: reserve as far as possible; truncate at first conflict.
    """

    ALL_OR_NOTHING = "all_or_nothing"
    BEST_EFFORT = "best_effort"


class PathAcquisitionResultKind(Enum):
    """Outcome flavour for create_path / extend_path success cases."""

    CREATED = "created"
    TRUNCATED_GREW = "truncated_grew"
    BLOCKED_NO_GROWTH = "blocked_no_growth"
    EXTENDED = "extended"


class PathAcquisitionFailureKind(Enum):
    """Failure kinds returned by create_path / extend_path.

    Canonical v1 values per design_new.md "create_path() / extend_path()
    return contract".
    """

    NO_VIABLE_PATH = "no_viable_path"
    BLOCKED_BEFORE_TARGET = "blocked_before_target"
    INVALID_REVERSE = "invalid_reverse"
    INVALID_REQUEST = "invalid_request"
    HEAD_ON_SWITCH_NO_BLOCK_AUTHORITY = "head_on_switch_no_block_authority"
    MISSING_OR_INACTIVE_PATH = "missing_or_inactive_path"
    TRAIN_ALREADY_HAS_ACTIVE_PATH = "train_already_has_active_path"


@dataclass
class PathAcquisitionResult:
    """Structured result for create_path / extend_path.

    `granted_resource_sequence` carries `ResourceSequenceEntry` objects (typed
    as `Any` here to avoid the path.py forward import). Per design_new.md
    "create_path() / extend_path() return contract", successful returns echo
    `requested_destination_block_id` / `requested_destination_direction` (the
    caller-supplied target contract for that attempt) alongside the live
    `authority_end_block_id`.
    """

    success: bool
    result_kind: Optional[PathAcquisitionResultKind] = None
    failure_kind: Optional[PathAcquisitionFailureKind] = None
    path_id: Optional[str] = None
    requested_destination_block_id: Optional[str] = None
    requested_destination_direction: Optional[str] = None
    authority_end_block_id: Optional[str] = None
    granted_resource_sequence: List[Any] = field(default_factory=list)
    reason: str = ""
    failed_resource_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "result_kind": self.result_kind.value if self.result_kind else None,
            "failure_kind": self.failure_kind.value if self.failure_kind else None,
            "path_id": self.path_id,
            "requested_destination_block_id": self.requested_destination_block_id,
            "requested_destination_direction": self.requested_destination_direction,
            "authority_end_block_id": self.authority_end_block_id,
            "granted_resource_sequence": [
                e.to_dict() if hasattr(e, "to_dict") else e
                for e in self.granted_resource_sequence
            ],
            "reason": self.reason,
            "failed_resource_id": self.failed_resource_id,
        }


@dataclass
class CancellationResult:
    """Structured result for PathDispatcher.cancel_path(...).

    Per design_new.md "Path cancellation flow"."""

    success: bool
    path_id: str = ""
    cancellation_kind: Optional[PathCancellationKind] = None
    reason: str = ""
    released_block_ids: List[str] = field(default_factory=list)
    released_switch_ids: List[str] = field(default_factory=list)
    train_id: Optional[str] = None
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "path_id": self.path_id,
            "cancellation_kind": (
                self.cancellation_kind.value if self.cancellation_kind else None
            ),
            "reason": self.reason,
            "released_block_ids": list(self.released_block_ids),
            "released_switch_ids": list(self.released_switch_ids),
            "train_id": self.train_id,
            "notes": self.notes,
        }


@dataclass
class Command:
    """Operator command queued on TrainController.command_inbox.

    `kind` is one of the canonical command kind strings (power_on, set_cab,
    set_mode, set_manual_mode_type, set_handle, set_manual_power_percent,
    activate_emergency_cmd, release_emergency_cmd, power_off, ato_go,
    ato_stop). `payload` carries kind-specific args.
    """

    kind: str
    payload: Dict[str, Any] = field(default_factory=dict)
    command_id: str = ""
    accepted_at: float = 0.0


# =============================================================================
# Fatal conflict diagnostic schemas
# =============================================================================


class FatalConflictKind(Enum):
    """Diagnostic categories for fatal physical-entry conflicts surfaced by
    the runtime.

    Per design_new.md "Fatal physical-entry conflict procedure" v1 values:
    a train tried to enter a block already held by another train, a block
    reserved for another train, a switch already occupied by another train,
    or a resource (block/switch) flagged disabled.
    """

    OCCUPIED_BY_OTHER_TRAIN = "occupied_by_other_train"
    RESERVED_FOR_OTHER_TRAIN = "reserved_for_other_train"
    SWITCH_OCCUPIED_BY_OTHER_TRAIN = "switch_occupied_by_other_train"
    DISABLED_RESOURCE_ENTRY = "disabled_resource_entry"


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


# =============================================================================
# Resolved-target schema (moved from route_executor.py)
# =============================================================================


@dataclass
class ResolvedRouteTarget:
    """Route-owned dispatcher request contract for one task attempt.

    Per design_new.md, carries exactly the route-owned target contract:
    `requested_destination_block_id` plus block-relative
    `requested_destination_direction` ("forward" / "reverse"). Dispatcher
    derives terminal frame from this block plus its own resource_sequence;
    the route never passes a centimeter target.
    """

    requested_destination_block_id: str
    requested_destination_direction: str  # "forward" | "reverse"


# =============================================================================
# Path planning result types (kept here so legacy helpers in path_dispatcher.py
# still compile; v1 public API uses PathAcquisitionResult / CancellationResult)
# =============================================================================


class ValidationErrorType(Enum):
    """Classification for path validation failures (legacy diagnostic only)."""

    UNBLOCKED_TILE = "unblocked_tile"
    BLOCK_OCCUPIED = "block_occupied"
    BLOCK_RESERVED = "block_reserved"
    BLOCK_DISABLED = "block_disabled"
    SWITCH_LOCKED = "switch_locked"
    FLANK_SWITCH_CONFLICT = "flank_switch_conflict"


@dataclass
class ValidationError:
    """Single validation failure record (legacy diagnostic)."""

    type: ValidationErrorType
    message: str = ""
    tile: Optional[Tuple[int, int]] = None
    block_id: Optional[str] = None
    position: Optional[Tuple[int, int]] = None
    occupied_by: Optional[str] = None
    locked_by_path: Optional[str] = None


@dataclass
class PathSegment:
    """Geometry-only path candidate produced by `find_paths(...)`.

    Carries the explored tile sequence, the included block IDs in traversal
    order, and the per-switch required state map. v1 builds the dispatcher's
    canonical `Path.resource_sequence` from one of these segments.
    """

    tiles: List[Tuple[int, int]] = field(default_factory=list)
    distance_cm: float = 0.0
    required_switches: Dict[Tuple[int, int], int] = field(default_factory=dict)
    blocks: List[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    """Result of path geometry validation."""

    valid: bool
    errors: List[ValidationError] = field(default_factory=list)
    path: Optional[PathSegment] = None


@dataclass
class PathResult:
    """Legacy result used by the in-tree `find_paths` flow."""

    success: bool
    path: Any = None  # path.Path; left as Any to avoid circular import
    error: str = ""
    attempted_paths: List[Any] = field(default_factory=list)
    conflict_tiles: List[Tuple[int, int]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "error": self.error,
            "path": self.path.to_dict() if self.path is not None else None,
            "conflict_tiles": [list(t) for t in self.conflict_tiles],
        }


@dataclass
class ExtensionResult:
    """Legacy result used by the in-tree `extend_path` flow."""

    success: bool
    extended_blocks: List[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "extended_blocks": list(self.extended_blocks),
            "error": self.error,
        }


# Conflict-resolution legacy types (returned by helpers that may still run)
class ConflictResolutionType(Enum):
    PROCEED = "proceed"
    BLOCK = "block"
    REROUTE = "reroute"


@dataclass
class ConflictResolution:
    type: ConflictResolutionType
    description: str = ""
