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
- PendingPathCancelRequest / PendingRouteAbortRequest / PendingRouteDeactivateRequest:
  the per-train Phase-3 mailbox payloads
- PathCancellationKind: the canonical cancellation enum

`TrainSpan` itself is referenced as a forward type and not defined in this module
because it is the canonical span schema owned by the train/controller side. This
module imports it under TYPE_CHECKING to avoid runtime circular imports.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, List, Literal, Optional, Tuple

if TYPE_CHECKING:
    # TrainSpan and its component schemas (SpanEndLocator, SpanTile,
    # SpanResourceInterval) are owned by the train/controller side. Until they
    # are formally defined elsewhere, we type-check against `Any` and let
    # producers/consumers carry whichever concrete shape they currently use.
    pass


# =============================================================================
# Cancellation diagnostic enum
# =============================================================================


class PathCancellationKind(Enum):
    """Canonical reason kinds for PathDispatcher.cancel_path(...).

    Diagnostic only; routing/planning layers must not branch on this value.
    """

    EB = "eb"
    OPERATOR_CANCELLED = "operator_cancelled"
    POWER_OFF = "power_off"
    REVOKED = "revoked"
    OUT_OF_SEQUENCE = "out_of_sequence"
    AUTHORITY_OVERSHOOT = "authority_overshoot"


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
    """Canonical payload from TrainPhysics.tick(...) into
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

    `direction` is required for block tail-vacate events (the vacated block's
    forward `exit_edge` for the traversal by which the train left the block)
    and is `None` for switch tail-vacate events and for entered-resource events.
    """

    sequence_index: Optional[int]
    resource_type: Literal["block", "switch"]
    resource_id: str
    direction: Optional[str] = None  # "N" / "E" / "S" / "W"
    block_id: Optional[str] = None


@dataclass
class ControllerProgressReport:
    """Canonical Phase-3 progress report produced by
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

    Consumed by Phase 3 only when the train is fully stopped. While the train
    is still rolling, the latch survives across ticks and is reconsidered on
    the next eligible Phase 3 pass.
    """

    cancellation_kind: PathCancellationKind
    reason: Optional[str] = None
    expected_path_id: Optional[str] = None


@dataclass
class PendingRouteAbortRequest:
    """Phase-3 latched route-abort payload written to TrainController.

    Consumed by Phase 3 only after the inclusive pending_path_cancel_request
    has committed and the active path is gone.
    """

    reason: str
    expected_path_id: Optional[str] = None


@dataclass
class PendingRouteDeactivateRequest:
    """Phase-3 latched route-deactivate payload written per assigned train when
    route.deactivate() runs.

    Consumed by Phase 3 only after the inclusive pending_path_cancel_request
    has committed and any active path is gone.
    """

    reason: str
    expected_path_id: Optional[str] = None


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
