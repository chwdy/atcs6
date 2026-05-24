"""
Path Plan Projection sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: this
sub-module is the input-layer projection that surfaces the
dispatcher-owned active ``Path`` to controller-side consumers as live
read-only derivations.

There are no owned fields — every read recomputes from
``self._c.path_dispatcher.get_active_path(train_id)`` (or other live
sources) and auto-flips to ``None`` when the dispatcher transitions
the path away from ``ACTIVE``.

External callers access path projections as:

  - ``controller.path_projection.received_path_plan`` (the canonical
    live read of the controller's active authority projection)
  - ``controller.path_projection.assigned_path_id``
  - ``controller.path_projection.ato_path_id`` (alias for
    ``assigned_path_id``)
  - ``controller.path_projection.ato_destination_block_id``
  - ``controller.path_projection.path_backed_run_active``
  - ``controller.path_projection.get_path_tiles()``
  - ``controller.path_projection.get_live_path_data()``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from enums import Direction
    from path import Path
    from train_controller import TrainController


class PathProjection:
    """Owns the controller's read-only path projections."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller

    @property
    def received_path_plan(self) -> Optional["Path"]:
        """Live read of the controller's active authority.

        Resolves to ``PathDispatcher.get_active_path(self.train_id)`` on
        every read. There is no setter and no stored field. When the
        dispatcher transitions the path's status away from ``ACTIVE``,
        this property returns ``None`` on the next read automatically.
        """
        c = self._c
        if c.path_dispatcher is None:
            return None
        getter = getattr(c.path_dispatcher, "get_active_path", None)
        if getter is None:
            return None
        return getter(c.train_id)

    @property
    def assigned_path_id(self) -> Optional[str]:
        """Computed read derived from ``received_path_plan.path_id``."""
        plan = self.received_path_plan
        if plan is None:
            return None
        return getattr(plan, "path_id", None) or getattr(plan, "id", None)

    @property
    def ato_path_id(self) -> Optional[str]:
        """Computed read derived from ``received_path_plan.path_id``."""
        return self.assigned_path_id

    @property
    def ato_destination_block_id(self) -> Optional[str]:
        """Computed read derived from
        ``received_path_plan.authority_end_block_id``.

        Per design_new.md ~line 80, ``Path`` carries
        ``authority_end_block_id`` as a derived read over
        ``resource_sequence``; this property simply forwards it.
        """
        plan = self.received_path_plan
        if plan is None:
            return None
        return plan.get_authority_end_block_id() or None

    @property
    def path_backed_run_active(self) -> bool:
        """Derived read: does this controller currently have a
        path/route/destination context backing the run?

        Per design_new.md "Latch table" canonical home, this is not a
        stored latch field — it is a derived projection of
        ``received_path_plan``, the active route execution state, and
        ``ato_destination_block_id``. Returns the same boolean that
        ``_has_path_backed_run_context(...)`` used to compute when this
        was a stored field.
        """
        c = self._c
        train = c._get_train()
        if train is None:
            return False
        return c._has_path_backed_run_context(train)

    def get_path_tiles(self) -> Optional[List[Tuple[int, int, "Direction"]]]:
        """Get path tiles from ``PathDispatcher`` (single source of truth).

        Reads ``Path.path_tiles`` from the dispatcher's currently active
        path for this train and converts it to ``(x, y, Direction)``
        format. Returns ``None`` if no active path exists.
        """
        c = self._c
        if not c.path_dispatcher:
            return None
        path = c.path_dispatcher.get_active_path(c.train_id)
        if not path:
            return None
        path_tiles = path.get_path_tiles()
        if not path_tiles:
            return None
        from path_utils import convert_tiles_to_train_path
        return convert_tiles_to_train_path(path_tiles)

    def get_live_path_data(self) -> Optional[Dict[str, Any]]:
        """Get live path data for serialization (``Train.to_dict``).

        Returns ``path_tiles`` and the current head ``path_index``, both
        derived from the ``PathDispatcher`` (source of truth). Returns
        ``None`` if the train has no active path.
        """
        c = self._c
        train = c._get_train()
        if not train:
            return None

        path_tiles = self.get_path_tiles()
        if path_tiles is None:
            return None

        # Derive head path index from train's physical position.
        head_tile = c._get_head_tile()
        if head_tile:
            path_index = c._get_head_path_index(head_tile)
        else:
            path_index = -1

        return {
            "path_tiles": path_tiles,
            "path_index": path_index,
        }
