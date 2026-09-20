"""TrainController (de)serialization, extracted from train_controller/__init__.py.

Free functions; the controller keeps thin ``to_dict`` / ``from_dict`` delegators.
``controller_from_dict`` constructs a ``TrainController``, so it imports the class
lazily to avoid an import cycle (the package ``__init__`` references these
functions only from inside the delegating methods).
"""
from __future__ import annotations

from typing import Any, Dict, Optional, TYPE_CHECKING

from enums import HandlePosition
from model_scale import get_speed_conversion_factor

if TYPE_CHECKING:
    from network import RailwayNetwork
    from train_controller import TrainController


def controller_to_dict(controller: "TrainController") -> Dict[str, Any]:
    """Convert controller state to dictionary for JSON serialization.

    Live runtime state for JSON output (per design_new.md).
    Persistent/configuration data lives on `Train.to_dict()`.
    """
    # Helper to convert infinity to None for JSON serialization
    def safe_float(value: float) -> Optional[float]:
        if value == float("inf") or value == float("-inf"):
            return None
        return value

    # Live snapshot of the derived lookahead (computed once for this dict)
    live_lookahead = controller._calculate_lookahead()

    span_dict = _span_to_dict(controller)

    return {
        "train_id": controller.train_id,
        "power_on": controller.power_on,
        # Stored power flag; `train_status` derives OFF from `not powered`
        # (design_new.md "Controller state machine").
        "powered": controller.powered,
        "eb_active": controller.eb.active,
        # Whether the active EB latch is an emergency cut-out (design_new.md
        # "Emergency cut-out"); only meaningful while `eb_active`.
        "eb_cut_out": controller.eb.cut_out,
        # `cab` on the wire is the physical end the span is framed head-forward
        # for ("A" == end_a, "B" == end_b); there is no operator cab (design_new.md
        # "Reservation-side head derivation").
        "cab": ("B" if controller._framed_head_end == "end_b" else "A"),
        "control_mode": controller.commands.control_mode,
        "manual_mode_type": controller.manual.manual_mode_type,
        "manual_power_percent": controller.manual.manual_power_percent,
        "travel_direction": controller.travel_direction,
        # Train facing (a `Direction` name) derived from the authority-framed span;
        # replaces the removed `Train.facing_direction` on the wire so the
        # client reads facing from the controller payload.
        "facing_direction": (lambda d: d.name if d is not None else None)(
            controller._facing_direction()
        ),
        # Derived overall status (`OFF` / `MANUAL` / `ATO` / `EB`); replaces the
        # removed stored `state` (design_new.md "Controller state machine").
        "train_status": controller.ops.train_status.value,
        "target_speed": controller.target_speed,
        "handle_position": controller.ops.handle_position.value,
        # The ATO engage bit (design_new.md "ATO status"), stored on
        # OperatorCommands and serialized under the key "engaged".
        "engaged": controller.commands.engaged,
        "ato_destination_block_id": controller.path_projection.ato_destination_block_id,
        "ato_path_id": controller.path_projection.ato_path_id,
        "assigned_path_id": controller.path_projection.assigned_path_id,
        "path_backed_run_active": controller.path_projection.path_backed_run_active,
        # Export the live received_path_plan using the canonical
        # received_path_plan schema (design_new.md): exactly
        # path_id, resource_sequence, reservation_end_block_id. Dispatcher-
        # only requested-target / lifecycle / progress fields stay on the
        # Path record and must not leak into the controller projection.
        "received_path_plan": controller.path_projection.received_path_plan_dict(),
        "clearance_ahead_cm": safe_float(controller.atc.clearance_ahead_cm),
        "atc_speed_limit": safe_float(controller.atc.speed_limit),
        "current_block_speed_limit": safe_float(controller.atc.current_block_speed_limit),
        "speed_limit_reason": controller.speed_limit_reason,
        # Current physical span
        "span": span_dict,
        # Include lookahead for display (live computed here)
        "lookahead": [
            {
                "distance_cm": lp.distance_cm,
                "speed_limit_kmh": safe_float(lp.speed_limit_kmh),
                "block_id": lp.block_id,
                "signal_state": lp.signal_state.value if lp.signal_state else None,
                "station_id": lp.station_id,
                "is_path_end": lp.is_path_end,
            }
            for lp in live_lookahead
        ],
        # Include ATO info
        "ato_info": {
            "path_id": controller.ato.info.path_id,
            "destination_block_id": controller.ato.info.destination_block_id,
            "destination_station_name": controller.ato.info.destination_station_name,
            "path_heading": controller.ato.info.path_heading,
            "required_cab": controller.ato.info.required_cab,
        }
        if controller.ato.info
        else None,
        # Live speed and stopped predicate are controller-owned per
        # design_new.md "Units contract".
        "current_speed": float(getattr(controller.physics_sync, "current_speed_kmh", 0.0) or 0.0),
        "is_stopped": controller.is_stopped(),
        # ATO ETA: integrates the operational_limit speed curve to the
        # current target stop. None when no path/target/curves.
        "time_to_target_sec": controller._calculate_time_to_target_sec(),
        # Model-scale factor (model cm/s per prototype km/h). Clients use
        # this to convert lookahead distances (model cm) and prototype
        # speeds (km/h) into ETA seconds when time_to_target_sec is None.
        "speed_conversion_factor": get_speed_conversion_factor(),
        # Run instance identifier — populated when a RouteRun is active.
        # Composed as `${route_id}#${run_number}` so the cab display can
        # tell apart multiple runs of the same route.
        "route_run_id": (
            f"{controller.route_executor.execution_state_for(controller.train_id).route_id}"
            f"#{controller.route_executor.execution_state_for(controller.train_id).run_number}"
            if controller.route_executor is not None
            and controller.route_executor.execution_state_for(controller.train_id) is not None
            else None
        ),
        # UI projection: True while an operator route teardown (abort or
        # deactivate) is latched but not yet consumed in Action. The layout
        # renderer tints the train light purple during this window. Excludes
        # power_off (ends in OFF, not a route teardown).
        "route_teardown_pending": (
            controller.route_hooks.phase_latches.is_route_teardown_pending()
        ),
        # True while no wired block backs this train's calculated position —
        # every block its span or its holdings could show current on reads
        # confirmed clear, so the train may physically be gone. The UI marks
        # the train with a question mark and offers removal (design_new.md
        # "Power", Presence unknown).
        "presence_unknown": bool(getattr(controller, "presence_unknown", False)),
        # Live "current path error" projection for the train panel badge and
        # the Quick Route no-path toast. Derived from the event stream through
        # `latest_error_for` — no entity stores an error (design_new.md "Error
        # events"). Nothing marks an event resolved, so this gates on live
        # state instead: it is shown ONLY while the train currently has no live
        # authority (path_backed_run_active is False), which is what keeps a
        # reason from before the train acquired authority off the screen.
        "path_error": (
            (lambda es: (
                controller.route_executor.latest_run_error(es)
                if es is not None
                and not controller.path_projection.path_backed_run_active
                else None
            ))(
                controller.route_executor.execution_state_for(controller.train_id)
                if controller.route_executor is not None
                else None
            )
        ),
    }


def _span_to_dict(controller: "TrainController") -> Optional[Dict[str, Any]]:
    """Serialize `current_span` into the live-position payload, or None."""
    span = getattr(controller.physics_sync, "current_span", None)
    if span is None:
        return None
    return {
        "end_a": {
            "tile": list(span.end_a.tile),
            "distance_cm": span.end_a.distance_cm,
            "entry_edge": span.end_a.entry_edge,
        },
        "end_b": {
            "tile": list(span.end_b.tile),
            "distance_cm": span.end_b.distance_cm,
            "entry_edge": span.end_b.entry_edge,
        },
        "occupied_tiles": [list(t) for t in span.occupied_tiles],
        "occupied_blocks": list(span.occupied_blocks),
        # leading_end is the authority-framed head end ("a"/"b"), or None when
        # there is no live authority (with `received_path_plan is None` the train
        # has no committed forward direction; the span itself carries no head/tail
        # label per the design_new.md TrainSpan schema). Clients render dual
        # end-markers when it is null.
        "leading_end": (
            None
            if controller.path_projection.received_path_plan is None
            else ("a" if controller._head_end_label() == "end_a" else "b")
        ),
    }


def controller_from_dict(
    data: Dict[str, Any], network: "RailwayNetwork"
) -> "TrainController":
    """Reconstruct a TrainController from a dictionary after deserialization."""
    from train_controller import TrainController  # lazy import to avoid a cycle

    handle_str = data.get("handle_position", "N")
    try:
        handle = HandlePosition(handle_str)
    except ValueError:
        handle = HandlePosition.N

    # `powered` is the stored power flag; the derived `train_status` is never
    # restored directly (design_new.md "Controller state machine"). Fall back to
    # the legacy `power_on` alias for older saves that predate the `powered`
    # key.
    powered = bool(data.get("powered", data.get("power_on", False)))

    # Per design_new.md, travel_direction / target_speed / speed_limit_reason
    # / ato_destination_block_id / ato_path_id are derived properties; we
    # do not seed them on construction. ato_destination_block_id / ato_path_id
    # come back through `received_path_plan` once a path is registered with
    # the dispatcher.
    controller = TrainController(
        train_id=data["train_id"],
        network=network,
        # `engaged` and `path_backed_run_active` are not constructor args: the
        # ATO engage bit lives on `commands.engaged` (restored below) and
        # `path_backed_run_active` is a derived @property that recomputes from
        # path/route context, so any persisted value is ignored.
    )
    # `powered` lives on the controller façade; `handle_position` on the
    # `OperationalStateMachine`; the ATO engage bit on `commands.engaged`.
    # Restore after construction.
    controller.commands.engaged = bool(data.get("engaged", False))
    controller.powered = powered
    controller.ops.handle_position = handle
    # Sub-module fields are created in `__post_init__`; restore
    # persisted values after construction.
    controller.eb.active = data.get("eb_active", False)
    controller.eb.cut_out = data.get("eb_cut_out", False)
    controller.manual.manual_mode_type = data.get("manual_mode_type", "simulation")
    controller.manual.manual_power_percent = int(
        data.get("manual_power_percent", 0) or 0
    )
    # Restore which physical end the span is framed head-forward for. The saved
    # span locators are already in that frame, so set the label directly without
    # re-expressing (design_new.md "Reservation-side head derivation"; there is no
    # operator cab).
    controller._framed_head_end = "end_b" if data.get("cab") == "B" else "end_a"
    controller.commands.control_mode = data.get("control_mode", "manual")
    return controller
