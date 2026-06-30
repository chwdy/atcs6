"""
Route Modules Package

This package contains Flask route definitions organized by domain. Every
module exposes a single ``register(app, state)`` function; nothing is
registered until ``register_all`` is called from the app factory.

Module map:
- static_files: ``/`` index + ``/<path:path>`` SPA fallback
- simulation: server lifecycle, simulation control, /api/network*, /api/scale*
- layouts: /api/layouts/* save/load/list/delete
- tracks: /api/track* and /api/tracks/* CRUD and bulk operations
- blocks: /api/blocks/* CRUD, state cycling, validation
- signals: /api/signal/* CRUD and validation
- stations: /api/stations/*
- platforms: /api/platforms/*
- power_districts: /api/power-districts/* CRUD + add/remove block
- trains: /api/trains/* CRUD and control
- paths: /api/paths/* pathfinding and management
- train_routes: /api/train-routes/* route definitions + lifecycle + viability
- route_runs: /api/route-runs/* read-only live + historical run records
- speed_profiles: /api/speed-profiles/*
- switches: /api/switches/* and /api/tracks/<x>/<y>/switch
- inspector: debug / inspector endpoints
"""

from routes import (
    static_files,
    simulation,
    layouts,
    tracks,
    blocks,
    signals,
    stations,
    platforms,
    power_districts,
    trains,
    paths,
    train_routes,
    route_runs,
    speed_profiles,
    switches,
    tunnels,
    inspector,
    settings,
    roster,
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
    blocks.register(app, state)
    signals.register(app, state)
    stations.register(app, state)
    platforms.register(app, state)
    power_districts.register(app, state)
    trains.register(app, state)
    paths.register(app, state)
    train_routes.register(app, state)
    route_runs.register(app, state)
    speed_profiles.register(app, state)
    switches.register(app, state)
    tunnels.register(app, state)
    inspector.register(app, state)
    settings.register(app, state)
    roster.register(app, state)
