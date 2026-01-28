"""
Track Routes - Track CRUD and bulk operations.
"""

from flask import jsonify, request

from app_state import AppState
from switch import Switch
from track import TrackSegment, FORBIDDEN_TRACK_BITS


def register(app, state: AppState) -> None:
    """Register track routes."""

    @app.route("/api/track/<sint:x>/<sint:y>", methods=["GET"])
    def get_track(x, y):
        track = state.network.get_track(x, y)
        if not track:
            return jsonify({"error": "Track not found"}), 404
        return jsonify(track.to_dict())

    @app.route("/api/track/<sint:x>/<sint:y>", methods=["PATCH"])
    def update_track(x, y):
        track = state.network.get_track(x, y)
        if not track:
            return jsonify({"error": "Track not found"}), 404

        data = request.json or {}
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON body"}), 400

        if "track_bits" in data:
            new_bits = data["track_bits"]
            if not track.set_track_bits(int(new_bits)):
                return jsonify(
                    {"success": False, "error": f"Invalid track_bits: {new_bits}"}
                ), 400

            if track.is_switch and track.block_section_id:
                state.network.remove_tile_from_block(x, y)

            if track.is_switch and not track.switch_id:
                switch_id = f"SW_{x}_{y}"
                existing = state.network.get_switch_at(x, y)
                if not existing:
                    switch = Switch(
                        switch_id=switch_id,
                        x=x,
                        y=y,
                        main_length_cm=0.0,
                        diverge_length_cm=0.0,
                        main_radius_cm=None,
                        diverge_radius_cm=None,
                    )
                    state.network.add_switch(switch)
                    track.switch_id = switch_id

        if "dead_end_ports" in data:
            try:
                ports = set(data["dead_end_ports"])
                track.dead_end_ports = {p for p in ports if p in {"N", "E", "S", "W"}}
            except Exception:
                return jsonify(
                    {"error": "dead_end_ports must be a list of N/E/S/W"}
                ), 400

        if "switch_state" in data:
            if track.is_locked_by_path():
                return jsonify(
                    {
                        "error": f"Switch is locked by path {track.locked_by_path}",
                        "locked_by_path": track.locked_by_path,
                        "locked_state": track.locked_state,
                    }
                ), 409
            track.switch_state = int(data["switch_state"])

        if "station_name" in data:
            track.station_name = data["station_name"]

        if "block_section_id" in data:
            track.block_section_id = data["block_section_id"]

        state.network.propagate_from_tile(x, y)

        if "switch_state" in data or "block_section_id" in data:
            state.network.update_signals_from_blocks()

        if track.track_bits == 0 and len(track.dead_end_ports) == 0:
            state.network.remove_track(x, y)
            return jsonify(
                {"status": "success", "message": f"Track at ({x}, {y}) removed (empty)"}
            )

        return jsonify({"status": "success", "track": track.to_dict()})

    @app.route("/api/track/<sint:x>/<sint:y>", methods=["DELETE"])
    def delete_track(x, y):
        track = state.network.get_track(x, y)
        if not track:
            return jsonify({"error": "Track not found"}), 404

        state.network.remove_track(x, y)
        return jsonify({"status": "success", "message": f"Track at ({x}, {y}) removed"})

    @app.route("/api/tracks/bulk", methods=["POST"])
    def add_tracks_bulk():
        data = request.json
        tracks_data = data.get("tracks", [])
        added = []
        updated = []

        for track_data in tracks_data:
            x = track_data.get("x")
            y = track_data.get("y")
            if x is None or y is None:
                continue

            new_bits = track_data.get("track_bits", 0)
            existing_track = state.network.get_track(x, y)

            if existing_track:
                merged_bits = existing_track.track_bits | new_bits
                if merged_bits in FORBIDDEN_TRACK_BITS:
                    continue
                existing_track.set_track_bits(merged_bits)

                if existing_track.is_switch and existing_track.block_section_id:
                    state.network.remove_tile_from_block(x, y)

                if existing_track.is_switch and not existing_track.switch_id:
                    switch_id = f"SW_{x}_{y}"
                    if not state.network.get_switch_at(x, y):
                        switch = Switch(
                            switch_id=switch_id,
                            x=x,
                            y=y,
                            main_length_cm=0.0,
                            diverge_length_cm=0.0,
                        )
                        state.network.add_switch(switch)
                        existing_track.switch_id = switch_id

                state.network.propagate_from_tile(x, y)
                updated.append(existing_track.to_dict())
            else:
                if new_bits in FORBIDDEN_TRACK_BITS:
                    continue
                track = TrackSegment(x=x, y=y, track_bits=new_bits)

                if track_data.get("station_name"):
                    track.station_name = track_data["station_name"]

                state.network.add_track(track)

                if track.is_switch and not track.switch_id:
                    switch_id = f"SW_{x}_{y}"
                    if not state.network.get_switch_at(x, y):
                        switch = Switch(
                            switch_id=switch_id,
                            x=x,
                            y=y,
                            main_length_cm=0.0,
                            diverge_length_cm=0.0,
                        )
                        state.network.add_switch(switch)
                        track.switch_id = switch_id

                added.append(track.to_dict())

        return jsonify(
            {
                "status": "success",
                "added": len(added),
                "updated": len(updated),
                "tracks": added + updated,
            }
        )

    @app.route("/api/tracks/bulk-delete", methods=["POST"])
    def delete_tracks_bulk():
        data = request.json
        positions = data.get("positions", [])
        deleted = []

        for pos in positions:
            if not isinstance(pos, (list, tuple)) or len(pos) < 2:
                continue
            x, y = pos[0], pos[1]
            track = state.network.get_track(x, y)
            if track:
                state.network.remove_track(x, y)
                deleted.append([x, y])

        return jsonify({"status": "success", "deleted": len(deleted)})
