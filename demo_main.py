"""
Railway System Demo

This is the main demonstration script for the railway management system.
It imports all components from separate modules and creates a comprehensive
demo showing train dispatching, scheduling, and safety systems in action.

Track Building using 6-bit track_bits system:
- TrackSegment(x, y, track_bits=TRACK_EW): East-West straight track
- TrackSegment(x, y, track_bits=TRACK_NS): North-South straight track
- TrackSegment(x, y, track_bits=TRACK_NE): North-East diagonal track
- TrackSegment(x, y, track_bits=TRACK_NW): North-West diagonal track
- TrackSegment(x, y, track_bits=TRACK_SE): South-East diagonal track
- TrackSegment(x, y, track_bits=TRACK_SW): South-West diagonal track
- TrackSegment(x, y, track_bits=TRACK_EW | TRACK_NE): 3-way switch
- Dead-ends are auto-detected by propagation system based on neighbor connectivity
"""

from datetime import datetime, timedelta
from enums import Direction, SignalState, SwitchPosition
from network import RailwayNetwork
from track import TrackSegment, TRACK_NS, TRACK_EW, TRACK_NE, TRACK_NW, TRACK_SE, TRACK_SW, FORBIDDEN_TRACK_BITS
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

    # Coordinate convention:
    # - X-axis positive = east
    # - Y-axis positive = north
    # Main line from (0,0) to (10,0) using TRACK_EW
    # Switch at (3,0) with branch going north to (3,3) using TRACK_NS

    # Main horizontal line: (0,0) to (10,0) with TRACK_EW
    # Start endpoint at (0,0) - will auto-detect as dead-end at W
    track = TrackSegment(0, 0, track_bits=TRACK_EW)
    track.block_section_id = "BLOCK_MAIN_0"
    network.add_track(track)

    # Middle tracks (1,0) to (2,0)
    for x in range(1, 3):
        track = TrackSegment(x, 0, track_bits=TRACK_EW)
        track.block_section_id = f"BLOCK_MAIN_{x}"
        network.add_track(track)

    # Switch at (3,0) - connects main line (EW) with branch toward NORTH via NW
    # Using TRACK_EW | TRACK_NW creates a switch at W edge (choose E or N when entering from W)
    # Switch state: 0 = MAIN (EW, long path), 1 = DIVERGE (NW, short path)
    track = TrackSegment(3, 0, track_bits=TRACK_EW | TRACK_NW)
    track.block_section_id = "BLOCK_SWITCH"
    track.switch_state = 0  # Initialize to MAIN (long path EW active)
    network.add_track(track)

    # Continue main line from (4,0) to (10,0)
    for x in range(4, 11):
        track = TrackSegment(x, 0, track_bits=TRACK_EW)
        track.block_section_id = f"BLOCK_MAIN_{x}"
        network.add_track(track)

    # Add a signal on the main line
    track = TrackSegment(5, 0, track_bits=TRACK_EW)
    track.signal_state = SignalState.GREEN
    track.signal_direction = Direction.EAST  # Signal facing east
    track.block_section_id = "BLOCK_MAIN_5"
    network.add_track(track)

    # Add a station on the main line
    track = TrackSegment(7, 0, track_bits=TRACK_EW)
    track.station_name = "Central Station"
    track.block_section_id = "BLOCK_MAIN_7"
    network.add_track(track)

    # Branch line going north from switch at (3,0): (3,1) to (3,3)
    for y in range(1, 4):
        track = TrackSegment(3, y, track_bits=TRACK_NS)
        track.block_section_id = f"BLOCK_BRANCH_{y}"
        network.add_track(track)

    # Add a station on the branch line
    track = TrackSegment(3, 2, track_bits=TRACK_NS)
    track.station_name = "North Station"
    track.block_section_id = "BLOCK_BRANCH_2"
    network.add_track(track)
    
    print("✓ Network created successfully")
    print(f"  Total tracks: {len(network.tracks)}")

    return network


def create_track_showcase(network: RailwayNetwork):
    """Create showcase of all valid track_bits configurations.

    Places all 42 valid non-empty track_bits (0-63 excluding forbidden) in a grid
    to the left of the main demo area. Each tile is isolated so dead-ends are
    auto-detected on all edges, demonstrating the buffer stop rendering.
    """
    # Valid track_bits values (1-63 minus forbidden, excluding 0 which is empty)
    valid_bits = [i for i in range(1, 64) if i not in FORBIDDEN_TRACK_BITS]

    # Place in a grid starting at (-15, -5) to avoid overlap with main demo
    start_x, start_y = -15, -5
    cols = 7  # 7 columns

    for idx, bits in enumerate(valid_bits):
        x = start_x + (idx % cols) * 2  # 2-tile spacing
        y = start_y - (idx // cols) * 2
        print(f"Adding track_bits={bits:06b} at ({x}, {y})")
        track = TrackSegment(x, y, track_bits=bits)
        network.add_track(track)

    print(f"✓ Track showcase created with {len(valid_bits)} valid configurations")
    print(f"  Location: ({start_x}, {start_y}) in a {cols}-column grid")

    return network




def run_comprehensive_demo():
    """Run comprehensive demonstration of isometric railway network"""

    # Create network
    network = create_comprehensive_demo()

    # Add showcase of all valid track configurations
    network = create_track_showcase(network)

    print("\n" + "=" * 70)
    print("Isometric Demo - Track Layout Summary")
    print("=" * 70)
    print(f"Main line: (0,0) → (10,0) using TRACK_EW (east-west)")
    print(f"  Total: {len([t for t in network.tracks.values() if t.y == 0])} tracks on main line")
    print(f"Branch line: (3,1) → (3,3) using TRACK_NS (north)")
    print(f"  Total: {len([t for t in network.tracks.values() if t.x == 3 and t.y > 0])} tracks on branch")
    print(f"Switch at (3,0) connects main line with branch")
    print(f"Stations: Central Station at (7,0), North Station at (3,2)")
    print(f"Signal: GREEN at (5,0) facing EAST")

    # Show network visualization
    network.visualization()
    
    print("\n" + "=" * 70)
    print("Demo Complete")
    print("=" * 70)


if __name__ == "__main__":
    run_comprehensive_demo()
