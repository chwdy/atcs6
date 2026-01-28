"""
Signal Routes - Signal CRUD and validation.
"""

from flask import jsonify, request

from app_state import AppState
from railway_signal import SignalType
from signal_validation import get_valid_directions


def register(app, state: AppState) -> None:
    """Register signal routes."""

    @app.route("/api/signal", methods=["POST"])
    def create_signal():
        """Create a new signal at specified position with validation"""
        data = request.json or {}

        x = data.get("x")
        y = data.get("y")
        detection_direction = data.get("detection_direction")

        if x is None or y is None:
            return jsonify({"error": "Missing x or y coordinate"}), 400
        if not detection_direction:
            return jsonify({"error": "Missing detection_direction"}), 400

        # Parse signal type
        signal_type_str = data.get("signal_type", "block")
        try:
            signal_type = SignalType(signal_type_str.lower())
        except ValueError:
            return jsonify({"error": f"Invalid signal_type: {signal_type_str}"}), 400

        name = data.get("name")
        auto_connect = data.get("auto_connect", True)
        signal_id = data.get("signal_id")

        try:
            signal = state.network.add_signal(
                x=int(x),
                y=int(y),
                detection_direction=detection_direction,
                signal_type=signal_type,
                name=name,
                auto_connect=auto_connect,
                signal_id=signal_id,
            )
            return jsonify(signal.to_dict())
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/signal/<signal_id>", methods=["GET"])
    def get_signal(signal_id):
        """Get signal by ID"""
        signal = state.network.get_signal(signal_id)
        if not signal:
            return jsonify({"error": "Signal not found"}), 404
        return jsonify(signal.to_dict())

    @app.route("/api/signal/<signal_id>", methods=["PATCH"])
    def update_signal(signal_id):
        """Update signal properties"""
        signal = state.network.get_signal(signal_id)
        if not signal:
            return jsonify({"error": "Signal not found"}), 404

        data = request.json or {}

        try:
            # Build kwargs for update
            update_kwargs = {}

            if "state" in data:
                update_kwargs["state"] = data["state"]
            if "name" in data:
                update_kwargs["name"] = data["name"]
            if "monitored_block_id" in data:
                update_kwargs["monitored_block_id"] = data["monitored_block_id"]
            if "monitored_switch_id" in data:
                update_kwargs["monitored_switch_id"] = data["monitored_switch_id"]
            if "monitored_signal_id" in data:
                update_kwargs["monitored_signal_id"] = data["monitored_signal_id"]

            updated = state.network.update_signal(signal_id, **update_kwargs)
            return jsonify(updated.to_dict())
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/signal/<signal_id>", methods=["DELETE"])
    def delete_signal(signal_id):
        """Delete signal by ID"""
        if state.network.remove_signal(signal_id):
            return jsonify(
                {"status": "success", "message": f"Signal {signal_id} deleted"}
            )
        return jsonify({"error": "Signal not found"}), 404

    @app.route("/api/signal/<sint:x>/<sint:y>", methods=["GET"])
    def get_signal_at(x, y):
        """Get signal at position"""
        signal = state.network.get_signal_at(x, y)
        if not signal:
            return jsonify({"error": f"No signal at ({x}, {y})"}), 404
        return jsonify(signal.to_dict())

    @app.route("/api/signal/<sint:x>/<sint:y>", methods=["DELETE"])
    def delete_signal_at(x, y):
        """Delete signal at position"""
        if state.network.remove_signal_at(x, y):
            return jsonify(
                {"status": "success", "message": f"Signal at ({x}, {y}) deleted"}
            )
        return jsonify({"error": f"No signal at ({x}, {y})"}), 404

    @app.route("/api/signal/<signal_id>/reachable-blocks", methods=["GET"])
    def get_reachable_blocks(signal_id):
        """Get all blocks that are reachable from a signal.

        Used by the UI to populate the 'block to monitor' dropdown
        with only valid options based on track connectivity.
        """
        signal = state.network.get_signal(signal_id)
        if not signal:
            return jsonify({"error": "Signal not found"}), 404

        reachable = state.network.get_reachable_blocks(signal_id)
        return jsonify({"signal_id": signal_id, "reachable_blocks": reachable})

    @app.route("/api/signal/validate/<sint:x>/<sint:y>", methods=["GET"])
    def validate_signal_at(x, y):
        """Check if a signal can be placed at the given position and return valid detection directions"""
        track = state.network.get_track(x, y)
        if not track:
            return jsonify(
                {
                    "can_place": False,
                    "error": "No track at position",
                    "valid_directions": [],
                    "available_directions": [],
                }
            )

        # Get valid detection directions for this track type
        valid_dirs = get_valid_directions(track.track_bits)

        # Determine max signals for this track type
        # Diagonal tracks (4, 8, 16, 32): 1 signal max
        # Straight tracks (1, 2): 2 signals max
        is_diagonal = track.track_bits in (4, 8, 16, 32)
        max_signals = 1 if is_diagonal else 2
        current_signal_count = len(track.signal_ids)

        # Check if we've already reached the limit
        if current_signal_count >= max_signals:
            return jsonify(
                {
                    "can_place": False,
                    "error": "Diagonal track can only have 1 signal"
                    if is_diagonal
                    else "Maximum signals reached",
                    "valid_directions": valid_dirs,
                    "available_directions": [],
                    "is_diagonal": is_diagonal,
                    "max_signals": max_signals,
                }
            )

        # Check which detection directions already have signals
        available_dirs = []
        for detection_dir in valid_dirs:
            if not state.network.get_signal_at_direction(x, y, detection_dir):
                available_dirs.append(detection_dir)

        can_place = len(available_dirs) > 0

        if not can_place:
            return jsonify(
                {
                    "can_place": False,
                    "error": "All valid directions already have signals",
                    "valid_directions": valid_dirs,
                    "available_directions": [],
                    "is_diagonal": is_diagonal,
                    "max_signals": max_signals,
                }
            )

        return jsonify(
            {
                "can_place": can_place,
                "valid_directions": valid_dirs,
                "available_directions": available_dirs,
                "track_bits": track.track_bits,
                "is_diagonal": is_diagonal,
                "max_signals": max_signals,
            }
        )
