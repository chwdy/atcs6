"""
Flask Application Factory

Definition:
This module provides a factory function for creating the Flask application
with shared configuration and URL converters.

TODO List:
- [x] Register route modules during server breakdown
- [ ] Accept optional runtime configuration inputs
"""

import json
from pathlib import Path

from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.routing import IntegerConverter

from app_state import AppState
from debug_inspector import register_debug_routes
from routes import register_all


class SignedIntConverter(IntegerConverter):
    """Custom converter for signed integers (supports negative coordinates)."""

    regex = r"-?\d+"


def create_app(state: AppState) -> Flask:
    """Create and configure a Flask app instance.

    Args:
        state: Shared application state container.

    Returns:
        Configured Flask application.
    """

    app = Flask(__name__, static_folder="static")
    CORS(app)
    app.url_map.converters["sint"] = SignedIntConverter
    app.extensions["state"] = state

    @app.after_request
    def _log_non_2xx(response):
        if not (200 <= response.status_code < 300):
            lines = [f"\n  !! {request.method} {request.path} -> {response.status_code}"]
            if request.args:
                lines.append(f"     params: {dict(request.args)}")
            json_body = request.get_json(silent=True)
            if json_body:
                lines.append(f"     body:   {json_body}")
            if not response.direct_passthrough:
                resp_text = response.get_data(as_text=True)
                if resp_text:
                    preview = resp_text[:400] + ("..." if len(resp_text) > 400 else "")
                    lines.append(f"     result: {preview}")
            app.logger.warning("\n".join(lines))
        return response

    # Register route modules
    register_all(app, state)

    # Register debug inspector routes
    register_debug_routes(app, state)

    # Dev-only: backs static/icon-preview.html so Claude can read the user's
    # picks back. Safe to delete once the icon set is locked in.
    _icon_selections_path = Path(__file__).parent / "static" / "icon-selections.json"

    @app.post("/api/icon-selections")
    def _save_icon_selections():
        payload = request.get_json(silent=True) or {}
        _icon_selections_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        return jsonify({"ok": True})

    return app
