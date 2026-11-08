"""
Flask Application Factory

Definition:
This module provides a factory function for creating the Flask application
with shared configuration and URL converters.

TODO List:
- [x] Register route modules during server breakdown
- [ ] Accept optional runtime configuration inputs
"""

from flask import Flask, request
from flask_cors import CORS
from werkzeug.routing import IntegerConverter

import locomotive_file
from app_state import AppState
from debug_inspector import register_debug_routes
from network_stream import STATE_CHANGING_METHODS
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

    # Register route modules
    register_all(app, state)

    # Register debug inspector routes
    register_debug_routes(app, state)

    @app.after_request
    def save_locomotive_file(response):
        # design_new.md "SpeedProfile", Where profiles and the roster are kept:
        # after every request that can change state, rewrite the locomotive
        # file when a profile or the roster changed. The file is off unless the
        # server named it at startup, so this is a no-op in unit tests.
        if request.method in STATE_CHANGING_METHODS and locomotive_file.enabled_path():
            with state.simulation_lock:
                locomotive_file.save_if_changed(
                    state.network,
                    getattr(state.sim_controller, "notification_center", None),
                )
        return response

    return app
