import time
from datetime import datetime
from typing import List, Optional
from network import RailwayNetwork
from train import Train
from dispatcher import TrainDispatcher
from enums import TrainStatus, SignalState, TrackType

class SimulationController:
    def __init__(self, network: RailwayNetwork):
        self.network = network
        self.dispatcher = TrainDispatcher(network)
        self.last_update_time = datetime.now()
        
    def update(self, delta_seconds: float):
        """
        Update simulation state.
        
        Args:
            delta_seconds: Time elapsed since last update in seconds
        """
        # 1. Auto-route trains
        self._auto_route_trains()
        
        # 2. Update each train
        for train in self.network.trains.values():
            self._update_train(train, delta_seconds)
            
        # 3. Update signals based on block occupancy
        for block in self.network.block_sections.values():
            block.update_signal_state(self.network)
            
    def _auto_route_trains(self):
        """Automatically schedule and route trains"""
        for train in self.network.trains.values():
            if train.status == TrainStatus.IDLE and train.schedule:
                # Check if it's time to depart
                target = train.schedule.get_current_target(self.dispatcher.current_time)
                if target:
                    # If we are at the start of a journey or idle
                    if not train.current_path:
                        print(f"Auto-dispatching train {train.train_id} to {target.station_name}")
                        self.dispatcher.dispatch_train(train, target.station_pos)
                        
                        # Request route immediately
                        if train.current_path:
                            # Find route ID for this path (simplified)
                            # In a real system, we'd look up the route covering the next section
                            # For demo, we just try to lock the main route if on main line
                            # This needs to be more robust
                            pass

    def _update_train(self, train: Train, delta_seconds: float):
        if train.status != TrainStatus.RUNNING:
            return
            
        if not train.current_path or train.path_index >= len(train.current_path):
            train.status = TrainStatus.IDLE
            train.current_speed = 0
            return

        # Get current track info
        current_pos = train.current_pos
        current_track = self.network.get_track(*current_pos)
        
        if not current_track:
            return
            
        # Determine target speed
        target_speed = train.max_speed
        
        # Check upcoming signals/stations
        # Look ahead in path
        remaining_dist_current_track = current_track.length - train.distance_on_track
        
        # Simple lookahead
        lookahead_dist = 0
        limit_speed = train.max_speed
        
        # Check next track
        if train.path_index + 1 < len(train.current_path):
            next_x, next_y, _ = train.current_path[train.path_index + 1]
            next_track = self.network.get_track(next_x, next_y)
            
            if next_track:
                # Check signal at entry to next track
                if current_track.track_type == TrackType.SIGNAL:
                    if current_track.signal_state == SignalState.RED:
                        limit_speed = 0
                    elif current_track.signal_state == SignalState.YELLOW:
                        limit_speed = 40 # Slow down
                
                # Check if next track is occupied by another train
                if next_track.is_occupied() and next_track.occupied_by != train.train_id:
                    limit_speed = 0
        else:
            # End of path
            limit_speed = 0
            
        # Apply speed limit if close enough
        # If we need to stop within remaining distance + next track length
        dist_to_limit = remaining_dist_current_track * 100 # Convert to meters
        braking_dist = train.braking_distance
        
        if limit_speed < train.current_speed:
            # Start braking if we are within braking distance
            if dist_to_limit <= braking_dist * 1.2: # 20% buffer
                target_speed = limit_speed
        
        # Update physics
        train.update_physics(delta_seconds, target_speed, current_track.length)
        
        # Check for track transition
        if train.distance_on_track >= current_track.length:
            self._advance_train_track(train)
            
    def _advance_train_track(self, train: Train):
        """Move train to next track segment"""
        if train.path_index + 1 >= len(train.current_path):
            # End of line
            train.distance_on_track = self.network.get_track(*train.current_pos).length
            train.current_speed = 0
            train.status = TrainStatus.AT_STATION
            return
            
        # Move to next
        old_pos = train.current_pos
        
        # Get next step
        next_x, next_y, next_dir = train.current_path[train.path_index + 1]
        
        # Verify we can enter (interlocking/occupancy)
        next_track = self.network.get_track(next_x, next_y)
        if not next_track:
            train.current_speed = 0
            return
            
        # Check signal again (hard stop if red)
        current_track = self.network.get_track(*old_pos)
        if current_track.track_type == TrackType.SIGNAL and current_track.signal_state == SignalState.RED:
            train.distance_on_track = current_track.length
            train.current_speed = 0
            return
            
        # Update occupancy
        block = self.network.get_block_section((next_x, next_y))
        if block:
            block.occupy(train.train_id)
            
        # Release old track (simplified - should be based on train length)
        # For now, release immediately when head leaves
        old_block = self.network.get_block_section(old_pos)
        if old_block and old_block.block_id != block.block_id:
             old_block.release()
             
        # Update train state
        train.current_pos = (next_x, next_y)
        train.current_direction = next_dir
        train.path_index += 1
        train.distance_on_track = 0.0 # Reset distance on new track
        
        # Trigger interlocking for further ahead
        # (Simplified: just ensure route is locked)
        route_id = "ROUTE_MAIN" # Hack for demo
        if train.current_pos == (1,0): # Entering main route
             self.network.interlocking.request_route(route_id, train.train_id, self.network)
