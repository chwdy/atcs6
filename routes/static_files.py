"""
Static File Routes - Serve index.html and SPA-style static assets.
"""

from flask import send_from_directory

from app_state import AppState


def register(app, state: AppState) -> None:
    """Register static file routes."""

    @app.route("/")
    def index():
        return send_from_directory("static", "index.html")

    @app.route("/<path:path>")
    def static_files(path):
        return send_from_directory("static", path)
