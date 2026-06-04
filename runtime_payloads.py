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

`TrainSpan` itself is referenced as a forward type and not defined in this module
because it is the canonical span schema owned by the train/controller side. This
module imports it under TYPE_CHECKING to avoid runtime circular imports.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    FrozenSet,
    List,
    Literal,
    Optional,
    Protocol,
    Tuple,
)

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

    `entry_edge` is required for switch events (both entered and tail-vacate)
    so the dispatcher can derive the unordered route key
    `frozenset({entry_edge, exit_edge_at_switch})` for route-keyed switch
    occupancy. For block events `entry_edge` is None — block occupancy is
    not route-keyed.

    `switch_exit_edge` is the exit edge through the switch tile itself
    (perpendicular to `exit_edge` semantics for blocks). For switch events
    it's required; for block events None.

    `traversal_direction` is the block-relative `"forward"` / `"reverse"`
    direction of the entered block, used by the head-advance route callback
    to match a waypoint task on block AND direction (design_new.md:462,
    465). It is populated by `PathDispatcher` from the entered block's
    `resource_sequence` entry before calling
    `RouteExecutor.handle_train_advanced(...)`; it is `None` for switch
    resources and `None` until the dispatcher computes it.
    """

    sequence_index: Optional[int]
    resource_type: Literal["block", "switch"]
    resource_id: str
    exit_edge: Optional[str] = None  # "N" / "E" / "S" / "W"
    block_id: Optional[str] = None
    entry_edge: Optional[str] = None  # switch events only
    switch_exit_edge: Optional[str] = None  # switch events only
    traversal_direction: Optional[str] = None  # "forward" / "reverse"; blocks only


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

    def is_route_teardown_pending(self) -> bool:
        """True while an operator route teardown (abort or deactivate) is
        latched but not yet consumed in Phase 3.

        Excludes `power_off` (that path ends in OFF, not a route teardown)
        and the inclusive `path_cancel_request` leaf. Used only as a UI
        projection on `TrainController.to_dict()`; runtime branching never
        keys on it.
        """
        return self.route_abort or self.route_deactivate

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
    """Outcome flavour for create_path / extend_path success cases.

    Per design_new.md "create_path() / extend_path() return contract":
      * REACHED_REQUESTED_TARGET — live authority reaches the requested target
      * TRUNCATED_GREW — best_effort acquired at least one new forward
        resource but the live authority is still short of the requested target
      * BLOCKED_NO_GROWTH — extend_path proved the existing path is still a
        valid prefix toward the requested target but could not acquire the
        next forward resource. Valid only for extend_path; create_path
        never returns this kind.
    """

    REACHED_REQUESTED_TARGET = "reached_requested_target"
    TRUNCATED_GREW = "truncated_grew"
    BLOCKED_NO_GROWTH = "blocked_no_growth"


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


class TargetResolutionFailureKind(Enum):
    """Why route target resolution produced no concrete block.

    Canonical v1 values per design_new.md "Unreachable and broken task
    handling". Classifies resolution failures only; a target that resolves
    but whose authority cannot be acquired is classified by
    `PathAcquisitionFailureKind` instead.
    """

    MISSING_TARGET = "missing_target"
    TARGET_UNAVAILABLE = "target_unavailable"


@dataclass
class PathAcquisitionResult:
    """Structured result for create_path / extend_path.

    Per design_new.md "create_path() / extend_path() return contract", the
    payload carries the success flag, the structured `result_kind` /
    `failure_kind` discriminators, the live `path_id`, the live
    `resource_sequence` (typed as `Any` to avoid the path.py forward
    import), the caller-supplied target contract, and the live authority
    boundary. Callers must branch on `success`/`result_kind`/`failure_kind`,
    not on `reason` text.
    """

    success: bool
    result_kind: Optional[PathAcquisitionResultKind] = None
    failure_kind: Optional[PathAcquisitionFailureKind] = None
    path_id: Optional[str] = None
    requested_destination_block_id: Optional[str] = None
    requested_destination_direction: Optional[str] = None
    authority_end_block_id: Optional[str] = None
    resource_sequence: List[Any] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "result_kind": self.result_kind.value if self.result_kind else None,
            "failure_kind": self.failure_kind.value if self.failure_kind else None,
            "path_id": self.path_id,
            "requested_destination_block_id": self.requested_destination_block_id,
            "requested_destination_direction": self.requested_destination_direction,
            "authority_end_block_id": self.authority_end_block_id,
            "resource_sequence": [
                e.to_dict() if hasattr(e, "to_dict") else e
                for e in self.resource_sequence
            ],
            "reason": self.reason,
        }


@dataclass
class CancellationResult:
    """Structured result for PathDispatcher.cancel_path(...).

    Per design_new.md "cancel_path() return contract" (lines 925-934):
    on success, `success=True`, `path_id` is required, `new_status` is
    `PathStatus.CANCELLED`. On failure, `success=False`, `path_id=None`,
    `new_status=None`, and `reason` is required. Callers must not branch
    on the exact `reason` text; the canonical strings are "path not found",
    "already completed", "already cancelled".
    """

    success: bool
    path_id: Optional[str] = None
    new_status: Any = None  # PathStatus from path.py; Any to avoid circular import.
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "path_id": self.path_id,
            "new_status": (
                self.new_status.value
                if self.new_status is not None and hasattr(self.new_status, "value")
                else self.new_status
            ),
            "reason": self.reason,
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
    # design_new.md line 663: a train physically on a switch must hold a
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


@dataclass(frozen=True)
class SwitchLockClaim:
    """Structured payload for `Switch.acquire_lock(claim)`.

    Per design_new.md "Switch entity" — the canonical claim shape carries
    `path_id`, `train_id`, `locked_state`, an optional `source_switch_id`,
    the operator-override flag `manual_override`, and the path's route
    through this switch (`route`, an unordered pair of edge names). The
    role (main/primary vs flank) is derived from `source_switch_id is
    None`; it is not a separate stored field. `manual_override` is not
    stored on the recorded lock entry; it is only consulted at the
    eligibility step inside `acquire_lock(...)`.

    `route` is the canonical entry/exit edge pair the path uses through
    this switch (e.g. `frozenset({"N","W"})` for an NW arc). When present,
    two locks may coexist when their routes are non-conflicting per
    `switch.routes_conflict(...)` — this is what lets two trains share a
    scissors crossover in DIVERGE state (one on NW, one on SE). When
    `route is None`, the lock is treated as "any route at this
    locked_state" and falls back to the same-train cross-primary rule.
    """

    path_id: str
    train_id: str
    locked_state: int  # 0 = MAIN, 1 = DIVERGE
    source_switch_id: Optional[str] = None
    manual_override: bool = False
    route: Optional[FrozenSet[str]] = None  # unordered pair of two edge-name strings, or None


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

    def is_occupancy_class(self) -> bool:
        # Live-state conflicts that BEST_EFFORT truncates around per
        # design_new.md ":938" ("If a switch acquisition fails ... truncate
        # the candidate to the previous block before that failed switch")
        # and the block-conflict half of the same contract. `_best_effort_
        # truncate` drops the contested resource entirely — for a
        # SWITCH_LOCKED conflict the boundary switch is excluded from the
        # truncated segment, so the requesting train never locks it, and
        # the same-state co-lock deadlock concern does not apply here.
        # Structural failures (BLOCK_DISABLED, BLOCK_MANUAL, SWITCH_DISABLED,
        # SWITCH_MANUAL) are never occupancy-class: they are permanent under
        # the current availability flags and need `no_viable_path`, not
        # retry-with-truncation. FLANK_SWITCH_CONFLICT is kept out for now;
        # flank protection has its own truncation rules.
        return self in (
            ValidationErrorType.BLOCK_OCCUPIED,
            ValidationErrorType.BLOCK_RESERVED,
            ValidationErrorType.SWITCH_LOCKED,
        )


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


class PathDispatcherForRouteExecutor(Protocol):
    """The PathDispatcher surface that RouteExecutor (the Phase-1 planner) drives.

    RouteExecutor acquires and reads authority through these members. It never
    calls `cancel_path(...)`: cancellation is latched in Phase 1 and committed
    by the Phase-3 cancel -> callback -> cleanup flow (see the RouteExecutor
    module docstring), so `cancel_path` is intentionally absent here.
    """

    # Path registry, read back after a successful create to inspect the new row.
    paths: Dict[str, Any]

    def active_path_id_by_train(self, train_id: str) -> Optional[str]: ...

    def create_path(
        self,
        train_id: str = "",
        requested_destination_block_id: str = "",
        requested_destination_direction: str = "forward",
        acquisition_mode: AcquisitionMode = AcquisitionMode.BEST_EFFORT,
        manual_override: bool = False,
    ) -> PathAcquisitionResult: ...

    def extend_path(
        self,
        path_id: str = "",
        requested_destination_block_id: str = "",
        requested_destination_direction: str = "forward",
        acquisition_mode: AcquisitionMode = AcquisitionMode.BEST_EFFORT,
        manual_override: bool = False,
    ) -> PathAcquisitionResult: ...


class RouteExecutorForPathDispatcher(Protocol):
    """The RouteExecutor surface that PathDispatcher calls back into during Phase 3.

    Each method is bookkeeping-only: it records the consequence of a Phase-3
    event on the route side and must not create or choose fresh authority.
    """

    def handle_train_advanced(
        self, train_id: str, path_id: str, resource_event: ResourceEvent
    ) -> None: ...

    def handle_path_cancellation(
        self,
        train_id: str,
        path_id: str,
        initiator: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None: ...

    def handle_path_completion(
        self,
        train_id: str,
        path_id: str,
        authority_end_block_id: Optional[str] = None,
        requested_destination_direction: Optional[str] = None,
        event_time: Optional[float] = None,
    ) -> bool: ...
