"""
Debug Inspector - Generic object inspector for runtime debugging.

Definition:
Provides a single API endpoint that can traverse the Python object graph
starting from AppState, with depth-limited JSON serialization. Powers the
standalone debug UI at /debug.

TODO List:
- [ ] Add variable watch list support
- [ ] Add WebSocket push for real-time updates
"""

import dataclasses
import types
from collections import deque
from enum import Enum
from typing import Any, Optional, Set

from flask import Flask, jsonify, request

from app_state import AppState

# Hard limits
MAX_DEPTH = 3
MAX_ITEMS = 500
MAX_STR_LEN = 200


def resolve_path(root: Any, path: str) -> Any:
    """Resolve a dotted path string to a Python object.

    Traverses dicts (by key), lists/tuples (by index), and object attributes.
    Supports tuple dict keys like '3,5' for grid lookups.

    Args:
        root: Starting object (typically AppState)
        path: Dotted path like 'network.trains.T1.max_speed'

    Returns:
        The resolved Python object

    Raises:
        KeyError: If any path segment cannot be resolved
    """
    if not path:
        return root

    current = root
    for segment in path.split("."):
        # Dict key lookup
        if isinstance(current, dict):
            if segment in current:
                current = current[segment]
                continue
            # Try tuple key: "3,5" → (3, 5)
            if "," in segment:
                try:
                    parts = segment.split(",")
                    tuple_key = tuple(int(p.strip()) for p in parts)
                    if tuple_key in current:
                        current = current[tuple_key]
                        continue
                except (ValueError, TypeError):
                    pass
            raise KeyError(f"Key '{segment}' not found in dict")

        # List/tuple index
        if isinstance(current, (list, tuple)):
            try:
                idx = int(segment)
                current = current[idx]
                continue
            except (ValueError, IndexError):
                raise KeyError(
                    f"Index '{segment}' not valid for {type(current).__name__}"
                )

        # Object attribute
        if hasattr(current, segment):
            current = getattr(current, segment)
            continue

        raise KeyError(f"'{segment}' not found on {type(current).__name__}")

    return current


def serialize_object(
    obj: Any,
    depth: int = 1,
    max_items: int = MAX_ITEMS,
    _seen: Optional[Set[int]] = None,
) -> Any:
    """Serialize a Python object to a JSON-safe representation.

    Depth-limited: at depth=0, only type info and keys are returned.
    At depth>0, child values are recursively serialized.

    Args:
        obj: The object to serialize
        depth: How many levels deep to expand (0 = type info only)
        max_items: Maximum items per collection before truncation
        _seen: Internal set for circular reference detection

    Returns:
        JSON-safe dict/list/primitive
    """
    if _seen is None:
        _seen = set()

    # Primitives — pass through
    if obj is None:
        return None
    if isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return obj[:MAX_STR_LEN] + "..." if len(obj) > MAX_STR_LEN else obj

    # Enum — show value and class
    if isinstance(obj, Enum):
        return {"_type": "enum", "value": obj.value, "class": type(obj).__name__}

    # Circular reference guard
    obj_id = id(obj)
    if obj_id in _seen:
        return {"_type": "circular", "repr": repr(obj)[:MAX_STR_LEN]}
    _seen.add(obj_id)

    try:
        return _serialize_complex(obj, depth, max_items, _seen)
    finally:
        _seen.discard(obj_id)


def _serialize_complex(obj: Any, depth: int, max_items: int, _seen: Set[int]) -> Any:
    """Serialize non-primitive objects. Called by serialize_object."""

    # Dict
    if isinstance(obj, dict):
        keys = list(obj.keys())
        if depth <= 0:
            return {
                "_type": "dict",
                "length": len(keys),
                "keys": [str(k) for k in keys[:max_items]],
                "_truncated": len(keys) > max_items,
            }
        items = {}
        for k in keys[:max_items]:
            items[str(k)] = serialize_object(obj[k], depth - 1, max_items, _seen)
        result = {"_type": "dict", "length": len(keys), "items": items}
        if len(keys) > max_items:
            result["_truncated"] = True
        return result

    # List / tuple
    if isinstance(obj, (list, tuple)):
        type_name = "list" if isinstance(obj, list) else "tuple"
        if depth <= 0:
            return {"_type": type_name, "length": len(obj)}
        items = [
            serialize_object(item, depth - 1, max_items, _seen)
            for item in obj[:max_items]
        ]
        result = {"_type": type_name, "length": len(obj), "items": items}
        if len(obj) > max_items:
            result["_truncated"] = True
        return result

    # Set / frozenset
    if isinstance(obj, (set, frozenset)):
        type_name = "set" if isinstance(obj, set) else "frozenset"
        if depth <= 0:
            return {"_type": type_name, "length": len(obj)}
        items_list = sorted(obj, key=lambda x: str(x))[:max_items]
        items = [
            serialize_object(item, depth - 1, max_items, _seen) for item in items_list
        ]
        result = {"_type": type_name, "length": len(obj), "items": items}
        if len(obj) > max_items:
            result["_truncated"] = True
        return result

    # Deque
    if isinstance(obj, deque):
        if depth <= 0:
            return {"_type": "deque", "length": len(obj), "maxlen": obj.maxlen}
        items = [
            serialize_object(item, depth - 1, max_items, _seen)
            for item in list(obj)[:max_items]
        ]
        result = {
            "_type": "deque",
            "length": len(obj),
            "maxlen": obj.maxlen,
            "items": items,
        }
        if len(obj) > max_items:
            result["_truncated"] = True
        return result

    # Callable (skip class objects themselves)
    if callable(obj) and not isinstance(obj, type):
        return {"_type": "method", "name": getattr(obj, "__name__", str(obj))}

    # Module / class — type info only
    if isinstance(obj, (type, types.ModuleType)):
        return {"_type": type(obj).__name__, "name": getattr(obj, "__name__", str(obj))}

    # Dataclass instance
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        class_name = type(obj).__name__
        dc_fields = dataclasses.fields(obj)
        if depth <= 0:
            return {
                "_type": "object",
                "class": class_name,
                "fields": [f.name for f in dc_fields],
            }
        fields = {}
        for f in dc_fields:
            fields[f.name] = serialize_object(
                getattr(obj, f.name), depth - 1, max_items, _seen
            )
        return {"_type": "object", "class": class_name, "fields": fields}

    # Generic object with __dict__
    if hasattr(obj, "__dict__"):
        class_name = type(obj).__name__
        attrs = {k: v for k, v in obj.__dict__.items() if not k.startswith("__")}
        if depth <= 0:
            return {
                "_type": "object",
                "class": class_name,
                "fields": list(attrs.keys()),
            }
        fields = {}
        for k, v in list(attrs.items())[:max_items]:
            fields[k] = serialize_object(v, depth - 1, max_items, _seen)
        result = {"_type": "object", "class": class_name, "fields": fields}
        if len(attrs) > max_items:
            result["_truncated"] = True
        return result

    # Fallback
    return {"_type": "unknown", "repr": repr(obj)[:MAX_STR_LEN]}


def register_debug_routes(app: Flask, state: AppState) -> None:
    """Register debug inspector routes on the Flask app.

    Args:
        app: Flask application instance
        state: Shared application state
    """

    @app.route("/api/debug/inspect", methods=["GET"])
    def debug_inspect():
        """Generic object inspector.

        Query params:
            path: Dotted path from AppState root (default: '' = AppState itself)
            depth: Serialization depth 0-3 (default: 1)
        """
        path = request.args.get("path", "")
        depth = min(request.args.get("depth", 1, type=int), MAX_DEPTH)

        try:
            obj = resolve_path(state, path)
        except KeyError as e:
            return jsonify({"error": str(e)}), 404

        result = serialize_object(obj, depth=depth)
        return jsonify({"path": path, "depth": depth, "data": result})

    @app.route("/api/debug/summary", methods=["GET"])
    def debug_summary():
        """Quick entity counts for the landing page."""
        network = state.network
        return jsonify(
            {
                "tracks": len(network.tracks),
                "blocks": len(network.blocks),
                "signals": len(network.signals),
                "switches": len(network.switches),
                "trains": len(network.trains),
                "platforms": len(network.platforms),
                "station_groups": len(network.station_groups),
                "routes": len(state.sim_controller.route_executor.route_registry),
                "active_paths": len(state.path_dispatcher.active_paths),
                "simulation_running": state.simulation_running,
            }
        )

    @app.route("/api/debug/block-history", methods=["GET"])
    def debug_block_history():
        """Recent block state transitions from BlockManager.history.

        Query params:
            limit: Max entries to return (default: 50)
        """
        network = state.network
        bm = network.block_manager
        if not bm or not hasattr(bm, "history"):
            return jsonify({"history": [], "count": 0, "total": 0})

        limit = request.args.get("limit", 50, type=int)
        history = list(bm.history)[-limit:]

        return jsonify(
            {
                "history": history,
                "count": len(history),
                "total": len(bm.history),
            }
        )

    @app.route("/debug")
    def debug_page():
        """Serve the standalone debug inspector UI."""
        return app.send_static_file("debug.html")
