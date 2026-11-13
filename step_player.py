"""
Step Player for ATCS6 Railway Simulation

Replays step-based layout files to reconstruct a network.
This allows layouts to be recreated from their construction history,
providing a way to verify that API changes don't break existing layouts.
"""

from typing import Any, Dict, List

from entity_ids import segment_id_for_block
from track import Tile


class StepPlayer:
    """Replays step files to reconstruct a network."""

    def __init__(self):
        """Initialize the StepPlayer."""
        self.context: Dict[str, Any] = {}  # For $prev references

    def play(
        self, step_data: dict, network: "RailwayNetwork", clear_first: bool = True
    ) -> dict:
        """
        Execute all steps on the network.

        Args:
            step_data: The step-based layout dictionary.
            network: The RailwayNetwork instance to populate.
            clear_first: If True, clears the network before playing.

        Returns:
            A dict with replay results including any verification mismatches.
        """
        if clear_first:
            network.tracks.clear()
            network.blocks.clear()

        steps = step_data.get("steps", [])
        errors = []
        self.context = {}

        for step in steps:
            try:
                self.play_step(step, network)
            except Exception as e:
                errors.append(
                    {
                        "step_id": step.get("id"),
                        "step_type": step.get("type"),
                        "error": str(e),
                    }
                )

        # Verify result if verification data is present
        verification = step_data.get("verification", {})
        mismatches = self._verify_result(network, verification)

        return {
            "success": len(errors) == 0 and len(mismatches) == 0,
            "steps_played": len(steps),
            "errors": errors,
            "verification_mismatches": mismatches,
        }

    def play_steps(
        self, steps: List[dict], network: "RailwayNetwork", clear_first: bool = True
    ) -> dict:
        """
        Execute a list of steps directly (without full document wrapper).
        Used for recovery when loading from merged format.

        Args:
            steps: List of step dictionaries.
            network: The RailwayNetwork instance to populate.
            clear_first: If True, clears the network before playing.

        Returns:
            A dict with replay results.
        """
        if clear_first:
            network.tracks.clear()
            network.blocks.clear()

        errors = []
        self.context = {}

        for step in steps:
            try:
                self.play_step(step, network)
            except Exception as e:
                errors.append(
                    {
                        "step_id": step.get("id"),
                        "step_type": step.get("type"),
                        "error": str(e),
                    }
                )

        return {
            "success": len(errors) == 0,
            "steps_played": len(steps),
            "errors": errors,
        }

    def play_step(self, step: dict, network: "RailwayNetwork") -> None:
        """
        Execute a single step.

        Args:
            step: The step dictionary containing operations.
            network: The RailwayNetwork instance.
        """
        operations = step.get("operations", [])

        for op in operations:
            result = self._execute_operation(op, network)

            # Store result for $prev reference
            if result is not None:
                self.context["$prev"] = result

    def _execute_operation(self, op: dict, network: "RailwayNetwork") -> Any:
        """
        Execute a single operation.

        Args:
            op: The operation dictionary.
            network: The RailwayNetwork instance.

        Returns:
            The result of the operation (e.g., created object ID).
        """
        op_type = op.get("op")

        if op_type == "add_track":
            return self._op_add_track(op, network)
        elif op_type == "add_switch":
            return self._op_add_switch(op, network)
        elif op_type == "create_block_with_tracks":
            return self._op_create_block(op, network)
        elif op_type in ("add_train", "add_signal", "add_platform", "extend_platform"):
            # v2 saves no trains, signals or platforms (design_v2.md "Trains",
            # "Out of scope"); an older file's step for one is skipped, as the
            # loader skips any field it does not know.
            return None
        else:
            raise ValueError(f"Unknown operation type: {op_type}")

    def _resolve_ref(self, value: Any) -> Any:
        """Resolve $prev and other references."""
        if isinstance(value, str) and value.startswith("$"):
            ref_name = value
            if ref_name in self.context:
                return self.context[ref_name]
            raise ValueError(f"Unresolved reference: {ref_name}")
        return value

    def _op_add_track(self, op: dict, network: "RailwayNetwork") -> str:
        """Execute add_track operation."""
        x = op["x"]
        y = op["y"]
        track_bits = op["track_bits"]

        track = Tile(x, y, track_bits=track_bits)
        network.add_track(track)
        return f"{x},{y}"

    def _op_add_switch(self, op: dict, network: "RailwayNetwork") -> str:
        """Execute add_switch operation.

        Registers a `Switch` entity at an already-placed switch tile. The
        underlying `Tile` must have been created by an earlier
        `add_track` step (the recorder orders tracks before switches).
        """
        from switch import Switch

        switch = Switch(
            switch_id=op["switch_id"],
            x=op["x"],
            y=op["y"],
            main_length_cm=op.get("main_length_cm", 0.0),
            diverge_length_cm=op.get("diverge_length_cm", 0.0),
            main_radius_cm=op.get("main_radius_cm"),
            diverge_radius_cm=op.get("diverge_radius_cm"),
            switch_type=op.get("switch_type", "standard"),
            name=op.get("name", ""),
            notes=op.get("notes", ""),
        )
        switch.disabled = bool(op.get("disabled", False))
        network.add_switch(switch)
        return switch.switch_id

    def _op_create_block(self, op: dict, network: "RailwayNetwork") -> str:
        """Execute create_segment_with_tracks operation.

        The op names a BLOCK — `block_id` is the id a recording's later steps
        and its checkpoint assertions read back — so the block id is pinned and
        the member segment takes the matching geometry-axis id (`entity_ids`).

        `name` names the segment the call creates; the optional `block_name`
        names the block that owns it. They are separate records with separate
        names, so a recording carries both (see `Block.__init__`).
        """
        block_id = op["block_id"]
        positions = [tuple(p) for p in op["positions"]]
        name = op.get("name", "")
        block_name = op.get("block_name", "")
        length_cm = op.get("length_cm", 0.0)
        speed_limit_kmh = op.get("speed_limit_kmh", op.get("max_speed_kmh"))

        # First, ensure all tracks exist
        for x, y in positions:
            if not network.get_track(x, y):
                # Track doesn't exist yet, skip block creation
                # This can happen if tracks were grouped separately
                pass

        block = network.create_segment_with_tracks(
            segment_id=segment_id_for_block(block_id),
            block_id=block_id,
            positions=positions,
            name=name,
            length_cm=length_cm,
            speed_limit_kmh=speed_limit_kmh,
            validate=False,  # Skip validation during replay
        )
        if block_name:
            block.name = block_name

        return block_id

    def _verify_result(
        self, network: "RailwayNetwork", verification: dict
    ) -> List[dict]:
        """
        Verify that the replayed network matches expected verification data.

        Args:
            network: The network after replay.
            verification: The expected verification data.

        Returns:
            List of mismatches (empty if verification passed).
        """
        if not verification:
            return []

        mismatches = []

        expected_track_count = verification.get("track_count")
        if (
            expected_track_count is not None
            and len(network.tracks) != expected_track_count
        ):
            mismatches.append(
                {
                    "field": "track_count",
                    "expected": expected_track_count,
                    "actual": len(network.tracks),
                }
            )

        expected_block_count = verification.get("block_count")
        if (
            expected_block_count is not None
            and len(network.blocks) != expected_block_count
        ):
            mismatches.append(
                {
                    "field": "block_count",
                    "expected": expected_block_count,
                    "actual": len(network.blocks),
                }
            )

        return mismatches
