"""
Physics Sync sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: Physics
Sync owns the live span and speed that ``TrainPhysics.tick(...)`` writes
back to the controller through the orchestrator entry point
``controller.on_physics_updated(...)``.

The orchestrator method stays flat on the façade because it (a) is the
canonical callback name ``TrainPhysics`` invokes, and (b) drives
cross-sub-module work (reactive state transitions on the operational
state machine, progress-report build that needs ``received_path_plan``,
etc.). The façade method delegates the simple field writes — and the
progress mailbox — to this sub-module.

External callers access live span and speed as:

  - ``controller.physics_sync.current_span`` (canonical TrainSpan
    storage; written by ``on_physics_updated``)
  - ``controller.physics_sync.current_speed_kmh`` (canonical live
    speed in km/h)
  - ``controller.physics_sync.queued_progress_reports`` (progress
    mailbox; drained by ``SimulationController`` in Observe)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from runtime_payloads import ControllerProgressReport, ResourceEvent

if TYPE_CHECKING:
    from train_controller import TrainController


class PhysicsSync:
    """Owns the controller's live span, live speed, and progress mailbox."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller
        self.current_span: Optional[Any] = None
        self.current_speed_kmh: float = 0.0
        self.queued_progress_reports: List[ControllerProgressReport] = []

        # Head-mapping progress anchor (design_new.md "Head-mapping rule"): the
        # last sequence_index this controller reported for the current
        # received_path_plan.path_id. Head resolution picks the lowest matching
        # sequence_index at or after this anchor, so a lapping path's first visit
        # to a duplicated resource maps to its earlier occurrence rather than the
        # later one. Resets to 0 whenever the path_id changes; 0 for a path whose
        # head has not been reported yet.
        self.head_progress_anchor: int = 0
        self.head_progress_anchor_path_id: Optional[str] = None

        # Block occupancy detection (design_new.md "Power -> Block occupancy
        # detection"). The global current detector confirms block entry/exit at
        # the top of each tick and writes this train's attributed slice here;
        # on_physics_updated drains it into the Observe progress report so the
        # existing on_controller_progress flow commits Block.occupied_by.
        self.pending_detected_entered_blocks: Set[str] = set()
        self.pending_detected_vacated_blocks: Set[str] = set()
        # Precise forward exit edge captured when the tail fully clears a block,
        # held until the debounced detector confirms the vacate so the caution
        # direction matches the physical exit edge.
        self.pending_block_exit_edges: Dict[str, str] = {}

        # Within-tick record of the head-entered blocks Observe committed, held
        # for this train's Plan settlement (design_new.md "Observe" /
        # "Stop-arrival procedure", step `640a`). Observe's occupancy commit
        # (`PathDispatcher.on_controller_progress`) advances the path head index
        # and appends each mapped head-entry here as `(path_id, ResourceEvent)`;
        # Plan settlement replays them through `RouteExecutor.handle_train_advanced(...)`
        # in `sequence_index` order and then clears the list. Observe finishes no
        # route task itself. Reset at the top of each tick's Observe drain so a
        # train that never reaches Plan settlement leaks nothing across ticks.
        self.entered_blocks_this_tick: List[Tuple[str, ResourceEvent]] = []
