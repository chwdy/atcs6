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
        # design_new.md "SpeedProfile", Where models, sets and instance
        # profiles are kept: after every request that can change state, rewrite
        # the locomotive file when a model, a set or a counter changed. The file is off unless the
        # server named it at startup, so this is a no-op in unit tests.
        if request.method in STATE_CHANGING_METHODS and locomotive_file.enabled_path():
            with state.simulation_lock:
                locomotive_file.save_if_changed(
                    state.network,
                    getattr(state.sim_controller, "notification_center", None),
                )
        return response

    @app.before_request
    def forget_last_lock_release():
        forget = getattr(state.simulation_lock, "forget_release", None)
        if forget is not None:
            forget()

    # Registered after `save_locomotive_file`, so Flask runs it first: it must
    # read this request's last lock release before that hook takes the lock
    # again.
    @app.after_request
    def record_operator_command(response):
        # design_new.md "Detector reading log", Operator commands are written
        # too: one line per state-changing API request, with the pass it ran
        # after, so a replay sends it at the same point in the run.
        log = getattr(state.sim_controller, "detector_reading_log", None)
        if (
            log is None
            or not log.enabled
            or request.method not in STATE_CHANGING_METHODS
            or not request.path.startswith("/api/")
        ):
            return response
        last_release = getattr(state.simulation_lock, "last_release", None)
        after_pass = last_release() if last_release is not None else None
        if after_pass is None:
            after_pass = log.pass_count
        with state.simulation_lock:
            log.record_command(
                method=request.method,
                path=request.path,
                query=request.query_string.decode("utf-8", "replace"),
                body=request.get_json(silent=True),
                status=response.status_code,
                after_pass=after_pass,
            )
        return response

    return app
