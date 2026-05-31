"""
Speed-Profile Routes - CRUD for ``SpeedProfile`` records that govern
acceleration/deceleration/voltage-curve behavior per train.
"""

from flask import jsonify, request

from app_state import AppState


def register(app, state: AppState) -> None:
    """Register speed-profile routes."""

    @app.route("/api/speed-profiles", methods=["GET"])
    def get_speed_profiles():
        """List all speed profiles.

        Returns:
            {
                "profiles": [
                    {
                        "profile_id": str,
                        "name": str,
                        "power_speed_curve": [[power_percent, speed_kmh], ...],
                        "voltage_power_curve": [[track_voltage, power_percent], ...],
                        "min_start_voltage": float,
                        "lighting_voltage": float,
                        "acceleration_rate": float,
                        "deceleration_rate": float,
                        "emergency_brake_rate": float
                    },
                    ...
                ],
                "count": int
            }
        """
        with state.simulation_lock:
            profile_ids = state.speed_profile_registry.list_profiles()
            profiles = []
            for profile_id in profile_ids:
                profile = state.speed_profile_registry.get(profile_id)
                if profile:
                    profiles.append(profile.to_dict())

        return jsonify({"profiles": profiles, "count": len(profiles)})

    @app.route("/api/speed-profiles", methods=["POST"])
    def create_speed_profile():
        """Create a new speed profile.

        Request body:
        {
            "profile_id": str (required, unique),
            "name": str (required),
            "power_speed_curve": [[power_percent, speed_kmh], ...] (optional),
            "voltage_power_curve": [[track_voltage, power_percent], ...] (optional),
            "min_start_voltage": float (optional, default 2.0),
            "lighting_voltage": float (optional, default 2.5),
            "acceleration_rate": float (optional, default 10.0),
            "deceleration_rate": float (optional, default 15.0),
            "emergency_brake_rate": float (optional, default 30.0)
        }

        Returns:
            201 Created with profile data
            400 Bad Request if validation fails
            409 Conflict if profile_id already exists
        """
        from speed_profile import SpeedProfile

        data = request.json or {}

        profile_id = data.get("profile_id")
        name = data.get("name")

        if not profile_id:
            return jsonify({"error": "profile_id is required"}), 400
        if not name:
            return jsonify({"error": "name is required"}), 400

        with state.simulation_lock:
            # Check for duplicate
            if state.speed_profile_registry.get(profile_id):
                return (
                    jsonify(
                        {
                            "error": f"Speed profile with profile_id '{profile_id}' already exists"
                        }
                    ),
                    409,
                )

            # Create profile from data
            try:
                profile = SpeedProfile.from_dict(data)
                state.speed_profile_registry.register(profile)
            except (ValueError, KeyError, TypeError) as e:
                return jsonify({"error": f"Invalid profile data: {str(e)}"}), 400

        return jsonify(profile.to_dict()), 201

    @app.route("/api/speed-profiles/<profile_id>", methods=["GET"])
    def get_speed_profile(profile_id: str):
        """Get a single speed profile by ID.

        Returns:
            200 OK with profile data
            404 Not Found if profile_id doesn't exist
        """
        with state.simulation_lock:
            profile = state.speed_profile_registry.get(profile_id)

        if not profile:
            return jsonify({"error": f"Speed profile '{profile_id}' not found"}), 404

        return jsonify(profile.to_dict())

    @app.route("/api/speed-profiles/<profile_id>", methods=["PUT"])
    def update_speed_profile(profile_id: str):
        """Update an existing speed profile.

        Request body: Same as POST (all fields optional except profile_id must match)

        Returns:
            200 OK with updated profile data
            400 Bad Request if validation fails
            404 Not Found if profile_id doesn't exist
        """
        from speed_profile import SpeedProfile

        data = request.json or {}

        # Ensure profile_id in body matches URL (if provided)
        if "profile_id" in data and data["profile_id"] != profile_id:
            return (
                jsonify(
                    {
                        "error": "profile_id in body must match URL parameter or be omitted"
                    }
                ),
                400,
            )

        with state.simulation_lock:
            # Check if exists
            existing_profile = state.speed_profile_registry.get(profile_id)
            if not existing_profile:
                return (
                    jsonify({"error": f"Speed profile '{profile_id}' not found"}),
                    404,
                )

            # Merge data with profile_id from URL
            data["profile_id"] = profile_id

            # If name not provided, keep existing
            if "name" not in data:
                data["name"] = existing_profile.name

            # Create updated profile
            try:
                updated_profile = SpeedProfile.from_dict(data)
                state.speed_profile_registry.register(updated_profile)
            except (ValueError, KeyError, TypeError) as e:
                return jsonify({"error": f"Invalid profile data: {str(e)}"}), 400

        return jsonify(updated_profile.to_dict())

    @app.route("/api/speed-profiles/<profile_id>", methods=["DELETE"])
    def delete_speed_profile(profile_id: str):
        """Delete a speed profile.

        Cannot delete if:
        - profile_id is "default"
        - Profile is currently assigned to any train

        Returns:
            204 No Content on success
            404 Not Found if profile_id doesn't exist
            409 Conflict if profile is in use or is default
        """
        with state.simulation_lock:
            # Check if profile exists
            profile = state.speed_profile_registry.get(profile_id)
            if not profile:
                return (
                    jsonify({"error": f"Speed profile '{profile_id}' not found"}),
                    404,
                )

            # Check if it's the default profile
            if profile_id == "default":
                return jsonify({"error": "Cannot delete default speed profile"}), 409

            # Check if any train is using this profile
            trains_using = []
            for train in state.network.trains.values():
                if (
                    hasattr(train, "speed_profile_id")
                    and train.speed_profile_id == profile_id
                ):
                    trains_using.append(train.train_id)

            if trains_using:
                return (
                    jsonify(
                        {
                            "error": f"Cannot delete speed profile in use by trains: {', '.join(trains_using)}"
                        }
                    ),
                    409,
                )

            # Delete profile
            success = state.speed_profile_registry.remove(profile_id)
            if not success:
                return (
                    jsonify(
                        {"error": f"Failed to delete speed profile '{profile_id}'"}
                    ),
                    500,
                )

        return "", 204
