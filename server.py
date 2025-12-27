import time
import threading
import json
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.routing import IntegerConverter
from network import RailwayNetwork
from track import (
    TrackSegment,
    FORBIDDEN_TRACK_BITS,
    TRACK_NS, TRACK_EW, TRACK_NE, TRACK_NW, TRACK_SE, TRACK_SW
)
from train import Train, TrainSchedule, ScheduleStop
from enums import Direction, TrackType, TrainStatus, SignalState, SwitchPosition
from datetime import datetime, timedelta
import demo_main

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
network = demo_main.create_comprehensive_demo()
network = demo_main.create_track_showcase(network)

sim_controller = SimulationController(network)
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
            sim_controller.update(sim_delta)
            
        time.sleep(0.1)

# Start simulation thread
sim_thread = threading.Thread(target=simulation_loop, daemon=True)
sim_thread.start()

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('static', path)

@app.route('/api/network', methods=['GET'])
def get_network():
    return jsonify(network.to_dict())

@app.route('/api/network/update', methods=['POST'])
def update_network():
    data = request.json
    network.from_dict(data)
    return jsonify({"status": "success", "message": "Network updated"})

@app.route('/api/track', methods=['POST'])
def add_track():
    """Add a single track segment using track_bits"""
    data = request.json
    x = data.get('x')
    y = data.get('y')

    if x is None or y is None:
        return jsonify({"error": "Missing x or y coordinate"}), 400

    # Accept track_bits directly
    track_bits = data.get('track_bits', 0)

    # Validate against forbidden configurations
    if track_bits in FORBIDDEN_TRACK_BITS:
        return jsonify({
            'success': False,
            'error': f'Forbidden configuration: {track_bits}'
        }), 400

    # Create or update track
    existing_track = network.get_track(x, y)
    if not existing_track:
        track = TrackSegment(x, y, track_bits=track_bits)

        # Handle additional properties
        if data.get('signal_state'):
            track.signal_state = SignalState[data['signal_state']]
        if data.get('signal_direction'):
            track.signal_direction = Direction[data['signal_direction']]
        if data.get('station_name'):
            track.station_name = data['station_name']

        network.add_track(track)  # Triggers propagation
    else:
        if not existing_track.set_track_bits(track_bits):  # Validates internally
            return jsonify({
                'success': False,
                'error': f'Invalid track_bits: {track_bits}'
            }), 400
        network.propagate_from_tile(x, y)  # Manual propagation

    track = network.get_track(x, y)
    return jsonify({
        'success': True,
        'track_bits': track.track_bits,
        'dead_end_ports': list(track.dead_end_ports),
        'switch_states': track.switch_states
    })

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

    # === Dead-ends (buffer stops) ===
    # Accept legacy key 'dead_ends' for compatibility
    if 'dead_end_ports' in data:
        # Replace set directly with normalized values
        try:
            ports = set(data['dead_end_ports'])
            track.dead_end_ports = {p for p in ports if p in {'N','E','S','W'}}
        except Exception:
            return jsonify({"error": "dead_end_ports must be a list of N/E/S/W"}), 400
    elif 'dead_ends' in data:
        # Use helper (deprecated) but still supported
        track.dead_end_ports.clear()
        for port in data['dead_ends']:
            track.add_dead_end(port)

    # === Switch state (simplified single int: 0=MAIN, 1=DIVERGE) ===
    if 'switch_state' in data:
        track.switch_state = int(data['switch_state'])

    # === Signals & station properties ===
    if 'signal_state' in data and data['signal_state']:
        track.signal_state = SignalState[data['signal_state']]
    if 'signal_direction' in data and data['signal_direction']:
        track.signal_direction = Direction[data['signal_direction']]
    if 'station_name' in data:
        track.station_name = data['station_name']

    # === Switch toggling via switch_position (legacy, maps to switch_state) ===
    if 'switch_position' in data:
        new_pos = data['switch_position']
        if track.is_switch:
            # MAIN=0, DIVERGING=1
            track.switch_state = 1 if new_pos == 'DIVERGING' else 0

    # Propagate changes to this tile and neighbors
    network.propagate_from_tile(x, y)

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

@app.route('/api/track/<sint:x>/<sint:y>/path', methods=['POST'])
def add_path_to_track(x, y):
    """Add a single path to a track (or create track if doesn't exist).
    
    Request body: {"path": {"port1": "N", "port2": "S", "direction": "both"}}
    or legacy: {"path": ["PORT1", "PORT2"]}
    """
    data = request.json
    path = data.get('path')
    
    if not path:
        return jsonify({"error": "Path required"}), 400
    
    # Parse path - support both dict and list formats
    if isinstance(path, dict):
        port1 = path.get('port1')
        port2 = path.get('port2')
        direction = path.get('direction', 'both')
    elif isinstance(path, list) and len(path) >= 2:
        port1, port2 = path[0], path[1]
        direction = path[2] if len(path) > 2 else 'both'
    else:
        return jsonify({"error": "Path must be {port1, port2, direction} or [port1, port2]"}), 400
    
    track = network.get_track(x, y)
    if not track:
        # Create new track
        track = TrackSegment(x=x, y=y)
        network.add_track(track)
    
    track.add_path(port1, port2, direction)
    return jsonify({"status": "success", "track": track.to_dict()})

@app.route('/api/track/<sint:x>/<sint:y>/path', methods=['DELETE'])
def remove_path_from_track(x, y):
    """Remove a single path from a track.
    
    Request body: {"path": {"port1": "N", "port2": "S"}} or {"path": ["PORT1", "PORT2"]}
    """
    data = request.json
    path = data.get('path')
    
    if not path:
        return jsonify({"error": "Path required"}), 400
    
    # Parse path - support both dict and list formats
    if isinstance(path, dict):
        port1 = path.get('port1')
        port2 = path.get('port2')
    elif isinstance(path, list) and len(path) >= 2:
        port1, port2 = path[0], path[1]
    else:
        return jsonify({"error": "Path must be {port1, port2} or [port1, port2]"}), 400
    
    track = network.get_track(x, y)
    if not track:
        return jsonify({"error": "Track not found"}), 404
    
    removed = track.remove_path(port1, port2)
    
    # If no paths left, optionally remove the track
    if len(track.directed_paths) == 0:
        network.remove_track(x, y)
        return jsonify({"status": "success", "message": f"Track at ({x}, {y}) removed (no paths left)"})
    
    return jsonify({"status": "success", "removed": removed, "track": track.to_dict()})

@app.route('/api/tracks/bulk', methods=['POST'])
def add_tracks_bulk():
    """Add multiple track segments at once (for drag-to-build).
    If a track already exists at a position, merge the new track_bits.
    
    Accepts:
    - track_bits: int - 6-bit track geometry (preferred)
    - directed_paths: [{port1, port2}, ...] - legacy format, converted to track_bits
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
        
        # Get track_bits - either directly or convert from directed_paths
        new_bits = track_data.get('track_bits', 0)
        
        # If no track_bits but has directed_paths, convert them
        if new_bits == 0 and 'directed_paths' in track_data:
            for p in track_data['directed_paths']:
                if isinstance(p, dict):
                    port1, port2 = p.get('port1', ''), p.get('port2', '')
                elif isinstance(p, list) and len(p) >= 2:
                    port1, port2 = p[0], p[1]
                else:
                    continue
                # Convert port names to bit
                e1 = port1[0].upper() if port1 else ''
                e2 = port2[0].upper() if port2 else ''
                bit_map = {
                    ('N','S'): 1, ('S','N'): 1,
                    ('E','W'): 2, ('W','E'): 2,
                    ('N','E'): 4, ('E','N'): 4,
                    ('N','W'): 8, ('W','N'): 8,
                    ('S','E'): 16, ('E','S'): 16,
                    ('S','W'): 32, ('W','S'): 32,
                }
                new_bits |= bit_map.get((e1, e2), 0)
        
        existing_track = network.get_track(x, y)
        
        if existing_track:
            # Merge new bits with existing (OR operation)
            merged_bits = existing_track.track_bits | new_bits
            if merged_bits in FORBIDDEN_TRACK_BITS:
                # Skip forbidden configurations
                continue
            existing_track.set_track_bits(merged_bits)
            network.propagate_from_tile(x, y)
            updated.append(existing_track.to_dict())
        else:
            # Create new track with track_bits
            if new_bits in FORBIDDEN_TRACK_BITS:
                continue
            track = TrackSegment(x=x, y=y, track_bits=new_bits)
            
            # Handle additional properties
            if track_data.get('signal_state'):
                track.signal_state = SignalState[track_data['signal_state']]
            if track_data.get('signal_direction'):
                track.signal_direction = Direction[track_data['signal_direction']]
            if track_data.get('station_name'):
                track.station_name = track_data['station_name']
            if track_data.get('type') == 'SIGNAL':
                track.signal_state = SignalState.RED
                track.signal_direction = Direction[track_data.get('signal_direction', 'EAST')]
            
            network.add_track(track)
            added.append(track.to_dict())
    
    return jsonify({
        "status": "success", 
        "added": len(added), 
        "updated": len(updated),
        "tracks": added + updated
    })

@app.route('/api/trains', methods=['POST'])
def create_train():
    data = request.json
    train_id = data.get('id')
    if not train_id:
        return jsonify({"error": "Missing train ID"}), 400
        
    train = Train(
        train_id=train_id,
        length=data.get('length', 1.0),
        min_curve_radius=data.get('min_curve_radius', 50.0),
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

@app.route('/api/simulation/state', methods=['GET'])
def get_simulation_state():
    # Return dynamic state (train positions, signal states)
    # For now, just return the whole network as it contains everything
    return jsonify(network.to_dict())

if __name__ == '__main__':
    app.run(debug=True, port=5001)
