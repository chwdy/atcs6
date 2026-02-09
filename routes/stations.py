"""Logical station grouping routes.

Station groups are logical containers of one or more physical platforms.
Physical platform CRUD stays under /api/platform.
"""

from typing import Any, Dict, List

from flask import jsonify, request

from app_state import AppState


def _build_station_response(network, station) -> Dict[str, Any]:
    """Build enriched station-group payload for API responses."""
    platforms: List[Dict[str, Any]] = []
    for platform_id in station.platform_ids:
        platform = network.get_platform(platform_id)
        if not platform:
            continue
        block_ids = list(platform.get_all_assigned_blocks())
        if not block_ids:
            for x, y in platform.positions:
                track = network.get_track(x, y)
                if track and track.block_section_id:
                    block_ids.append(track.block_section_id)
            block_ids = sorted(set(block_ids))

        platforms.append(
            {
                "platform_id": platform.platform_id,
                "name": platform.name,
                "positions": [list(pos) for pos in platform.positions],
                "block_ids": block_ids,
            }
        )

    center_position = station.get_center_position(network)
    all_block_ids = sorted(station.get_all_block_ids(network))

    payload = station.to_dict()
    payload["platforms"] = platforms
    payload["center_position"] = list(center_position) if center_position else None
    payload["all_block_ids"] = all_block_ids
    return payload


def register(app, state: AppState) -> None:
    """Register logical station-group routes."""

    @app.route("/api/station", methods=["POST"])
    def create_station_group():
        data = request.get_json() or {}
        try:
            station = state.network.create_station_group(
                station_id=data.get("station_id"),
                name=data.get("name", ""),
                platform_ids=data.get("platform_ids", []),
            )
            return jsonify(_build_station_response(state.network, station))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/station", methods=["GET"])
    def list_station_groups():
        stations = [
            _build_station_response(state.network, station)
            for station in state.network.station_groups.values()
        ]
        return jsonify({"stations": stations})

    @app.route("/api/station/<station_id>", methods=["GET"])
    def get_station_group(station_id: str):
        station = state.network.station_groups.get(station_id)
        if not station:
            return jsonify({"error": "Station not found"}), 404
        return jsonify(_build_station_response(state.network, station))

    @app.route("/api/station/<station_id>", methods=["PATCH"])
    def update_station_group(station_id: str):
        station = state.network.station_groups.get(station_id)
        if not station:
            return jsonify({"error": "Station not found"}), 404

        data = request.get_json() or {}
        if "name" in data:
            station.name = data["name"]

        if "platform_ids" in data:
            try:
                platform_ids = list(data.get("platform_ids") or [])
                unique_platform_ids = []
                for platform_id in platform_ids:
                    if platform_id in unique_platform_ids:
                        continue
                    if state.network.get_platform(platform_id) is None:
                        raise ValueError(f"Platform {platform_id} not found")
                    owner = state.network.get_station_for_platform(platform_id)
                    if owner and owner != station_id:
                        raise ValueError(
                            f"Platform {platform_id} already belongs to station {owner}"
                        )
                    unique_platform_ids.append(platform_id)
                station.platform_ids = unique_platform_ids
            except ValueError as e:
                return jsonify({"error": str(e)}), 400

        return jsonify(_build_station_response(state.network, station))

    @app.route("/api/station/<station_id>", methods=["DELETE"])
    def delete_station_group(station_id: str):
        if not state.network.delete_station_group(station_id):
            return jsonify({"error": "Station not found"}), 404
        return jsonify({"success": True})

    @app.route("/api/station/<station_id>/add-platform", methods=["POST"])
    def add_platform_to_station_group(station_id: str):
        data = request.get_json() or {}
        platform_id = data.get("platform_id")
        if not platform_id:
            return jsonify({"error": "Missing platform_id"}), 400

        try:
            added = state.network.add_platform_to_station_group(station_id, platform_id)
            station = state.network.station_groups.get(station_id)
            response = _build_station_response(state.network, station)
            response["added"] = added
            return jsonify(response)
        except ValueError as e:
            if "not found" in str(e).lower():
                return jsonify({"error": str(e)}), 404
            return jsonify({"error": str(e)}), 400

    @app.route("/api/station/<station_id>/remove-platform", methods=["POST"])
    def remove_platform_from_station_group(station_id: str):
        data = request.get_json() or {}
        platform_id = data.get("platform_id")
        if not platform_id:
            return jsonify({"error": "Missing platform_id"}), 400

        try:
            removed = state.network.remove_platform_from_station_group(
                station_id, platform_id
            )
            station = state.network.station_groups.get(station_id)
            response = _build_station_response(state.network, station)
            response["removed"] = removed
            return jsonify(response)
        except ValueError as e:
            if "not found" in str(e).lower():
                return jsonify({"error": str(e)}), 404
            return jsonify({"error": str(e)}), 400
