"""
Step Recorder for ATCS6 Railway Simulation

Generates step-based layout files from network state, similar to STEP files in CAD.
This format records the construction steps used to build a layout, making it:
- Future-proof: Resilient to API/parameter changes
- Self-testing: If replaying steps produces a different result, it indicates a regression
- Human-readable: Easy to understand and manually edit
"""

from typing import Dict, List, Optional

from track import Tile


class StepRecorder:
    """Generates step-based layout files from network state."""

    SCHEMA_VERSION = "1.0"

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
        creation sees switch-aware tiles).
        """
        steps: List[dict] = []
        step_id = 1
        for category in (
            self._generate_track_steps,
            self._generate_switch_steps,
            self._generate_block_steps,
        ):
            for step in category(network):
                step["id"] = step_id
                steps.append(step)
                step_id += 1
        return steps

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
        tracks_by_block: Dict[Optional[str], List[Tile]] = {}

        for pos, track in network.tracks.items():
            block_id = track.block_id
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

        A switch Tile is laid down by `add_track`, but the
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
        """Generate block creation steps."""
        steps = []

        for block_id, block in sorted(network.blocks.items()):
            # Find all tracks belonging to this block
            positions = []
            for pos, track in network.tracks.items():
                if track.block_id == block_id:
                    positions.append([track.x, track.y])

            # Sort positions for consistent ordering
            positions.sort()

            # `name` is the segment's — the replay op creates the geometry and
            # names it. The block's own name rides in `block_name`, recorded
            # only when it carries one, since the two are separate records.
            op = {
                "op": "create_block_with_tracks",
                "block_id": block_id,
                "positions": positions,
                "name": block.segments[0].name if block.segments else "",
            }
            if block.name:
                op["block_name"] = block.name

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
