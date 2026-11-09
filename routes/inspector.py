"""Inspector Routes — debug snapshot of live entity state.

Phase 1 scope (intentional): Train + TrainController only.
Path / Block / Switch / Route inspectors are Phase 2+.

Single endpoint: GET /api/inspector/entity/train/<train_id>
Returns a structured snapshot organized into 4 groups:
    1. Train (config) — static-ish dataclass fields
    2. TrainController (live stored) — per-tick mutating fields
    3. TrainController (derived properties) — @property reads with formula docs
    4. TrainController (mailbox & latches) — pending_*/queued_* with writer/consumer

Each item carries a `source` tag (stored / property / latch / external) and,
where applicable, `computed_from` and `formula_doc` strings to make the
backend → spec → UI link traceable.
"""

from __future__ import annotations

import dataclasses
import enum
import time
from typing import Any, Dict, List, Optional

from flask import jsonify

from app_state import AppState


# ---------------------------------------------------------------------------
# Field lists — enumerate explicitly so we never leak helper internals via
# vars(...) and never miss @property values that aren't in the dataclass dict.
# ---------------------------------------------------------------------------

_TRAIN_CONFIG_FIELDS: List[str] = [
    "train_id",
    "max_speed",
    "num_cars",
    "priority",
    "model",
    "car_number",
    "min_radius_cm",
    "max_slope",
    "length_cm",  # @property on Train but a pure config readout
]

_CONTROLLER_STORED_FIELDS: List[str] = [
    "powered",
    "ops.train_status",
    "_framed_head_end",
    "physics_sync.current_span",
    "physics_sync.current_speed_kmh",
    "eb.active",
    "commands.engaged",
    "commands.control_mode",
    "manual.manual_mode_type",
    "ops.handle_position",
    "manual.manual_power_percent",
    "power_on",
    "atc.clearance_ahead_cm",
    "atc.speed_limit",
    "atc.current_block_speed_limit",
    "atc.safety_margin_cm",
    "atc.signal_lookahead_blocks",
    "ato.info",
    "ops.handle_reason",
]

_CONTROLLER_DERIVED_FIELDS: List[str] = [
    "path_projection.received_path_plan",
    "path_projection.assigned_path_id",
    "path_projection.ato_path_id",
    "path_projection.ato_destination_block_id",
    "path_projection.path_backed_run_active",
    "travel_direction",
    "lookahead",
    "target_speed",
    "speed_limit_reason",
]

# Direct controller fields (queued_progress_reports) remain attached to
# TrainController. The five Action latches live nested under
# `controller.route_hooks.phase_latches`. Each resolver path is set in
# `_LATCH_META`'s `path` key. There is no toast queue to inspect: the
# `Notifications` sub-module writes straight through to the one
# `NotificationCenter` stream and holds no state.
_CONTROLLER_LATCH_FIELDS: List[str] = [
    "queued_progress_reports",
    "pending_path_cancel_request",
    "pending_route_abort",
    "pending_power_off",
    "pending_route_deactivate",
    "pending_emergency_request",
]


# ---------------------------------------------------------------------------
# Documentation tables. Edit these — not handler logic — when formulas drift.
# `formula_doc` strings cite both the implementation file (stable line via
# `@property` blocks) and the design_new.md spec section.
# ---------------------------------------------------------------------------

_DERIVED_DOC: Dict[str, Dict[str, Any]] = {
    "path_projection.received_path_plan": {
        "computed_from": ["path_dispatcher.get_active_path(train_id)"],
        "formula_doc": (
            "train_controller/path_projection.py:42 / design_new.md §Path — "
            "live read of PathDispatcher.get_active_path(train_id); "
            "auto-flips to None when dispatcher transitions path away from "
            "ACTIVE."
        ),
    },
    "path_projection.assigned_path_id": {
        "computed_from": ["received_path_plan.path_id"],
        "formula_doc": (
            "train_controller/path_projection.py:84 — derived from "
            "received_path_plan.path_id (or .id fallback)."
        ),
    },
    "path_projection.ato_path_id": {
        "computed_from": ["assigned_path_id"],
        "formula_doc": "train_controller/path_projection.py:92 — alias for assigned_path_id.",
    },
    "path_projection.ato_destination_block_id": {
        "computed_from": ["received_path_plan.reservation_end_block_id"],
        "formula_doc": (
            "train_controller/path_projection.py:97 — derived from "
            "received_path_plan.reservation_end_block_id "
            "(Path.get_reservation_end_block_id())."
        ),
    },
    "path_projection.path_backed_run_active": {
        "computed_from": [
            "received_path_plan",
            "active route execution state",
            "ato_destination_block_id",
        ],
        "formula_doc": (
            "train_controller/path_projection.py:111 — derived projection; "
            "True when received_path_plan, an active route, or "
            "ato_destination_block_id backs the run "
            "(_has_path_backed_run_context)."
        ),
    },
    "travel_direction": {
        "computed_from": ["received_path_plan", "current_span"],
        "formula_doc": (
            "train_controller/__init__.py:472 / design_new.md ~L2283 — the "
            "authority-facing physical end is the head; direction is from the "
            "opposite end through the head end into the next span resource "
            "ahead. Returns None when received_path_plan is None."
        ),
    },
    "lookahead": {
        "computed_from": [
            "current_span",
            "received_path_plan",
            "network",
            "signals",
            "current_speed_kmh",
            "eb.active",
        ],
        "formula_doc": (
            "train_controller/__init__.py:512 / design_new.md §Lookahead — "
            "pure derived; recomputes on every read via _calculate_lookahead(). "
            "Distance-0 entry carries the ATC operational limit."
        ),
    },
    "target_speed": {
        "computed_from": ["lookahead[0].speed_limit_kmh"],
        "formula_doc": (
            "train_controller/__init__.py:523 / design_new.md §Lookahead — "
            "first lookahead point's speed_limit_kmh; returns 0.0 when inf."
        ),
    },
    "speed_limit_reason": {
        "computed_from": ["_last_speed_limit_reason (cached during atc.speed_limit_now())"],
        "formula_doc": (
            "train_controller/__init__.py:546 — last ATC speed-limit reason "
            "cached during this tick's ATC step; recomputed next tick."
        ),
    },
}

_LATCH_META: Dict[str, Dict[str, str]] = {
    "queued_progress_reports": {
        "path": "physics_sync.queued_progress_reports",
        "writer": "Observe — TrainController.on_physics_updated",
        "consumer": "Observe — PathDispatcher.on_controller_progress (drains list)",
    },
    "pending_path_cancel_request": {
        "path": "route_hooks.phase_latches.path_cancel_request",
        "writer": "Plan reconcile / Action cmd-drain or ATC / API handler",
        "consumer": "Action cancel step (gated by is_stopped() == True)",
    },
    "pending_route_abort": {
        "path": "route_hooks.phase_latches.route_abort",
        "writer": "API / route executor",
        "consumer": "Action — route abort step",
    },
    "pending_power_off": {
        "path": "route_hooks.phase_latches.power_off",
        "writer": "Action — operator command drain",
        "consumer": "Action — power-off step (gated by is_stopped())",
    },
    "pending_route_deactivate": {
        "path": "route_hooks.phase_latches.route_deactivate",
        "writer": "API / route executor",
        "consumer": "Action — route-deactivate step",
    },
    "pending_emergency_request": {
        "path": "route_hooks.phase_latches.emergency_request",
        "writer": "Observe — locked-foreign-entry brake / overrun cut-out",
        "consumer": "Action — command drain (eb.activate / eb.activate_cut_out)",
    },
}


def _resolve_latch(controller: Any, name: str, meta: Dict[str, Any]) -> Any:
    """Resolve a latch value, walking the dotted `path` from meta when set.

    Latches that live directly on TrainController (e.g. `queued_progress_reports`)
    have no `path` and are read by attribute name. Latches that live nested
    under `controller.route_hooks.phase_latches` carry a `path` like
    `phase_latches.path_cancel_request`.
    """
    path = meta.get("path", name)
    obj: Any = controller
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _resolve_attr_path(obj: Any, dotted: str) -> Any:
    """Walk a dotted attribute path, e.g. ``path_projection.received_path_plan``.

    A bare name (no dot) is a single ``getattr``. Used by both the stored
    and derived group readers so sub-module fields (``eb.active``,
    ``path_projection.assigned_path_id``) resolve the same way.
    """
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

# Cap nested-list rendering so payloads stay sane for slow trains with long
# lookaheads or deeply queued reports.
_LIST_CAP = 12


def _safe_property(obj: Any, name: str) -> Dict[str, Any]:
    """Read attribute and capture exceptions verbatim.

    Returns either {"ok": True, "value": <raw>} or
    {"ok": False, "error": "<TypeName>: <msg>"}.

    Errored property reads are themselves debug signal — the inspector
    surfaces them as red rows in the UI.
    """
    try:
        return {"ok": True, "value": _resolve_attr_path(obj, name)}
    except Exception as exc:  # noqa: BLE001 — intentional broad catch
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _serialize_value(value: Any, depth: int = 0) -> Any:
    """Convert arbitrary Python values to JSON-safe shapes.

    Strategy (top to bottom):
      - None / primitives → as-is
      - Enum → .value (snake_case where applicable)
      - dataclass → asdict (recurse to convert nested fields)
      - list/tuple → recurse, cap at _LIST_CAP entries with placeholder
      - dict → recurse over values
      - object with __dict__ → repr() (avoids leaking large internal graphs)
      - fallback → repr()
    """
    if depth > 4:
        return repr(value)

    if value is None or isinstance(value, (bool, int, float, str)):
        # JSON can choke on inf/nan; keep them as strings for inspection.
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return repr(value)
        return value

    if isinstance(value, enum.Enum):
        return value.value if hasattr(value, "value") else value.name

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            raw = dataclasses.asdict(value)
        except Exception:  # noqa: BLE001
            return repr(value)
        return {k: _serialize_value(v, depth + 1) for k, v in raw.items()}

    if isinstance(value, (list, tuple)):
        items = list(value)
        if len(items) > _LIST_CAP:
            head = [_serialize_value(v, depth + 1) for v in items[:_LIST_CAP]]
            head.append(f"... {len(items) - _LIST_CAP} more")
            return head
        return [_serialize_value(v, depth + 1) for v in items]

    if isinstance(value, dict):
        return {str(k): _serialize_value(v, depth + 1) for k, v in value.items()}

    # Any other object — try its __dict__ shallowly.
    if hasattr(value, "__dict__"):
        try:
            shallow = {
                k: _serialize_value(v, depth + 1)
                for k, v in vars(value).items()
                if not k.startswith("_")
            }
            shallow["__type__"] = type(value).__name__
            return shallow
        except Exception:  # noqa: BLE001
            return repr(value)

    return repr(value)


def _type_name(value: Any) -> str:
    """Best-effort type label for the UI."""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, enum.Enum):
        return type(value).__name__
    if isinstance(value, (list, tuple)):
        if value:
            inner = type(value[0]).__name__
            return f"List[{inner}]"
        return "List[Any]"
    if isinstance(value, dict):
        return "Dict"
    return type(value).__name__


def _build_item(
    name: str,
    raw_value: Any,
    source: str,
    *,
    error: Optional[str] = None,
    computed_from: Optional[List[str]] = None,
    formula_doc: Optional[str] = None,
    writer: Optional[str] = None,
    consumer: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble one row of the inspector payload."""
    item: Dict[str, Any] = {
        "name": name,
        "value": _serialize_value(raw_value) if error is None else None,
        "type": _type_name(raw_value) if error is None else "error",
        "source": source,
    }
    if error is not None:
        item["error"] = error
    if computed_from is not None:
        item["computed_from"] = computed_from
    if formula_doc is not None:
        item["formula_doc"] = formula_doc
    if writer is not None:
        item["writer"] = writer
    if consumer is not None:
        item["consumer"] = consumer
    return item


# ---------------------------------------------------------------------------
# Group builders
# ---------------------------------------------------------------------------


def _build_train_config_group(train: Any) -> Dict[str, Any]:
    items = []
    for name in _TRAIN_CONFIG_FIELDS:
        try:
            raw = getattr(train, name)
            items.append(_build_item(name, raw, "stored"))
        except Exception as exc:  # noqa: BLE001
            items.append(
                _build_item(
                    name,
                    None,
                    "stored",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return {"name": "Train (config)", "items": items}


def _build_controller_live_group(controller: Any) -> Dict[str, Any]:
    items = []
    for name in _CONTROLLER_STORED_FIELDS:
        try:
            # Names may be dotted paths after the sub-module split
            # (e.g. ``eb.active``, ``atc.speed_limit``). Walk the dots.
            raw = _resolve_attr_path(controller, name)
            items.append(_build_item(name, raw, "stored"))
        except Exception as exc:  # noqa: BLE001
            items.append(
                _build_item(
                    name,
                    None,
                    "stored",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return {"name": "TrainController (live)", "items": items}


def _build_controller_derived_group(controller: Any) -> Dict[str, Any]:
    items = []
    for name in _CONTROLLER_DERIVED_FIELDS:
        result = _safe_property(controller, name)
        meta = _DERIVED_DOC.get(name, {})
        if result["ok"]:
            items.append(
                _build_item(
                    name,
                    result["value"],
                    "property",
                    computed_from=meta.get("computed_from"),
                    formula_doc=meta.get("formula_doc"),
                )
            )
        else:
            items.append(
                _build_item(
                    name,
                    None,
                    "property",
                    error=result["error"],
                    computed_from=meta.get("computed_from"),
                    formula_doc=meta.get("formula_doc"),
                )
            )
    return {"name": "TrainController (derived)", "items": items}


def _build_controller_latches_group(controller: Any) -> Dict[str, Any]:
    items = []
    for name in _CONTROLLER_LATCH_FIELDS:
        meta = _LATCH_META.get(name, {})
        try:
            raw = _resolve_latch(controller, name, meta)
            items.append(
                _build_item(
                    name,
                    raw,
                    "latch",
                    writer=meta.get("writer"),
                    consumer=meta.get("consumer"),
                )
            )
        except Exception as exc:  # noqa: BLE001
            items.append(
                _build_item(
                    name,
                    None,
                    "latch",
                    error=f"{type(exc).__name__}: {exc}",
                    writer=meta.get("writer"),
                    consumer=meta.get("consumer"),
                )
            )
    return {"name": "TrainController (mailbox & latches)", "items": items}


# Field order + per-row docs for the ATO Debug group. Keys must match what
# `ATOComputer.compute_handle` writes into `controller.ato.debug`.
_ATO_DEBUG_FIELDS: List[Dict[str, Any]] = [
    {"name": "mode", "doc": "branch taken: stopping / stopped / coast / power_up / brake / no_curves / no_train"},
    {"name": "delta_seconds", "doc": "tick duration (s) used to scale the planning horizon"},
    {"name": "current_speed_kmh", "doc": "controller.physics_sync.current_speed_kmh at compute time"},
    {"name": "op_decel_kmh_s", "doc": "operational deceleration = decel_rate × operational_decel_ratio (B6 = 1.00× this rate)"},
    {"name": "decel_rate_kmh_s", "doc": "raw service decel from speed_profile.deceleration_rate (ATC's curve uses this)"},
    {"name": "operational_decel_ratio", "doc": "speed_profile.operational_decel_ratio — fraction of decel_rate ATO plans against"},
    {"name": "curve_at_zero_kmh", "doc": "speed_curves[d=0].operational_limit"},
    {"name": "max_speed_kmh", "doc": "train.max_speed cap"},
    {"name": "atc_speed_limit_kmh", "doc": "controller.atc.speed_limit at compute time"},
    {"name": "block_speed_limit_kmh", "doc": "controller.atc.current_block_speed_limit (None if uncapped)"},
    {"name": "ceiling_at_zero_kmh", "doc": "min(curve@0, max_speed, atc, block) — train must not exceed"},
    {"name": "lookahead_travel_cm", "doc": "speed_cm_s × delta_seconds — distance travelled this tick"},
    {"name": "lookahead_distance_cm", "doc": "max(MIN_LOOKAHEAD_CM, travel × (1 + LOOKAHEAD_HEADROOM)), capped at max_curve_distance"},
    {"name": "max_curve_distance_cm", "doc": "speed_curves[-1].distance_cm — how far the curve extends"},
    {"name": "samples", "doc": "per-distance sample {distance_cm, target_kmh} across [0, look]; binding row is the lowest target"},
    {"name": "binding_distance_cm", "doc": "distance of the sample that produced the highest required_decel"},
    {"name": "binding_target_kmh", "doc": "target speed at the binding distance"},
    {"name": "required_decel_kmh_s", "doc": "max required deceleration across all non-skipped samples"},
    {"name": "multiplier_needed", "doc": "required_decel / op_decel — looked up against BRAKE_MULTIPLIERS"},
    {"name": "deadband_approached", "doc": "True when a sample was skipped only because gap ∈ (0, BRAKE_DEADBAND] — suppresses power-up"},
    {"name": "target_handle", "doc": "notch picked by pick_brake_notch_for_multiplier (or power/coast in non-brake modes)"},
    {"name": "overspeed_at_zero", "doc": "True when speed > ceiling + ato_speed_tolerance(ref)"},
    {"name": "rate_limited", "doc": "True when rate_limit_handle clipped target_handle to one notch closer to current"},
    {"name": "final_handle", "doc": "the handle actually returned by compute_handle this tick"},
]


def _build_ato_debug_group(controller: Any) -> Dict[str, Any]:
    """Surface `controller.ato.debug` (populated by ATOComputer.compute_handle)
    as a fixed-order group so the kinematic decel calc is inspectable per tick.
    """
    debug: Optional[Dict[str, Any]] = getattr(
        getattr(controller, "ato", None), "debug", None
    )
    items: List[Dict[str, Any]] = []
    if not debug:
        items.append(
            _build_item(
                "(empty)",
                None,
                "stored",
                formula_doc=(
                    "ato.debug is populated by ATOComputer.compute_handle; "
                    "appears after the first ATO tick on this controller."
                ),
            )
        )
        return {"name": "ATO Debug (compute_handle)", "items": items}

    for spec in _ATO_DEBUG_FIELDS:
        name = spec["name"]
        if name in debug:
            items.append(
                _build_item(
                    name,
                    debug[name],
                    "stored",
                    formula_doc=spec.get("doc"),
                )
            )
    # Surface any extra keys the navigator wrote that we forgot to enumerate,
    # so future debug additions show up automatically.
    known = {spec["name"] for spec in _ATO_DEBUG_FIELDS}
    for key, value in debug.items():
        if key not in known:
            items.append(_build_item(key, value, "stored"))
    return {"name": "ATO Debug (compute_handle)", "items": items}


# ---------------------------------------------------------------------------
# Top-level snapshot assembly
# ---------------------------------------------------------------------------


def _build_train_inspector(train: Any, controller: Any) -> Dict[str, Any]:
    return {
        "type": "train",
        "id": getattr(train, "train_id", None),
        # A locomotive has one name (design_new.md "Train Entity").
        "label": getattr(train, "car_number", "") or getattr(train, "train_id", ""),
        "groups": [
            _build_train_config_group(train),
            _build_controller_live_group(controller),
            _build_controller_derived_group(controller),
            _build_controller_latches_group(controller),
            _build_ato_debug_group(controller),
        ],
        "fetched_at": time.time(),
    }


# ---------------------------------------------------------------------------
# Blueprint registration
# ---------------------------------------------------------------------------


def register(app, state: AppState) -> None:
    """Register inspector routes."""

    def _get_train_controller(train):
        # Mirrors routes/trains.py:19 — existing project convention.
        return state.sim_controller._get_or_create_controller(train)

    @app.route("/api/inspector/entity/train/<train_id>", methods=["GET"])
    def get_train_inspector(train_id: str):
        if not getattr(state, "network", None):
            return jsonify({"error": "Network not initialized"}), 503

        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        if not getattr(state, "sim_controller", None):
            return jsonify({"error": "Simulation not initialized"}), 503

        controller = _get_train_controller(train)
        return jsonify(_build_train_inspector(train, controller))
