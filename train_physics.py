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
from enums import BRAKE_MULTIPLIERS, POWER_MULTIPLIERS, Direction, HandlePosition, TrainState
from model_scale import kmh_to_cm_per_sec
from runtime_payloads import PhysicsSpanUpdate, TileTraversal
from train_controller_types import (
    PhysicsCommand,
    PHYSICS_KIND_EMERGENCY_BRAKE,
    PHYSICS_KIND_HANDLE,
    PHYSICS_KIND_TARGET_SPEED,
)
from train_span import SpanEndLocator, TrainSpan

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
class _SpanWorkState:
    """Mutable per-tick movement scratchpad for `TrainPhysics`.

    Mirrors the field layout the movement helpers expect (two physical ends
    plus ordered occupied tiles/blocks) so the intricate tile-walk math is
    unchanged by the live-position migration. `head_is_a` records which
    physical end the controller's cab selects as the head (cab "A"/None ->
    end_a, "B" -> end_b); the helpers advance that end and trace the other as
    the tail. It is built from the controller-owned `TrainSpan` at tick start
    and written back into a fresh `TrainSpan` for the `PhysicsSpanUpdate`. The
    span itself never carries a head/tail label (design_new.md TrainSpan
    schema); the head side lives only here, derived from the cab.
    """

    head_is_a: bool
    end_a_tile: Tuple[int, int]
    end_a_distance_cm: float
    end_a_entry_edge: str
    end_b_tile: Tuple[int, int]
    end_b_distance_cm: float
    end_b_entry_edge: str
    occupied_tiles: List[Tuple[int, int]] = field(default_factory=list)
    occupied_blocks: List[str] = field(default_factory=list)


def _work_from_span(span: "TrainSpan", head_is_a: bool) -> _SpanWorkState:
    """Build a movement scratchpad from the controller-owned span."""
    return _SpanWorkState(
        head_is_a=head_is_a,
        end_a_tile=tuple(span.end_a.tile),
        end_a_distance_cm=float(span.end_a.distance_cm),
        end_a_entry_edge=span.end_a.entry_edge,
        end_b_tile=tuple(span.end_b.tile),
        end_b_distance_cm=float(span.end_b.distance_cm),
        end_b_entry_edge=span.end_b.entry_edge,
        occupied_tiles=[tuple(t) for t in (span.occupied_tiles or [])],
        occupied_blocks=list(span.occupied_blocks or []),
    )


def _span_from_work(work: _SpanWorkState) -> "TrainSpan":
    """Build the canonical 4-field `TrainSpan` from a moved scratchpad."""
    return TrainSpan(
        end_a=SpanEndLocator(
            tile=work.end_a_tile,
            distance_cm=work.end_a_distance_cm,
            entry_edge=work.end_a_entry_edge,
        ),
        end_b=SpanEndLocator(
            tile=work.end_b_tile,
            distance_cm=work.end_b_distance_cm,
            entry_edge=work.end_b_entry_edge,
        ),
        occupied_tiles=list(work.occupied_tiles),
        occupied_blocks=list(work.occupied_blocks),
    )


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
    # Per-tick movement scratchpad, built from the controller-owned TrainSpan
    # at the start of each powered-on movement step. The movement helpers mutate
    # it in place; the result is written back into a fresh TrainSpan for the
    # PhysicsSpanUpdate. None outside a movement step.
    _work: Optional["_SpanWorkState"] = None

    # =========================================================================
    # Internal Helpers
    # =========================================================================

    def _get_train(self) -> Optional["Train"]:
        """Get the Train entity this physics instance controls."""
        if self.network is None:
            return None
        return self.network.trains.get(self.train_id)

    def _get_path_tiles(self) -> Optional[List[Tuple[int, int, "Direction"]]]:
        """Get active path tiles from the controller as `(x, y, Direction)`.

        Sourced from the dispatcher-owned authority via the controller's
        `received_path_plan` geometry (design_new.md "Movement-authority input
        in TrainPhysics"); the per-tile direction is tunnel-aware.
        """
        if self.controller:
            return self.controller._get_effective_path_tiles()
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

        # 2. Read the controller-owned canonical span and build a per-tick
        # movement scratchpad. The cab-derived head end (resolved on the
        # controller) decides which physical end advances; the span itself
        # carries no head/tail label (design_new.md TrainSpan schema). When no
        # span is seeded yet, speed still integrates below, but no movement or
        # occupancy change happens this tick.
        span = controller.physics_sync.current_span
        if span is not None:
            head_is_a = controller._head_end_label() == "end_a"
            self._work = _work_from_span(span, head_is_a)
        else:
            # No live span yet (train not seeded via add_train): no movement or
            # occupancy change this tick. Speed still integrates below.
            self._work = None

        # Capture pre-move state for tile-delta computation: the previous
        # occupied tile list and the pre-move tail locator drive the
        # added_tiles / removed_tiles delta the controller consumes.
        prev_occupied_tiles = (
            list(self._work.occupied_tiles) if self._work is not None else []
        )
        prev_tail_locator = self._get_tail_locator()

        # 3. Resolve speed for this tick from controller-owned storage
        # (canonical source per design).
        profile = self._get_speed_profile()
        previous_speed = self._read_current_speed(train)

        # Per design_new.md ~line 168, the controller publishes a
        # `PhysicsCommand` each tick; physics dispatches on `kind` and never
        # inspects controller mode fields directly.
        command = self._read_physics_command(controller)
        new_speed = self._apply_physics_command(
            command, profile, previous_speed, delta
        )

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
        if self._work is not None and distance_cm > 0:
            self._move_train(train, distance_cm)
            logger.debug(
                "[TrainPhysics %s] moved distance_cm=%.2f", self.train_id, distance_cm
            )

            if self._force_stop:
                new_speed = 0.0

            # 11. Update block occupancy. Recomputes the scratchpad's
            # `occupied_blocks` from the current `occupied_tiles` so the `TrainSpan`
            # forwarded to `TrainController.on_physics_updated(...)` carries the live
            # block ownership view. The actual `Block.occupied_by` / `Block.caution`
            # writes still flow through Phase 3 via
            # `PathDispatcher.on_controller_progress(...)` →
            # `BlockManager.on_block_tail_vacated(...)` / `occupy_blocks(...)`.
            self._update_block_occupancy(train)
            logger.debug(
                "[TrainPhysics %s] occupied_blocks=%s",
                self.train_id,
                sorted(self._get_blocks_for_train(train)),
            )

            # 12. Build and emit per-boundary PhysicsSpanUpdate callbacks plus
            # one final no-boundary callback for this tick.
            new_occupied_tiles = (
                list(self._work.occupied_tiles) if self._work is not None else []
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
        else:
            # No movement this tick (stopped / no live span): block occupancy
            # and the span are unchanged, so forward the controller-owned span
            # as-is via the final no-boundary callback instead of rebuilding a
            # fresh `TrainSpan` (cheaper on the common powered-but-stationary
            # hold tick, and the only callback _emit_span_updates would emit
            # here anyway is that same final no-boundary one).
            self._emit_final_no_boundary_callback(
                controller, train, event_time, new_speed
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
        power_multipliers: Dict[HandlePosition, float] = POWER_MULTIPLIERS,
        brake_multipliers: Dict[HandlePosition, float] = BRAKE_MULTIPLIERS,
    ) -> float:
        """
        Get acceleration/deceleration rate for current handle position.

        Brake notches multiply `operational_decel_rate` (the rate ATO uses
        for its planned slow-down curve). With the standard table B6 = 1.00×
        exactly tracks that curve; B7-B8 sit slightly above 1× for catch-up;
        B1-B5 sit below. EB bypasses the table and applies
        `emergency_brake_rate`.

        Args:
            handle: Current handle position
            acceleration_rate: Maximum acceleration rate (km/h/s)
            operational_decel_rate: Operational deceleration rate (km/h/s) =
                profile.deceleration_rate × profile.operational_decel_ratio
            emergency_brake_rate: Emergency braking rate (km/h/s)
            power_multipliers / brake_multipliers: per-train notch tables
                (from the train's SpeedProfile); default to the standard table.

        Returns:
            Positive value for acceleration (km/h/s)
            Negative value for deceleration (km/h/s)
            Zero for neutral (coast)
        """
        # Power notches (acceleration)
        if handle in power_multipliers:
            return acceleration_rate * power_multipliers[handle]

        # Neutral (coast)
        if handle == HandlePosition.N:
            return 0.0

        # Brake notches (deceleration relative to op_decel)
        if handle in brake_multipliers:
            return -operational_decel_rate * brake_multipliers[handle]

        # Emergency brake
        if handle == HandlePosition.EB:
            return -emergency_brake_rate

        return 0.0

    def _read_physics_command(
        self, controller: Optional["TrainController"]
    ) -> PhysicsCommand:
        """Ask the controller for its per-tick PhysicsCommand.

        Per design_new.md ~line 168, physics is an executor only and the
        controller owns mode/EB/ATC policy. Physics calls a single
        controller-side producer instead of inspecting `control_mode`,
        `manual_mode_type`, `eb_active`, `atc_speed_limit`, etc. directly.

        Falls back to a coast-handle command when no controller is wired
        or the test fixture does not implement `produce_physics_command()`
        (a few test stubs in tests/test_train_system.py rely on this).
        """
        producer = getattr(controller, "produce_physics_command", None)
        if producer is None:
            return PhysicsCommand(
                kind=PHYSICS_KIND_HANDLE,
                handle=HandlePosition.N,
                voltage_source_speed_kmh=0.0,
            )
        return producer()

    def _apply_physics_command(
        self,
        command: PhysicsCommand,
        profile: "SpeedProfile",
        previous_speed: float,
        delta: float,
    ) -> float:
        """Compute the next-tick speed by dispatching on the command kind.

        - PHYSICS_KIND_EMERGENCY_BRAKE: decelerate at `emergency_brake_rate`.
        - PHYSICS_KIND_TARGET_SPEED: snap to `target_speed_kmh` (the
          controller has already clamped it against ATC and EB rules).
        - PHYSICS_KIND_HANDLE: integrate the controller-set handle through
          the standard handle→accel mapping plus friction and drag.
        """
        if command.kind == PHYSICS_KIND_EMERGENCY_BRAKE:
            new_speed = max(
                0.0, previous_speed - profile.emergency_brake_rate * delta
            )
            if new_speed <= STOPPED_SPEED_THRESHOLD_KMH:
                new_speed = 0.0
            return new_speed

        if command.kind == PHYSICS_KIND_TARGET_SPEED:
            return max(0.0, float(command.target_speed_kmh))

        # PHYSICS_KIND_HANDLE: integrate from previous_speed.
        operational_decel = (
            profile.deceleration_rate * profile.operational_decel_ratio
        )
        acceleration = self._get_acceleration_for_handle(
            command.handle,
            profile.acceleration_rate,
            operational_decel,
            profile.emergency_brake_rate,
            profile.power_multipliers,
            profile.brake_multipliers,
        )

        new_speed = previous_speed + (acceleration * delta)
        new_speed = max(0.0, new_speed)

        if acceleration < 0 and new_speed <= STOPPED_SPEED_THRESHOLD_KMH:
            new_speed = 0.0

        if command.handle == HandlePosition.N and previous_speed > 0:
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

        return new_speed

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
    # Track voltage (canonical home per design_new.md "Power-controller role")
    # =========================================================================

    def compute_track_voltage(
        self,
        controller: Optional["TrainController"],
        speed_profile: Optional["SpeedProfile"],
    ) -> float:
        """Canonical home for `track_voltage`.

        Per design_new.md "Voltage behavior" and "Power-controller role",
        `track_voltage` is per-tick derived state owned by `TrainPhysics`.
        Operational state (OFF / READY / RUNNING_AT_STATION / RUNNING_MOVING)
        selects the voltage shape; for RUNNING_MOVING the speed scalar is
        published by the controller as `PhysicsCommand.voltage_source_speed_kmh`
        so physics never inspects manual-mode fields directly.

        `BlockPowerController` consumes this scalar without making any
        manual-mode policy decisions itself.
        """
        # OFF or no controller: 0V.
        # Read the operational state through `controller.state`, which is a
        # property proxying `ops.state` (TrainController has no stored top-level
        # `state`). Per design_new.md "SpeedProfile", the stopped-but-lit states
        # (READY / RUNNING_AT_STATION) output `lighting_voltage`.
        operational_state = (
            getattr(controller, "state", None) if controller is not None else None
        )
        if controller is None or operational_state == TrainState.OFF:
            return 0.0

        lighting_voltage = (
            speed_profile.lighting_voltage if speed_profile is not None else 0.0
        )

        # Stopped lit states: lighting voltage scalar.
        if operational_state in (TrainState.READY, TrainState.RUNNING_AT_STATION):
            return lighting_voltage

        # RUNNING_MOVING: the speed reference comes from the controller's
        # PhysicsCommand. EB collapses voltage_source_speed_kmh to 0.0 in
        # the producer, so no extra branch is needed here.
        command = self._read_physics_command(controller)
        voltage_speed = float(command.voltage_source_speed_kmh)
        if speed_profile is not None:
            return speed_profile.speed_to_voltage_two_stage(voltage_speed)
        return self._linear_speed_to_voltage(voltage_speed)

    @staticmethod
    def _linear_speed_to_voltage(speed_kmh: float) -> float:
        """Fallback linear conversion when no SpeedProfile is supplied."""
        return min(12.0, max(0.0, float(speed_kmh) / 10.0))

    # =========================================================================
    # Span-update / boundary-callback helpers
    # =========================================================================

    def _read_current_speed(self, train: "Train") -> float:
        """Return the canonical current speed.

        Per design_new.md the controller's PhysicsSync sub-module owns
        ``current_speed_kmh``.
        """
        return float(
            getattr(self.controller.physics_sync, "current_speed_kmh", 0.0) or 0.0
        )

    def _get_tail_locator(
        self,
    ) -> Optional[Tuple[Tuple[int, int], str]]:
        """Return (tail_tile, tail_entry_edge) from the movement scratchpad."""
        work = self._work
        if work is None:
            return None
        if work.head_is_a:
            return (work.end_b_tile, work.end_b_entry_edge)
        return (work.end_a_tile, work.end_a_entry_edge)

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
            current_entry = self._entry_edge_on_next(current_tile, exit_edge, next_tile)
            current_tile = next_tile

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
        post-move span.

        Per the design contract ("when the train head enters a resource,
        that future reservation/lock is consumed"), every tile the head
        traversed during this tick must produce an `entered_resources`
        event, even when the body's `occupied_tiles` diff misses it.
        Zero-length resources, and tiles the head fully passes through in
        a single tick, are the two cases that need this guarantee.

        Emission order (physical traversal order):
          1. Head-swept tiles that the body never came to cover — emitted
             in the order `_advance_end` walked them, so the controller
             sees them in physical head-traversal order.
          2. Tiles the body settled into (new − prev) — walked from the
             post-move tail forward along the rail.
        """
        prev_set = set(prev_occupied_tiles)
        new_set = set(new_occupied_tiles)
        body_added = new_set - prev_set

        if self._work is None:
            return []

        # 1. Head-swept tiles missed by the body diff. Emitted in
        # walk order so the controller observes them in the physical
        # sequence the head crossed.
        swept = getattr(self, "_head_swept_traversals", []) or []
        seen: Set[Tuple[int, int]] = set()
        head_swept_frames: List[TileTraversal] = []
        for tile, entry_edge, exit_edge in swept:
            if tile in prev_set or tile in new_set:
                continue
            if tile in seen:
                continue
            seen.add(tile)
            head_swept_frames.append(
                self._make_tile_traversal(tile, entry_edge, exit_edge)
            )

        # 2. Body-diff additions — walk from the post-move tail. This
        # covers tiles the body settled into and any tile the head
        # crossed that the body now covers.
        body_frames: List[TileTraversal] = []
        if body_added:
            tail_locator = self._get_tail_locator()
            if tail_locator is not None:
                tail_tile, tail_entry = tail_locator
                body_frames = self._walk_tile_frames(
                    tail_tile, tail_entry, body_added
                )

        return head_swept_frames + body_frames

    def _build_removed_tile_traversals(
        self,
        prev_occupied_tiles: List[Tuple[int, int]],
        new_occupied_tiles: List[Tuple[int, int]],
        prev_tail_locator: Optional[Tuple[Tuple[int, int], str]],
    ) -> List[TileTraversal]:
        """Compute per-tile traversal frames for tiles vacated since the
        previous tick.

        Same zero-length-resource and fast-sweep cases as
        `_build_added_tile_traversals`: tiles the tail traversed during
        this tick but the body never straddled at a tick boundary must
        still produce a `tail_vacated_resources` event so the resource's
        physical `occupied_by` clears. `_move_train` records those in
        ``_tail_swept_traversals``.

        Emission order (physical tail-vacate order):
          1. Body-diff vacated tiles (prev − new) — walked from the
             pre-move tail forward; tiles emerge closest-to-old-tail first.
          2. Tail-swept tiles the body never covered at a tick boundary
             — emitted in the order `_move_train` recorded them.
        """
        prev_set = set(prev_occupied_tiles)
        new_set = set(new_occupied_tiles)
        body_removed = prev_set - new_set

        # 1. Body-diff vacated frames.
        body_frames: List[TileTraversal] = []
        if body_removed and prev_tail_locator is not None:
            prev_tail_tile, prev_tail_entry = prev_tail_locator
            body_frames = self._walk_tile_frames(
                prev_tail_tile, prev_tail_entry, body_removed
            )

        # 2. Tail-swept tiles the body diff missed. Emit in walk order.
        swept = getattr(self, "_tail_swept_traversals", []) or []
        seen: Set[Tuple[int, int]] = set()
        tail_swept_frames: List[TileTraversal] = []
        for tile, entry_edge, exit_edge in swept:
            if tile in new_set or tile in prev_set:
                continue
            if tile in seen:
                continue
            seen.add(tile)
            tail_swept_frames.append(
                self._make_tile_traversal(tile, entry_edge, exit_edge)
            )

        return body_frames + tail_swept_frames

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

        The ``new_span`` is built from the post-move movement scratchpad via
        ``_span_from_work(...)``; the controller owns the canonical span storage
        and uses ``span_update.new_span`` to refresh it.
        """
        new_span = _span_from_work(self._work) if self._work is not None else None

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
        movement occurred (powered-off / no-span / stationary ticks). The span
        is unchanged, so the controller-owned span is forwarded by reference
        (None when not yet seeded) rather than rebuilt. Consumers of
        `PhysicsSpanUpdate.new_span` must treat it as read-only: they may store
        or read it but must not mutate it in place, since on these ticks it IS
        the controller's canonical span object."""
        new_span = controller.physics_sync.current_span
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
        position = self._work

        # Reset head-swept traversal log for this move. `_advance_end`
        # appends to this list every tile the head crosses, including
        # zero-length resources and tiles the head fully passes within a
        # single tick. Consumed below by `_build_added_tile_traversals` so
        # those tiles still produce `entered_resources` events even though
        # the body's `occupied_tiles` diff misses them.
        self._head_swept_traversals: List[Tuple[Tuple[int, int], str, str]] = []

        # Capture the pre-move tail location so we can derive tail-swept
        # tiles (the tail counterpart to head-swept) below. Same rationale
        # as head-swept — the body diff misses zero-length tiles that the
        # tail clears within a single tick, and without an entry on
        # `tail_vacated_resources` the switch's `occupied_by` never clears.
        if position.head_is_a:
            prev_tail_tile = position.end_b_tile
            prev_tail_entry = position.end_b_entry_edge
        else:
            prev_tail_tile = position.end_a_tile
            prev_tail_entry = position.end_a_entry_edge

        # Determine which end is head based on leading_end
        if position.head_is_a:
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

        # Compute tail-swept tiles for this tick by walking forward from
        # the pre-move tail by `distance_cm` along the rail. The tail
        # always advances by the same physical distance as the head, so
        # this walk covers every tile the tail crossed during the tick
        # (including zero-length tiles the body never straddled at a
        # tick boundary). Used by `_build_removed_tile_traversals`.
        self._tail_swept_traversals = self._collect_swept_tiles_forward(
            prev_tail_tile, prev_tail_entry, distance_cm
        )

    def _collect_swept_tiles_forward(
        self,
        start_tile: Tuple[int, int],
        start_entry_edge: str,
        distance_cm: float,
    ) -> List[Tuple[Tuple[int, int], str, str]]:
        """Walk forward from `start_tile` (entered via `start_entry_edge`)
        for `distance_cm` along the rail, returning each tile the walker
        leaves behind (i.e., every tile passed through except the final
        one the walker stops in). Used to derive tail-swept tiles —
        zero-length tiles in particular need this because they never enter
        the body's `occupied_tiles` set.
        """
        swept: List[Tuple[Tuple[int, int], str, str]] = []
        if distance_cm <= 0:
            return swept

        current_tile = start_tile
        current_entry = start_entry_edge
        remaining = distance_cm

        max_iterations = 100
        for _ in range(max_iterations):
            tile_length = self._get_tile_length_cm(current_tile)

            # Zero-length tile: the walker passes straight through. Record
            # the tile as swept and continue. Bail if rail topology can't
            # resolve an exit.
            if tile_length <= 0:
                exit_edge = self._calculate_exit_edge(current_tile, current_entry)
                if not exit_edge:
                    break
                next_tile = self._get_neighbor_tile(current_tile, exit_edge)
                if next_tile is None:
                    break
                swept.append((current_tile, current_entry, exit_edge))
                new_entry = self._entry_edge_on_next(current_tile, exit_edge, next_tile)
                current_tile = next_tile
                current_entry = new_entry
                continue

            if remaining < tile_length:
                # Walker stops inside this tile; don't record it as swept.
                break

            remaining -= tile_length
            exit_edge = self._calculate_exit_edge(current_tile, current_entry)
            if not exit_edge:
                break
            next_tile = self._get_neighbor_tile(current_tile, exit_edge)
            if next_tile is None:
                break
            swept.append((current_tile, current_entry, exit_edge))
            new_entry = self._entry_edge_on_next(current_tile, exit_edge, next_tile)
            current_tile = next_tile
            current_entry = new_entry

        return swept

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
        position = self._work
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

                # Record this zero-length tile as a tile the head traversed
                # this tick. Without this, zero-length switches/blocks would
                # never produce an `entered_resources` event — the body
                # diff (`new_occupied_tiles - prev_occupied_tiles`) skips
                # them because no body tile ever covers them at a tick
                # boundary, and the design contract ("when the train head
                # enters a resource, that future reservation/lock is
                # consumed") would be violated.
                self._head_swept_traversals.append(
                    (new_tile, new_entry_edge, exit_edge)
                )

                new_entry_edge = self._entry_edge_on_next(new_tile, exit_edge, next_tile)
                new_tile = next_tile
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

            # Record this fully-traversed tile as one the head crossed
            # during the tick. Non-zero-length tiles that the head sweeps
            # entirely past in a single tick (high-speed / short-tile case)
            # would otherwise be skipped by the body-diff added_set the
            # same way zero-length tiles are.
            self._head_swept_traversals.append(
                (new_tile, new_entry_edge, exit_edge)
            )

            # Move to next tile
            new_entry_edge = self._entry_edge_on_next(new_tile, exit_edge, next_tile)
            new_tile = next_tile

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
        position = self._work
        if not position:
            return

        # Get head position
        if position.head_is_a:
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
            if position.head_is_a:
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
            # `exit_edge` is the edge on `prev_tile` that connects back to
            # `current_tile`. Grid-adjacent: opposite of the entry edge.
            # Tunnel-paired: the tunnel partner's edge on `prev_tile`.
            exit_edge = self._entry_edge_on_next(current_tile, current_entry, prev_tile)
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

        if position.head_is_a:
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
        position = self._work
        if not position:
            return

        # Get head and tail tiles
        if position.head_is_a:
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
            new_entry = self._entry_edge_on_next(current_tile, exit_edge, next_tile)
            current_tile = next_tile
            current_entry = new_entry

        if position.head_is_a:
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
        method maintains:

        - the scratchpad's `occupied_blocks`: ordered block-level ownership
          view (written into the new `TrainSpan` so Phase 3's tail-vacate
          suppression guard `still_occupied_block_ids` sees the live set)
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

        # Record block-level ownership on the scratchpad in physical tile
        # order. `_span_from_work(...)` carries this list into the new
        # `TrainSpan`, which `TrainController.on_physics_updated(...)` uses as
        # `still_occupied_block_ids` to suppress spurious tail-vacate reports
        # when a train moves between tiles of the same multi-tile block.
        if self._work is not None:
            ordered_blocks: List[str] = []
            seen: Set[str] = set()
            tiles = self._work.occupied_tiles or []
            for tile in tiles:
                track = self.network.tracks.get(tile) if self.network else None
                bid = getattr(track, "block_section_id", None) if track else None
                if bid and bid not in seen:
                    ordered_blocks.append(bid)
                    seen.add(bid)
            self._work.occupied_blocks = ordered_blocks

        self._last_occupied_blocks = new_occupied

    def _get_blocks_for_train(self, train: "Train") -> Set[str]:
        """
        Get all block IDs occupied by the train.

        Uses occupied_tiles from the movement scratchpad.
        """
        blocks: Set[str] = set()

        if not self.network:
            return blocks

        # Get occupied tiles from the movement scratchpad
        work = self._work
        if work is None or not work.occupied_tiles:
            return blocks
        tiles = work.occupied_tiles

        # Get block for each tile
        for tile in tiles:
            track = self.network.tracks.get(tile)
            if track and track.block_section_id:
                blocks.add(track.block_section_id)

        return blocks

    # =========================================================================
    # Physical-detection reconciliation (snap to the seam)
    # =========================================================================

    def reconcile_to_detected_occupancy(
        self, train: "Train", controller: "TrainController"
    ) -> bool:
        """Snap the calculated span forward to agree with the current detector.

        Physical detection outranks the calculated position (design_new.md
        "Power -> Block occupancy detection"). When the detector has confirmed a
        block entered or vacated for this train but the calculated span still
        disagrees, slide the rigid span forward one tile at a time until the head
        has reached every confirmed-entered block and the tail has cleared every
        confirmed-vacated block — "snap to the seam at least".

        One-directional: it only advances the train forward, never backward, and
        is a no-op when the calculated span already agrees. Under the v1
        simulated sensor (current derived from the span) the span always already
        agrees, so this never moves; it becomes active only with real hardware,
        or an early/late/fault-injecting sensor, that leads the calculated
        position. Returns True if it moved the span.
        """
        ps = controller.physics_sync
        span = ps.current_span
        if span is None:
            return False
        entered = ps.pending_detected_entered_blocks
        vacated = ps.pending_detected_vacated_blocks
        if not entered and not vacated:
            return False

        head_is_a = controller._head_end_label() == "end_a"
        self._work = _work_from_span(span, head_is_a)

        def disagrees() -> bool:
            occupied = set(self._work.occupied_blocks or [])
            if any(block_id not in occupied for block_id in entered):
                return True
            if any(block_id in occupied for block_id in vacated):
                return True
            return False

        moved = False
        guard = 0
        while disagrees() and guard < 200:
            guard += 1
            if not self._advance_head_to_next_tile(train):
                break  # end of track / no forward tile — cannot reconcile further
            moved = True

        if moved:
            ps.current_span = _span_from_work(self._work)
        self._work = None
        return moved

    def _advance_head_to_next_tile(self, train: "Train") -> bool:
        """Move the head end to the next forward tile (at its entry seam) and
        retrace the tail and occupied span. Returns False when there is no
        forward tile. Used only by reconcile_to_detected_occupancy; it records no
        swept traversals, so it emits no movement events."""
        work = self._work
        if work is None:
            return False
        if work.head_is_a:
            head_tile = work.end_a_tile
            head_entry = work.end_a_entry_edge
        else:
            head_tile = work.end_b_tile
            head_entry = work.end_b_entry_edge

        exit_edge = self._calculate_exit_edge(head_tile, head_entry)
        if not exit_edge:
            return False
        next_tile = self._get_neighbor_tile(head_tile, exit_edge)
        if next_tile is None:
            return False
        next_entry = self._entry_edge_on_next(head_tile, exit_edge, next_tile)

        if work.head_is_a:
            work.end_a_tile = next_tile
            work.end_a_entry_edge = next_entry
            work.end_a_distance_cm = 0.0
        else:
            work.end_b_tile = next_tile
            work.end_b_entry_edge = next_entry
            work.end_b_distance_cm = 0.0

        self._update_tail_position(train)
        self._update_occupied_tiles(train)
        # Rebuild the ordered occupied_blocks from the new tiles, mirroring
        # _update_block_occupancy but without its diagnostic transition
        # bookkeeping (last_block_transitions / _last_occupied_blocks), which the
        # reconciliation must not perturb.
        ordered_blocks: List[str] = []
        seen: Set[str] = set()
        for tile in work.occupied_tiles or []:
            track = self.network.tracks.get(tile) if self.network else None
            bid = getattr(track, "block_section_id", None) if track else None
            if bid and bid not in seen:
                ordered_blocks.append(bid)
                seen.add(bid)
        work.occupied_blocks = ordered_blocks
        return True

    def _validate_position_sync(self, train: "Train") -> bool:
        """
        Diagnostic-only sync check between tile-derived blocks and the local
        last-occupied-blocks tracker.

        The tile-derived block set should match the local
        _last_occupied_blocks set on every tick. We only log the discrepancy
        here — block-state corrections belong to the dispatcher progress flow.
        """
        if self._work is None:
            return True

        expected_blocks = self._get_blocks_for_train(train)
        if expected_blocks != self._last_occupied_blocks:
            sc = getattr(self.controller, "simulation_controller", None)
            nc = (
                getattr(sc, "notification_center", None) if sc is not None else None
            )
            emitted = False
            if nc is not None:
                try:
                    from notification_center import Severity as _Severity

                    nc.emit(
                        source="TrainPhysics",
                        kind="block_tracker_desync",
                        severity=_Severity.WARNING,
                        event_time=time.time(),
                        train_id=train.train_id,
                        payload={
                            "tile_derived": sorted(expected_blocks),
                            "local_tracker": sorted(self._last_occupied_blocks),
                        },
                    )
                    emitted = True
                except Exception:
                    pass
            if not emitted:
                logger.warning(
                    "[TrainPhysics %s] tile-derived blocks %s differ from local "
                    "tracker %s (NotificationCenter unavailable)",
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

        Per design_new.md ~line 168, voltage emission delegates to
        `compute_track_voltage(...)` so physics does not inspect controller
        mode fields. The EB-zero override is already baked into the
        controller-published `PhysicsCommand.voltage_source_speed_kmh`.
        """
        power_controller = self._get_power_controller()
        if not power_controller:
            return

        voltage = self.compute_track_voltage(controller, self._get_speed_profile())
        self._set_voltage_for_blocks(train, voltage)

    def _set_voltage_for_blocks(self, train: "Train", voltage: float) -> None:
        """Energize every block this train holds at the same `track_voltage`.

        The held set is the train's occupied blocks plus its reserved-ahead
        authority corridor (`Block.reserved_for_train`). Pre-energizing the
        reserved corridor at the same voltage as the occupied blocks means the
        loco crosses each block boundary without a power gap — the block it is
        about to enter is already at running voltage rather than dead (see
        design_new.md "Power", Block power-controller role). This method drives
        only this train's held blocks; a block whose holder clears (tail vacate /
        reservation release) is returned to 0V by SimulationController draining
        `BlockManager.take_blocks_to_deenergize()` after every train has emitted.
        """
        power_controller = self._get_power_controller()
        if not power_controller:
            return

        for block_id in self._held_blocks_for_train(train):
            power_controller.set_block_voltage(block_id, voltage)

    def _held_blocks_for_train(self, train: "Train") -> Set[str]:
        """Blocks this train holds: occupied span plus reserved-ahead corridor.

        Occupied blocks come from the movement scratchpad (the physics-canonical
        view of where the train is this tick). The reserved-ahead corridor is the
        dispatcher-owned future authority, read from the BlockManager's per-train
        reserved index (`reserved_blocks_for_train`) — the O(reserved) reverse of
        `Block.reserved_for_train`, not an all-blocks scan.
        """
        held = self._get_blocks_for_train(train)
        if self.network is None:
            return held
        block_manager = getattr(self.network, "block_manager", None)
        if block_manager is not None:
            held |= block_manager.reserved_blocks_for_train(train.train_id)
        else:
            # Fallback for a network with no BlockManager wired (isolated unit
            # tests). Production networks always create one (RailwayNetwork).
            tid = train.train_id
            for block_id, block in self.network.blocks.items():
                if getattr(block, "reserved_for_train", None) == tid:
                    held.add(block_id)
        return held

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
        if not self.network:
            return None
        neighbor = self.network.get_neighbor(tile[0], tile[1], edge)
        return (neighbor.x, neighbor.y) if neighbor else None

    def _entry_edge_on_next(
        self,
        from_tile: Tuple[int, int],
        from_exit_edge: str,
        to_tile: Tuple[int, int],
    ) -> str:
        """Edge on `to_tile` reached by exiting `from_tile` through `from_exit_edge`.

        Grid-adjacent: opposite of the exit edge. Tunnel-paired: the far end's
        edge from `network.tunnels`, which can be any cardinal (e.g. the demo_4
        S1↔S4 tunnel maps `N ↔ W`, not `N ↔ S`).
        """
        if self.network is not None:
            tunnel_far = self.network.tunnels.get(
                (from_tile[0], from_tile[1], from_exit_edge)
            )
            if tunnel_far is not None and (tunnel_far[0], tunnel_far[1]) == to_tile:
                return tunnel_far[2]
        return OPPOSITE_EDGES[from_exit_edge]

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
