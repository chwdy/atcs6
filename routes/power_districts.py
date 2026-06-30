"""Power district routes (design_new.md "Power district").

A power district groups blocks onto one shared power feed. It is the electrical
counterpart of a logical station group: a station groups blocks for operations,
a district groups them for power. The stored model is just an id, a name, and
its member block ids — track voltage is runtime state driven by whichever train
holds the district, not stored config. A block the operator has not grouped is
its own single-block district and is not stored here.
"""

from typing import Any, Dict, List

from flask import jsonify, request

from app_state import AppState


def _build_district_response(network, district) -> Dict[str, Any]:
    """Enriched power-district payload: identity + member blocks with the runtime
    state the panel needs (occupancy, reservation, shared feed voltage)."""
    blocks: List[Dict[str, Any]] = []
    held_by = None
    voltage = 0.0
    for block_id in district.block_ids:
        block = network.blocks.get(block_id)
        if block is None:
            continue
        blocks.append(
            {
                "block_id": block.block_id,
                "name": block.name,
                "state": block.state.value,
                "occupied_by": block.occupied_by,
                "reserved_for_train": block.reserved_for_train,
                "current_voltage": block.current_voltage,
            }
        )
        # The whole district shares one feed (design_new.md "Power"), so any
        # member a train occupies or has reserved fixes the district's holder,
        # and any member's non-zero voltage is the feed voltage.
        holder = block.occupied_by or block.reserved_for_train
        if holder is not None and held_by is None:
            held_by = holder
        if abs(block.current_voltage) > abs(voltage):
            voltage = block.current_voltage

    payload = district.to_dict()
    payload["blocks"] = blocks
    payload["held_by"] = held_by
    payload["voltage"] = voltage
    return payload


def _gen_district_id(network) -> str:
    """Generate a unique district id like ``D_001``."""
    n = 1
    while f"D_{n:03d}" in network.power_districts:
        n += 1
    return f"D_{n:03d}"


def register(app, state: AppState) -> None:
    """Register power-district routes."""

    @app.route("/api/power-districts", methods=["POST"])
    def create_power_district():
        data = request.get_json() or {}
        block_ids = [str(b) for b in (data.get("block_ids") or [])]
        # Non-empty rule (mirrors station groups): a district must group at least
        # one block. To ungroup a block, remove it (it becomes its own
        # single-block district) or delete the district outright.
        if not block_ids:
            return (
                jsonify({"error": "A power district must contain at least one block"}),
                400,
            )
        for block_id in block_ids:
            if block_id not in state.network.blocks:
                return jsonify({"error": f"Block {block_id} not found"}), 400

        # Connected-region rule (design_new.md "Power district" — the electrical
        # counterpart of a block's contiguous tiles): the blocks must form one
        # run directly joined to each other, with no switch between them.
        if not state.network.power_district_blocks_connected(block_ids):
            return (
                jsonify(
                    {
                        "error": "A power district must be one connected run of "
                        "blocks with no switch between them. The selected blocks "
                        "are not all directly adjacent."
                    }
                ),
                400,
            )

        district_id = data.get("district_id") or _gen_district_id(state.network)
        if district_id in state.network.power_districts:
            return (
                jsonify({"error": f"Power district {district_id} already exists"}),
                400,
            )

        district = state.network.create_power_district(
            district_id=district_id,
            block_ids=block_ids,
            name=data.get("name", ""),
        )
        return jsonify(_build_district_response(state.network, district))

    @app.route("/api/power-districts", methods=["GET"])
    def list_power_districts():
        districts = [
            _build_district_response(state.network, d)
            for d in state.network.power_districts.values()
        ]
        return jsonify({"power_districts": districts})

    @app.route("/api/power-districts/<district_id>", methods=["GET"])
    def get_power_district(district_id: str):
        district = state.network.power_districts.get(district_id)
        if not district:
            return jsonify({"error": "Power district not found"}), 404
        return jsonify(_build_district_response(state.network, district))

    @app.route("/api/power-districts/<district_id>", methods=["PATCH"])
    def update_power_district(district_id: str):
        district = state.network.power_districts.get(district_id)
        if not district:
            return jsonify({"error": "Power district not found"}), 404
        data = request.get_json() or {}
        if "name" in data:
            district.name = str(data["name"])
        return jsonify(_build_district_response(state.network, district))

    @app.route("/api/power-districts/<district_id>", methods=["DELETE"])
    def delete_power_district(district_id: str):
        if not state.network.delete_power_district(district_id):
            return jsonify({"error": "Power district not found"}), 404
        return jsonify({"success": True})

    @app.route("/api/power-districts/<district_id>/add-block", methods=["POST"])
    def add_block_to_power_district(district_id: str):
        data = request.get_json() or {}
        # Accept one block (`block_id`) or several (`block_ids`) in one call.
        block_ids = data.get("block_ids")
        if block_ids is None:
            block_ids = [data["block_id"]] if data.get("block_id") else []
        block_ids = [str(b) for b in block_ids]
        if not block_ids:
            return jsonify({"error": "Missing block_id"}), 400

        district = state.network.power_districts.get(district_id)
        if district is None:
            return jsonify({"error": "Power district not found"}), 404
        for block_id in block_ids:
            if block_id not in state.network.blocks:
                return jsonify({"error": f"Block {block_id} not found"}), 404

        # Keep the district one connected run with no switch between members:
        # the existing members plus the new blocks must still form one connected
        # region (see the create rule). Validating the whole union is order-
        # independent, so a multi-block add can join blocks that are only reached
        # through one another.
        union = list(dict.fromkeys(list(district.block_ids) + block_ids))
        if not state.network.power_district_blocks_connected(union):
            return (
                jsonify(
                    {
                        "error": "Those blocks would not keep the district one "
                        "connected run. Every block must stay directly joined "
                        "with no switch between them."
                    }
                ),
                400,
            )

        added_any = False
        for block_id in block_ids:
            if state.network.add_block_to_power_district(district_id, block_id):
                added_any = True
        response = _build_district_response(
            state.network, state.network.power_districts[district_id]
        )
        response["added"] = added_any
        return jsonify(response)

    @app.route("/api/power-districts/<district_id>/remove-block", methods=["POST"])
    def remove_block_from_power_district(district_id: str):
        data = request.get_json() or {}
        block_id = data.get("block_id")
        if not block_id:
            return jsonify({"error": "Missing block_id"}), 400

        # Keep the district one connected run: a removal that would strand the
        # remaining members in two pieces is rejected (remove an end block, or
        # delete the district). Removing the block itself is always fine — it
        # just becomes its own single-block feed.
        district = state.network.power_districts.get(district_id)
        if district is not None and str(block_id) in district.block_ids:
            remaining = [b for b in district.block_ids if b != str(block_id)]
            if len(remaining) >= 2 and not state.network.power_district_blocks_connected(
                remaining
            ):
                return (
                    jsonify(
                        {
                            "error": f"Removing {block_id} would split this district "
                            "into disconnected pieces. Remove an end block, or "
                            "delete the district."
                        }
                    ),
                    400,
                )
        try:
            removed = state.network.remove_block_from_power_district(
                district_id, str(block_id)
            )
        except ValueError as e:
            code = 404 if "not found" in str(e).lower() else 400
            return jsonify({"error": str(e)}), code

        district = state.network.power_districts.get(district_id)
        if district is None:
            # Removing the last block auto-deleted the district (non-empty rule);
            # report the deletion rather than rendering a district that is gone.
            return jsonify(
                {
                    "district_id": district_id,
                    "removed": removed,
                    "district_deleted": True,
                }
            )
        response = _build_district_response(state.network, district)
        response["removed"] = removed
        response["district_deleted"] = False
        return jsonify(response)
