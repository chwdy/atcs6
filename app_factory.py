"""
Flask Application Factory

Definition:
This module provides a factory function for creating the Flask application
with shared configuration and URL converters.

TODO List:
- [ ] Register route modules during server breakdown
- [ ] Accept optional runtime configuration inputs
"""

from flask import Flask
from flask_cors import CORS
from werkzeug.routing import IntegerConverter

from app_state import AppState


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

    return app
