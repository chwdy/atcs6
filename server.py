import time
import threading
import json
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from network import RailwayNetwork
from train import Train, TrainSchedule, ScheduleStop
from enums import Direction, TrackType, TrainStatus
from datetime import datetime, timedelta
import demo_main

app = Flask(__name__, static_folder='static')
CORS(app)

from simulation import SimulationController

# Global state
network = RailwayNetwork()
# Initialize with demo data for now so it's not empty
# In a real app, we might want to start empty or load from save
network = demo_main.create_comprehensive_demo()

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
