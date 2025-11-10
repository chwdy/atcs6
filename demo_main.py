"""
Railway System Demo

This is the main demonstration script for the railway management system.
It imports all components from separate modules and creates a comprehensive
demo showing train dispatching, scheduling, and safety systems in action.
"""

from datetime import datetime, timedelta
from enums import Direction, SignalState, SwitchPosition
from network import RailwayNetwork
from train import Train, TrainSchedule, ScheduleStop
from interlocking import InterlockingRoute
from dispatcher import TrainDispatcher

# ============= Demo Functions =============

def create_comprehensive_demo():
    """Create comprehensive demo railway network"""
    print("\n" + "=" * 70)
    print("Creating Railway Network...")
    print("=" * 70)
    
    network = RailwayNetwork()
    
    # Main line: (0,0) -> (10,0)
    for x in range(11):
        block_id = f"BLOCK_MAIN_{x}"
        network.add_straight_track(x, 0, Direction.EAST, block_id)
    
    # Add signals at various positions
    network.add_signal(1, 0, Direction.EAST, SignalState.GREEN, "BLOCK_MAIN_1")
    network.add_signal(4, 0, Direction.EAST, SignalState.GREEN, "BLOCK_MAIN_4")
    network.add_signal(7, 0, Direction.EAST, SignalState.GREEN, "BLOCK_MAIN_7")
    
    # Add switch at (3,0) creating branch line
    network.add_switch(
        3, 0,
        entry_dir=Direction.WEST,
        main_exit=Direction.EAST,
        diverging_exit=Direction.NORTH,
    )
    
    # Branch line tracks
    for y in range(1, 4):
        block_id = f"BLOCK_BRANCH_{y}"
        network.add_straight_track(3, y, Direction.NORTH, block_id)
    
    # Branch line signal
    network.add_signal(3, 2, Direction.NORTH, SignalState.GREEN, "BLOCK_BRANCH_2")
    
    # Add stations
    network.add_station(5, 0, Direction.EAST, "Central Station", "BLOCK_MAIN_5")
    network.add_station(9, 0, Direction.EAST, "Terminal Station", "BLOCK_MAIN_9")
    network.add_station(3, 3, Direction.NORTH, "Branch Station", "BLOCK_BRANCH_3")
    
    # Create block sections
    network.create_block_section("BLOCK_1", [(0,0), (1,0)], (0,0), (1,0))
    network.create_block_section("BLOCK_2", [(2,0), (3,0), (4,0)], (1,0), (4,0))
    network.create_block_section("BLOCK_3", [(5,0), (6,0), (7,0)], (4,0), (7,0))
    network.create_block_section("BLOCK_4", [(8,0), (9,0), (10,0)], (7,0), (10,0))
    network.create_block_section("BLOCK_BRANCH", [(3,1), (3,2), (3,3)], (3,0), (3,3))
    
    # Create interlocking routes
    route1 = InterlockingRoute(
        route_id="ROUTE_MAIN",
        start_signal=(1, 0),
        end_signal=(7, 0),
        path=[(2,0), (3,0), (4,0), (5,0), (6,0), (7,0)],
        required_switch_positions={(3, 0): SwitchPosition.MAIN}
    )
    network.interlocking.add_route(route1)
    
    route2 = InterlockingRoute(
        route_id="ROUTE_BRANCH",
        start_signal=(1, 0),
        end_signal=(3, 3),
        path=[(2,0), (3,0), (3,1), (3,2), (3,3)],
        required_switch_positions={(3, 0): SwitchPosition.DIVERGING}
    )
    network.interlocking.add_route(route2)
    
    # Set route conflicts
    network.interlocking.set_route_conflict("ROUTE_MAIN", "ROUTE_BRANCH")
    
    print("✓ Network created successfully")
    print(f"  Total tracks: {len(network.tracks)}")
    print(f"  Block sections: {len(network.block_sections)}")
    print(f"  Interlocking routes: {len(network.interlocking.routes)}")
    
    return network


def run_comprehensive_demo():
    """Run comprehensive demonstration"""
    
    # Create network
    network = create_comprehensive_demo()
    
    # Create dispatcher
    dispatcher = TrainDispatcher(network)
    base_time = datetime.now()
    dispatcher.update_time(base_time)
    
    # Create trains
    print("\n" + "=" * 70)
    print("Creating Trains...")
    print("=" * 70)
    
    train1 = Train(
        train_id="T001",
        length=3.0,
        min_curve_radius=4.0,
        max_speed=120.0,
        current_pos=(0, 0),
        current_direction=Direction.EAST
    )
    
    train2 = Train(
        train_id="T002",
        length=2.5,
        min_curve_radius=3.0,
        max_speed=100.0,
        current_pos=(0, 0),
        current_direction=Direction.EAST
    )
    
    network.trains[train1.train_id] = train1
    network.trains[train2.train_id] = train2
    
    print(f"✓ Created train {train1.train_id}")
    print(f"✓ Created train {train2.train_id}")
    
    # Create schedules
    print("\n" + "=" * 70)
    print("Creating Schedules...")
    print("=" * 70)
    
    schedule1 = TrainSchedule(
        train_id="T001",
        schedule_id="SCH001",
        priority=1,
        stops=[
            ScheduleStop(
                station_pos=(5, 0),
                station_name="Central Station",
                arrival_time=base_time + timedelta(minutes=10),
                departure_time=base_time + timedelta(minutes=12)
            ),
            ScheduleStop(
                station_pos=(9, 0),
                station_name="Terminal Station",
                arrival_time=base_time + timedelta(minutes=20),
                departure_time=base_time + timedelta(minutes=22)
            )
        ]
    )
    
    schedule2 = TrainSchedule(
        train_id="T002",
        schedule_id="SCH002",
        priority=2,
        stops=[
            ScheduleStop(
                station_pos=(3, 3),
                station_name="Branch Station",
                arrival_time=base_time + timedelta(minutes=15),
                departure_time=base_time + timedelta(minutes=17)
            )
        ]
    )
    
    dispatcher.schedule_train(train1, schedule1)
    dispatcher.schedule_train(train2, schedule2)
    
    # Check schedule conflicts
    print("\n" + "=" * 70)
    print("Checking Schedule Conflicts...")
    print("=" * 70)
    
    conflicts = dispatcher.check_schedule_conflicts()
    if conflicts:
        print("⚠ Schedule conflicts found:")
        for t1, t2 in conflicts:
            print(f"  {t1} <-> {t2}")
    else:
        print("✓ No schedule conflicts")
    
    # Scenario 1: Dispatch T001 to Central Station (main line)
    print("\n" + "=" * 70)
    print("Scenario 1: Dispatch T001 to Central Station (Main Line)")
    print("=" * 70)
    
    success = dispatcher.dispatch_train(train1, (5, 0))
    
    if success:
        # Request main line route lock
        print("\nRequesting main line route lock...")
        route_locked = network.interlocking.request_route(
            "ROUTE_MAIN", train1.train_id, network
        )
        
        if route_locked:
            # Simulate train movement
            print("\nSimulating train movement...")
            for step in range(min(5, len(train1.current_path))):
                success = dispatcher.move_train_one_step(train1)
                if success:
                    print(f"  Step {step+1}: Train at {train1.current_pos}")
                else:
                    break
    
    # Show network visualization
    network.visualization()
    
    # Show status reports
    print(dispatcher.get_train_status_report())
    print(dispatcher.get_block_status_report())
    
    print("\n" + "=" * 70)
    print("Demo Complete")
    print("=" * 70)


if __name__ == "__main__":
    run_comprehensive_demo()
