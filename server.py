import time
import threading
import json
import uuid
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.routing import IntegerConverter
from network import RailwayNetwork
from track import (
    TrackSegment,
    FORBIDDEN_TRACK_BITS,
    TRACK_NS,
    TRACK_EW,
    TRACK_NE,
    TRACK_NW,
    TRACK_SE,
    TRACK_SW,
)
from train import Train, TrainSchedule, ScheduleStop, TrainCar
from train_route import Route, Waypoint, TimingMode, RouteMode
from path_utils import assign_path_to_train_with_prepend, assign_complete_path_to_train
from enums import (
    Direction,
    TrackType,
    TrainState,
    SignalState as EnumSignalState,
    SwitchPosition,
)
from railway_signal import Signal, SignalType, SignalState
from signal_validation import validate_signal_placement, get_valid_directions
from datetime import datetime, timedelta
import demo_main
from persistence import PersistenceManager
from path_dispatcher import PathDispatcher

from speed_profile import SpeedProfile, SpeedProfileRegistry

app = Flask(__name__, static_folder="static")
CORS(app)


# Custom converter for signed integers (supports negative coordinates)
class SignedIntConverter(IntegerConverter):
    regex = r"-?\d+"


app.url_map.converters["sint"] = SignedIntConverter

from simulation import SimulationController

# Global state
network = RailwayNetwork()
speed_profile_registry = SpeedProfileRegistry()
# Initialize with demo data for now so it's not empty
# In a real app, we might want to start empty or load from save


def run_startup_tests():
    """Run comprehensive tests before starting the server.

    This ensures no regressions have been introduced.
    Tests run:
    1. Block validation tests (already present)
    2. Full backend test suite (if pytest available)
    """
    import subprocess
    import sys

    print("\n" + "=" * 70)
    print("Server Startup - Running Regression Tests")
    print("=" * 70)

    # Test 1: Block validation (always run - no dependencies)
    print("\n[1/2] Running Block Validation Tests...")
    print("-" * 70)
    try:
        demo_main.test_block_validation()
        print("✅ Block validation tests PASSED")
    except Exception as e:
        print(f"❌ Block validation tests FAILED: {e}")
        print("\n🛑 BLOCKING: Cannot start server with failing validation tests.")
        sys.exit(1)

    # Test 2: Full backend test suite (if pytest available)
    print("\n[2/2] Running Full Backend Test Suite...")
    print("-" * 70)
    try:
        result = subprocess.run(
            ["python3", "-m", "pytest", "tests/test_backend.py", "-v", "--tb=short"],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            # Check if pytest is not installed
            if "No module named pytest" in result.stderr:
                print("⚠️  WARNING: pytest not installed. Skipping full test suite.")
                print("   Install with: pip install pytest")
            else:
                print("❌ Backend tests FAILED!")
                print("\nTest output:")
                print(
                    result.stdout[-2000:]
                    if len(result.stdout) > 2000
                    else result.stdout
                )
                print("\n🛑 BLOCKING: Cannot start server with failing tests.")
                sys.exit(1)
        else:
            print("✅ Full backend test suite PASSED")
            # Show summary line
            if result.stdout:
                lines = result.stdout.split("\n")
                for line in lines:
                    if "passed" in line.lower() or "failed" in line.lower():
                        print(f"   {line.strip()}")

    except subprocess.TimeoutExpired:
        print("❌ Backend tests TIMEOUT (exceeded 60 seconds)")
        print("\n🛑 BLOCKING: Cannot start server with timeout.")
        sys.exit(1)
    except Exception as e:
        print(f"⚠️  WARNING: Could not run full test suite: {e}")
        print("   Continuing with just block validation...")

    print("\n" + "=" * 70)
    print("✅ All Startup Tests Passed - Initializing Server")
    print("=" * 70)


# Run startup tests
run_startup_tests()

# After tests pass, create demo network

network = demo_main.create_comprehensive_demo()
network = demo_main.create_track_showcase(network)

path_dispatcher = PathDispatcher(network)
demo_main.setup_demo_train(network, path_dispatcher)
sim_controller = SimulationController(network, path_dispatcher)
persistence_manager = PersistenceManager()
simulation_lock = threading.Lock()
simulation_running = False
simulation_speed = 1.0
last_update_time = datetime.now()


def simulation_loop():
    global last_update_time
    while True:
        current_time = datetime.now()
        delta_seconds = (current_time - last_update_time).total_seconds()
        last_update_time = current_time

        if simulation_running:
            # Scale delta by simulation speed
            sim_delta = delta_seconds * simulation_speed
            with simulation_lock:
                sim_controller.update(sim_delta)

        time.sleep(0.1)


# Start simulation thread
sim_thread = threading.Thread(target=simulation_loop, daemon=True)
sim_thread.start()

# ============================================================================
# Auto-Save Configuration
# ============================================================================
AUTOSAVE_INTERVAL = 300  # 5 minutes in seconds
AUTOSAVE_FILENAME = "_autosave"
last_autosave_time = time.time()


def autosave_loop():
    """Background thread that saves to _autosave every 5 minutes."""
    global last_autosave_time
    while True:
        time.sleep(60)  # Check every minute
        if time.time() - last_autosave_time >= AUTOSAVE_INTERVAL:
            with simulation_lock:
                try:
                    result = persistence_manager.save_merged_layout(
                        AUTOSAVE_FILENAME, network, "Auto-saved"
                    )
                    last_autosave_time = time.time()
                    msg = f"[AutoSave] Saved at {datetime.now().isoformat()}"
                    if result.get("steps_error"):
                        msg += f" (steps failed: {result['steps_error']})"
                    print(msg)
                except Exception as e:
                    print(f"[AutoSave] Failed: {e}")


# Start autosave thread
autosave_thread = threading.Thread(target=autosave_loop, daemon=True)
autosave_thread.start()


# ============================================================================
# Helper Functions
# ============================================================================


def _convert_path_tiles_to_train_path(tiles):
    from path_utils import convert_tiles_to_train_path

    return convert_tiles_to_train_path(tiles)


def _sort_block_positions(positions, network):
    """
    Sort block positions in traversal order.

    Algorithm:
    1. Count track bit occurrences to detect dominant orientation
    2. For NS: sort by y (descending)
    3. For EW: sort by x (ascending)
    4. For NE_SW diagonal: sort by (x+y)
    5. For NW_SE diagonal: sort by (y-x)
    6. Fallback: BFS traversal for complex shapes

    Args:
        positions: List of (x, y) tuples
        network: RailwayNetwork instance

    Returns:
        List of (x, y) tuples sorted in traversal order
    """
    from collections import Counter
    from track import BIT_TO_GROUP

    if len(positions) <= 1:
        return positions

    # Count track bit groups to detect dominant orientation
    orientation_counts = Counter()
    for x, y in positions:
        track = network.get_track(x, y)
        if track:
            for bit_idx, group in BIT_TO_GROUP.items():
                if track.track_bits & (1 << bit_idx):
                    orientation_counts[group] += 1

    if not orientation_counts:
        return positions

    # Get dominant orientation
    dominant_orientation = orientation_counts.most_common(1)[0][0]

    # Sort based on orientation
    if dominant_orientation == "NS":
        # North-South: sort by y descending (north to south)
        return sorted(positions, key=lambda pos: -pos[1])
    elif dominant_orientation == "EW":
        # East-West: sort by x ascending (west to east)
        return sorted(positions, key=lambda pos: pos[0])
    elif dominant_orientation == "NE_SW":
        # Northeast-Southwest diagonal: sort by (x-y) ascending, then by x ascending
        return sorted(positions, key=lambda pos: (pos[0] - pos[1], pos[0]))
    elif dominant_orientation == "NW_SE":
        # Northwest-Southeast diagonal: sort by (x+y) ascending, then by x ascending
        return sorted(positions, key=lambda pos: (pos[0] + pos[1], pos[0]))
    else:
        # Fallback: return as-is
        return positions


# ============================================================================
# API Routes
# ============================================================================


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory("static", path)


@app.route("/api/network", methods=["GET"])
def get_network():
    return jsonify(network.to_dict())


@app.route("/api/track/<sint:x>/<sint:y>", methods=["GET"])
def get_track(x, y):
    """Get a single track segment"""
    track = network.get_track(x, y)
    if not track:
        return jsonify({"error": "Track not found"}), 404

    # Return full track data using to_dict() which includes switch_points
    return jsonify(track.to_dict())


@app.route("/api/track/<sint:x>/<sint:y>", methods=["PATCH"])
def update_track(x, y):
    """Update a single track segment (6-bit track_bits system)"""
    track = network.get_track(x, y)
    if not track:
        return jsonify({"error": "Track not found"}), 404

    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON body"}), 400

    # === Geometry updates ===
    if "track_bits" in data:
        new_bits = data["track_bits"]
        # Validate and set bits
        if not track.set_track_bits(int(new_bits)):
            return jsonify(
                {"success": False, "error": f"Invalid track_bits: {new_bits}"}
            ), 400

        # If track became a switch, remove from block (switches are block boundaries)
        if track.is_switch and track.block_section_id:
            network.remove_tile_from_block(x, y)

        # Auto-create switch entity if track is now a switch and doesn't have one
        if track.is_switch and not track.switch_id:
            from switch import Switch

            switch_id = f"SW_{x}_{y}"
            # Check if switch already exists (shouldn't, but be safe)
            existing = network.get_switch_at(x, y)
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
                network.add_switch(switch)
                # Link track to switch
                track.switch_id = switch_id

    # === Dead-ends (buffer stops) ===
    if "dead_end_ports" in data:
        # Replace set directly with normalized values
        try:
            ports = set(data["dead_end_ports"])
            track.dead_end_ports = {p for p in ports if p in {"N", "E", "S", "W"}}
        except Exception:
            return jsonify({"error": "dead_end_ports must be a list of N/E/S/W"}), 400

    # === Switch state (simplified single int: 0=MAIN, 1=DIVERGE) ===
    if "switch_state" in data:
        # Check if switch is locked by a path - reject if locked
        if track.is_locked_by_path():
            return jsonify(
                {
                    "error": f"Switch is locked by path {track.locked_by_path}",
                    "locked_by_path": track.locked_by_path,
                    "locked_state": track.locked_state,
                }
            ), 409  # 409 Conflict
        track.switch_state = int(data["switch_state"])

    # === Station properties ===
    if "station_name" in data:
        track.station_name = data["station_name"]  # Can be None to clear

    # === Block section ===
    if "block_section_id" in data:
        track.block_section_id = data["block_section_id"]  # Can be None to clear

    # Propagate changes to this tile and neighbors
    network.propagate_from_tile(x, y)

    # Recalculate signal states after switch or block changes
    if "switch_state" in data or "block_section_id" in data:
        network.update_signals_from_blocks()

    # If track has no geometry and no dead-ends, delete it
    if track.track_bits == 0 and len(track.dead_end_ports) == 0:
        network.remove_track(x, y)
        return jsonify(
            {"status": "success", "message": f"Track at ({x}, {y}) removed (empty)"}
        )

    return jsonify({"status": "success", "track": track.to_dict()})


@app.route("/api/track/<sint:x>/<sint:y>", methods=["DELETE"])
def delete_track(x, y):
    """Delete a single track segment"""
    track = network.get_track(x, y)
    if not track:
        return jsonify({"error": "Track not found"}), 404

    network.remove_track(x, y)
    return jsonify({"status": "success", "message": f"Track at ({x}, {y}) removed"})


@app.route("/api/tracks/bulk", methods=["POST"])
def add_tracks_bulk():
    """Add multiple track segments at once (for drag-to-build).
    If a track already exists at a position, merge the new track_bits.

    Requires track_bits: int - 6-bit track geometry
    """
    from switch import Switch

    data = request.json
    tracks_data = data.get("tracks", [])
    added = []
    updated = []

    for track_data in tracks_data:
        x = track_data.get("x")
        y = track_data.get("y")
        if x is None or y is None:
            continue

        # Get track_bits (required)
        new_bits = track_data.get("track_bits", 0)

        existing_track = network.get_track(x, y)

        if existing_track:
            # Merge new bits with existing (OR operation)
            merged_bits = existing_track.track_bits | new_bits
            if merged_bits in FORBIDDEN_TRACK_BITS:
                # Skip forbidden configurations
                continue
            existing_track.set_track_bits(merged_bits)

            # If track became a switch, remove from block (switches are block boundaries)
            if existing_track.is_switch and existing_track.block_section_id:
                network.remove_tile_from_block(x, y)

            # Auto-create switch entity if track is now a switch
            if existing_track.is_switch and not existing_track.switch_id:
                switch_id = f"SW_{x}_{y}"
                if not network.get_switch_at(x, y):
                    switch = Switch(
                        switch_id=switch_id,
                        x=x,
                        y=y,
                        main_length_cm=0.0,
                        diverge_length_cm=0.0,
                    )
                    network.add_switch(switch)
                    existing_track.switch_id = switch_id

            network.propagate_from_tile(x, y)
            updated.append(existing_track.to_dict())
        else:
            # Create new track with track_bits
            if new_bits in FORBIDDEN_TRACK_BITS:
                continue
            track = TrackSegment(x=x, y=y, track_bits=new_bits)

            # Handle additional properties
            if track_data.get("station_name"):
                track.station_name = track_data["station_name"]

            network.add_track(track)

            # Auto-create switch entity if new track is a switch
            if track.is_switch and not track.switch_id:
                switch_id = f"SW_{x}_{y}"
                if not network.get_switch_at(x, y):
                    switch = Switch(
                        switch_id=switch_id,
                        x=x,
                        y=y,
                        main_length_cm=0.0,
                        diverge_length_cm=0.0,
                    )
                    network.add_switch(switch)
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
    """Delete multiple track segments at once (for eraser drag)."""
    data = request.json
    positions = data.get("positions", [])
    deleted = []

    for pos in positions:
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            continue
        x, y = pos[0], pos[1]
        track = network.get_track(x, y)
        if track:
            network.remove_track(x, y)
            deleted.append([x, y])

    return jsonify({"status": "success", "deleted": len(deleted)})


# === Block Management Endpoints ===


@app.route("/api/blocks", methods=["GET"])
def get_blocks():
    """List all blocks with their track counts and positions"""
    result = []
    for block in network.blocks.values():
        block_dict = block.to_dict()
        tracks = network.get_block_tracks(block.block_id)
        block_dict["track_count"] = len(tracks)
        block_dict["track_positions"] = network.get_block_positions(block.block_id)

        # Find associated stations
        station_names = set()
        for track in tracks:
            if track.station_id:
                station = network.stations.get(track.station_id)
                if station:
                    station_names.add(station.name)

        if station_names:
            block_dict["station_names"] = sorted(list(station_names))

        result.append(block_dict)
    return jsonify(result)


@app.route("/api/blocks", methods=["POST"])
def create_block():
    """Create a new block"""
    data = request.json or {}
    block_id = data.get("block_id") or f"BLK_{uuid.uuid4().hex[:8].upper()}"
    name = data.get("name", "")
    try:
        block = network.create_block(block_id, name)
        # Set optional properties
        if "length_cm" in data:
            block.length_cm = float(data["length_cm"])
        if "min_radius_cm" in data:
            block.min_radius_cm = data["min_radius_cm"]
        if "max_speed_kmh" in data:
            block.max_speed_kmh = data["max_speed_kmh"]
        return jsonify(block.to_dict())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/blocks/<block_id>", methods=["GET"])
def get_block(block_id):
    """Get block details including validation status and connected signals"""
    block = network.get_block(block_id)
    if not block:
        return jsonify({"error": "Block not found"}), 404
    result = block.to_dict()
    tracks = network.get_block_tracks(block_id)
    result["track_count"] = len(tracks)

    # Get positions (unsorted for backwards compatibility)
    positions = network.get_block_positions(block_id)
    result["track_positions"] = positions

    # Add sorted positions for UI display
    result["sorted_positions"] = _sort_block_positions(positions, network)

    # Find associated stations
    station_names = set()
    for track in tracks:
        if track.station_id:
            station = network.stations.get(track.station_id)
            if station:
                station_names.add(station.name)

    if station_names:
        result["station_names"] = sorted(list(station_names))
    is_valid, errors = network.validate_block_continuity(block_id)
    result["is_continuous"] = is_valid
    result["validation_errors"] = errors

    # Find signals monitoring this block
    monitoring_signals = [
        signal.to_dict()
        for signal in network.signals.values()
        if signal.monitored_block_id == block_id
    ]
    result["monitoring_signals"] = monitoring_signals

    return jsonify(result)


@app.route("/api/blocks/<block_id>", methods=["PATCH"])
def update_block(block_id):
    """Update block properties"""
    from enums import BlockState as BlockStateEnum

    block = network.get_block(block_id)
    if not block:
        return jsonify({"error": "Block not found"}), 404

    data = request.json or {}
    if "name" in data:
        block.name = data["name"] if data["name"] is not None else ""
    if "length_cm" in data:
        block.length_cm = float(data["length_cm"])
    if "min_radius_cm" in data:
        block.min_radius_cm = data["min_radius_cm"]
    if "max_speed_kmh" in data:
        block.max_speed_kmh = data["max_speed_kmh"]
    if "state" in data:
        # Convert string to BlockState enum and use set_block_state for proper signal updates
        state_str = data["state"].lower()
        try:
            new_state = BlockStateEnum(state_str)
            network.set_block_state(block_id, new_state)
        except ValueError:
            return jsonify(
                {
                    "error": f"Invalid state: {state_str}. Valid: free, reserved, occupied, caution"
                }
            ), 400

    return jsonify(block.to_dict())


@app.route("/api/blocks/<block_id>", methods=["DELETE"])
def delete_block(block_id):
    """Delete block and clear track references"""
    if network.delete_block(block_id):
        return jsonify({"status": "success", "message": f"Block {block_id} deleted"})
    return jsonify({"error": "Block not found"}), 404


@app.route("/api/blocks/<block_id>/cycle-state", methods=["POST"])
def cycle_block_state(block_id):
    """
    Cycle block state: FREE -> RESERVED -> OCCUPIED -> CAUTION -> FREE

    Used by UI right-click to manually cycle through states for testing.
    Rejects changes if block is reserved by an active route.
    """
    # Check if block is reserved by a route - reject manual changes
    block = network.get_block(block_id)
    if not block:
        return jsonify({"error": "Block not found"}), 404

    if block.reserved_by_path:
        return jsonify(
            {
                "error": f"Block is reserved by path {block.reserved_by_path}",
                "reserved_by_path": block.reserved_by_path,
                "current_state": block.state.value,
            }
        ), 409  # 409 Conflict

    new_state = network.cycle_block_state(block_id)
    if new_state is None:
        return jsonify({"error": "Block not found"}), 404

    block = network.get_block(block_id)
    return jsonify(
        {
            "status": "success",
            "block_id": block_id,
            "new_state": new_state.value,
            "block": block.to_dict(),
        }
    )


@app.route("/api/blocks/create-with-tracks", methods=["POST"])
def create_block_with_tracks():
    """
    Create a new block with validated track assignments.

    Request body:
    {
        "block_id": "BLK_001",
        "positions": [[0,0], [1,0], [2,0]],
        "name": "Main Line",
        "length_cm": 150.0,
        "max_speed_kmh": 80.0
    }
    """
    data = request.json
    block_id = data.get("block_id")
    positions = data.get("positions", [])

    if not block_id:
        return jsonify({"error": "block_id required"}), 400
    if not positions:
        return jsonify({"error": "positions required"}), 400

    try:
        # Convert positions to tuples
        positions = [tuple(pos) for pos in positions]

        block = network.create_block_with_tracks(
            block_id=block_id,
            positions=positions,
            name=data.get("name", ""),
            length_cm=data.get("length_cm", 0.0),
            max_speed_kmh=data.get("max_speed_kmh"),
        )
        return jsonify(block.to_dict())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/blocks/<block_id>/extend", methods=["POST"])
def extend_block_endpoint(block_id):
    """
    Extend an existing block with new track positions.

    Request body:
    {
        "positions": [[3,0], [4,0]],
        "force": false,  // Optional: if true, invalidates signals in the path
        "force_overwrite": false  // Optional: if true, allows overwriting tracks from other blocks
    }

    Response includes 'invalidated_signals' array if any signals were invalidated.
    Invalidated signals have their monitored_block cleared and show a warning triangle.
    """
    data = request.json
    positions = data.get("positions", [])
    force = data.get("force", False)
    force_overwrite = data.get("force_overwrite", False)

    if not positions:
        return jsonify({"error": "positions required"}), 400

    try:
        positions = [tuple(pos) for pos in positions]
        block = network.extend_block(
            block_id, positions, force=force, force_overwrite=force_overwrite
        )

        # Build response with invalidated signals info
        response = block.to_dict()
        if hasattr(block, "_invalidated_signals") and block._invalidated_signals:
            response["invalidated_signals"] = block._invalidated_signals
            # Clean up temporary attribute
            del block._invalidated_signals

        return jsonify(response)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/blocks/<block_id>/tracks", methods=["POST"])
def add_tracks_to_block(block_id):
    """Add tracks to a block by positions"""
    block = network.get_block(block_id)
    if not block:
        return jsonify({"error": "Block not found"}), 404

    data = request.json or {}
    positions = data.get("positions", [])
    force = data.get("force", False)  # Allow reassignment if force=True
    added = []
    reassigned = []

    for pos in positions:
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            continue
        x, y = pos[0], pos[1]

        # Check if track already assigned to a different block
        track = network.get_track(x, y)
        if track and track.block_section_id and track.block_section_id != block_id:
            if force:
                # Unassign from old block first
                old_block_id = track.block_section_id
                track.block_section_id = None
                reassigned.append(
                    {"position": [x, y], "from": old_block_id, "to": block_id}
                )
            else:
                # Skip if not forcing reassignment
                continue

        # Now assign to new block
        try:
            network.assign_track_to_block(x, y, block_id)
            added.append([x, y])
        except ValueError:
            pass

    return jsonify(
        {
            "status": "success",
            "added": len(added),
            "reassigned": len(reassigned),
            "positions": added,
            "reassignment_details": reassigned,
            "block_id": block_id,
        }
    )


@app.route("/api/blocks/<block_id>/tracks", methods=["DELETE"])
def remove_tracks_from_block(block_id):
    """Remove tracks from a block (clear their block_section_id) with edge validation"""
    block = network.get_block(block_id)
    if not block:
        return jsonify({"error": "Block not found"}), 404

    data = request.json or {}
    positions = data.get("positions", [])
    removed = []
    errors = []

    # Get all current block positions
    current_positions = network.get_block_positions(block_id)
    position_set = set((x, y) for x, y in current_positions)

    for pos in positions:
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            continue
        x, y = pos[0], pos[1]

        track = network.get_track(x, y)
        if not track or track.block_section_id != block_id:
            errors.append(f"Position ({x}, {y}) is not part of block {block_id}")
            continue

        # Check if this is an edge tile
        # Edge tiles have at least one exit that either:
        # 1. Has no neighbor track, OR
        # 2. Has a neighbor track that's not in the block

        # Get all connected edges from this track (excludes dead-ends)
        exits = []
        for bit in range(6):
            if track.track_bits & (1 << bit):
                from track import BIT_TO_EDGES

                e1, e2 = BIT_TO_EDGES[bit]
                exits.extend([e1, e2])
        exits = list(set(exits))  # Remove duplicates

        is_edge = False
        for exit_edge in exits:
            # Get neighbor position based on exit edge
            neighbor_pos = network._get_neighbor_coords(x, y, exit_edge)
            if not neighbor_pos:
                # No neighbor (edge of map) -> definitely an edge tile
                is_edge = True
                break

            nx, ny = neighbor_pos
            neighbor_track = network.get_track(nx, ny)

            if not neighbor_track or (nx, ny) not in position_set:
                # Exit leads outside block -> edge tile
                is_edge = True
                break

        # If no exit leads outside the block, this is not an edge tile
        if not is_edge:
            errors.append(f"Cannot remove tile ({x}, {y}): not an edge tile")
            continue

        # Temporarily remove track to check continuity
        original_block_id = track.block_section_id
        track.block_section_id = None

        # Check if remaining block is still continuous (only if block has more than 1 track left)
        remaining_tracks = network.get_block_tracks(block_id)
        if len(remaining_tracks) > 1:
            is_continuous, validation_errors = network.validate_block_continuity(
                block_id
            )
            if not is_continuous:
                # Restore the block_section_id
                track.block_section_id = original_block_id
                errors.append(
                    f"Cannot remove tile ({x}, {y}): would break block continuity"
                )
                continue

        # Keep the removal (track.block_section_id is already None)
        removed.append([x, y])
        position_set.discard((x, y))

    # Check if block is now empty and delete it
    remaining_tracks = network.get_block_tracks(block_id)
    block_deleted = False
    if len(remaining_tracks) == 0:
        network.delete_block(block_id)
        block_deleted = True

    return jsonify(
        {
            "status": "success" if removed else "partial" if errors else "no_changes",
            "removed": len(removed),
            "positions": removed,
            "errors": errors,
            "block_deleted": block_deleted,
        }
    )


@app.route("/api/blocks/<block_id>/voltage", methods=["GET"])
def get_block_voltage(block_id: str):
    """Get current voltage for a block.

    Returns:
        200 OK with voltage data:
        {
            "block_id": str,
            "voltage": float,
            "timestamp": str (ISO format)
        }
        404 Not Found if block doesn't exist
    """
    with simulation_lock:
        block = network.get_block(block_id)
        if not block:
            return jsonify({"error": f"Block '{block_id}' not found"}), 404

        voltage = sim_controller.power_controller.get_block_voltage(block_id)

    return jsonify(
        {
            "block_id": block_id,
            "voltage": voltage,
            "timestamp": datetime.now().isoformat(),
        }
    )


@app.route("/api/blocks/<block_id>/end_tiles", methods=["GET"])
def get_block_end_tiles(block_id):
    """Get the start and end tiles of a block (sorted in traversal order)"""
    block = network.get_block(block_id)
    if not block:
        return jsonify({"error": "Block not found"}), 404

    positions = network.get_block_positions(block_id)
    if len(positions) == 0:
        return jsonify({"start_tile": None, "end_tile": None})

    # Sort positions using the helper function
    sorted_positions = _sort_block_positions(positions, network)

    start_tile = {"x": sorted_positions[0][0], "y": sorted_positions[0][1]}
    end_tile = {"x": sorted_positions[-1][0], "y": sorted_positions[-1][1]}

    return jsonify({"start_tile": start_tile, "end_tile": end_tile})


@app.route("/api/blocks/<block_id>/can_extend", methods=["POST"])
def can_extend_block(block_id):
    """Check if a position can be added to the block (validation without mutation)"""
    data = request.json
    x = data.get("x")
    y = data.get("y")

    if x is None or y is None:
        return jsonify({"error": "Missing x or y coordinate"}), 400

    block = network.get_block(block_id)
    if not block:
        return jsonify({"error": "Block not found"}), 404

    # Check if track exists
    track = network.get_track(x, y)
    if not track:
        return jsonify({"can_extend": False, "reason": "No track at position"})

    # Check for crossings (never allowed)
    from block_validation import is_crossing

    if is_crossing(track.track_bits):
        return jsonify(
            {
                "can_extend": False,
                "reason": "Cannot add crossing to block - crossings isolate paths",
            }
        )

    # Check for switches (never allowed)
    if track.is_switch:
        return jsonify(
            {
                "can_extend": False,
                "reason": "Cannot add switch to block - switches are block boundaries",
            }
        )

    # Check for signals (requires force flag in actual extend operation)
    if track.signal_ids:
        return jsonify(
            {
                "can_extend": False,
                "reason": "Tile has signal(s) - use force=true to extend past signal",
                "requires_force": True,
            }
        )

    # Check if tile belongs to another block (requires force_overwrite flag)
    if track.block_section_id and track.block_section_id != block_id:
        return jsonify(
            {
                "can_extend": False,
                "reason": f"Tile already in block {track.block_section_id} - use force_overwrite=true",
                "requires_force_overwrite": True,
            }
        )

    # Check adjacency
    existing_positions = network.get_block_positions(block_id)
    is_adjacent = False
    for old_x, old_y in existing_positions:
        if abs(x - old_x) + abs(y - old_y) == 1:
            is_adjacent = True
            break

    if not is_adjacent and len(existing_positions) > 0:
        return jsonify(
            {
                "can_extend": False,
                "reason": "Position must be adjacent to existing block",
            }
        )

    return jsonify({"can_extend": True})


# === Signal Management Endpoints ===


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
        signal = network.add_signal(
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
    signal = network.get_signal(signal_id)
    if not signal:
        return jsonify({"error": "Signal not found"}), 404
    return jsonify(signal.to_dict())


@app.route("/api/signal/<signal_id>", methods=["PATCH"])
def update_signal(signal_id):
    """Update signal properties"""
    signal = network.get_signal(signal_id)
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

        updated = network.update_signal(signal_id, **update_kwargs)
        return jsonify(updated.to_dict())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/signal/<signal_id>", methods=["DELETE"])
def delete_signal(signal_id):
    """Delete signal by ID"""
    if network.remove_signal(signal_id):
        return jsonify({"status": "success", "message": f"Signal {signal_id} deleted"})
    return jsonify({"error": "Signal not found"}), 404


@app.route("/api/signal/<sint:x>/<sint:y>", methods=["GET"])
def get_signal_at(x, y):
    """Get signal at position"""
    signal = network.get_signal_at(x, y)
    if not signal:
        return jsonify({"error": f"No signal at ({x}, {y})"}), 404
    return jsonify(signal.to_dict())


@app.route("/api/signal/<sint:x>/<sint:y>", methods=["DELETE"])
def delete_signal_at(x, y):
    """Delete signal at position"""
    if network.remove_signal_at(x, y):
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
    signal = network.get_signal(signal_id)
    if not signal:
        return jsonify({"error": "Signal not found"}), 404

    reachable = network.get_reachable_blocks(signal_id)
    return jsonify({"signal_id": signal_id, "reachable_blocks": reachable})


@app.route("/api/signal/validate/<sint:x>/<sint:y>", methods=["GET"])
def validate_signal_at(x, y):
    """Check if a signal can be placed at the given position and return valid detection directions"""
    track = network.get_track(x, y)
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
        if not network.get_signal_at_direction(x, y, detection_dir):
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


# === Station Endpoints ===


@app.route("/api/station", methods=["POST"])
def create_station():
    """Create a new station at specified position"""
    data = request.json
    x = data.get("x")
    y = data.get("y")

    if x is None or y is None:
        return jsonify({"error": "Missing x or y coordinate"}), 400

    try:
        station = network.add_station(
            x=x,
            y=y,
            station_id=data.get("station_id"),
            name=data.get("name", ""),
            length_cm=data.get("length_cm", 0.0),
            platform_side=data.get("platform_side", "both"),
            top_speed_kmh=data.get("top_speed_kmh"),
        )
        return jsonify(station.to_dict_with_orientation(network))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/station/<station_id>", methods=["GET"])
def get_station(station_id):
    """Get station by ID"""
    station = network.get_station(station_id)
    if not station:
        return jsonify({"error": "Station not found"}), 404
    return jsonify(station.to_dict_with_orientation(network))


@app.route("/api/station/<station_id>", methods=["PATCH"])
def update_station(station_id):
    """Update station properties"""
    station = network.get_station(station_id)
    if not station:
        return jsonify({"error": "Station not found"}), 404

    data = request.json

    # Update allowed fields
    if "name" in data:
        station.name = data["name"]
    if "length_cm" in data:
        station.length_cm = data["length_cm"]
    if "platform_side" in data:
        from enums import PlatformSide

        station.platform_side = PlatformSide(data["platform_side"])
    if "top_speed_kmh" in data:
        station.top_speed_kmh = data["top_speed_kmh"]

    return jsonify(station.to_dict_with_orientation(network))


@app.route("/api/station/<station_id>", methods=["DELETE"])
def delete_station(station_id):
    """Delete station"""
    success = network.delete_station(station_id)
    if not success:
        return jsonify({"error": "Station not found"}), 404
    return jsonify({"success": True})


@app.route("/api/station/validate/<sint:x>/<sint:y>", methods=["GET"])
def validate_station_at(x, y):
    """Check if station can be placed at position"""
    try:
        track = network.get_track(x, y)

        if not track:
            return jsonify(
                {"can_place": False, "error": f"No track at position ({x}, {y})"}
            )

        if track.is_switch:
            return jsonify(
                {"can_place": False, "error": "Cannot place station on switch"}
            )

        if track.signal_ids:
            return jsonify(
                {
                    "can_place": False,
                    "error": "Cannot place station on tile with signal",
                }
            )

        if track.station_id is not None:
            return jsonify(
                {
                    "can_place": False,
                    "error": f"Tile already belongs to station {track.station_id}",
                }
            )

        return jsonify({"can_place": True, "existing_block": track.block_section_id})
    except Exception as e:
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e), "can_place": False}), 500


@app.route("/api/station/<station_id>/extend", methods=["POST"])
def extend_station(station_id):
    """Extend station platform with new tiles"""
    data = request.json
    positions = data.get("positions", [])

    if not positions:
        return jsonify({"error": "Missing positions"}), 400

    # Convert positions to tuples
    positions = [tuple(pos) for pos in positions]

    try:
        station = network.extend_station(station_id, positions)
        return jsonify(station.to_dict_with_orientation(network))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/station/<station_id>/remove_tile", methods=["POST"])
def remove_station_tile(station_id):
    """Remove a single tile from a station with edge validation"""
    data = request.json
    x = data.get("x")
    y = data.get("y")

    if x is None or y is None:
        return jsonify({"error": "Missing x or y coordinate"}), 400

    station = network.get_station(station_id)
    if not station:
        return jsonify({"error": "Station not found"}), 404

    pos = (x, y)
    if pos not in station.positions:
        return jsonify(
            {"error": f"Position ({x}, {y}) is not part of station {station_id}"}
        ), 400

    # Check if this is an edge tile
    # Edge tiles have at least one exit that either:
    # 1. Has no neighbor track, OR
    # 2. Has a neighbor track that's not in the station
    track = network.get_track(x, y)
    if not track:
        return jsonify({"error": f"No track at ({x}, {y})"}), 400

    # Get all connected edges from this track (excludes dead-ends)
    # We only care about actual track connections, not dead-ends
    exits = []
    for bit in range(6):
        if track.track_bits & (1 << bit):
            from track import BIT_TO_EDGES

            e1, e2 = BIT_TO_EDGES[bit]
            exits.extend([e1, e2])
    exits = list(set(exits))  # Remove duplicates

    position_set = set(station.positions)

    is_edge = False
    for exit_edge in exits:
        # Get neighbor position based on exit edge
        neighbor_pos = network._get_neighbor_coords(x, y, exit_edge)
        if not neighbor_pos:
            # No neighbor (edge of map) -> definitely an edge tile
            is_edge = True
            break

        nx, ny = neighbor_pos
        neighbor_track = network.get_track(nx, ny)

        if not neighbor_track or (nx, ny) not in position_set:
            # Exit leads outside station -> edge tile
            is_edge = True
            break

    # If no exit leads outside the station, this is not an edge tile
    if not is_edge:
        return jsonify(
            {
                "error": f"Cannot remove tile ({x}, {y}): not an edge tile. Only edge tiles can be removed.",
                "is_edge": False,
            }
        ), 400

    # Remove the position from station
    new_positions = [p for p in station.positions if p != pos]

    # Check that remaining positions are still continuous
    if len(new_positions) > 1:
        is_continuous, errors = network._validate_station_continuity(new_positions)
        if not is_continuous:
            return jsonify(
                {
                    "error": f"Cannot remove tile ({x}, {y}): would break station continuity",
                    "validation_errors": errors,
                    "is_edge": True,
                }
            ), 400

    # Update station positions
    station.positions = new_positions

    # Clear station_id from the track
    track = network.get_track(x, y)
    if track and track.station_id == station_id:
        track.station_id = None

        # Auto-shorten block: Remove tile from ANY block it belongs to
        # Since this tile is no longer part of the station, remove it from its block
        if track.block_section_id:
            old_block_id = track.block_section_id

            # Use the network's remove_tile_from_block method which handles:
            # - Removing the tile
            # - Checking block connectivity
            # - Splitting blocks if needed
            # - Deleting empty blocks
            network.remove_tile_from_block(x, y)

            print(
                f"[Auto-Shorten] Removed tile ({x}, {y}) from block {old_block_id} due to station shrink"
            )

    # If station is now empty, delete it
    if len(station.positions) == 0:
        network.delete_station(station_id)
        return jsonify({"success": True, "station_deleted": True})

    return jsonify(
        {"success": True, "station": station.to_dict_with_orientation(network)}
    )


@app.route("/api/station/<station_id>/end_tiles", methods=["GET"])
def get_station_end_tiles(station_id):
    """Get the start and end tiles of a station (sorted by orientation)"""
    station = network.get_station(station_id)
    if not station:
        return jsonify({"error": "Station not found"}), 404

    # Use the existing sorted positions from to_dict_with_orientation
    sorted_positions = station._sort_positions_by_orientation()

    if len(sorted_positions) == 0:
        return jsonify({"start_tile": None, "end_tile": None})

    start_tile = {"x": sorted_positions[0][0], "y": sorted_positions[0][1]}
    end_tile = {"x": sorted_positions[-1][0], "y": sorted_positions[-1][1]}

    return jsonify({"start_tile": start_tile, "end_tile": end_tile})


@app.route("/api/station/<station_id>/can_extend", methods=["POST"])
def can_extend_station(station_id):
    """Check if a position can be added to the station (validation without mutation)"""
    data = request.json
    x = data.get("x")
    y = data.get("y")

    if x is None or y is None:
        return jsonify({"error": "Missing x or y coordinate"}), 400

    station = network.get_station(station_id)
    if not station:
        return jsonify({"error": "Station not found"}), 404

    # Check if track exists
    track = network.get_track(x, y)
    if not track:
        return jsonify({"can_extend": False, "reason": "No track at position"})

    # Check if it's a switch
    if track.is_switch:
        return jsonify({"can_extend": False, "reason": "Cannot add switch to station"})

    # Check if track is on straight path
    if not network._is_track_on_straight_path(x, y):
        return jsonify(
            {
                "can_extend": False,
                "reason": "Stations require tracks on straight paths (pure NS/EW or simple diagonal tracks)",
            }
        )

    # Check orientation match
    from track import BIT_TO_GROUP

    new_tile_orientation = None
    for bit_idx, group in BIT_TO_GROUP.items():
        if track.track_bits & (1 << bit_idx):
            new_tile_orientation = group
            break

    if (
        station.initial_orientation
        and new_tile_orientation != station.initial_orientation
    ):
        return jsonify(
            {
                "can_extend": False,
                "reason": f"Orientation mismatch: station is {station.initial_orientation}, tile is {new_tile_orientation}",
            }
        )

    # Check if tile has signals
    if track.signal_ids:
        return jsonify(
            {"can_extend": False, "reason": "Cannot add tile with signal to station"}
        )

    # Check if tile already belongs to another station
    if track.station_id is not None and track.station_id != station_id:
        return jsonify(
            {
                "can_extend": False,
                "reason": f"Tile already belongs to station {track.station_id}",
            }
        )

    # Check adjacency
    is_adjacent = False
    for old_x, old_y in station.positions:
        if abs(x - old_x) + abs(y - old_y) == 1:
            is_adjacent = True
            break

    if not is_adjacent and len(station.positions) > 0:
        return jsonify(
            {
                "can_extend": False,
                "reason": "Position must be adjacent to existing station",
            }
        )

    # Check continuity
    all_positions = station.positions + [(x, y)]
    is_continuous, errors = network._validate_station_continuity(all_positions)
    if not is_continuous:
        return jsonify(
            {
                "can_extend": False,
                "reason": f"Would break continuity: {', '.join(errors)}",
            }
        )

    return jsonify({"can_extend": True})


@app.route("/api/station/<station_id>/assign_tile", methods=["POST"])
def assign_tile_to_block(station_id):
    """Assign a station tile to a specific block"""
    data = request.json
    x = data.get("x")
    y = data.get("y")
    block_id = data.get("block_id")

    if x is None or y is None:
        return jsonify({"error": "Missing x or y coordinate"}), 400

    if not block_id:
        return jsonify({"error": "Missing block_id"}), 400

    station = network.get_station(station_id)
    if not station:
        return jsonify({"error": "Station not found"}), 404

    # Verify block exists
    block = network.get_block(block_id)
    if not block:
        return jsonify({"error": "Block not found"}), 404

    # Assign tile to block
    success = station.assign_tile_to_block(x, y, block_id)
    if not success:
        return jsonify({"error": "Tile not in station"}), 400

    return jsonify({"status": "success", "station": station.to_dict()})


@app.route("/api/station/<station_id>/unassign_tile", methods=["POST"])
def unassign_tile_from_block(station_id):
    """Remove block assignment from a station tile"""
    data = request.json
    x = data.get("x")
    y = data.get("y")

    if x is None or y is None:
        return jsonify({"error": "Missing x or y coordinate"}), 400

    station = network.get_station(station_id)
    if not station:
        return jsonify({"error": "Station not found"}), 404

    # Unassign tile from block
    success = station.unassign_tile_from_block(x, y)
    if not success:
        return (
            jsonify({"error": "Tile has no block assignment or not in station"}),
            400,
        )

    return jsonify({"status": "success", "station": station.to_dict()})


@app.route("/api/station/<station_id>/block_assignments", methods=["GET"])
def get_block_assignments(station_id):
    """Get all block assignments for a station"""
    station = network.get_station(station_id)
    if not station:
        return jsonify({"error": "Station not found"}), 404

    # Build response with detailed block assignment info
    assignments = []
    for (x, y), block_id in station.block_assignments.items():
        assignments.append({"x": x, "y": y, "block_id": block_id})

    # Also include summary by block
    blocks_summary = {}
    for block_id in station.get_all_assigned_blocks():
        tiles = station.get_tiles_for_block(block_id)
        blocks_summary[block_id] = {
            "tile_count": len(tiles),
            "tiles": [{"x": x, "y": y} for x, y in tiles],
        }

    return jsonify(
        {
            "station_id": station_id,
            "assignments": assignments,
            "blocks_summary": blocks_summary,
        }
    )


@app.route("/api/trains", methods=["POST"])
def create_train():
    data = request.json
    train_id = data.get("id")
    if not train_id:
        return jsonify({"error": "Missing train ID"}), 400

    # Create a default locomotive car with the specified length
    # API provides length in track units (approx 100m)
    length_units = data.get("length", 1.0)
    length_meters = length_units * 100.0

    car = TrainCar(
        car_id=f"{train_id}_loco",
        car_type="LOCOMOTIVE",
        length=length_meters,
        max_speed=data.get("max_speed", 100.0),
    )

    train = Train(
        train_id=train_id, consist=[car], max_speed=data.get("max_speed", 100.0)
    )

    if "position" in data:
        pos = data["position"]
        direction = Direction[data.get("direction", "EAST")]
        network.add_train(train)
        try:
            network.place_train(train, pos["x"], pos["y"], direction)
        except ValueError as e:
            network.remove_train(train_id)
            return jsonify({"error": str(e)}), 400
    else:
        network.add_train(train)

    train.update_occupancy(network)
    network.update_signals_from_blocks()

    return jsonify({"status": "success", "train": train.to_dict(network)})


@app.route("/api/trains/<train_id>", methods=["GET"])
def get_train(train_id):
    """Get details for a single train"""
    train = network.trains.get(train_id)
    if not train:
        return jsonify({"error": "Train not found"}), 404
    return jsonify(train.to_dict(network))


@app.route("/api/trains/<train_id>/power-on", methods=["POST"])
def train_power_on(train_id):
    """Power on a train (OFF -> IDLE)"""
    train = network.trains.get(train_id)
    if not train:
        return jsonify({"error": "Train not found"}), 404

    if train.power_on():
        return jsonify({"status": "success", "train": train.to_dict(network)})
    return jsonify({"error": "Failed to power on train"}), 400


@app.route("/api/trains/<train_id>/power-off", methods=["POST"])
def train_power_off(train_id):
    """Power off a train (any state -> OFF). Must be stopped."""
    train = network.trains.get(train_id)
    if not train:
        return jsonify({"error": "Train not found"}), 404

    if train.power_off():
        return jsonify({"status": "success", "train": train.to_dict(network)})
    return jsonify({"error": "Cannot power off while train is moving"}), 400


@app.route("/api/trains/<train_id>/set-direction", methods=["POST"])
def train_set_direction(train_id):
    """Set direction and transition to STANDBY (IDLE -> STANDBY)"""
    train = network.trains.get(train_id)
    if not train:
        return jsonify({"error": "Train not found"}), 404

    data = request.json or {}
    direction_str = data.get("direction")
    if not direction_str:
        return jsonify({"error": "Missing direction"}), 400

    try:
        direction = Direction[direction_str]
    except KeyError:
        return jsonify({"error": f"Invalid direction: {direction_str}"}), 400

    if train.set_direction_and_standby(direction):
        return jsonify({"status": "success", "train": train.to_dict(network)})
    return jsonify({"error": "Cannot set direction (train not in IDLE state)"}), 400


@app.route("/api/trains/<train_id>/clear-direction", methods=["POST"])
def train_clear_direction(train_id):
    """Clear direction and transition back to IDLE (STANDBY -> IDLE)"""
    train = network.trains.get(train_id)
    if not train:
        return jsonify({"error": "Train not found"}), 404

    if train.clear_direction():
        return jsonify({"status": "success", "train": train.to_dict(network)})
    return jsonify(
        {"error": "Cannot clear direction (train not in STANDBY state)"}
    ), 400


@app.route("/api/trains/<train_id>/depart", methods=["POST"])
def train_depart(train_id):
    train = network.trains.get(train_id)
    if not train:
        return jsonify({"error": "Train not found"}), 404

    if train.state == TrainState.RUNNING_WAITING:
        if train.clear_to_move():
            return jsonify({"status": "success", "train": train.to_dict(network)})

    if train.state == TrainState.RUNNING_AT_STATION:
        if train.depart_from_station(has_more_waypoints=True):
            return jsonify({"status": "success", "train": train.to_dict(network)})

    if train.depart():
        return jsonify({"status": "success", "train": train.to_dict(network)})
    return jsonify({"error": "Cannot depart (train not in STANDBY state)"}), 400


@app.route("/api/trains/<train_id>/arrive", methods=["POST"])
def train_arrive(train_id):
    """Arrive at station (RUNNING_MOVING -> RUNNING_AT_STATION)"""
    train = network.trains.get(train_id)
    if not train:
        return jsonify({"error": "Train not found"}), 404

    if train.arrive_at_station():
        return jsonify({"status": "success", "train": train.to_dict(network)})
    return jsonify({"error": "Cannot arrive (train not in RUNNING_MOVING state)"}), 400


@app.route("/api/trains/<train_id>/turn-around", methods=["POST"])
def train_turn_around(train_id):
    """Flip facing direction (cab switch). Only allowed when stopped."""
    train = network.trains.get(train_id)
    if not train:
        return jsonify({"error": "Train not found"}), 404

    if train.turn_around():
        return jsonify({"status": "success", "train": train.to_dict(network)})
    return jsonify(
        {"error": "Cannot turn around (train is moving or no direction set)"}
    ), 400


@app.route("/api/trains/<train_id>/enable-manual", methods=["POST"])
def train_enable_manual(train_id):
    """Enable manual control mode. Train must be stopped."""
    train = network.trains.get(train_id)
    if not train:
        return jsonify({"error": "Train not found"}), 404

    if train.enable_manual():
        return jsonify({"status": "success", "train": train.to_dict(network)})
    return jsonify(
        {"error": "Cannot enable manual mode (train must be stopped and not OFF)"}
    ), 400


@app.route("/api/trains/<train_id>/disable-manual", methods=["POST"])
def train_disable_manual(train_id):
    """Disable manual control mode. Train must be stopped."""
    train = network.trains.get(train_id)
    if not train:
        return jsonify({"error": "Train not found"}), 404

    if train.disable_manual():
        return jsonify({"status": "success", "train": train.to_dict(network)})
    return jsonify(
        {
            "error": "Cannot disable manual mode (train must be stopped and in MANUAL state)"
        }
    ), 400


@app.route("/api/trains/<train_id>/manual-speed", methods=["POST"])
def train_set_manual_speed(train_id):
    """Set manual target speed (only in MANUAL mode)."""
    train = network.trains.get(train_id)
    if not train:
        return jsonify({"error": "Train not found"}), 404

    data = request.json or {}
    speed = data.get("speed", 0)
    if not isinstance(speed, (int, float)):
        return jsonify({"error": "Invalid speed value"}), 400

    if train.set_manual_speed(float(speed)):
        return jsonify({"status": "success", "train": train.to_dict(network)})
    return jsonify({"error": "Cannot set manual speed (train not in MANUAL mode)"}), 400


@app.route("/api/trains/<train_id>/travel-direction", methods=["POST"])
def train_set_travel_direction(train_id):
    """Set travel direction for MANUAL mode ('forward' or 'reverse')."""
    train = network.trains.get(train_id)
    if not train:
        return jsonify({"error": "Train not found"}), 404

    data = request.json or {}
    direction = data.get("direction")
    if not direction or direction not in ("forward", "reverse"):
        return jsonify(
            {"error": "Missing or invalid direction (must be 'forward' or 'reverse')"}
        ), 400

    if train.set_travel_direction(direction):
        return jsonify({"status": "success", "train": train.to_dict(network)})
    return jsonify(
        {"error": "Cannot set travel direction (train not in MANUAL mode)"}
    ), 400


@app.route("/api/trains/<train_id>/emergency-stop", methods=["POST"])
def train_emergency_stop(train_id):
    """Emergency stop from any operational state."""
    train = network.trains.get(train_id)
    if not train:
        return jsonify({"error": "Train not found"}), 404

    train.emergency_stop()
    return jsonify({"status": "success", "train": train.to_dict(network)})


@app.route("/api/trains", methods=["GET"])
def list_trains():
    """List all trains in the network."""
    trains = []
    for train in network.trains.values():
        trains.append(train.to_dict(network))
    return jsonify({"trains": trains})


@app.route("/api/trains/<train_id>", methods=["DELETE"])
def delete_train(train_id):
    """Delete a train from the network."""
    if not network.remove_train(train_id):
        return jsonify({"error": "Train not found"}), 404

    return jsonify({"status": "success", "message": f"Train {train_id} deleted"})


@app.route("/api/trains/<train_id>", methods=["PATCH"])
def update_train(train_id):
    """Update train properties (deprecated - use specific endpoints instead)"""
    train = network.trains.get(train_id)
    if not train:
        return jsonify({"error": "Train not found"}), 404

    data = request.json or {}
    # Allow updates to properties
    if "current_speed" in data:
        train.current_speed = float(data["current_speed"])
    if "target_speed" in data:
        train.target_speed = float(data["target_speed"])
    if "num_cars" in data:
        train.num_cars = int(data["num_cars"])

    return jsonify(train.to_dict(network))


@app.route("/api/trains/<train_id>/position", methods=["PUT"])
def update_train_position(train_id: str):
    """Update train position.

    Request body:
    {
        "current_pos": [x, y] (required),
        "path_index": int (optional),
        "distance_on_track": float (optional, 0.0-1.0)
    }

    Returns:
        200 OK with updated train data
        400 Bad Request if validation fails
        404 Not Found if train doesn't exist
    """
    data = request.json or {}

    with simulation_lock:
        train = network.trains.get(train_id)
        if not train:
            return jsonify({"error": f"Train '{train_id}' not found"}), 404

        current_pos = data.get("current_pos")
        if not current_pos:
            return jsonify({"error": "current_pos is required"}), 400

        if not isinstance(current_pos, list) or len(current_pos) != 2:
            return jsonify({"error": "current_pos must be [x, y] array"}), 400

        x, y = int(current_pos[0]), int(current_pos[1])

        track = network.tracks.get((x, y))
        if not track:
            return jsonify({"error": f"No track at position ({x}, {y})"}), 400

        train.current_pos = (x, y)

        if "path_index" in data:
            train.path_index = int(data["path_index"])

        if "distance_on_track" in data:
            distance_ratio = float(data["distance_on_track"])
            if distance_ratio < 0.0 or distance_ratio > 1.0:
                return jsonify({"error": "distance_on_track must be 0.0-1.0"}), 400
            tile_length_cm = network._get_tile_length_cm(x, y)
            train.distance_on_track = distance_ratio * tile_length_cm

        train.update_occupancy(network)

    return jsonify(train.to_dict(network))


@app.route("/api/trains/<train_id>/speed-profile", methods=["PUT"])
def assign_train_speed_profile(train_id: str):
    """Assign speed profile to a train.

    Request body:
    {
        "profile_id": str (required)
    }

    Returns:
        200 OK with updated train data
        400 Bad Request if validation fails
        404 Not Found if train or profile doesn't exist
    """
    data = request.json or {}

    profile_id = data.get("profile_id")
    if not profile_id:
        return jsonify({"error": "profile_id is required"}), 400

    with simulation_lock:
        train = network.trains.get(train_id)
        if not train:
            return jsonify({"error": f"Train '{train_id}' not found"}), 404

        profile = speed_profile_registry.get(profile_id)
        if not profile:
            return jsonify({"error": f"Speed profile '{profile_id}' not found"}), 404

        train.speed_profile_id = profile_id

    return jsonify(train.to_dict(network))


@app.route("/api/train-routes", methods=["GET"])
def list_train_routes():
    routes = []
    for route in network.routes.values():
        routes.append(route.to_dict())
    return jsonify({"routes": routes})


@app.route("/api/train-routes", methods=["POST"])
def create_train_route():
    data = request.get_json() or {}

    route_id = data.get("route_id") or f"ROUTE-{uuid.uuid4().hex[:6].upper()}"
    name = data.get("name", "")
    mode_str = data.get("mode", "one_way")

    try:
        mode = RouteMode(mode_str)
    except ValueError:
        mode = RouteMode.ONE_WAY

    route = Route(route_id=route_id, name=name, mode=mode)

    waypoints_data = data.get("waypoints", [])
    for wp_data in waypoints_data:
        station_id = wp_data.get("station_id", "")
        dwell_time = float(wp_data.get("dwell_time_sec", 30.0))

        timing_mode_str = wp_data.get("timing_mode", "none")
        try:
            timing_mode = TimingMode(timing_mode_str)
        except ValueError:
            timing_mode = TimingMode.NONE

        target_time = wp_data.get("target_time")

        waypoint = Waypoint(
            station_id=station_id,
            dwell_time_sec=dwell_time,
            timing_mode=timing_mode,
            target_time=target_time,
        )
        route.add_waypoint(waypoint)

    network.routes[route_id] = route

    return jsonify({"status": "success", "route": route.to_dict()})


@app.route("/api/train-routes/<route_id>", methods=["GET"])
def get_train_route(route_id):
    route = network.routes.get(route_id)
    if not route:
        return jsonify({"error": "Route not found"}), 404
    return jsonify(route.to_dict())


@app.route("/api/train-routes/<route_id>", methods=["PUT"])
def update_train_route(route_id: str):
    """Update an existing train route.

    Request body:
    {
        "name": str (optional),
        "mode": str (optional, "one_way"|"loop"|"round_trip"),
        "waypoints": [
            {
                "station_id": str,
                "dwell_time_sec": float,
                "timing_mode": str ("none"|"relative"|"absolute"),
                "target_time": float (optional)
            },
            ...
        ] (optional, replaces all waypoints)
    }

    Returns:
        200 OK with updated route data
        400 Bad Request if validation fails
        404 Not Found if route doesn't exist
    """
    with simulation_lock:
        route = network.routes.get(route_id)
        if not route:
            return jsonify({"error": f"Route '{route_id}' not found"}), 404

        data = request.json or {}

        if "name" in data:
            route.name = data["name"]

        if "mode" in data:
            mode_str = data["mode"]
            try:
                route.mode = RouteMode(mode_str)
            except ValueError:
                return jsonify({"error": f"Invalid mode '{mode_str}'"}), 400

        if "waypoints" in data:
            waypoints_data = data["waypoints"]
            route.waypoints.clear()

            for wp_data in waypoints_data:
                station_id = wp_data.get("station_id")
                if not station_id:
                    return jsonify(
                        {"error": "station_id required for each waypoint"}
                    ), 400

                dwell_time = float(wp_data.get("dwell_time_sec", 30.0))

                timing_mode_str = wp_data.get("timing_mode", "none")
                try:
                    timing_mode = TimingMode(timing_mode_str)
                except ValueError:
                    timing_mode = TimingMode.NONE

                target_time = wp_data.get("target_time")

                waypoint = Waypoint(
                    station_id=station_id,
                    dwell_time_sec=dwell_time,
                    timing_mode=timing_mode,
                    target_time=target_time,
                )
                route.add_waypoint(waypoint)

            if route.is_active:
                route.current_waypoint_index = min(
                    route.current_waypoint_index, max(0, len(route.waypoints) - 1)
                )

    return jsonify(route.to_dict())


@app.route("/api/train-routes/<route_id>", methods=["DELETE"])
def delete_train_route(route_id):
    if route_id not in network.routes:
        return jsonify({"error": "Route not found"}), 404

    for train in network.trains.values():
        if train.assigned_route_id == route_id:
            train.assigned_route_id = None

    del network.routes[route_id]
    return jsonify({"status": "success", "message": f"Route {route_id} deleted"})


@app.route("/api/trains/<train_id>/assign-route", methods=["PUT"])
def assign_route_to_train(train_id):
    train = network.trains.get(train_id)
    if not train:
        return jsonify({"error": "Train not found"}), 404

    data = request.get_json() or {}
    route_id = data.get("route_id")

    if route_id is None:
        train.assigned_route_id = None
        return jsonify({"status": "success", "train": train.to_dict(network)})

    route = network.routes.get(route_id)
    if not route:
        return jsonify({"error": "Route not found"}), 404

    train.assigned_route_id = route_id
    return jsonify({"status": "success", "train": train.to_dict(network)})


@app.route("/api/train-routes/<route_id>/activate", methods=["POST"])
def activate_train_route(route_id):
    route = network.routes.get(route_id)
    if not route:
        return jsonify({"error": "Route not found"}), 404

    route.activate(sim_controller.simulation_time)
    return jsonify({"status": "success", "route": route.to_dict()})


@app.route("/api/train-routes/<route_id>/deactivate", methods=["POST"])
def deactivate_train_route(route_id):
    route = network.routes.get(route_id)
    if not route:
        return jsonify({"error": "Route not found"}), 404

    route.deactivate()
    return jsonify({"status": "success", "route": route.to_dict()})


# ============================================================================
# Schedule Management API
# ============================================================================


@app.route("/api/schedules", methods=["GET"])
def list_schedules():
    """List all train schedules."""
    schedules = []
    for schedule in network.schedules.values():
        stops_data = []
        for stop in schedule.stops:
            stops_data.append(
                {
                    "station_pos": list(stop.station_pos),
                    "station_name": stop.station_name,
                    "arrival_time": stop.arrival_time.isoformat()
                    if stop.arrival_time
                    else None,
                    "departure_time": stop.departure_time.isoformat()
                    if stop.departure_time
                    else None,
                    "platform": stop.platform,
                }
            )
        schedules.append(
            {
                "schedule_id": schedule.schedule_id,
                "train_id": schedule.train_id,
                "stops": stops_data,
                "start_time": schedule.start_time.isoformat()
                if schedule.start_time
                else None,
                "end_time": schedule.end_time.isoformat()
                if schedule.end_time
                else None,
                "priority": schedule.priority,
            }
        )
    return jsonify({"schedules": schedules})


@app.route("/api/schedules", methods=["POST"])
def create_schedule():
    """Create a new train schedule.

    Request body:
        train_id: Train to assign schedule to (required)
        stops: List of stops with station_name, arrival_time, departure_time
        priority: Schedule priority (optional, default 0)
    """
    data = request.get_json() or {}

    train_id = data.get("train_id")
    if not train_id:
        return jsonify({"error": "train_id is required"}), 400

    if train_id not in network.trains:
        return jsonify({"error": f"Train '{train_id}' not found"}), 404

    schedule_id = data.get("schedule_id") or f"SCH-{uuid.uuid4().hex[:6].upper()}"
    priority = data.get("priority", 0)

    schedule = TrainSchedule(
        train_id=train_id, schedule_id=schedule_id, priority=priority
    )

    stops_data = data.get("stops", [])
    for stop_data in stops_data:
        station_name = stop_data.get("station_name", "Unknown")
        station_pos = tuple(stop_data.get("station_pos", [0, 0]))

        arrival_str = stop_data.get("arrival_time")
        departure_str = stop_data.get("departure_time")

        try:
            arrival_time = (
                datetime.fromisoformat(arrival_str) if arrival_str else datetime.now()
            )
            departure_time = (
                datetime.fromisoformat(departure_str)
                if departure_str
                else arrival_time + timedelta(minutes=2)
            )
        except ValueError:
            arrival_time = datetime.now()
            departure_time = arrival_time + timedelta(minutes=2)

        stop = ScheduleStop(
            station_pos=station_pos,
            station_name=station_name,
            arrival_time=arrival_time,
            departure_time=departure_time,
            platform=stop_data.get("platform"),
        )
        schedule.add_stop(stop)

    if schedule.stops:
        schedule.start_time = schedule.stops[0].arrival_time
        schedule.end_time = schedule.stops[-1].departure_time

    network.add_schedule(schedule)

    train = network.trains[train_id]
    train.schedule = schedule

    return jsonify(
        {
            "status": "success",
            "schedule_id": schedule_id,
            "message": f"Schedule created and assigned to train {train_id}",
        }
    )


@app.route("/api/schedules/<schedule_id>", methods=["GET"])
def get_schedule(schedule_id):
    """Get a specific schedule by ID."""
    if schedule_id not in network.schedules:
        return jsonify({"error": "Schedule not found"}), 404

    schedule = network.schedules[schedule_id]
    stops_data = []
    for stop in schedule.stops:
        stops_data.append(
            {
                "station_pos": list(stop.station_pos),
                "station_name": stop.station_name,
                "arrival_time": stop.arrival_time.isoformat()
                if stop.arrival_time
                else None,
                "departure_time": stop.departure_time.isoformat()
                if stop.departure_time
                else None,
                "platform": stop.platform,
            }
        )

    return jsonify(
        {
            "schedule_id": schedule.schedule_id,
            "train_id": schedule.train_id,
            "stops": stops_data,
            "start_time": schedule.start_time.isoformat()
            if schedule.start_time
            else None,
            "end_time": schedule.end_time.isoformat() if schedule.end_time else None,
            "priority": schedule.priority,
        }
    )


@app.route("/api/schedules/<schedule_id>", methods=["DELETE"])
def delete_schedule(schedule_id):
    """Delete a schedule."""
    if schedule_id not in network.schedules:
        return jsonify({"error": "Schedule not found"}), 404

    schedule = network.schedules[schedule_id]

    if schedule.train_id in network.trains:
        train = network.trains[schedule.train_id]
        if train.schedule and train.schedule.schedule_id == schedule_id:
            train.schedule = None

    del network.schedules[schedule_id]

    return jsonify({"status": "success", "message": f"Schedule {schedule_id} deleted"})


@app.route("/api/simulation/control", methods=["POST"])
def control_simulation():
    global simulation_running, simulation_speed
    data = request.json
    action = data.get("action")

    if action == "start":
        simulation_running = True
    elif action == "stop":
        simulation_running = False
    elif action == "reset":
        simulation_running = False
        # Reset logic here
        pass
    elif action == "set_speed":
        simulation_speed = data.get("speed", 1.0)

    return jsonify(
        {"status": "success", "running": simulation_running, "speed": simulation_speed}
    )


# ============================================================================
# Layout Persistence API
# ============================================================================


@app.route("/api/layouts", methods=["GET"])
def list_layouts():
    """List all saved layouts."""
    layouts = persistence_manager.list_layouts()
    return jsonify({"layouts": layouts})


@app.route("/api/layouts", methods=["POST"])
def save_layout():
    """Save current network state as a layout.

    Request body:
        name: Layout name (required)
        description: Optional description

    Saves both snapshot and steps in a single merged file.
    """
    global simulation_running, last_autosave_time

    data = request.get_json() or {}
    name = data.get("name", "").strip()
    description = data.get("description", "")

    if not name:
        return jsonify({"error": "Layout name is required"}), 400

    was_running = simulation_running
    simulation_running = False

    try:
        with simulation_lock:
            result = persistence_manager.save_merged_layout(name, network, description)
            last_autosave_time = time.time()

        response = {
            "status": "success",
            "message": f"Layout '{name}' saved successfully",
            "path": result["path"],
            "steps_generated": result["steps_generated"],
            "steps_count": result["steps_count"],
        }

        # Notify user if steps failed to generate
        if result["steps_error"]:
            response["warning"] = (
                f"Steps could not be generated: {result['steps_error']}"
            )

        return jsonify(response)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except IOError as e:
        return jsonify({"error": str(e)}), 500
    finally:
        simulation_running = was_running


@app.route("/api/layouts/<name>/load", methods=["POST"])
def load_layout(name):
    """Load a layout, replacing the current network state.

    Uses snapshot by default, falls back to steps if snapshot fails.
    """
    global network, sim_controller, simulation_running, last_autosave_time

    was_running = simulation_running
    simulation_running = False

    try:
        with simulation_lock:
            result = persistence_manager.load_layout_with_fallback(name, network)

            if not result.get("success"):
                return jsonify(
                    {
                        "error": result.get("error", "Failed to load layout"),
                        "method": result.get("method"),
                    }
                ), 400

            sim_controller.network = network
            last_autosave_time = time.time()

        response = {
            "status": "success",
            "message": f"Layout '{name}' loaded successfully",
            "method": result.get("method"),
        }
        if result.get("recovery"):
            response["recovery"] = True
            response["message"] = (
                f"Layout '{name}' recovered from steps (snapshot was corrupted)"
            )

        return jsonify(response)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        simulation_running = was_running


@app.route("/api/layouts/<name>", methods=["DELETE"])
def delete_layout(name):
    """Delete a saved layout."""
    if persistence_manager.delete_layout(name):
        return jsonify({"status": "success", "message": f"Layout '{name}' deleted"})
    else:
        return jsonify({"error": f"Layout '{name}' not found"}), 404


@app.route("/api/network/clear", methods=["POST"])
def clear_network():
    """Clear the current network, giving a fresh canvas.

    Returns counts of what was cleared for confirmation display.
    """
    global network, sim_controller, simulation_running

    # Pause simulation
    was_running = simulation_running
    simulation_running = False

    try:
        with simulation_lock:
            # Count what we're clearing
            counts = {
                "tracks": len(network.tracks),
                "blocks": len(network.blocks),
                "signals": len(network.signals),
                "stations": len(network.stations),
                "trains": len(network.trains),
            }

            # Clear everything
            network.tracks.clear()
            network.blocks.clear()
            network.signals.clear()
            network.stations.clear()
            network.trains.clear()
            network.interlocking.routes.clear()
            network.schedules.clear()

            # Update simulation controller reference
            sim_controller.network = network

        return jsonify(
            {"status": "success", "message": "Network cleared", "cleared": counts}
        )
    finally:
        simulation_running = was_running


@app.route("/api/network/stats", methods=["GET"])
def get_network_stats():
    """Get counts of items in the network (for checking if empty)."""
    with simulation_lock:
        return jsonify(
            {
                "tracks": len(network.tracks),
                "blocks": len(network.blocks),
                "signals": len(network.signals),
                "stations": len(network.stations),
                "trains": len(network.trains),
                "is_empty": len(network.tracks) == 0,
            }
        )


# ============================================================================
# Route Dispatcher API
# ============================================================================


@app.route("/api/paths", methods=["POST"])
def create_path():
    """Create a new path between two blocks.

    Request body:
    {
        "start_block_id": "BLK_001",
        "end_block_id": "BLK_002",
        "train_id": "TRAIN_001",       // Optional: pre-assign a train
        "priority": 5,                 // Optional: 1-10, default 5
        "dispatcher_id": "user"        // Optional: who created this
    }

    Returns:
        PathResult with success flag and path details or error info
    """
    data = request.json or {}

    start_block_id = data.get("start_block_id")
    end_block_id = data.get("end_block_id")

    if not start_block_id:
        return jsonify({"error": "start_block_id required"}), 400
    if not end_block_id:
        return jsonify({"error": "end_block_id required"}), 400

    train_id = data.get("train_id")
    priority = data.get("priority", 5)
    dispatcher_id = data.get("dispatcher_id", "user")

    try:
        with simulation_lock:
            result = path_dispatcher.create_path(
                start_block_id=start_block_id,
                end_block_id=end_block_id,
                dispatcher_id=dispatcher_id,
                priority=priority,
                train_id=train_id,
            )
            if result.success and result.path:
                network.update_signals_from_blocks()
                if train_id and train_id in network.trains:
                    train = network.trains[train_id]
                    raw_path = _convert_path_tiles_to_train_path(result.path.path_tiles)
                    success, error = assign_complete_path_to_train(
                        network, train, raw_path
                    )
                    if not success:
                        train.current_path = raw_path
                        train.path_index = 0
                    train.assigned_path_id = result.path.path_id
        return jsonify(result.to_dict()), 200 if result.success else 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/paths", methods=["GET"])
def list_paths():
    """List all active paths.

    Query params:
        train_id: Filter by train
        block_id: Filter by block usage
        status: Filter by status (set, active, etc.)

    Returns:
        List of path objects
    """
    train_id = request.args.get("train_id")
    block_id = request.args.get("block_id")
    status = request.args.get("status")

    with simulation_lock:
        paths = path_dispatcher.get_active_paths()

    # Apply filters
    if train_id:
        paths = [p for p in paths if p.train_id == train_id]
    if block_id:
        paths = [p for p in paths if block_id in p.block_sequence]
    if status:
        paths = [p for p in paths if p.status.value == status]

    return jsonify({"paths": [p.to_dict() for p in paths], "count": len(paths)})


@app.route("/api/paths/<path_id>", methods=["GET"])
def get_path(path_id):
    """Get details of a specific path.

    Returns:
        Path object or 404 error
    """
    with simulation_lock:
        path = path_dispatcher.get_path(path_id)

    if not path:
        return jsonify({"error": f"Path {path_id} not found"}), 404

    return jsonify(path.to_dict())


@app.route("/api/paths/<path_id>", methods=["DELETE"])
def cancel_path(path_id):
    """Cancel a path and release its resources.

    Query params:
        force: If "true", force cancellation even with approach locking

    Returns:
        CancellationResult with released resources info
    """
    force = request.args.get("force", "").lower() == "true"

    with simulation_lock:
        path = path_dispatcher.get_path(path_id)
        if not path:
            return jsonify({"error": f"Path {path_id} not found"}), 404

        result = path_dispatcher.cancel_path(path_id, force=force)

        # Update signal states after path cancellation (blocks are now released)
        if result.success:
            network.update_signals_from_blocks()

    return jsonify(result.to_dict()), 200 if result.success else 400


@app.route("/api/paths/<path_id>/extend", methods=["PUT"])
def extend_path(path_id):
    """Extend an existing path to a new destination.

    Request body:
    {
        "new_end_block_id": "BLK_003"
    }

    Returns:
        ExtensionResult with extended blocks or error info
    """
    data = request.json or {}
    new_end_block_id = data.get("new_end_block_id")

    if not new_end_block_id:
        return jsonify({"error": "new_end_block_id required"}), 400

    with simulation_lock:
        path = path_dispatcher.get_path(path_id)
        if not path:
            return jsonify({"error": f"Path {path_id} not found"}), 404

        result = path_dispatcher.extend_path(path_id, new_end_block_id)

    return jsonify(result.to_dict()), 200 if result.success else 400


@app.route("/api/paths/<path_id>/reachable-extensions", methods=["GET"])
def get_path_reachable_extensions(path_id):
    """Get all blocks reachable from a path's end in the FORWARD direction only.

    This is used for path extension to ensure users can only extend in the
    direction the path is already heading (no backward extension).

    Query params:
        include_stations: If "true", include station IDs (default: true)

    Returns:
        {
            "path_id": str,
            "reachable_endpoints": [str],
            "count": int,
            "exit_direction": str | null,  // N/E/S/W
            "error": str | null
        }
    """
    include_stations = request.args.get("include_stations", "true").lower() != "false"

    with simulation_lock:
        path = path_dispatcher.get_path(path_id)
        if not path:
            return jsonify({"error": f"Path {path_id} not found"}), 404

        reachable, error = path_dispatcher.get_reachable_endpoints_for_extension(
            path_id, include_stations=include_stations
        )

    if error:
        return jsonify(
            {
                "path_id": path_id,
                "reachable_endpoints": [],
                "count": 0,
                "exit_direction": None,
                "error": error,
            }
        ), 400

    return jsonify(
        {
            "path_id": path_id,
            "reachable_endpoints": reachable,
            "count": len(reachable),
            "exit_direction": path.get_exit_direction() if path else None,
            "error": None,
        }
    )


@app.route("/api/paths/<path_id>/complete", methods=["POST"])
def complete_path(path_id):
    """Mark a path as completed and release all resources.

    Used when train reaches destination.

    Returns:
        Success message or error
    """
    with simulation_lock:
        path = path_dispatcher.get_path(path_id)
        if not path:
            return jsonify({"error": f"Path {path_id} not found"}), 404

        success = path_dispatcher.complete_path(path_id)

    if success:
        return jsonify({"status": "success", "message": f"Path {path_id} completed"})
    return jsonify({"error": "Failed to complete path"}), 400


@app.route("/api/dispatcher/reachable/<block_id>", methods=["GET"])
def get_reachable_endpoints(block_id):
    """Get all blocks reachable from a starting block.

    Used by UI to populate destination dropdown.

    Query params:
        include_stations: If "true", include station IDs (default: true)

    Returns:
        List of reachable block IDs (and optionally station IDs)
    """
    include_stations = request.args.get("include_stations", "true").lower() != "false"

    if block_id not in network.blocks:
        return jsonify({"error": f"Block {block_id} not found"}), 404

    with simulation_lock:
        reachable = path_dispatcher.get_reachable_endpoints(
            block_id, include_stations=include_stations
        )

    return jsonify(
        {
            "start_block_id": block_id,
            "reachable_endpoints": reachable,
            "count": len(reachable),
        }
    )


@app.route("/api/dispatcher/paths", methods=["POST"])
def find_dispatcher_paths():
    """Find all possible paths between two blocks.

    Request body:
    {
        "start_block_id": "BLK_001",
        "end_block_id": "BLK_002",
        "train_id": "TRAIN_001"  // Optional
    }

    Returns:
        List of possible paths sorted by distance
    """
    data = request.json or {}
    start_block_id = data.get("start_block_id")
    end_block_id = data.get("end_block_id")
    train_id = data.get("train_id")

    if not start_block_id:
        return jsonify({"error": "start_block_id required"}), 400
    if not end_block_id:
        return jsonify({"error": "end_block_id required"}), 400

    with simulation_lock:
        paths = path_dispatcher.find_paths(start_block_id, end_block_id, train_id)

    return jsonify(
        {
            "paths": [p.to_dict() for p in paths],
            "count": len(paths),
            "start_block_id": start_block_id,
            "end_block_id": end_block_id,
        }
    )


# ============================================================
# SpeedProfile CRUD Endpoints
# ============================================================


@app.route("/api/speed-profiles", methods=["GET"])
def get_speed_profiles():
    """List all speed profiles.

    Returns:
        {
            "profiles": [
                {
                    "profile_id": str,
                    "name": str,
                    "speed_voltage_curve": [[speed, voltage], ...],
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
    with simulation_lock:
        profile_ids = speed_profile_registry.list_profiles()
        profiles = []
        for profile_id in profile_ids:
            profile = speed_profile_registry.get(profile_id)
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
        "speed_voltage_curve": [[speed, voltage], ...] (optional),
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
    data = request.json or {}

    profile_id = data.get("profile_id")
    name = data.get("name")

    if not profile_id:
        return jsonify({"error": "profile_id is required"}), 400
    if not name:
        return jsonify({"error": "name is required"}), 400

    with simulation_lock:
        # Check for duplicate
        if speed_profile_registry.get(profile_id):
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
            speed_profile_registry.register(profile)
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
    with simulation_lock:
        profile = speed_profile_registry.get(profile_id)

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
    data = request.json or {}

    # Ensure profile_id in body matches URL (if provided)
    if "profile_id" in data and data["profile_id"] != profile_id:
        return (
            jsonify(
                {"error": "profile_id in body must match URL parameter or be omitted"}
            ),
            400,
        )

    with simulation_lock:
        # Check if exists
        existing_profile = speed_profile_registry.get(profile_id)
        if not existing_profile:
            return jsonify({"error": f"Speed profile '{profile_id}' not found"}), 404

        # Merge data with profile_id from URL
        data["profile_id"] = profile_id

        # If name not provided, keep existing
        if "name" not in data:
            data["name"] = existing_profile.name

        # Create updated profile
        try:
            updated_profile = SpeedProfile.from_dict(data)
            speed_profile_registry.register(updated_profile)
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
    with simulation_lock:
        # Check if profile exists
        profile = speed_profile_registry.get(profile_id)
        if not profile:
            return jsonify({"error": f"Speed profile '{profile_id}' not found"}), 404

        # Check if it's the default profile
        if profile_id == "default":
            return jsonify({"error": "Cannot delete default speed profile"}), 409

        # Check if any train is using this profile
        trains_using = []
        for train in network.trains.values():
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
        success = speed_profile_registry.remove(profile_id)
        if not success:
            return (
                jsonify({"error": f"Failed to delete speed profile '{profile_id}'"}),
                500,
            )

    return "", 204


# =============================================================================
# Scale Configuration Endpoints
# =============================================================================


@app.route("/api/scale", methods=["GET"])
def get_scale():
    """Get current model scale configuration.

    Returns:
        JSON with current scale value and available presets
    """
    from train_config import get_model_scale, SCALE_PRESETS

    return jsonify({"scale": get_model_scale(), "presets": SCALE_PRESETS})


@app.route("/api/scale", methods=["PUT"])
def set_scale():
    """Set model scale.

    Request Body:
        {"scale": 160.0}  - Direct scale value
        OR
        {"preset": "1:160"}  - Use a preset

    Returns:
        JSON with new scale value
    """
    from train_config import get_model_scale, set_model_scale, SCALE_PRESETS

    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    with simulation_lock:
        try:
            if "preset" in data:
                preset_name = data["preset"]
                if preset_name not in SCALE_PRESETS:
                    return jsonify(
                        {
                            "error": f"Unknown preset '{preset_name}'",
                            "available_presets": list(SCALE_PRESETS.keys()),
                        }
                    ), 400
                set_model_scale(SCALE_PRESETS[preset_name])
            elif "scale" in data:
                scale = float(data["scale"])
                set_model_scale(scale)
            else:
                return jsonify({"error": "Must provide 'scale' or 'preset'"}), 400

            return jsonify({"scale": get_model_scale(), "status": "success"})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400


# =============================================================================
# Switch Configuration API
# =============================================================================


@app.route("/api/switches", methods=["GET"])
def get_switches():
    """Get all switch configurations."""
    return jsonify([s.to_dict() for s in network.switches.values()])


@app.route("/api/switches/<switch_id>", methods=["GET"])
def get_switch(switch_id: str):
    """Get specific switch configuration."""
    switch = network.get_switch(switch_id)
    if not switch:
        return jsonify({"error": "Switch not found"}), 404
    return jsonify(switch.to_dict())


@app.route("/api/tracks/<int:x>/<int:y>/switch", methods=["POST"])
def create_switch_at_track(x: int, y: int):
    """
    Create a switch configuration for a track tile.

    User requirement: Auto-created when switch track is placed.
    This endpoint is called automatically by the frontend.
    """
    try:
        track = network.get_track(x, y)
        if not track:
            return jsonify({"error": "No track at position"}), 404
        if not track.is_switch:
            return jsonify({"error": "Track is not a switch"}), 400

        # Check if switch already exists
        existing = network.get_switch_at(x, y)
        if existing:
            # Return existing switch (idempotent)
            return jsonify(existing.to_dict()), 200

        # Generate switch ID
        switch_id = f"SW_{x}_{y}"

        from switch import Switch

        data = request.json if request.json else {}

        # User requirement: Default to 0.0 cm for both branches
        switch = Switch(
            switch_id=switch_id,
            x=x,
            y=y,
            main_length_cm=data.get("main_length_cm", 0.0),
            diverge_length_cm=data.get("diverge_length_cm", 0.0),
            main_radius_cm=data.get("main_radius_cm"),
            diverge_radius_cm=data.get("diverge_radius_cm"),
            name=data.get("name", ""),
            switch_type=data.get("switch_type", "standard"),
            notes=data.get("notes", ""),
        )

        network.add_switch(switch)

        return jsonify(switch.to_dict()), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/switches/<switch_id>", methods=["PUT"])
def update_switch(switch_id: str):
    """Update switch configuration."""
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400

        switch = network.update_switch(switch_id, **data)
        if not switch:
            return jsonify({"error": "Switch not found"}), 404

        return jsonify(switch.to_dict())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/switches/<switch_id>", methods=["DELETE"])
def delete_switch(switch_id: str):
    """Remove switch configuration."""
    try:
        success = network.remove_switch(switch_id)
        if not success:
            return jsonify({"error": "Switch not found"}), 404

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/switches/<switch_id>/state", methods=["PUT"])
def change_switch_state(switch_id: str):
    """
    Change switch state (MAIN=0, DIVERGE=1).

    Fails with 409 Conflict if switch is locked by a path.
    Use lock field to check if switch is locked before calling.

    Request body:
        {"state": 0 or 1}

    Returns:
        200: {"success": true, "message": "..."}
        404: {"error": "Switch not found"}
        409: {"error": "Switch locked by path ..."}
        400: {"error": "Invalid state value"}
    """
    try:
        data = request.json
        if not data or "state" not in data:
            return jsonify({"error": "Missing 'state' in request body"}), 400

        new_state = data["state"]
        if new_state not in (0, 1):
            return jsonify(
                {"error": "Invalid state value. Must be 0 (MAIN) or 1 (DIVERGE)"}
            ), 400

        switch = network.switches.get(switch_id)
        if not switch:
            return jsonify({"error": f"Switch {switch_id} not found"}), 404

        # Use request_state_change which checks lock status
        success, message = switch.request_state_change(new_state, network)

        if success:
            return jsonify(
                {"success": True, "message": message, "switch": switch.to_dict()}
            )
        else:
            # Switch is locked - return 409 Conflict
            return jsonify({"error": message}), 409

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/switches/<switch_id>/lock", methods=["POST"])
def lock_switch(switch_id: str):
    """
    Lock a switch for a path.

    Request body:
        {"path_id": "PATH_123", "state": 0 or 1}

    Returns:
        200: {"success": true, "switch": {...}}
        404: {"error": "Switch not found"}
        409: {"error": "Switch already locked by different path"}
    """
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400

        path_id = data.get("path_id")
        state = data.get("state")

        if not path_id:
            return jsonify({"error": "Missing 'path_id' in request body"}), 400
        if state not in (0, 1):
            return jsonify(
                {"error": "Invalid state value. Must be 0 (MAIN) or 1 (DIVERGE)"}
            ), 400

        switch = network.switches.get(switch_id)
        if not switch:
            return jsonify({"error": f"Switch {switch_id} not found"}), 404

        # First change state, then lock
        track = network.get_track(switch.x, switch.y)
        if track:
            track.set_switch_state(state)

        success = switch.lock(path_id, state)

        if success:
            return jsonify({"success": True, "switch": switch.to_dict()})
        else:
            return jsonify(
                {"error": f"Switch already locked by path {switch.locked_by_path}"}
            ), 409

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/switches/<switch_id>/unlock", methods=["POST"])
def unlock_switch(switch_id: str):
    """
    Unlock a switch.

    Request body:
        {"path_id": "PATH_123"}

    Returns:
        200: {"success": true, "switch": {...}}
        404: {"error": "Switch not found"}
        403: {"error": "Switch not locked by this path"}
    """
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400

        path_id = data.get("path_id")
        if not path_id:
            return jsonify({"error": "Missing 'path_id' in request body"}), 400

        switch = network.switches.get(switch_id)
        if not switch:
            return jsonify({"error": f"Switch {switch_id} not found"}), 404

        success = switch.unlock(path_id)

        if success:
            return jsonify({"success": True, "switch": switch.to_dict()})
        else:
            if switch.is_locked():
                return jsonify(
                    {
                        "error": f"Switch locked by different path: {switch.locked_by_path}"
                    }
                ), 403
            else:
                return jsonify({"error": "Switch is not locked"}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5001)
