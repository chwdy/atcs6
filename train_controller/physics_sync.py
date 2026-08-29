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

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from runtime_payloads import ControllerProgressReport

if TYPE_CHECKING:
    from train_controller import TrainController


class PhysicsSync:
    """Owns the controller's live span, live speed, and progress mailbox."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller
        self.current_span: Optional[Any] = None
        self.current_speed_kmh: float = 0.0
        self.queued_progress_reports: List[ControllerProgressReport] = []

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
