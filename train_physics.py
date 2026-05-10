"""
Train Physics - Physical Movement, Block Management, and Voltage Output

Definition:
This module provides the TrainPhysics class which handles the physical execution
of train movement. While TrainController decides WHAT speed the train should have,
TrainPhysics executes HOW the train moves.

Responsibilities:
- Convert handle position to acceleration/deceleration rates
- Update train speed based on acceleration and time delta
- Move train position along the track (head and tail)
- Track tile-level traversal frames per crossed resource boundary
- Output voltage to BlockPowerController

Architecture:
    SimulationController (Orchestrator / Clock)
            |
            v
    +-----------------+     +---------------+
    | TrainController |<----| TrainPhysics  |
    | (Control Logic) |     | (Movement)    |
    +-----------------+     +---------------+
            |                      |
            v                      v
        +-------+           +------------------+
        | Train |           |BlockPowerController|
        | (Data)|           +------------------+
        +-------+

Design Principles:
1. Pure Physics: Only handles movement calculations and world interactions
2. Reads from Controller: Gets handle_position, power state, current_speed_kmh
3. Writes to Controller: Movement results delivered via on_physics_updated
4. Writes to World: BlockPowerController voltage emission (hardware-output path)
5. No Decision Making: Doesn't decide speed limits or handle positions
"""

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from constants import STOPPED_SPEED_THRESHOLD_KMH
from enums import (
    BRAKE_MULTIPLIERS,
    POWER_MULTIPLIERS,
    BlockState,
    Direction,
    HandlePosition,
    LeadingEnd,
    TrainState,
)
from model_scale import kmh_to_cm_per_sec
from runtime_payloads import PhysicsSpanUpdate, TileTraversal
from train_span import TrainSpan

if TYPE_CHECKING:
    from block_manager import BlockManager
    from block_power import BlockPowerController
    from network import RailwayNetwork
    from speed_profile import SpeedProfile
    from train import Train
    from train_controller import TrainController


logger = logging.getLogger(__name__)

# Edge opposites for track geometry
OPPOSITE_EDGES = {"N": "S", "S": "N", "E": "W", "W": "E"}


@dataclass
class TrainPhysics:
    """
    Physical movement, block management, and voltage output for one train.

    TrainPhysics executes the physical simulation:
    - Converts handle position to acceleration/deceleration
    - Updates train speed and position
    - Manages block occupancy (occupy/release)
    - Outputs voltage to BlockPowerController

    It does NOT decide:
    - Speed limits (that's TrainController/ATC)
    - Handle position (that's TrainController/ATO)
    - When to start/stop (that's TrainController)

    Attributes:
        train_id: ID of the train this physics instance controls
        network: Reference to the network for track/block access
        controller: Reference to TrainController for reading state
    """

    train_id: str
    network: Optional["RailwayNetwork"] = None
    controller: Optional["TrainController"] = None

    # Track last occupied blocks for delta calculations
    _last_occupied_blocks: Set[str] = field(default_factory=set)
    _caution_blocks: Set[str] = field(default_factory=set)
    last_block_transitions: Dict[str, Set[str]] = field(
        default_factory=lambda: {"entered": set(), "vacated": set()}
    )
    # Tick counter for periodic sync validation
    _tick_count: int = 0
    _sync_validation_interval: int = 10  # Validate every N ticks
    # Set transiently inside _advance_end when the train hits the end of track.
    # The tick body inspects this flag, applies the stop to new_speed, and then
    # delivers it to TrainController.on_physics_updated(...). The flag is reset
    # at the start of every movement step.
    _force_stop: bool = False

    # =========================================================================
    # Internal Helpers
    # =========================================================================

    def _get_train(self) -> Optional["Train"]:
        """Get the Train entity this physics instance controls."""
        if self.network is None:
            return None
        return self.network.trains.get(self.train_id)

    def _get_path_tiles(self) -> Optional[List[Tuple[int, int, "Direction"]]]:
        """Get path tiles from controller (reads from dispatcher)."""
        if self.controller:
            return self.controller.get_path_tiles()
        return None

    def _get_speed_profile(self) -> "SpeedProfile":
        """Get the SpeedProfile for this train."""
        from speed_profile import DEFAULT_SPEED_PROFILE

        train = self._get_train()
        if not train or not self.network:
            return DEFAULT_SPEED_PROFILE

        profile_id = train.speed_profile_id
        if profile_id and hasattr(self.network, "speed_profiles"):
            profile = self.network.speed_profiles.get(profile_id)
            if profile:
                return profile

        return DEFAULT_SPEED_PROFILE

    def _get_block_manager(self) -> Optional["BlockManager"]:
        """Get the BlockManager from network."""
        if self.network and hasattr(self.network, "block_manager"):
            return self.network.block_manager
        return None

    def _get_power_controller(self) -> Optional["BlockPowerController"]:
        """Get the BlockPowerController from network."""
        if self.network and hasattr(self.network, "power_controller"):
            return self.network.power_controller
        return None

    # =========================================================================
    # Tick Method (Called Each Simulation Tick)
    # =========================================================================

    def tick(self, delta: float) -> None:
        """
        Called each simulation tick by SimulationController.

        Executes physical movement and emits one ordered PhysicsSpanUpdate per
        crossed resource boundary plus one final no-boundary callback every tick
        through TrainController.on_physics_updated(event_time, span_update).

        The voltage emission to BlockPowerController is preserved as the
        software-to-hardware output layer.

        Args:
            delta: Time elapsed since last tick (seconds)
        """
        train = self._get_train()
        if not train:
            return

        controller = self.controller
        if not controller:
            return

        event_time = time.time()

        # 1. Skip movement if powered off, but still set voltage to 0 and emit
        # the canonical no-boundary callback so controller-owned span/speed
        # storage stays canonical even on OFF ticks.
        if not controller.power_on:
            self._set_voltage_for_blocks(train, 0.0)
            current_speed_kmh = self._read_current_speed(train)
            self._emit_final_no_boundary_callback(
                controller, train, event_time, current_speed_kmh
            )
            return

        # 2. Capture pre-move state for tile-delta computation. The previous
        # occupied tile list and the pre-move tail/head locator drive the
        # added_tiles / removed_tiles delta the controller consumes.
        prev_occupied_tiles = (
            list(train.position.occupied_tiles)
            if train.position and train.position.occupied_tiles
            else []
        )
        prev_tail_locator = self._get_tail_locator(train)

        # 3. Resolve speed for this tick. Read previous speed from controller-
        # owned storage when available (canonical source per design); fall back
        # to the train-side mirror only as a transitional safety net.
        profile = self._get_speed_profile()
        previous_speed = self._read_current_speed(train)

        # Manual power_percent mode: per design_new.md "Ownership split", the
        # controller passes raw percentage power to TrainPhysics, which
        # immediately converts it via SpeedProfile.power_speed_curve into the
        # calibrated current speed when no braking override is active. ATC and
        # EB braking force the percent contribution to 0 for the tick. Handle
        # mode (simulation) keeps the existing acceleration-integration path.
        if (
            controller.control_mode == "manual"
            and getattr(controller, "manual_mode_type", None) == "power_percent"
        ):
            new_speed = self._compute_power_percent_speed(
                controller, profile, previous_speed, delta
            )
        else:
            operational_decel = (
                profile.deceleration_rate * profile.operational_decel_ratio
            )
            acceleration = self._get_acceleration_for_handle(
                controller.handle_position,
                profile.acceleration_rate,
                operational_decel,
                profile.emergency_brake_rate,
            )

            new_speed = previous_speed + (acceleration * delta)
            new_speed = max(0.0, new_speed)

            if acceleration < 0 and new_speed <= STOPPED_SPEED_THRESHOLD_KMH:
                new_speed = 0.0

            if controller.handle_position == HandlePosition.N and previous_speed > 0:
                friction_decel = profile.friction_rate * delta
                new_speed = max(0.0, new_speed - friction_decel)

            if (
                profile.air_resistance_enabled
                and previous_speed > 0
                and profile.reference_speed_kmh > 0
            ):
                speed_ratio = previous_speed / profile.reference_speed_kmh
                drag_decel = profile.drag_coefficient * (speed_ratio**2) * delta
                new_speed = max(0.0, new_speed - drag_decel)

        logger.debug(
            "[TrainPhysics %s] speed update: new_speed=%.2f delta=%.2f speed_delta=%.2f",
            self.train_id,
            new_speed,
            delta,
            new_speed - previous_speed,
        )

        # 9. Calculate distance to move
        distance_cm = self._speed_to_distance(new_speed, delta)
        logger.debug("[TrainPhysics %s] distance_cm=%.2f", self.train_id, distance_cm)

        # 10. Move train position. _move_train / _advance_end may force a stop
        # when the train hits the end of track; that stop is captured in
        # self._force_stop and applied to new_speed before the callback.
        self._force_stop = False
        if distance_cm > 0:
            self._move_train(train, distance_cm)
            logger.debug(
                "[TrainPhysics %s] moved distance_cm=%.2f", self.train_id, distance_cm
            )

        if self._force_stop:
            new_speed = 0.0

        # 11. Update block occupancy. The dispatcher-mediated block release
        # path remains in place for in-flight v1 wiring; it will be lifted to
        # PathDispatcher.on_controller_progress(...) by Agent B. The line that
        # mirrors occupied_blocks back onto train.position has been removed —
        # the controller derives that via the just-delivered span_update.
        self._update_block_occupancy(train)
        logger.debug(
            "[TrainPhysics %s] occupied_blocks=%s caution_blocks=%s",
            self.train_id,
            sorted(self._get_blocks_for_train(train)),
            sorted(self._caution_blocks),
        )

        # 12. Build and emit per-boundary PhysicsSpanUpdate callbacks plus
        # one final no-boundary callback for this tick.
        new_occupied_tiles = (
            list(train.position.occupied_tiles)
            if train.position and train.position.occupied_tiles
            else []
        )
        added_traversals = self._build_added_tile_traversals(
            prev_occupied_tiles, new_occupied_tiles
        )
        removed_traversals = self._build_removed_tile_traversals(
            prev_occupied_tiles, new_occupied_tiles, prev_tail_locator
        )
        self._emit_span_updates(
            controller,
            train,
            event_time,
            new_speed,
            added_traversals,
            removed_traversals,
        )

        # 13. Periodic position-block sync diagnostic (no auto-correction now
        # that the controller owns occupied_blocks state via the span).
        self._tick_count += 1
        if self._tick_count >= self._sync_validation_interval:
            self._validate_position_sync(train)
            self._tick_count = 0

        # 14. Output voltage to occupied blocks (hardware-output path)
        self._update_voltage_output(train, controller)
        logger.debug("[TrainPhysics %s] voltage update complete", self.train_id)

    # =========================================================================
    # Speed and Acceleration
    # =========================================================================

    def _get_acceleration_for_handle(
        self,
        handle: HandlePosition,
        acceleration_rate: float,
        operational_decel_rate: float,
        emergency_brake_rate: float,
    ) -> float:
        """
        Get acceleration/deceleration rate for current handle position.

        Brake notches multiply `operational_decel_rate` (the rate ATO uses
        for its planned slow-down curve). B6 = 1.00× exactly tracks that
        curve; B7-B8 sit slightly above 1× for catch-up; B1-B5 sit below.
        EB bypasses the table and applies `emergency_brake_rate`.

        Args:
            handle: Current handle position
            acceleration_rate: Maximum acceleration rate (km/h/s)
            operational_decel_rate: Operational deceleration rate (km/h/s) =
                profile.deceleration_rate × profile.operational_decel_ratio
            emergency_brake_rate: Emergency braking rate (km/h/s)

        Returns:
            Positive value for acceleration (km/h/s)
            Negative value for deceleration (km/h/s)
            Zero for neutral (coast)
        """
        # Power notches (acceleration)
        if handle in POWER_MULTIPLIERS:
            return acceleration_rate * POWER_MULTIPLIERS[handle]

        # Neutral (coast)
        if handle == HandlePosition.N:
            return 0.0

        # Brake notches (deceleration relative to op_decel)
        if handle in BRAKE_MULTIPLIERS:
            return -operational_decel_rate * BRAKE_MULTIPLIERS[handle]

        # Emergency brake
        if handle == HandlePosition.EB:
            return -emergency_brake_rate

        return 0.0

    def _compute_power_percent_speed(
        self,
        controller: "TrainController",
        profile: "SpeedProfile",
        previous_speed: float,
        delta: float,
    ) -> float:
        """
        Compute the new speed for manual power_percent mode.

        Per design_new.md "Ownership split":
            user drives with percentage power; the controller passes raw
            percentage power to TrainPhysics, which immediately converts it
            via SpeedProfile.power_speed_curve into both track voltage and
            the calibrated current speed when no braking override is active.

        Per the implementation index manual-mode rule, "ATC/service/EB braking
        forces the percent to 0 for that tick." The controller publishes the
        live ATC ceiling at distance 0 in `controller.atc_speed_limit`.

        Behavior summary:
        - EB latched: emergency-brake decel from previous_speed.
        - ATC ceiling below previous speed (service brake required): force
          percent contribution to 0 and apply service decel from
          previous_speed.
        - Otherwise: convert percent through SpeedProfile.power_speed_curve
          and clamp at the ATC ceiling.
        """
        if controller.eb_active:
            new_speed = max(
                0.0, previous_speed - profile.emergency_brake_rate * delta
            )
            if new_speed <= STOPPED_SPEED_THRESHOLD_KMH:
                new_speed = 0.0
            return new_speed

        atc_limit_kmh = float(getattr(controller, "atc_speed_limit", 0.0) or 0.0)

        # ATC requires deceleration: percent treated as 0 for this tick.
        if previous_speed > atc_limit_kmh + 0.01:
            new_speed = max(
                0.0, previous_speed - profile.deceleration_rate * delta
            )
            if new_speed <= STOPPED_SPEED_THRESHOLD_KMH:
                new_speed = 0.0
            return new_speed

        # No braking override: convert percent through calibration curve.
        pct = float(getattr(controller, "manual_power_percent", 0.0) or 0.0)
        target_kmh = profile.power_percent_to_speed(pct)
        if atc_limit_kmh > 0 and target_kmh > atc_limit_kmh:
            target_kmh = atc_limit_kmh
        return max(0.0, target_kmh)

    def _speed_to_distance(self, speed_kmh: float, delta: float) -> float:
        """
        Convert speed (km/h) and time (seconds) to distance (cm).

        Uses kmh_to_cm_per_sec which applies model scale.

        Args:
            speed_kmh: Speed in km/h
            delta: Time in seconds

        Returns:
            Distance in cm
        """
        cm_per_second = kmh_to_cm_per_sec(speed_kmh)
        return cm_per_second * delta

    # =========================================================================
    # Span-update / boundary-callback helpers
    # =========================================================================

    def _read_current_speed(self, train: "Train") -> float:
        """Return the canonical current speed.

        Per design_new.md the controller owns ``current_speed_kmh``.
        """
        return float(getattr(self.controller, "current_speed_kmh", 0.0) or 0.0)

    def _get_tail_locator(
        self, train: "Train"
    ) -> Optional[Tuple[Tuple[int, int], str]]:
        """Return (tail_tile, tail_entry_edge) for the current train position."""
        if not train.position:
            return None
        if train.position.leading_end == LeadingEnd.A:
            return (train.position.end_b_tile, train.position.end_b_entry_edge)
        return (train.position.end_a_tile, train.position.end_a_entry_edge)

    def _walk_tile_frames(
        self,
        start_tile: Tuple[int, int],
        start_entry_edge: str,
        target_tiles: Set[Tuple[int, int]],
    ) -> List[TileTraversal]:
        """Walk tiles forward from ``start_tile`` (entered via ``start_entry_edge``)
        and emit one ``TileTraversal`` per visited tile, stopping after the walk
        has covered every tile in ``target_tiles``.

        The walk uses the same exit-edge resolution as ``_calculate_exit_edge``,
        which honors live track/switch geometry. Each emitted ``TileTraversal``
        carries the tile-level entry/exit edges plus best-effort
        ``resource_type`` / ``resource_id`` / ``block_id`` populated from the
        ``RailwayNetwork`` track index so the controller can derive resource
        events without re-walking the network.
        """
        frames: List[TileTraversal] = []
        if not target_tiles:
            return frames

        remaining = set(target_tiles)
        current_tile = start_tile
        current_entry = start_entry_edge

        max_iterations = 200
        for _ in range(max_iterations):
            exit_edge = self._calculate_exit_edge(current_tile, current_entry)
            if not exit_edge:
                # End of track / unresolvable exit; emit current tile with
                # entry but no clean exit, then stop.
                if current_tile in remaining:
                    frames.append(
                        self._make_tile_traversal(
                            current_tile, current_entry, current_entry
                        )
                    )
                    remaining.discard(current_tile)
                break

            if current_tile in remaining:
                frames.append(
                    self._make_tile_traversal(current_tile, current_entry, exit_edge)
                )
                remaining.discard(current_tile)
                if not remaining:
                    break

            next_tile = self._get_neighbor_tile(current_tile, exit_edge)
            if next_tile is None:
                break
            current_tile = next_tile
            current_entry = OPPOSITE_EDGES[exit_edge]

        return frames

    def _make_tile_traversal(
        self,
        tile: Tuple[int, int],
        entry_edge: str,
        exit_edge: str,
    ) -> TileTraversal:
        """Build a ``TileTraversal`` populated with resource metadata from the
        live network track index when available."""
        resource_type: Optional[str] = None
        resource_id: Optional[str] = None
        block_id: Optional[str] = None

        if self.network:
            track = self.network.get_track(*tile)
            if track is not None:
                # block_id always reflects the block this tile sits inside,
                # even when the primary resource for the tile is a switch.
                if track.block_section_id:
                    block_id = track.block_section_id
                if getattr(track, "is_switch", False) and track.switch_id:
                    resource_type = "switch"
                    resource_id = track.switch_id
                elif track.block_section_id:
                    resource_type = "block"
                    resource_id = track.block_section_id

        return TileTraversal(
            tile_xy=tile,
            entry_edge=entry_edge,
            exit_edge=exit_edge,
            resource_type=resource_type,  # type: ignore[arg-type]
            resource_id=resource_id,
            block_id=block_id,
        )

    def _build_added_tile_traversals(
        self,
        prev_occupied_tiles: List[Tuple[int, int]],
        new_occupied_tiles: List[Tuple[int, int]],
    ) -> List[TileTraversal]:
        """Compute per-tile traversal frames for tiles newly covered by the
        post-move span. Walk forward from the post-move tail through the new
        occupied tile chain so every added tile gets a directed entry/exit
        edge in physical traversal order."""
        prev_set = set(prev_occupied_tiles)
        new_set = set(new_occupied_tiles)
        added_set = new_set - prev_set
        if not added_set:
            return []

        train = self._get_train()
        if not train or not train.position:
            return []

        # Walk from the post-move tail forward; this covers every tile in the
        # new span, including the new head-side additions.
        tail_locator = self._get_tail_locator(train)
        if tail_locator is None:
            return []

        tail_tile, tail_entry = tail_locator
        return self._walk_tile_frames(tail_tile, tail_entry, added_set)

    def _build_removed_tile_traversals(
        self,
        prev_occupied_tiles: List[Tuple[int, int]],
        new_occupied_tiles: List[Tuple[int, int]],
        prev_tail_locator: Optional[Tuple[Tuple[int, int], str]],
    ) -> List[TileTraversal]:
        """Compute per-tile traversal frames for tiles vacated since the
        previous tick. Walk forward from the pre-move tail; the vacated tiles
        emerge in tail-vacate order (closest to old tail first)."""
        prev_set = set(prev_occupied_tiles)
        new_set = set(new_occupied_tiles)
        removed_set = prev_set - new_set
        if not removed_set:
            return []

        if prev_tail_locator is None:
            return []

        prev_tail_tile, prev_tail_entry = prev_tail_locator
        return self._walk_tile_frames(prev_tail_tile, prev_tail_entry, removed_set)

    def _group_traversals_by_resource(
        self, traversals: List[TileTraversal]
    ) -> List[List[TileTraversal]]:
        """Group consecutive same-resource tile traversals into one batch per
        resource boundary. Tiles without resolved resource metadata fall into
        their own single-tile groups so the controller can still account for
        them.
        """
        if not traversals:
            return []
        groups: List[List[TileTraversal]] = []
        current: List[TileTraversal] = []
        current_key: Optional[Tuple[Optional[str], Optional[str]]] = None
        for tile in traversals:
            key = (tile.resource_type, tile.resource_id)
            if current_key is None:
                current_key = key
                current = [tile]
                continue
            if key == current_key and key != (None, None):
                current.append(tile)
            else:
                groups.append(current)
                current = [tile]
                current_key = key
        if current:
            groups.append(current)
        return groups

    def _emit_span_updates(
        self,
        controller: "TrainController",
        train: "Train",
        event_time: float,
        new_speed: float,
        added_traversals: List[TileTraversal],
        removed_traversals: List[TileTraversal],
    ) -> None:
        """Emit one PhysicsSpanUpdate per resource boundary plus one final
        no-boundary callback for this tick.

        Per design_new.md "Physical progress / deviation procedure":
          - one ordered span_update per crossed resource boundary in physical
            traversal order
          - one final no-boundary callback every tick so Phase 3 always has a
            deterministic consume point

        The current span object passed as ``new_span`` is the post-move
        ``train.position`` snapshot; the controller owns the canonical span
        storage and uses ``span_update.new_span`` to refresh it.
        """
        new_span = (
            TrainSpan.from_train_position(train.position)
            if train.position is not None
            else None
        )

        added_groups = self._group_traversals_by_resource(added_traversals)
        removed_groups = self._group_traversals_by_resource(removed_traversals)

        # Per-boundary callbacks for entered resources first.
        for group in added_groups:
            update = PhysicsSpanUpdate(
                new_span=new_span,
                current_speed_kmh=new_speed,
                added_tiles=group,
                removed_tiles=[],
            )
            controller.on_physics_updated(event_time, update)

        # Per-boundary callbacks for tail-vacated resources next.
        for group in removed_groups:
            update = PhysicsSpanUpdate(
                new_span=new_span,
                current_speed_kmh=new_speed,
                added_tiles=[],
                removed_tiles=group,
            )
            controller.on_physics_updated(event_time, update)

        # Final no-boundary callback (always emitted).
        final_update = PhysicsSpanUpdate(
            new_span=new_span,
            current_speed_kmh=new_speed,
            added_tiles=[],
            removed_tiles=[],
        )
        controller.on_physics_updated(event_time, final_update)

    def _emit_final_no_boundary_callback(
        self,
        controller: "TrainController",
        train: "Train",
        event_time: float,
        current_speed_kmh: float,
    ) -> None:
        """Emit the final no-boundary PhysicsSpanUpdate for ticks where no
        movement occurred (powered-off ticks)."""
        new_span = (
            TrainSpan.from_train_position(train.position)
            if train.position is not None
            else None
        )
        update = PhysicsSpanUpdate(
            new_span=new_span,
            current_speed_kmh=current_speed_kmh,
            added_tiles=[],
            removed_tiles=[],
        )
        controller.on_physics_updated(event_time, update)

    # =========================================================================
    # Movement Logic
    # =========================================================================

    def _move_train(self, train: "Train", distance_cm: float) -> None:
        """
        Move train forward by distance_cm along the track.

        Updates both head and tail positions, handling tile transitions.

        Args:
            train: Train to move
            distance_cm: Distance to move in cm
        """
        if not train.position:
            logger.warning(
                "[TrainPhysics] Train %s missing TrainPosition; skipping move",
                train.train_id,
            )
            return

        position = train.position

        # Determine which end is head based on leading_end
        if position.leading_end == LeadingEnd.A:
            # end_a is head
            self._advance_end(
                train,
                position.end_a_tile,
                position.end_a_distance_cm,
                position.end_a_entry_edge,
                distance_cm,
                is_end_a=True,
            )
        else:
            # end_b is head
            self._advance_end(
                train,
                position.end_b_tile,
                position.end_b_distance_cm,
                position.end_b_entry_edge,
                distance_cm,
                is_end_a=False,
            )

        # Recalculate tail position based on train length
        self._update_tail_position(train)

        self._update_occupied_tiles(train)

    def _advance_end(
        self,
        train: "Train",
        current_tile: Tuple[int, int],
        current_distance: float,
        entry_edge: str,
        distance_cm: float,
        is_end_a: bool,
    ) -> None:
        """
        Advance one end of the train by distance_cm.

        Handles tile transitions when end moves past tile boundary.
        """
        position = train.position
        if not position:
            return

        new_distance = current_distance + distance_cm
        new_tile = current_tile
        new_entry_edge = entry_edge

        # Handle tile transition if distance exceeds tile length
        max_iterations = 100
        for _ in range(max_iterations):
            tile_length = self._get_tile_length_cm(new_tile)

            if tile_length <= 0:
                exit_edge = self._calculate_exit_edge(new_tile, new_entry_edge)
                if not exit_edge:
                    new_distance = 0.0
                    self._force_stop = True
                    break

                next_tile = self._get_neighbor_tile(new_tile, exit_edge)
                if next_tile is None:
                    new_distance = 0.0
                    self._force_stop = True
                    break

                new_tile = next_tile
                new_entry_edge = OPPOSITE_EDGES[exit_edge]
                continue

            if new_distance < tile_length:
                break

            new_distance -= tile_length

            # Get exit edge based on track geometry
            exit_edge = self._calculate_exit_edge(new_tile, new_entry_edge)
            if not exit_edge:
                # End of track - stop at tile boundary
                new_distance = tile_length
                self._force_stop = True
                break

            # Get next tile
            next_tile = self._get_neighbor_tile(new_tile, exit_edge)
            if next_tile is None:
                # End of track - stop at tile boundary
                new_distance = tile_length
                self._force_stop = True
                break

            # Move to next tile
            new_tile = next_tile
            new_entry_edge = OPPOSITE_EDGES[exit_edge]

        # Update position
        if is_end_a:
            position.end_a_tile = new_tile
            position.end_a_distance_cm = new_distance
            position.end_a_entry_edge = new_entry_edge
        else:
            position.end_b_tile = new_tile
            position.end_b_distance_cm = new_distance
            position.end_b_entry_edge = new_entry_edge

    def _update_tail_position(self, train: "Train") -> None:
        """
        Calculate tail position by tracing backward from head by train length.

        The tail is always train.length_cm behind the head along the track.
        """
        position = train.position
        if not position:
            return

        # Get head position
        if position.leading_end == LeadingEnd.A:
            head_tile = position.end_a_tile
            head_entry = position.end_a_entry_edge
            head_distance = position.end_a_distance_cm
        else:
            head_tile = position.end_b_tile
            head_entry = position.end_b_entry_edge
            head_distance = position.end_b_distance_cm

        # Calculate remaining length to trace backward
        remaining_length = train.length_cm

        # Subtract head's distance from entry (how far we are into this tile)
        remaining_length -= head_distance

        current_tile = head_tile
        current_entry = head_entry

        # If train fits in current tile
        if remaining_length <= 0:
            if position.leading_end == LeadingEnd.A:
                position.end_b_tile = current_tile
                position.end_b_entry_edge = current_entry
                position.end_b_distance_cm = head_distance - train.length_cm
            else:
                position.end_a_tile = current_tile
                position.end_a_entry_edge = current_entry
                position.end_a_distance_cm = head_distance - train.length_cm
            return

        # Trace backward through tiles
        while remaining_length > 0:
            # Go to previous tile (enter from entry edge direction)
            prev_tile = self._get_neighbor_tile(current_tile, current_entry)

            if prev_tile is None:
                # Start of track - tail is at entry edge of first tile
                break

            prev_track = self.network.get_track(*prev_tile) if self.network else None
            exit_edge = OPPOSITE_EDGES.get(current_entry, current_entry)
            entry_edge = None
            if prev_track:
                entry_edge = prev_track.get_entry_for_exit(
                    exit_edge, use_switch_state=True
                )

            # Move to previous tile
            current_tile = prev_tile
            if entry_edge:
                current_entry = entry_edge
            else:
                current_entry = exit_edge

            tile_length = self._get_tile_length_cm(current_tile)
            remaining_length -= tile_length

        # Tail is in this tile
        tail_distance = abs(remaining_length) if remaining_length < 0 else 0.0

        if position.leading_end == LeadingEnd.A:
            position.end_b_tile = current_tile
            position.end_b_entry_edge = current_entry
            position.end_b_distance_cm = tail_distance
        else:
            position.end_a_tile = current_tile
            position.end_a_entry_edge = current_entry
            position.end_a_distance_cm = tail_distance

    def _update_occupied_tiles(self, train: "Train") -> None:
        """
        Update the list of tiles occupied by the train.

        Traces from tail to head and records all tiles.
        """
        position = train.position
        if not position:
            return

        # Get head and tail tiles
        if position.leading_end == LeadingEnd.A:
            head_tile = position.end_a_tile
            tail_tile = position.end_b_tile
            head_entry = position.end_a_entry_edge
            tail_entry = position.end_b_entry_edge
        else:
            head_tile = position.end_b_tile
            tail_tile = position.end_a_tile
            head_entry = position.end_b_entry_edge
            tail_entry = position.end_a_entry_edge

        # Simple case: head and tail on same tile
        if head_tile == tail_tile:
            position.occupied_tiles = [head_tile]
            return

        # Trace from tail to head
        occupied = [tail_tile]
        current_tile = tail_tile
        current_entry = tail_entry

        max_iterations = 100  # Safety limit
        for _ in range(max_iterations):
            if current_tile == head_tile:
                break

            # Get exit edge and next tile
            exit_edge = self._calculate_exit_edge(current_tile, current_entry)
            if not exit_edge:
                break

            next_tile = self._get_neighbor_tile(current_tile, exit_edge)
            if not next_tile:
                break

            occupied.append(next_tile)
            current_tile = next_tile
            current_entry = OPPOSITE_EDGES[exit_edge]

        if position.leading_end == LeadingEnd.A:
            position.occupied_tiles = list(reversed(occupied))
        else:
            position.occupied_tiles = occupied

    # =========================================================================
    # Block Occupancy Management
    # =========================================================================

    def _update_block_occupancy(self, train: "Train") -> None:
        """
        Recompute which blocks this train occupies and record the per-tick
        delta. TrainPhysics does not write Block state — those writes flow
        through PathDispatcher.on_controller_progress(report) →
        BlockManager.occupy_blocks(...) / on_block_tail_vacated(...). This
        method only maintains local diagnostics:

        - last_block_transitions: entered/vacated sets, read by tests
        - _last_occupied_blocks: cached set used by _validate_position_sync
          and by serialize/restore round-trips
        """
        # Calculate current occupied blocks from position
        new_occupied = self._get_blocks_for_train(train)

        old_occupied = self._last_occupied_blocks
        newly_entered = new_occupied - old_occupied
        newly_vacated = old_occupied - new_occupied
        self.last_block_transitions = {
            "entered": newly_entered,
            "vacated": newly_vacated,
        }

        # Update tracking. occupied_blocks is no longer mirrored back onto
        # train.position; the controller derives it from the span_update
        # delivered through TrainController.on_physics_updated(...).
        self._last_occupied_blocks = new_occupied

    def _get_blocks_for_train(self, train: "Train") -> Set[str]:
        """
        Get all block IDs occupied by the train.

        Uses occupied_tiles from TrainPosition.
        """
        blocks: Set[str] = set()

        if not self.network:
            return blocks

        # Get occupied tiles
        if not train.position or not train.position.occupied_tiles:
            return blocks
        tiles = train.position.occupied_tiles

        # Get block for each tile
        for tile in tiles:
            track = self.network.tracks.get(tile)
            if track and track.block_section_id:
                blocks.add(track.block_section_id)

        return blocks

    def _validate_position_sync(self, train: "Train") -> bool:
        """
        Diagnostic-only sync check between tile-derived blocks and the local
        last-occupied-blocks tracker.

        The tile-derived block set should match the local
        _last_occupied_blocks set on every tick. We only log the discrepancy
        here — block-state corrections belong to the dispatcher progress flow.
        """
        if not train.position:
            return True

        expected_blocks = self._get_blocks_for_train(train)
        if expected_blocks != self._last_occupied_blocks:
            logger.warning(
                "[TrainPhysics %s] tile-derived blocks %s differ from local "
                "tracker %s",
                train.train_id,
                sorted(expected_blocks),
                sorted(self._last_occupied_blocks),
            )
            return False
        return True

    # =========================================================================
    # Voltage Output
    # =========================================================================

    def _update_voltage_output(
        self, train: "Train", controller: "TrainController"
    ) -> None:
        """
        Output voltage to all blocks occupied by this train.

        Uses SpeedProfile to convert target speed to voltage. Voltage is
        per-tick derived state owned by TrainPhysics and emitted as the
        software-to-hardware output layer through BlockPowerController.
        """
        power_controller = self._get_power_controller()
        if not power_controller:
            return

        profile = self._get_speed_profile()

        # State source: controller state (canonical owner per design_new.md).
        operational_state = getattr(controller, "state", TrainState.OFF)

        # Voltage selection by operational state. v1 states are OFF, READY,
        # RUNNING_MOVING, RUNNING_AT_STATION.
        if operational_state == TrainState.OFF:
            voltage = 0.0
        elif operational_state == TrainState.RUNNING_MOVING:
            # Manual power_percent mode: voltage tracks the calibrated speed
            # produced by SpeedProfile.power_speed_curve (per design_new.md
            # "Ownership split"). All other modes use lookahead target_speed.
            if (
                controller.control_mode == "manual"
                and getattr(controller, "manual_mode_type", None)
                == "power_percent"
            ):
                voltage = profile.speed_to_voltage(
                    float(getattr(controller, "current_speed_kmh", 0.0) or 0.0)
                )
            else:
                voltage = profile.speed_to_voltage(controller.target_speed)
        else:
            # READY / RUNNING_AT_STATION: powered but not moving.
            voltage = profile.lighting_voltage

        # Emergency stop override
        if controller.eb_active:
            voltage = 0.0

        # Set voltage for all occupied blocks
        self._set_voltage_for_blocks(train, voltage)

    def _set_voltage_for_blocks(self, train: "Train", voltage: float) -> None:
        """Set specific voltage for all occupied blocks."""
        power_controller = self._get_power_controller()
        if not power_controller:
            return

        occupied = self._get_blocks_for_train(train)
        for block_id in occupied:
            power_controller.set_block_voltage(block_id, voltage)

    # =========================================================================
    # Track Geometry Helpers
    # =========================================================================

    def _get_tile_length_cm(self, tile: Tuple[int, int]) -> float:
        """Get length of a tile in cm."""
        if not self.network:
            return 100.0  # Default fallback

        return self.network._get_tile_length_cm(tile[0], tile[1])

    def _calculate_exit_edge(
        self, tile: Tuple[int, int], entry_edge: str
    ) -> Optional[str]:
        """
        Calculate exit edge based on track geometry and entry edge.

        Uses the 6-bit track system to determine valid paths through the tile.
        """
        if not self.network:
            return None

        track = self.network.get_track(*tile)
        if not track:
            return None

        # Get all possible exits from entry edge based on track bits
        possible_exits = track.get_exits(entry_edge, use_switch_state=True)

        if not possible_exits:
            return None

        # If train has a path, follow the path
        train = self._get_train()
        path_tiles = self._get_path_tiles()
        if train and path_tiles:
            if (path_tiles[-1][0], path_tiles[-1][1]) == tile:
                return None
            next_tile_in_path = self._get_next_tile_in_path(path_tiles, tile)
            if next_tile_in_path:
                # Determine which edge leads to the next tile
                for exit_edge in possible_exits:
                    neighbor = self._get_neighbor_tile(tile, exit_edge)
                    if neighbor == next_tile_in_path:
                        return exit_edge

        # Default: use first available exit
        return possible_exits[0] if possible_exits else None

    def _get_next_tile_in_path(
        self, path_tiles: List[Tuple[int, int, Any]], current_tile: Tuple[int, int]
    ) -> Optional[Tuple[int, int]]:
        """Get next tile in train's path after current tile."""
        if not path_tiles:
            return None

        # Find current tile in path
        for i, (x, y, _) in enumerate(path_tiles):
            if (x, y) == current_tile:
                # Return next tile if exists
                if i + 1 < len(path_tiles):
                    next_x, next_y, _ = path_tiles[i + 1]
                    return (next_x, next_y)
                break

        return None

    def _get_neighbor_tile(
        self, tile: Tuple[int, int], edge: str
    ) -> Optional[Tuple[int, int]]:
        """Get neighbor tile in given direction."""
        dx, dy = self._get_direction_delta(edge)
        neighbor = (tile[0] + dx, tile[1] + dy)

        # Check if neighbor has a track
        if self.network and self.network.get_track(*neighbor):
            return neighbor

        return None

    def _get_direction_delta(self, edge: str) -> Tuple[int, int]:
        """Get (dx, dy) coordinate delta for moving through an edge."""
        mapping = {
            "N": (0, 1),
            "E": (1, 0),
            "S": (0, -1),
            "W": (-1, 0),
        }
        return mapping.get(edge, (0, 0))

    def _direction_to_edge(self, direction) -> str:
        """Convert Direction enum to edge string (N/E/S/W)."""
        from enums import Direction

        mapping = {
            Direction.NORTH: "S",  # Entering from south means heading north
            Direction.EAST: "W",
            Direction.SOUTH: "N",
            Direction.WEST: "E",
        }
        return mapping.get(direction, "N")

    def _edge_to_direction(self, edge: str):
        """Convert edge string (N/E/S/W) to Direction enum."""
        from enums import Direction

        mapping = {
            "N": Direction.NORTH,
            "E": Direction.EAST,
            "S": Direction.SOUTH,
            "W": Direction.WEST,
        }
        return mapping.get(edge, Direction.NORTH)

    # =========================================================================
    # Serialization
    # =========================================================================

    def to_dict(self) -> Dict[str, Any]:
        """Convert physics state to dictionary for JSON serialization."""
        return {
            "train_id": self.train_id,
            "last_occupied_blocks": list(self._last_occupied_blocks),
        }

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        network: "RailwayNetwork",
        controller: "TrainController",
    ) -> "TrainPhysics":
        """Reconstruct from dictionary after deserialization."""
        physics = cls(
            train_id=data["train_id"],
            network=network,
            controller=controller,
        )
        physics._last_occupied_blocks = set(data.get("last_occupied_blocks", []))
        return physics


# =============================================================================
# Factory Function
# =============================================================================


def create_physics_for_train(
    train_id: str,
    network: "RailwayNetwork",
    controller: "TrainController",
) -> TrainPhysics:
    """
    Create a TrainPhysics instance for a train.

    Args:
        train_id: ID of the train
        network: Railway network
        controller: TrainController for this train

    Returns:
        TrainPhysics instance
    """
    return TrainPhysics(
        train_id=train_id,
        network=network,
        controller=controller,
    )
