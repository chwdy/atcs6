"""
Static File Routes - Serve index.html and SPA-style static assets.
"""

from flask import send_from_directory

from app_state import AppState


def register(app, state: AppState) -> None:
    """Register static file routes."""

    def _no_cache(response):
        # The built bundle (static/dist/main.js) and the CSS are referenced
        # without a version hash, and browsers cache `<script type="module">`
        # aggressively — so a rebuilt bundle would not show up on a normal
        # refresh. `no-cache` keeps the cache but forces the browser to
        # revalidate on every load; Flask answers 304 from the ETag/Last-Modified
        # `send_from_directory` already sets when the file is unchanged, so an
        # edited asset always loads on a plain reload with no full re-download.
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.route("/")
    def index():
        return _no_cache(send_from_directory("static", "index.html"))

    @app.route("/<path:path>")
    def static_files(path):
        return _no_cache(send_from_directory("static", path))
