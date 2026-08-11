"""Train roster catalog routes (design_new.md "Train Roster").

CRUD over the operator's locomotive catalog (`network.train_roster`). The roster
is configuration data persisted with the layout snapshot; a placement may import
an entry by passing `roster_entry_id` to `POST /api/trains`. POST is
create-or-replace by `entry_id` (unspecified fields reset to defaults).
"""

import uuid

from flask import jsonify, request

from app_state import AppState
from train_roster import RosterEntry


def register(app, state: AppState) -> None:
    def _roster():
        return state.network.train_roster

    def _profiles():
        """Every registered `SpeedProfile`, for the model match."""
        registry = state.network.speed_profiles
        return [
            p
            for p in (registry.get(pid) for pid in registry.list_profiles())
            if p is not None
        ]

    @app.route("/api/roster", methods=["GET"])
    def list_roster():
        return jsonify({"roster": _roster().to_dict()})

    @app.route("/api/roster", methods=["POST"])
    def create_roster_entry():
        data = dict(request.get_json() or {})
        data["entry_id"] = (
            data.get("entry_id") or f"ROSTER-{uuid.uuid4().hex[:6].upper()}"
        )
        entry = RosterEntry.from_dict(data)
        # Profiles are matched by model (design_new.md "Train Roster"): an entry
        # that names no profile links the one already measured for its model, so
        # a second locomotive of that model starts from a real curve. The match
        # is made here, once, as the entry's model is written; every later read
        # follows the stored `speed_profile_id`.
        _roster().link_profile_by_model(entry, _profiles())
        _roster().add_entry(entry)
        return jsonify({"status": "success", "entry": entry.to_dict()})

    @app.route("/api/roster/<entry_id>", methods=["GET"])
    def get_roster_entry(entry_id: str):
        entry = _roster().get_entry(entry_id)
        if entry is None:
            return jsonify({"error": "Roster entry not found"}), 404
        return jsonify(entry.to_dict())

    @app.route("/api/roster/<entry_id>", methods=["DELETE"])
    def delete_roster_entry(entry_id: str):
        if not _roster().remove_entry(entry_id):
            return jsonify({"error": "Roster entry not found"}), 404
        return jsonify({"status": "success"})
