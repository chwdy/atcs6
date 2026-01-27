"""
Route Modules Package

This package contains Flask route definitions organized by domain:
- misc: Static files, network endpoint, simulation control
- tracks: Track CRUD and bulk operations
- blocks: Block management and state
- signals: Signal CRUD and validation
- stations: Station management
- trains: Train state and control
- paths: Pathfinding and path management
"""

from routes import misc, tracks, blocks, signals, stations, trains, paths


def register_all(app, state) -> None:
    """Register all route modules with the Flask app.

    Args:
        app: Flask application instance.
        state: AppState container with shared runtime objects.
    """
    misc.register(app, state)
    tracks.register(app, state)
    blocks.register(app, state)
    signals.register(app, state)
    stations.register(app, state)
    trains.register(app, state)
    paths.register(app, state)
