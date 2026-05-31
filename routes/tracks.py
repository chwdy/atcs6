"""
Track Routes - Track CRUD and bulk operations.
"""

from flask import jsonify, request

from app_state import AppState
from switch import Switch
from track import TrackSegment, FORBIDDEN_TRACK_BITS, is_square_corner_switch


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

        force = bool(data.get("force", False))

        # Impact pre-check for bit/dead-end changes. If the proposed change
        # would orphan a switch/signal/platform, return 409 so the editor
        # can confirm with the user. The caller retries with `force=true`
        # once confirmed; the cleanup then happens below.
        if "track_bits" in data or "dead_end_ports" in data:
            new_bits = (
                int(data["track_bits"]) if "track_bits" in data else track.track_bits
            )
            if "dead_end_ports" in data:
                try:
                    raw = set(data["dead_end_ports"])
                    new_dead_ends = {p for p in raw if p in {"N", "E", "S", "W"}}
                except Exception:
                    return jsonify(
                        {"error": "dead_end_ports must be a list of N/E/S/W"}
                    ), 400
            else:
                new_dead_ends = track.dead_end_ports

            impact = state.network.compute_bit_change_impact(
                x, y, new_bits, new_dead_ends
            )
            has_impact = (
                impact["removes_switch"] is not None
                or bool(impact["removes_signals"])
                or impact["removes_platform"] is not None
            )
            if has_impact and not force:
                return jsonify(
                    {
                        "requires_confirmation": True,
                        "impact": impact,
                    }
                ), 409

            # Confirmed (or no orphans). Clean up entities that would otherwise
            # be left dangling once the tile keeps its bits. When the tile is
            # being fully removed we leave cleanup to remove_track at the end.
            if not impact["tile_will_be_removed"]:
                for sid in impact["removes_signals"]:
                    state.network.remove_signal(sid)
                if impact["removes_switch"]:
                    state.network.remove_switch(impact["removes_switch"])

        if "track_bits" in data:
            new_bits = int(data["track_bits"])
            if is_square_corner_switch(new_bits):
                return jsonify(
                    {
                        "success": False,
                        "error": (
                            "Invalid switch placement: a switch branch is 90° to "
                            "a connecting segment (square corner). Add a diagonal "
                            "(NS/EW) leg so the branch eases at 45°."
                        ),
                    }
                ), 400
            if not track.set_track_bits(new_bits):
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
            # Switch lock ownership lives on `Switch.locks` (design_new.md:651);
            # resolve the owning Switch and reject if it holds any future lock
            # or train-owned protection.
            switch_entity = (
                state.network.switches.get(track.switch_id)
                if track.switch_id is not None
                else None
            )
            if switch_entity is not None and switch_entity.is_locked():
                lock_items = list(switch_entity.locks.items())
                holder_path = lock_items[0][0][0] if lock_items else None
                holder_state = lock_items[0][1][1] if lock_items else None
                return jsonify(
                    {
                        "error": f"Switch is locked by path {holder_path}",
                        "locked_by_path": holder_path,
                        "locked_state": holder_state,
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
                if merged_bits in FORBIDDEN_TRACK_BITS or is_square_corner_switch(merged_bits):
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
                if new_bits in FORBIDDEN_TRACK_BITS or is_square_corner_switch(new_bits):
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
        data = request.json or {}
        positions = data.get("positions", [])
        force = bool(data.get("force", False))

        # Impact pre-check: aggregate the orphans across every tile being
        # removed. Mirrors the single-tile PATCH at /api/track/<x>/<y> so
        # the editor can confirm with the user before the destructive op.
        switches_set = []
        signals_set = []
        platforms_set = []
        seen_switches = set()
        seen_signals = set()
        seen_platforms = set()
        for pos in positions:
            if not isinstance(pos, (list, tuple)) or len(pos) < 2:
                continue
            x, y = pos[0], pos[1]
            track = state.network.get_track(x, y)
            if not track:
                continue
            impact = state.network.compute_bit_change_impact(x, y, 0, set())
            if impact["removes_switch"] and impact["removes_switch"] not in seen_switches:
                seen_switches.add(impact["removes_switch"])
                switches_set.append(impact["removes_switch"])
            for sid in impact["removes_signals"]:
                if sid not in seen_signals:
                    seen_signals.add(sid)
                    signals_set.append(sid)
            if impact["removes_platform"] and impact["removes_platform"] not in seen_platforms:
                seen_platforms.add(impact["removes_platform"])
                platforms_set.append(impact["removes_platform"])

        aggregated = {
            "removes_switches": switches_set,
            "removes_signals": signals_set,
            "removes_platforms": platforms_set,
        }
        has_impact = bool(switches_set or signals_set or platforms_set)
        if has_impact and not force:
            return jsonify(
                {"requires_confirmation": True, "impact": aggregated}
            ), 409

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
