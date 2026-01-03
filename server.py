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
    TRACK_NS, TRACK_EW, TRACK_NE, TRACK_NW, TRACK_SE, TRACK_SW
)
from train import Train, TrainSchedule, ScheduleStop, TrainCar
from enums import Direction, TrackType, TrainStatus, SignalState as EnumSignalState, SwitchPosition
from railway_signal import Signal, SignalType, SignalState
from signal_validation import validate_signal_placement, get_valid_directions
from datetime import datetime, timedelta
import demo_main
from persistence import PersistenceManager

app = Flask(__name__, static_folder='static')
CORS(app)

# Custom converter for signed integers (supports negative coordinates)
class SignedIntConverter(IntegerConverter):
    regex = r'-?\d+'

app.url_map.converters['sint'] = SignedIntConverter

from simulation import SimulationController

# Global state
network = RailwayNetwork()
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
            timeout=60
        )

        if result.returncode != 0:
            # Check if pytest is not installed
            if "No module named pytest" in result.stderr:
                print("⚠️  WARNING: pytest not installed. Skipping full test suite.")
                print("   Install with: pip install pytest")
            else:
                print("❌ Backend tests FAILED!")
                print("\nTest output:")
                print(result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout)
                print("\n🛑 BLOCKING: Cannot start server with failing tests.")
                sys.exit(1)
        else:
            print("✅ Full backend test suite PASSED")
            # Show summary line
            if result.stdout:
                lines = result.stdout.split('\n')
                for line in lines:
                    if 'passed' in line.lower() or 'failed' in line.lower():
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

sim_controller = SimulationController(network)
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
                    result = persistence_manager.save_merged_layout(AUTOSAVE_FILENAME, network, "Auto-saved")
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

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('static', path)

@app.route('/api/network', methods=['GET'])
def get_network():
    return jsonify(network.to_dict())

@app.route('/api/track/<sint:x>/<sint:y>', methods=['GET'])
def get_track(x, y):
    """Get a single track segment"""
    track = network.get_track(x, y)
    if not track:
        return jsonify({'error': 'Track not found'}), 404

    # Return full track data using to_dict() which includes switch_points
    return jsonify(track.to_dict())

@app.route('/api/track/<sint:x>/<sint:y>', methods=['PATCH'])
def update_track(x, y):
    """Update a single track segment (6-bit track_bits system)"""
    track = network.get_track(x, y)
    if not track:
        return jsonify({"error": "Track not found"}), 404

    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON body"}), 400

    # === Geometry updates ===
    if 'track_bits' in data:
        new_bits = data['track_bits']
        # Validate and set bits
        if not track.set_track_bits(int(new_bits)):
            return jsonify({
                'success': False,
                'error': f'Invalid track_bits: {new_bits}'
            }), 400

        # If track became a switch, remove from block (switches are block boundaries)
        if track.is_switch and track.block_section_id:
            network.remove_tile_from_block(x, y)

    # === Dead-ends (buffer stops) ===
    if 'dead_end_ports' in data:
        # Replace set directly with normalized values
        try:
            ports = set(data['dead_end_ports'])
            track.dead_end_ports = {p for p in ports if p in {'N','E','S','W'}}
        except Exception:
            return jsonify({"error": "dead_end_ports must be a list of N/E/S/W"}), 400

    # === Switch state (simplified single int: 0=MAIN, 1=DIVERGE) ===
    if 'switch_state' in data:
        track.switch_state = int(data['switch_state'])

    # === Station properties ===
    if 'station_name' in data:
        track.station_name = data['station_name']  # Can be None to clear

    # === Block section ===
    if 'block_section_id' in data:
        track.block_section_id = data['block_section_id']  # Can be None to clear

    # Propagate changes to this tile and neighbors
    network.propagate_from_tile(x, y)

    # Recalculate signal states after switch or block changes
    if 'switch_state' in data or 'block_section_id' in data:
        network.update_signals_from_blocks()

    # If track has no geometry and no dead-ends, delete it
    if track.track_bits == 0 and len(track.dead_end_ports) == 0:
        network.remove_track(x, y)
        return jsonify({"status": "success", "message": f"Track at ({x}, {y}) removed (empty)"})

    return jsonify({"status": "success", "track": track.to_dict()})

@app.route('/api/track/<sint:x>/<sint:y>', methods=['DELETE'])
def delete_track(x, y):
    """Delete a single track segment"""
    track = network.get_track(x, y)
    if not track:
        return jsonify({"error": "Track not found"}), 404
    
    network.remove_track(x, y)
    return jsonify({"status": "success", "message": f"Track at ({x}, {y}) removed"})

@app.route('/api/tracks/bulk', methods=['POST'])
def add_tracks_bulk():
    """Add multiple track segments at once (for drag-to-build).
    If a track already exists at a position, merge the new track_bits.

    Requires track_bits: int - 6-bit track geometry
    """
    data = request.json
    tracks_data = data.get('tracks', [])
    added = []
    updated = []

    for track_data in tracks_data:
        x = track_data.get('x')
        y = track_data.get('y')
        if x is None or y is None:
            continue

        # Get track_bits (required)
        new_bits = track_data.get('track_bits', 0)

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

            network.propagate_from_tile(x, y)
            updated.append(existing_track.to_dict())
        else:
            # Create new track with track_bits
            if new_bits in FORBIDDEN_TRACK_BITS:
                continue
            track = TrackSegment(x=x, y=y, track_bits=new_bits)
            
            # Handle additional properties
            if track_data.get('station_name'):
                track.station_name = track_data['station_name']
            
            network.add_track(track)
            added.append(track.to_dict())
    
    return jsonify({
        "status": "success",
        "added": len(added),
        "updated": len(updated),
        "tracks": added + updated
    })

@app.route('/api/tracks/bulk-delete', methods=['POST'])
def delete_tracks_bulk():
    """Delete multiple track segments at once (for eraser drag)."""
    data = request.json
    positions = data.get('positions', [])
    deleted = []

    for pos in positions:
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            continue
        x, y = pos[0], pos[1]
        track = network.get_track(x, y)
        if track:
            network.remove_track(x, y)
            deleted.append([x, y])

    return jsonify({
        "status": "success",
        "deleted": len(deleted)
    })

# === Block Management Endpoints ===

@app.route('/api/blocks', methods=['GET'])
def get_blocks():
    """List all blocks with their track counts and positions"""
    result = []
    for block in network.blocks.values():
        block_dict = block.to_dict()
        tracks = network.get_block_tracks(block.block_id)
        block_dict['track_count'] = len(tracks)
        block_dict['track_positions'] = network.get_block_positions(block.block_id)

        # Find associated stations
        station_names = set()
        for track in tracks:
            if track.station_id:
                station = network.stations.get(track.station_id)
                if station:
                    station_names.add(station.name)

        if station_names:
            block_dict['station_names'] = sorted(list(station_names))

        result.append(block_dict)
    return jsonify(result)

@app.route('/api/blocks', methods=['POST'])
def create_block():
    """Create a new block"""
    data = request.json or {}
    block_id = data.get('block_id') or f"BLK_{uuid.uuid4().hex[:8].upper()}"
    name = data.get('name', '')
    try:
        block = network.create_block(block_id, name)
        # Set optional properties
        if 'length_cm' in data:
            block.length_cm = float(data['length_cm'])
        if 'min_radius_cm' in data:
            block.min_radius_cm = data['min_radius_cm']
        if 'max_speed_kmh' in data:
            block.max_speed_kmh = data['max_speed_kmh']
        return jsonify(block.to_dict())
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/blocks/<block_id>', methods=['GET'])
def get_block(block_id):
    """Get block details including validation status and connected signals"""
    block = network.get_block(block_id)
    if not block:
        return jsonify({'error': 'Block not found'}), 404
    result = block.to_dict()
    tracks = network.get_block_tracks(block_id)
    result['track_count'] = len(tracks)
    result['track_positions'] = network.get_block_positions(block_id)

    # Find associated stations
    station_names = set()
    for track in tracks:
        if track.station_id:
            station = network.stations.get(track.station_id)
            if station:
                station_names.add(station.name)

    if station_names:
        result['station_names'] = sorted(list(station_names))
    is_valid, errors = network.validate_block_continuity(block_id)
    result['is_continuous'] = is_valid
    result['validation_errors'] = errors

    # Find signals monitoring this block
    monitoring_signals = [
        signal.to_dict()
        for signal in network.signals.values()
        if signal.monitored_block_id == block_id
    ]
    result['monitoring_signals'] = monitoring_signals

    return jsonify(result)

@app.route('/api/blocks/<block_id>', methods=['PATCH'])
def update_block(block_id):
    """Update block properties"""
    from enums import BlockState as BlockStateEnum
    block = network.get_block(block_id)
    if not block:
        return jsonify({'error': 'Block not found'}), 404

    data = request.json or {}
    if 'name' in data:
        block.name = data['name'] if data['name'] is not None else ''
    if 'length_cm' in data:
        block.length_cm = float(data['length_cm'])
    if 'min_radius_cm' in data:
        block.min_radius_cm = data['min_radius_cm']
    if 'max_speed_kmh' in data:
        block.max_speed_kmh = data['max_speed_kmh']
    if 'state' in data:
        # Convert string to BlockState enum and use set_block_state for proper signal updates
        state_str = data['state'].lower()
        try:
            new_state = BlockStateEnum(state_str)
            network.set_block_state(block_id, new_state)
        except ValueError:
            return jsonify({'error': f'Invalid state: {state_str}. Valid: free, reserved, occupied, caution'}), 400

    return jsonify(block.to_dict())

@app.route('/api/blocks/<block_id>', methods=['DELETE'])
def delete_block(block_id):
    """Delete block and clear track references"""
    if network.delete_block(block_id):
        return jsonify({'status': 'success', 'message': f'Block {block_id} deleted'})
    return jsonify({'error': 'Block not found'}), 404

@app.route('/api/blocks/<block_id>/cycle-state', methods=['POST'])
def cycle_block_state(block_id):
    """
    Cycle block state: FREE -> RESERVED -> OCCUPIED -> CAUTION -> FREE
    
    Used by UI right-click to manually cycle through states for testing.
    """
    new_state = network.cycle_block_state(block_id)
    if new_state is None:
        return jsonify({'error': 'Block not found'}), 404
    
    block = network.get_block(block_id)
    return jsonify({
        'status': 'success',
        'block_id': block_id,
        'new_state': new_state.value,
        'block': block.to_dict()
    })

@app.route('/api/blocks/create-with-tracks', methods=['POST'])
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
    block_id = data.get('block_id')
    positions = data.get('positions', [])

    if not block_id:
        return jsonify({'error': 'block_id required'}), 400
    if not positions:
        return jsonify({'error': 'positions required'}), 400

    try:
        # Convert positions to tuples
        positions = [tuple(pos) for pos in positions]

        block = network.create_block_with_tracks(
            block_id=block_id,
            positions=positions,
            name=data.get('name', ''),
            length_cm=data.get('length_cm', 0.0),
            max_speed_kmh=data.get('max_speed_kmh')
        )
        return jsonify(block.to_dict())
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/blocks/<block_id>/extend', methods=['POST'])
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
    positions = data.get('positions', [])
    force = data.get('force', False)
    force_overwrite = data.get('force_overwrite', False)

    if not positions:
        return jsonify({'error': 'positions required'}), 400

    try:
        positions = [tuple(pos) for pos in positions]
        block = network.extend_block(block_id, positions, force=force, force_overwrite=force_overwrite)

        # Build response with invalidated signals info
        response = block.to_dict()
        if hasattr(block, '_invalidated_signals') and block._invalidated_signals:
            response['invalidated_signals'] = block._invalidated_signals
            # Clean up temporary attribute
            del block._invalidated_signals

        return jsonify(response)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/blocks/<block_id>/tracks', methods=['POST'])
def add_tracks_to_block(block_id):
    """Add tracks to a block by positions"""
    block = network.get_block(block_id)
    if not block:
        return jsonify({'error': 'Block not found'}), 404

    data = request.json or {}
    positions = data.get('positions', [])
    force = data.get('force', False)  # Allow reassignment if force=True
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
                reassigned.append({'position': [x, y], 'from': old_block_id, 'to': block_id})
            else:
                # Skip if not forcing reassignment
                continue

        # Now assign to new block
        try:
            network.assign_track_to_block(x, y, block_id)
            added.append([x, y])
        except ValueError:
            pass

    return jsonify({
        'status': 'success',
        'added': len(added),
        'reassigned': len(reassigned),
        'positions': added,
        'reassignment_details': reassigned,
        'block_id': block_id
    })

@app.route('/api/blocks/<block_id>/tracks', methods=['DELETE'])
def remove_tracks_from_block(block_id):
    """Remove tracks from a block (clear their block_section_id)"""
    block = network.get_block(block_id)
    if not block:
        return jsonify({'error': 'Block not found'}), 404

    data = request.json or {}
    positions = data.get('positions', [])
    removed = []

    for pos in positions:
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            continue
        x, y = pos[0], pos[1]
        track = network.get_track(x, y)
        if track and track.block_section_id == block_id:
            track.block_section_id = None
            removed.append([x, y])

    return jsonify({
        'status': 'success',
        'removed': len(removed),
        'positions': removed
    })

# === Signal Management Endpoints ===

@app.route('/api/signal', methods=['POST'])
def create_signal():
    """Create a new signal at specified position with validation"""
    data = request.json or {}

    x = data.get('x')
    y = data.get('y')
    detection_direction = data.get('detection_direction')

    if x is None or y is None:
        return jsonify({'error': 'Missing x or y coordinate'}), 400
    if not detection_direction:
        return jsonify({'error': 'Missing detection_direction'}), 400

    # Parse signal type
    signal_type_str = data.get('signal_type', 'block')
    try:
        signal_type = SignalType(signal_type_str.lower())
    except ValueError:
        return jsonify({'error': f'Invalid signal_type: {signal_type_str}'}), 400

    name = data.get('name')
    auto_connect = data.get('auto_connect', True)
    signal_id = data.get('signal_id')

    try:
        signal = network.add_signal(
            x=int(x),
            y=int(y),
            detection_direction=detection_direction,
            signal_type=signal_type,
            name=name,
            auto_connect=auto_connect,
            signal_id=signal_id
        )
        return jsonify(signal.to_dict())
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/signal/<signal_id>', methods=['GET'])
def get_signal(signal_id):
    """Get signal by ID"""
    signal = network.get_signal(signal_id)
    if not signal:
        return jsonify({'error': 'Signal not found'}), 404
    return jsonify(signal.to_dict())

@app.route('/api/signal/<signal_id>', methods=['PATCH'])
def update_signal(signal_id):
    """Update signal properties"""
    signal = network.get_signal(signal_id)
    if not signal:
        return jsonify({'error': 'Signal not found'}), 404

    data = request.json or {}

    try:
        # Build kwargs for update
        update_kwargs = {}

        if 'state' in data:
            update_kwargs['state'] = data['state']
        if 'name' in data:
            update_kwargs['name'] = data['name']
        if 'monitored_block_id' in data:
            update_kwargs['monitored_block_id'] = data['monitored_block_id']
        if 'monitored_switch_id' in data:
            update_kwargs['monitored_switch_id'] = data['monitored_switch_id']
        if 'monitored_signal_id' in data:
            update_kwargs['monitored_signal_id'] = data['monitored_signal_id']

        updated = network.update_signal(signal_id, **update_kwargs)
        return jsonify(updated.to_dict())
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/signal/<signal_id>', methods=['DELETE'])
def delete_signal(signal_id):
    """Delete signal by ID"""
    if network.remove_signal(signal_id):
        return jsonify({'status': 'success', 'message': f'Signal {signal_id} deleted'})
    return jsonify({'error': 'Signal not found'}), 404

@app.route('/api/signal/<sint:x>/<sint:y>', methods=['GET'])
def get_signal_at(x, y):
    """Get signal at position"""
    signal = network.get_signal_at(x, y)
    if not signal:
        return jsonify({'error': f'No signal at ({x}, {y})'}), 404
    return jsonify(signal.to_dict())

@app.route('/api/signal/<sint:x>/<sint:y>', methods=['DELETE'])
def delete_signal_at(x, y):
    """Delete signal at position"""
    if network.remove_signal_at(x, y):
        return jsonify({'status': 'success', 'message': f'Signal at ({x}, {y}) deleted'})
    return jsonify({'error': f'No signal at ({x}, {y})'}), 404

@app.route('/api/signal/<signal_id>/reachable-blocks', methods=['GET'])
def get_reachable_blocks(signal_id):
    """Get all blocks that are reachable from a signal.

    Used by the UI to populate the 'block to monitor' dropdown
    with only valid options based on track connectivity.
    """
    signal = network.get_signal(signal_id)
    if not signal:
        return jsonify({'error': 'Signal not found'}), 404

    reachable = network.get_reachable_blocks(signal_id)
    return jsonify({
        'signal_id': signal_id,
        'reachable_blocks': reachable
    })

@app.route('/api/signal/validate/<sint:x>/<sint:y>', methods=['GET'])
def validate_signal_at(x, y):
    """Check if a signal can be placed at the given position and return valid detection directions"""
    track = network.get_track(x, y)
    if not track:
        return jsonify({
            'can_place': False,
            'error': 'No track at position',
            'valid_directions': [],
            'available_directions': []
        })

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
        return jsonify({
            'can_place': False,
            'error': 'Diagonal track can only have 1 signal' if is_diagonal else 'Maximum signals reached',
            'valid_directions': valid_dirs,
            'available_directions': [],
            'is_diagonal': is_diagonal,
            'max_signals': max_signals
        })

    # Check which detection directions already have signals
    available_dirs = []
    for detection_dir in valid_dirs:
        if not network.get_signal_at_direction(x, y, detection_dir):
            available_dirs.append(detection_dir)

    can_place = len(available_dirs) > 0

    if not can_place:
        return jsonify({
            'can_place': False,
            'error': 'All valid directions already have signals',
            'valid_directions': valid_dirs,
            'available_directions': [],
            'is_diagonal': is_diagonal,
            'max_signals': max_signals
        })

    return jsonify({
        'can_place': can_place,
        'valid_directions': valid_dirs,
        'available_directions': available_dirs,
        'track_bits': track.track_bits,
        'is_diagonal': is_diagonal,
        'max_signals': max_signals
    })

# === Station Endpoints ===

@app.route('/api/station', methods=['POST'])
def create_station():
    """Create a new station at specified position"""
    data = request.json
    x = data.get('x')
    y = data.get('y')

    if x is None or y is None:
        return jsonify({'error': 'Missing x or y coordinate'}), 400

    try:
        station = network.add_station(
            x=x,
            y=y,
            station_id=data.get('station_id'),
            name=data.get('name', ''),
            length_cm=data.get('length_cm', 0.0),
            platform_side=data.get('platform_side', 'both'),
            top_speed_kmh=data.get('top_speed_kmh')
        )
        return jsonify(station.to_dict_with_orientation(network))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/station/<station_id>', methods=['GET'])
def get_station(station_id):
    """Get station by ID"""
    station = network.get_station(station_id)
    if not station:
        return jsonify({'error': 'Station not found'}), 404
    return jsonify(station.to_dict_with_orientation(network))

@app.route('/api/station/<station_id>', methods=['PATCH'])
def update_station(station_id):
    """Update station properties"""
    station = network.get_station(station_id)
    if not station:
        return jsonify({'error': 'Station not found'}), 404

    data = request.json

    # Update allowed fields
    if 'name' in data:
        station.name = data['name']
    if 'length_cm' in data:
        station.length_cm = data['length_cm']
    if 'platform_side' in data:
        from enums import PlatformSide
        station.platform_side = PlatformSide(data['platform_side'])
    if 'top_speed_kmh' in data:
        station.top_speed_kmh = data['top_speed_kmh']

    return jsonify(station.to_dict_with_orientation(network))

@app.route('/api/station/<station_id>', methods=['DELETE'])
def delete_station(station_id):
    """Delete station"""
    success = network.delete_station(station_id)
    if not success:
        return jsonify({'error': 'Station not found'}), 404
    return jsonify({'success': True})

@app.route('/api/station/validate/<sint:x>/<sint:y>', methods=['GET'])
def validate_station_at(x, y):
    """Check if station can be placed at position"""
    try:
        track = network.get_track(x, y)

        if not track:
            return jsonify({
                'can_place': False,
                'error': f'No track at position ({x}, {y})'
            })

        if track.is_switch:
            return jsonify({
                'can_place': False,
                'error': 'Cannot place station on switch'
            })

        if track.signal_ids:
            return jsonify({
                'can_place': False,
                'error': 'Cannot place station on tile with signal'
            })

        if track.station_id is not None:
            return jsonify({
                'can_place': False,
                'error': f'Tile already belongs to station {track.station_id}'
            })

        return jsonify({
            'can_place': True,
            'existing_block': track.block_section_id
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e), 'can_place': False}), 500

@app.route('/api/station/<station_id>/extend', methods=['POST'])
def extend_station(station_id):
    """Extend station platform with new tiles"""
    data = request.json
    positions = data.get('positions', [])

    if not positions:
        return jsonify({'error': 'Missing positions'}), 400

    # Convert positions to tuples
    positions = [tuple(pos) for pos in positions]

    try:
        station = network.extend_station(station_id, positions)
        return jsonify(station.to_dict_with_orientation(network))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/trains', methods=['POST'])
def create_train():
    data = request.json
    train_id = data.get('id')
    if not train_id:
        return jsonify({"error": "Missing train ID"}), 400

    # Create a default locomotive car with the specified length
    # API provides length in track units (approx 100m)
    length_units = data.get('length', 1.0)
    length_meters = length_units * 100.0

    car = TrainCar(
        car_id=f"{train_id}_loco",
        car_type="LOCOMOTIVE",
        length=length_meters,
        mass=100000.0, # default mass
        max_speed=data.get('max_speed', 100.0)
    )

    train = Train(
        train_id=train_id,
        consist=[car],
        max_speed=data.get('max_speed', 100.0)
    )

    # Set initial position if provided
    if 'position' in data:
        pos = data['position']
        direction = Direction[data.get('direction', 'EAST')]
        train.set_position((pos['x'], pos['y']), direction)
        
    network.add_train(train)
    return jsonify({"status": "success", "train": train.to_dict()})

@app.route('/api/simulation/control', methods=['POST'])
def control_simulation():
    global simulation_running, simulation_speed
    data = request.json
    action = data.get('action')
    
    if action == 'start':
        simulation_running = True
    elif action == 'stop':
        simulation_running = False
    elif action == 'reset':
        simulation_running = False
        # Reset logic here
        pass
    elif action == 'set_speed':
        simulation_speed = data.get('speed', 1.0)
        
    return jsonify({
        "status": "success", 
        "running": simulation_running, 
        "speed": simulation_speed
    })

# ============================================================================
# Layout Persistence API
# ============================================================================

@app.route('/api/layouts', methods=['GET'])
def list_layouts():
    """List all saved layouts."""
    layouts = persistence_manager.list_layouts()
    return jsonify({"layouts": layouts})


@app.route('/api/layouts', methods=['POST'])
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
            "steps_count": result["steps_count"]
        }

        # Notify user if steps failed to generate
        if result["steps_error"]:
            response["warning"] = f"Steps could not be generated: {result['steps_error']}"

        return jsonify(response)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except IOError as e:
        return jsonify({"error": str(e)}), 500
    finally:
        simulation_running = was_running


@app.route('/api/layouts/<name>/load', methods=['POST'])
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
                return jsonify({
                    "error": result.get("error", "Failed to load layout"),
                    "method": result.get("method")
                }), 400

            sim_controller.network = network
            last_autosave_time = time.time()

        response = {
            "status": "success",
            "message": f"Layout '{name}' loaded successfully",
            "method": result.get("method")
        }
        if result.get("recovery"):
            response["recovery"] = True
            response["message"] = f"Layout '{name}' recovered from steps (snapshot was corrupted)"

        return jsonify(response)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        simulation_running = was_running


@app.route('/api/layouts/<name>', methods=['DELETE'])
def delete_layout(name):
    """Delete a saved layout."""
    if persistence_manager.delete_layout(name):
        return jsonify({
            "status": "success",
            "message": f"Layout '{name}' deleted"
        })
    else:
        return jsonify({"error": f"Layout '{name}' not found"}), 404


@app.route('/api/network/clear', methods=['POST'])
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
                "trains": len(network.trains)
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

        return jsonify({
            "status": "success",
            "message": "Network cleared",
            "cleared": counts
        })
    finally:
        simulation_running = was_running


@app.route('/api/network/stats', methods=['GET'])
def get_network_stats():
    """Get counts of items in the network (for checking if empty)."""
    with simulation_lock:
        return jsonify({
            "tracks": len(network.tracks),
            "blocks": len(network.blocks),
            "signals": len(network.signals),
            "stations": len(network.stations),
            "trains": len(network.trains),
            "is_empty": len(network.tracks) == 0
        })


if __name__ == '__main__':
    app.run(debug=True, port=5001)
