"""
Route Modules Package

This package contains Flask route definitions organized by domain. Every
module exposes a single ``register(app, state)`` function; nothing is
registered until ``register_all`` is called from the app factory.

Module map:
- static_files: ``/`` index + ``/<path:path>`` SPA fallback
- simulation: server lifecycle, /api/network*, /api/event-log
- layouts: /api/layouts/* save/load/list/delete
- tracks: /api/track* and /api/tracks/* CRUD and bulk operations
- segments: /api/segments/* geometry CRUD, tiles, extend, can_extend, end_tiles
- blocks: /api/blocks/* grouping, config/availability, voltage
- signals: /api/signal/* CRUD and validation
- stations: /api/stations/*
- platforms: /api/platforms/*
- running_trains: /api/trains/* — the trains' requests (design_v2.md "Running a train")
- switches: /api/switches/* and /api/tracks/<x>/<y>/switch
"""

from routes import (
    static_files,
    simulation,
    layouts,
    tracks,
    segments,
    blocks,
    signals,
    stations,
    platforms,
    running_trains,
    switches,
    tunnels,
    settings,
    hardware,
)


def register_all(app, state) -> None:
    """Register all route modules with the Flask app.

    Args:
        app: Flask application instance.
        state: AppState container with shared runtime objects.
    """
    static_files.register(app, state)
    simulation.register(app, state)
    layouts.register(app, state)
    tracks.register(app, state)
    segments.register(app, state)
    blocks.register(app, state)
    signals.register(app, state)
    stations.register(app, state)
    platforms.register(app, state)
    running_trains.register(app, state)
    switches.register(app, state)
    tunnels.register(app, state)
    settings.register(app, state)
    hardware.register(app, state)
