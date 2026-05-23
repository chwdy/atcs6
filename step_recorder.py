"""
Step Recorder for ATCS6 Railway Simulation

Generates step-based layout files from network state, similar to STEP files in CAD.
This format records the construction steps used to build a layout, making it:
- Future-proof: Resilient to API/parameter changes
- Self-testing: If replaying steps produces a different result, it indicates a regression
- Human-readable: Easy to understand and manually edit
"""

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from track import TrackSegment


class StepRecorder:
    """Generates step-based layout files from network state."""

    SCHEMA_VERSION = "1.0"

    def generate_steps(
        self, network: "RailwayNetwork", name: str = "", description: str = ""
    ) -> dict:
        """
        Analyze network and generate ordered construction steps.

        Args:
            network: The RailwayNetwork instance to analyze.
            name: Name for the layout.
            description: Description of the layout.

        Returns:
            A dictionary containing the step-based layout format.
        """
        steps = self._collect_all_steps(network)

        # Build the full document
        now = datetime.now().isoformat()
        result = {
            "version": self.SCHEMA_VERSION,
            "format": "steps",
            "name": name,
            "description": description,
            "created": now,
            "modified": now,
            "steps": steps,
            "verification": self._generate_verification(network),
        }

        return result

    def generate_steps_list(self, network: "RailwayNetwork") -> List[dict]:
        """
        Generate just the steps list without the full document wrapper.
        Used by save_merged_layout() for embedding in the main file.
        """
        return self._collect_all_steps(network)

    def _collect_all_steps(self, network: "RailwayNetwork") -> List[dict]:
        """Run every per-category generator in canonical order and number them.

        Order matters: tracks must come before switches (network.add_switch
        looks up the underlying tile), switches before blocks (so block
        creation sees switch-aware tiles), and platforms before trains
        (trains may reference platform-owned blocks). Station-owned blocks
        are created by `add_platform` itself and are skipped in
        `_generate_block_steps`.
        """
        steps: List[dict] = []
        step_id = 1
        for category in (
            self._generate_track_steps,
            self._generate_switch_steps,
            self._generate_block_steps,
            self._generate_signal_steps,
            self._generate_platform_steps,
            self._generate_train_steps,
        ):
            for step in category(network):
                step["id"] = step_id
                steps.append(step)
                step_id += 1
        return steps

    @staticmethod
    def _platform_owned_block_ids(network: "RailwayNetwork") -> Set[str]:
        """Block IDs that `add_platform` creates on its own.

        `StationRegistry.add_station` (the implementation behind
        `network.add_platform`) creates a block named
        ``f"{platform_id}_BLOCK"`` and assigns the platform's tile to it.
        The recorder must skip those blocks in `_generate_block_steps`
        otherwise replay double-creates them and `create_block_with_tracks`
        fails with "Block ... already exists".
        """
        return {f"{platform_id}_BLOCK" for platform_id in network.platforms.keys()}

    def _generate_track_steps(self, network: "RailwayNetwork") -> List[dict]:
        """
        Generate track creation steps, grouped by block assignment.

        Strategy:
        - Tracks assigned to blocks: group by block_id
        - Unassigned tracks (switches, boundaries): group as "unassigned"
        - Switches are processed first to ensure proper topology
        """
        steps = []

        # Separate tracks by block assignment
        tracks_by_block: Dict[Optional[str], List[TrackSegment]] = {}

        for pos, track in network.tracks.items():
            block_id = track.block_section_id
            if block_id not in tracks_by_block:
                tracks_by_block[block_id] = []
            tracks_by_block[block_id].append(track)

        # First, handle unassigned tracks (switches and boundaries)
        unassigned = tracks_by_block.pop(None, [])
        if unassigned:
            # Separate switches from regular unassigned tracks
            switches = [t for t in unassigned if t.is_switch]
            non_switches = [t for t in unassigned if not t.is_switch]

            if switches:
                operations = []
                for track in sorted(switches, key=lambda t: (t.x, t.y)):
                    op = {
                        "op": "add_track",
                        "x": track.x,
                        "y": track.y,
                        "track_bits": track.track_bits,
                    }
                    if track.switch_state != 0:
                        op["switch_state"] = track.switch_state
                    operations.append(op)

                steps.append(
                    {
                        "type": "track_group",
                        "description": "Switches and junctions",
                        "operations": operations,
                    }
                )

            if non_switches:
                operations = []
                for track in sorted(non_switches, key=lambda t: (t.x, t.y)):
                    operations.append(
                        {
                            "op": "add_track",
                            "x": track.x,
                            "y": track.y,
                            "track_bits": track.track_bits,
                        }
                    )

                steps.append(
                    {
                        "type": "track_group",
                        "description": "Unassigned tracks",
                        "operations": operations,
                    }
                )

        # Then, handle tracks by block (but DON'T create tracks here - they're created with blocks)
        # We just record them for reference, the actual creation happens in block steps
        for block_id, tracks in sorted(tracks_by_block.items()):
            block = network.blocks.get(block_id)
            block_name = block.name if block else block_id

            operations = []
            for track in sorted(tracks, key=lambda t: (t.x, t.y)):
                operations.append(
                    {
                        "op": "add_track",
                        "x": track.x,
                        "y": track.y,
                        "track_bits": track.track_bits,
                    }
                )

            steps.append(
                {
                    "type": "track_group",
                    "description": f"Tracks for {block_name}",
                    "block_id": block_id,
                    "operations": operations,
                }
            )

        return steps

    def _generate_switch_steps(self, network: "RailwayNetwork") -> List[dict]:
        """Generate Switch-entity creation steps.

        A switch TrackSegment is laid down by `add_track`, but the
        corresponding `Switch` entity (carrying lengths, radii, type, name)
        must be registered separately via `network.add_switch(...)`. Without
        this step, switches survive snapshot round-trip but vanish on the
        steps-fallback recovery path.
        """
        steps = []
        for switch_id, switch in sorted(network.switches.items()):
            op = {
                "op": "add_switch",
                "switch_id": switch.switch_id,
                "x": switch.x,
                "y": switch.y,
                "main_length_cm": switch.main_length_cm,
                "diverge_length_cm": switch.diverge_length_cm,
                "switch_type": switch.switch_type,
            }
            if switch.main_radius_cm is not None:
                op["main_radius_cm"] = switch.main_radius_cm
            if switch.diverge_radius_cm is not None:
                op["diverge_radius_cm"] = switch.diverge_radius_cm
            if switch.name:
                op["name"] = switch.name
            if switch.notes:
                op["notes"] = switch.notes
            if switch.disabled:
                op["disabled"] = True

            steps.append(
                {
                    "type": "switch",
                    "description": f"Switch: {switch.name or switch_id}",
                    "operations": [op],
                }
            )
        return steps

    def _generate_block_steps(self, network: "RailwayNetwork") -> List[dict]:
        """Generate block creation steps.

        Skips platform-owned blocks — `add_platform` creates those itself
        (see `_platform_owned_block_ids`).
        """
        steps = []
        skip_ids = self._platform_owned_block_ids(network)

        for block_id, block in sorted(network.blocks.items()):
            if block_id in skip_ids:
                continue

            # Find all tracks belonging to this block
            positions = []
            for pos, track in network.tracks.items():
                if track.block_section_id == block_id:
                    positions.append([track.x, track.y])

            # Sort positions for consistent ordering
            positions.sort()

            op = {
                "op": "create_block_with_tracks",
                "block_id": block_id,
                "positions": positions,
                "name": block.name,
            }

            if block.length_cm > 0:
                op["length_cm"] = block.length_cm
            if block.speed_limit_kmh is not None:
                op["speed_limit_kmh"] = block.speed_limit_kmh

            steps.append(
                {
                    "type": "block",
                    "description": f"Block: {block.name or block_id}",
                    "operations": [op],
                }
            )

        return steps

    def _generate_signal_steps(self, network: "RailwayNetwork") -> List[dict]:
        """Generate signal creation steps."""
        steps = []

        for signal_id, signal in sorted(network.signals.items()):
            # Handle both enum and string values for direction/type
            detection_dir = signal.detection_direction
            if detection_dir is None:
                detection_dir = "east"
            elif hasattr(detection_dir, "name"):
                detection_dir = detection_dir.name.lower()
            else:
                detection_dir = str(detection_dir).lower()

            signal_type = signal.signal_type
            if signal_type is None:
                signal_type = "block"
            elif hasattr(signal_type, "name"):
                signal_type = signal_type.name.lower()
            else:
                signal_type = str(signal_type).lower()

            op = {
                "op": "add_signal",
                "x": signal.position[0],
                "y": signal.position[1],
                "detection_direction": detection_dir,
                "signal_type": signal_type,
            }

            if signal.name:
                op["name"] = signal.name
            if signal.monitored_block_id:
                op["monitored_block_id"] = signal.monitored_block_id

            steps.append(
                {
                    "type": "signal",
                    "description": f"Signal: {signal.name or signal_id}",
                    "operations": [op],
                }
            )

        return steps

    def _generate_platform_steps(self, network: "RailwayNetwork") -> List[dict]:
        """Generate platform creation steps."""
        steps = []

        for platform_id, platform in sorted(network.platforms.items()):
            if not platform.positions:
                continue

            # First position is the base, rest are extensions
            sorted_positions = sorted(platform.positions)
            base_pos = sorted_positions[0]
            extension_positions = sorted_positions[1:]

            operations = []

            # Base platform creation
            base_op = {
                "op": "add_platform",
                "x": base_pos[0],
                "y": base_pos[1],
                "name": platform.name,
                "platform_id": platform.platform_id,
            }

            if platform.length_cm > 0:
                base_op["length_cm"] = platform.length_cm
            if platform.platform_side:
                base_op["platform_side"] = platform.platform_side.value
            if platform.top_speed_kmh is not None:
                base_op["top_speed_kmh"] = platform.top_speed_kmh

            operations.append(base_op)

            # Extension operations
            if extension_positions:
                operations.append(
                    {
                        "op": "extend_platform",
                        "platform_id": platform.platform_id,
                        "positions": [[p[0], p[1]] for p in extension_positions],
                    }
                )

            steps.append(
                {
                    "type": "platform",
                    "description": f"Platform: {platform.name or platform_id}",
                    "operations": operations,
                }
            )

        return steps

    def _generate_train_steps(self, network: "RailwayNetwork") -> List[dict]:
        """Generate train creation steps (config only).

        Per design_new.md, Train carries only persistent/config state — live
        position/span comes from `SimulationController.add_train(...)` at
        run time. Steps therefore record the same fields Train.to_dict
        writes and never emit a `position` field.
        """
        steps = []

        for train_id, train in sorted(network.trains.items()):
            op = {
                "op": "add_train",
                "train_id": train.train_id,
                "max_speed": train.max_speed,
                "num_cars": train.num_cars,
            }
            if train.model:
                op["model"] = train.model
            if train.car_number:
                op["car_number"] = train.car_number
            if train.min_radius_cm is not None:
                op["min_radius_cm"] = train.min_radius_cm
            if train.speed_profile_id is not None:
                op["speed_profile_id"] = train.speed_profile_id

            steps.append(
                {
                    "type": "train",
                    "description": f"Train: {train_id}",
                    "operations": [op],
                }
            )

        return steps

    def _generate_verification(self, network: "RailwayNetwork") -> dict:
        """
        Generate verification data for detecting replay mismatches.

        Includes counts and a checksum of the network state.
        """
        # Collect key data for checksum
        data_parts = []

        # Track positions and bits
        for pos in sorted(network.tracks.keys()):
            track = network.tracks[pos]
            data_parts.append(f"T:{pos[0]},{pos[1]}:{track.track_bits}")

        # Block IDs
        for block_id in sorted(network.blocks.keys()):
            data_parts.append(f"B:{block_id}")

        # Signal positions
        for signal_id in sorted(network.signals.keys()):
            signal = network.signals[signal_id]
            data_parts.append(f"S:{signal.position[0]},{signal.position[1]}")

        # Platform IDs
        for platform_id in sorted(network.platforms.keys()):
            data_parts.append(f"PL:{platform_id}")

        # Compute checksum
        data_string = "|".join(data_parts)
        checksum = hashlib.sha256(data_string.encode()).hexdigest()[:16]

        return {
            "track_count": len(network.tracks),
            "block_count": len(network.blocks),
            "signal_count": len(network.signals),
            "platform_count": len(network.platforms),
            "train_count": len(network.trains),
            "checksum": f"sha256:{checksum}",
        }
