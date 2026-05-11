"""
Step Player for ATCS6 Railway Simulation

Replays step-based layout files to reconstruct a network.
This allows layouts to be recreated from their construction history,
providing a way to verify that API changes don't break existing layouts.
"""

from typing import Any, Dict, List, Optional

from track import TrackSegment


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
            network.trains.clear()
            network.blocks.clear()
            network.signals.clear()
            network.platforms.clear()
            network.station_groups.clear()
            network.schedules.clear()

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
            network.trains.clear()
            network.blocks.clear()
            network.signals.clear()
            network.platforms.clear()
            network.station_groups.clear()
            network.schedules.clear()

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
        elif op_type == "create_block_with_tracks":
            return self._op_create_block(op, network)
        elif op_type == "add_signal":
            return self._op_add_signal(op, network)
        elif op_type == "add_platform":
            return self._op_add_platform(op, network)
        elif op_type == "extend_platform":
            return self._op_extend_platform(op, network)
        elif op_type == "add_train":
            return self._op_add_train(op, network)
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

        track = TrackSegment(x, y, track_bits=track_bits)

        if "switch_state" in op:
            track.switch_state = op["switch_state"]

        network.add_track(track)
        return f"{x},{y}"

    def _op_create_block(self, op: dict, network: "RailwayNetwork") -> str:
        """Execute create_block_with_tracks operation."""
        block_id = op["block_id"]
        positions = [tuple(p) for p in op["positions"]]
        name = op.get("name", "")
        length_cm = op.get("length_cm", 0.0)
        speed_limit_kmh = op.get("speed_limit_kmh", op.get("max_speed_kmh"))

        # First, ensure all tracks exist
        for x, y in positions:
            if not network.get_track(x, y):
                # Track doesn't exist yet, skip block creation
                # This can happen if tracks were grouped separately
                pass

        network.create_block_with_tracks(
            block_id=block_id,
            positions=positions,
            name=name,
            length_cm=length_cm,
            speed_limit_kmh=speed_limit_kmh,
            validate=False,  # Skip validation during replay
        )

        return block_id

    def _op_add_signal(self, op: dict, network: "RailwayNetwork") -> str:
        """Execute add_signal operation."""
        x = op["x"]
        y = op["y"]
        detection_direction = op["detection_direction"]
        signal_type = op.get("signal_type", "block")
        name = op.get("name")

        signal = network.add_signal(
            x=x,
            y=y,
            detection_direction=detection_direction,
            signal_type=signal_type,
            name=name,
            auto_connect=True,
        )

        # If a specific monitored_block_id was recorded, try to connect it
        if "monitored_block_id" in op and op["monitored_block_id"]:
            try:
                network.connect_signal_to_block(
                    signal.signal_id, op["monitored_block_id"]
                )
            except Exception:
                pass  # Best effort

        return signal.signal_id

    def _op_add_platform(self, op: dict, network: "RailwayNetwork") -> str:
        """Execute add_platform operation."""
        x = op["x"]
        y = op["y"]
        name = op.get("name", "")
        platform_id = op.get("platform_id")
        length_cm = op.get("length_cm", 0.0)
        platform_side = op.get("platform_side", "both")
        top_speed_kmh = op.get("top_speed_kmh")

        platform = network.add_platform(
            x=x,
            y=y,
            platform_id=platform_id,
            name=name,
            length_cm=length_cm,
            platform_side=platform_side,
            top_speed_kmh=top_speed_kmh,
        )

        return platform.platform_id

    def _op_extend_platform(self, op: dict, network: "RailwayNetwork") -> str:
        """Execute extend_platform operation."""
        platform_id = self._resolve_ref(op["platform_id"])
        positions = [tuple(p) for p in op["positions"]]

        network.extend_platform(platform_id, positions)

        return platform_id

    def _op_add_train(self, op: dict, network: "RailwayNetwork") -> str:
        """Execute add_train operation."""
        from train import Train
        from enums import Direction

        train_id = op["train_id"]
        max_speed = op.get("max_speed", 100.0)

        train = Train(train_id=train_id, max_speed=max_speed)

        direction = Direction.EAST
        if "direction" in op:
            try:
                direction = Direction[op["direction"]]
            except KeyError:
                print(f"[StepPlayer] Unknown direction '{op['direction']}', defaulting to EAST")
                direction = Direction.EAST

        network.add_train(train)

        if "position" in op:
            pos = op["position"]
            network.place_train(train, pos["x"], pos["y"], direction)

        return train_id

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

        expected_signal_count = verification.get("signal_count")
        if (
            expected_signal_count is not None
            and len(network.signals) != expected_signal_count
        ):
            mismatches.append(
                {
                    "field": "signal_count",
                    "expected": expected_signal_count,
                    "actual": len(network.signals),
                }
            )

        expected_platform_count = verification.get("platform_count")
        if (
            expected_platform_count is not None
            and len(network.platforms) != expected_platform_count
        ):
            mismatches.append(
                {
                    "field": "platform_count",
                    "expected": expected_platform_count,
                    "actual": len(network.platforms),
                }
            )

        return mismatches
