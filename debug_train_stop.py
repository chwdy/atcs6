#!/usr/bin/env python3
"""
Debug script to trace why train stops prematurely at tile (9,4).

Expected: Train should stop at 16.67cm (21.67cm tile - 5cm safety margin)
Actual: Train stops at 9.65cm

This script captures:
- ATC speed limit calculation
- ATO target speed
- Clearance ahead values
- Remaining distance in current block
"""

import sys
import os

sys.path.insert(0, "/Users/Diwei/Documents/exp_self/atcs6")

from demo_main import create_comprehensive_demo, setup_demo_train
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from train_controller import ATC_PLATFORM_SAFETY_MARGIN_CM, STOPPED_SPEED_THRESHOLD_KMH


class DebugSimulationController(SimulationController):
    """Extended simulation controller with debug output for tile (9,4)."""

    def __init__(self, network, path_dispatcher=None, delta_time=0.1):
        super().__init__(network, path_dispatcher)
        self.delta_time = delta_time
        self.debug_tick_count = 0
        self.debug_interval = 5  # Print every 5 ticks
        self.target_tile = (9, 4)  # The tile we want to debug
        self.stopped_logged = False

    def tick(self):
        """Run one simulation tick with debug output."""
        super().update(self.delta_time)
        self.debug_tick_count += 1

        # Find the demo train
        demo_train = self.network.trains.get("DEMO_TRAIN")
        if not demo_train:
            return

        # Get train controller
        controller = self.controllers.get("DEMO_TRAIN")
        if not controller:
            return

        # Check if train is in target tile (9,4)
        if demo_train.position and demo_train.position.head_tile == self.target_tile:
            # Always log when speed is approaching 0
            speed = demo_train.current_speed
            head_distance = demo_train.position.head_distance_cm

            # Get tile length
            tile_length = self.network._get_tile_length_cm(*self.target_tile)

            # Print debug info every N ticks or when stopping
            should_print = (
                self.debug_tick_count % self.debug_interval == 0
                or (speed <= 5.0 and not self.stopped_logged)
                or speed <= STOPPED_SPEED_THRESHOLD_KMH
            )

            if should_print:
                # Calculate key values
                clearance = controller.clearance_ahead_cm
                atc_limit = controller.atc_speed_limit
                target_speed = controller.target_speed
                safety_margin = controller._get_effective_safety_margin()
                remaining_in_block = (
                    controller._get_remaining_distance_in_current_block()
                )

                # Get current block info
                current_block_id = controller._get_current_block_id()
                block = (
                    self.network.blocks.get(current_block_id)
                    if current_block_id
                    else None
                )

                print(f"\n{'=' * 80}")
                print(
                    f"TICK {self.debug_tick_count} - Train in tile {self.target_tile}"
                )
                print(f"{'=' * 80}")
                print(f"Position:")
                print(
                    f"  - Head distance in tile: {head_distance:.2f} cm / {tile_length:.2f} cm"
                )
                print(f"  - Current speed: {speed:.2f} km/h")
                print(f"  - Train state: {demo_train.state.value}")
                print(f"  - ATO running: {controller.ato_running}")
                print(f"\nATC Calculations:")
                print(f"  - Clearance ahead: {clearance:.2f} cm")
                print(f"  - Safety margin: {safety_margin:.2f} cm")
                print(
                    f"  - Effective clearance (clearance - margin): {max(0, clearance - safety_margin):.2f} cm"
                )
                print(f"  - ATC speed limit: {atc_limit:.2f} km/h")
                print(f"  - Speed limit reason: {controller.speed_limit_reason}")
                print(f"\nBlock Info:")
                print(f"  - Current block: {current_block_id}")
                print(f"  - Block length: {block.length_cm if block else 'N/A'} cm")
                print(f"  - Remaining in block: {remaining_in_block:.2f} cm")
                print(
                    f"  - Block max speed: {block.max_speed_kmh if block and block.max_speed_kmh else 'N/A'} km/h"
                )
                print(f"\nATO Info:")
                print(f"  - ATO target speed: {target_speed:.2f} km/h")
                print(
                    f"  - ATO destination block: {controller.ato_destination_block_id}"
                )
                print(f"  - Handle position: {controller.handle_position.value}")

                # Check if destination block has station
                if controller.ato_destination_block_id:
                    station_id = controller._get_station_in_block(
                        controller.ato_destination_block_id
                    )
                    if station_id:
                        station = self.network.stations.get(station_id)
                        print(
                            f"  - Destination has station: {station.name if station else station_id}"
                        )

                # Calculate where train should stop
                expected_stop = tile_length - safety_margin
                print(f"\nStopping Analysis:")
                print(f"  - Tile length: {tile_length:.2f} cm")
                print(f"  - Safety margin: {safety_margin:.2f} cm")
                print(f"  - Expected stop position: {expected_stop:.2f} cm")
                print(f"  - Current position: {head_distance:.2f} cm")
                print(
                    f"  - Distance to expected stop: {expected_stop - head_distance:.2f} cm"
                )

                if speed <= STOPPED_SPEED_THRESHOLD_KMH:
                    print(f"\n*** TRAIN STOPPED at {head_distance:.2f} cm ***")
                    print(f"*** Expected to stop at: {expected_stop:.2f} cm ***")
                    print(
                        f"*** ERROR: Stopped {expected_stop - head_distance:.2f} cm early! ***"
                    )
                    self.stopped_logged = True

                print(f"{'=' * 80}\n")


def run_debug_simulation():
    """Run the demo with debug output."""
    print("=" * 80)
    print("TRAIN STOPPING DEBUG - Tile (9,4)")
    print("=" * 80)
    print("\nThis debug trace will show:")
    print("- ATC speed limit calculations")
    print("- ATO target speed decisions")
    print("- Clearance and safety margin values")
    print("- When and why the train decides to stop")
    print("\nExpected: Train should stop at 16.67cm (21.67cm - 5cm safety margin)")
    print("=" * 80)

    # Create the demo network
    network = create_comprehensive_demo()

    # Create path dispatcher
    path_dispatcher = PathDispatcher(network)

    # Setup the demo train
    setup_demo_train(network, path_dispatcher)

    # Create debug simulation controller
    sim = DebugSimulationController(
        network, path_dispatcher=path_dispatcher, delta_time=0.1
    )

    # Initialize controllers by running a few ticks
    for _ in range(3):
        sim.tick()

    # Print initial train info
    demo_train = network.trains.get("DEMO_TRAIN")
    demo_controller = sim.controllers.get("DEMO_TRAIN") if sim else None
    if demo_train:
        head_pos = demo_train.position.head_tile if demo_train.position else None
        dest_block = (
            demo_controller.ato_destination_block_id if demo_controller else None
        )
        print(f"\nInitial Train Position: {head_pos}")
        print(f"Destination block: {dest_block}")
        print(f"Train state: {demo_train.state.value if demo_train.state else 'N/A'}")
        print(f"Train speed: {demo_train.current_speed:.2f} km/h")
        print(
            f"ATO running: {demo_controller.ato_running if demo_controller else 'N/A'}"
        )
        print(
            f"Path length: {len(demo_train.current_path) if demo_train.current_path else 0} tiles"
        )

        # Show path to destination
        if demo_train.current_path:
            print("\nPath tiles:")
            head_tile_pos = (
                demo_train.position.head_tile if demo_train.position else None
            )
            for i, (x, y, _) in enumerate(demo_train.current_path):
                marker = (
                    " <-- HEAD" if head_tile_pos and (x, y) == head_tile_pos else ""
                )
                print(f"  [{i}] ({x}, {y}){marker}")

    print("\n" + "=" * 80)
    print("Starting simulation...")
    print("=" * 80 + "\n")

    # Run simulation until train stops in tile (9,4) or max ticks
    max_ticks = 5000
    tick_count = 0

    while tick_count < max_ticks:
        sim.tick()
        tick_count += 1

        # Check if train has stopped in target tile
        if demo_train and demo_train.position:
            if (
                demo_train.position.head_tile == (9, 4)
                and demo_train.current_speed <= STOPPED_SPEED_THRESHOLD_KMH
            ):
                print(f"\n{'=' * 80}")
                print(f"SIMULATION COMPLETE - Train stopped in tile (9,4)")
                print(f"{'=' * 80}")
                print(f"Total ticks: {tick_count}")
                print(f"Final position: {demo_train.position.head_distance_cm:.2f} cm")
                print(f"Expected position: 16.67 cm (tile_length - 5cm safety margin)")
                print(
                    f"Position error: {16.67 - demo_train.position.head_distance_cm:.2f} cm"
                )
                break

        # Safety check - stop if train stops elsewhere
        if demo_train and demo_train.current_speed <= STOPPED_SPEED_THRESHOLD_KMH:
            if demo_train.position and demo_train.position.head_tile != (9, 4):
                print(
                    f"\n*** WARNING: Train stopped in wrong tile: {demo_train.position.head_tile}"
                )
                print(f"*** Expected to stop in (9,4)")
                break
    else:
        print(f"\n*** SIMULATION TIMEOUT after {max_ticks} ticks ***")

    return sim


if __name__ == "__main__":
    sim = run_debug_simulation()
