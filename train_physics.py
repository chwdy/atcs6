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
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Set,
    Tuple,
)

from constants import OVERRUN_REPORT_MIN_BLOCKS, STOPPED_SPEED_THRESHOLD_KMH
from enums import BRAKE_MULTIPLIERS, POWER_MULTIPLIERS, Direction, HandlePosition
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

# How far short of a block seam a capped end is parked. Small enough to be
# invisible against centimetre track geometry, large enough that the end
# stays inside its tile instead of tipping over into the next one — landing
# exactly on the seam would put the head on the first tile of the very block
# the cap is holding it out of (design_new.md "Power", The calculated span
# never runs ahead of the detector).
_SEAM_HOLD_EPSILON_CM = 1e-6

# Tile budget for one cap walk. A cap only ever looks one tick's travel
# ahead, so this is a runaway-geometry backstop, not a reach limit.
_CAP_WALK_MAX_TILES = 200
# How many blocks past the head the line of travel is continued when signing a
# block the detector credits to a train beyond its calculated span. Such a block
# neighbours the span, so it is the first block past the head or, across a short
# block, the one after.
_LINE_OF_TRAVEL_BLOCKS = 2

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
    # The detected block this tick's cap is holding the head out of, or None
    # when no cap is holding it there (design_new.md "Power", The calculated
    # span never runs ahead of the detector). Per-tick derived state, recomputed
    # from the confirmed set every tick and read back through
    # `pending_entry_block`.
    _pending_entry_block: Optional[str] = None
    # The one reserved-ahead block this train energizes — the first block of its
    # reserved corridor the head reaches by going forward (design_new.md
    # "Power", Only the next reserved block is pre-energized). Per-tick derived
    # state, resolved at emission and read back through `next_reserved_block`.
    _next_reserved_block: Optional[str] = None
    # What the last emission energized, and what fell out of it since. A block
    # leaves the energized set for two different reasons: its holder cleared,
    # which `BlockManager` queues on its own, or it stayed reserved but stopped
    # being the next one ahead — a corridor the train reversed on, or one a
    # replan reordered. Nothing queues the second kind, so the emission side
    # records it and `SimulationController` drains both together.
    _last_energized_blocks: Set[str] = field(default_factory=set)
    _blocks_dropped_from_energized: Set[str] = field(default_factory=set)
    # The blocks the detector credits to this train that lie past the end of its
    # authority, which the emission set leaves unpowered (design_new.md "Power",
    # A block past the authority end is never energized). Per-tick derived
    # state, resolved at emission and read back through `blocks_past_authority`
    # so the de-energize drain does not hold them either.
    _blocks_past_authority: Set[str] = field(default_factory=set)
    # The tiles a detector correction carried the head across, as
    # (tile, entry_edge, exit_edge), waiting for the next movement report. A
    # switch's physical entry comes from the train's calculated traversal, and
    # the correction's traversal is one; the next `integrate_movement(...)`
    # reports the switch tiles among them as entered, as it does tiles the head
    # crosses under power (design_new.md "Block occupancy detection", The
    # correction walks through the train's own blocks only).
    _correction_swept: List[Tuple[Tuple[int, int], str, str]] = field(
        default_factory=list
    )
    # The switch tiles a detector correction carried the tail off, as
    # (tile, entry_edge, exit_edge), waiting for the next movement report. A
    # switch is released only when a movement report lists its tile as
    # tail-vacated, and a correction moves the span without one; the next
    # `integrate_movement(...)` reports them, as it does tiles the tail leaves
    # under power (design_new.md "Block occupancy detection", A vacated block
    # moves the span end to the seam).
    _correction_vacated: List[Tuple[Tuple[int, int], str, str]] = field(
        default_factory=list
    )

    # Named dependencies, handed in by `wire_dependencies(...)`: the event-log
    # sink, a call that returns the runtime's current `BlockOccupancyDetector`
    # (an activation builds a new one), and the `UnidentifiedTrainTracker`. A
    # physics instance built alone (unit tests) has none: it logs instead of
    # emitting and runs without the detector and tracker reads.
    _notification_center: Optional[Any] = None
    _occupancy_detector: Optional[Callable[[], Any]] = None
    _unidentified_trains: Optional[Any] = None

    def wire_dependencies(
        self,
        *,
        notification_center: Optional[Any] = None,
        occupancy_detector: Optional[Callable[[], Any]] = None,
        unidentified_trains: Optional[Any] = None,
    ) -> None:
        """Inject the named dependencies TrainPhysics needs. Called by
        SimulationController when it creates the instance; physics holds no
        SimulationController reference and does not reach one through its
        controller."""
        self._notification_center = notification_center
        self._occupancy_detector = occupancy_detector
        self._unidentified_trains = unidentified_trains

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
            # resolve(...) applies the model/instance overlay so a partial
            # instance profile is never driven directly (design_new.md
            # "SpeedProfile", Model profiles and instance profiles). For a model
            # profile it returns the profile unchanged.
            profile = self.network.speed_profiles.resolve(profile_id)
            if profile:
                return profile

        # No assigned/resolvable profile: anchor the default power curve to THIS
        # train's max_speed instead of the module-global DEFAULT_SPEED_PROFILE
        # (which maps 100% -> 100 km/h regardless of the train). Covers trains
        # created by paths that do not seed a profile (from_dict load, step
        # replay, editor add-without-place). The memoized fallback lives in one
        # shared home so the controller and physics read paths cannot drift.
        from speed_profile import resolve_memoized_default_profile

        return resolve_memoized_default_profile(self, train)

    def _get_power_controller(self) -> Optional["BlockPowerController"]:
        """Get the BlockPowerController from network."""
        if self.network and hasattr(self.network, "power_controller"):
            return self.network.power_controller
        return None

    # =========================================================================
    # Tick Method (Called Each Simulation Tick)
    # =========================================================================

    def tick(self, delta: float) -> None:
        """Run this train's physics for the tick: integrate movement, then set
        the track voltage that drives the next tick.

        Kept as a thin wrapper so existing callers and the current tick order
        are unchanged. The two halves are split so the runtime can later run
        `integrate_movement(...)` at the front of the tick (Observe) and
        `apply_track_voltage()` at the back (Action); calling them back-to-back
        here reproduces the original single-pass behaviour exactly.
        """
        self.integrate_movement(delta)
        self.apply_track_voltage()

    def integrate_movement(self, delta: float) -> None:
        """
        Integrate this train's motion for the tick (the movement half of the
        physics pass).

        Executes physical movement and emits one ordered PhysicsSpanUpdate per
        crossed resource boundary plus one final no-boundary callback every tick
        through TrainController.on_physics_updated(event_time, span_update).

        Setting the track voltage is a separate step, `apply_track_voltage()`.

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

        # Neither of these carries across ticks: both are recomputed below from
        # this tick's confirmed set and reservations, and an OFF train that
        # returns early has no cap at all.
        self._pending_entry_block = None
        self._next_reserved_block = None
        self._blocks_past_authority = set()

        # 1. Skip movement if powered off, but still emit the canonical
        # no-boundary callback so controller-owned span/speed storage stays
        # canonical even on OFF ticks. The reported speed is 0: an OFF train's
        # rails float and a model locomotive stops as soon as its power is
        # removed — the same momentum-free stop the emergency cut-out uses
        # (design_new.md "Movement feedback runs only when movement is
        # calculated; detection feedback always runs", Running pass, OFF
        # controller). So a train turned OFF while rolling stops by itself here.
        # Driving the blocks to 0 V is the voltage half's job
        # (`apply_track_voltage()`).
        correction_swept, self._correction_swept = self._correction_swept, []
        correction_vacated, self._correction_vacated = self._correction_vacated, []

        if not controller.power_on:
            span = controller.physics_sync.current_span
            if (correction_swept or correction_vacated) and span is not None:
                head_is_a = controller._head_end_label() == "end_a"
                self._work = _work_from_span(span, head_is_a)
                frames = self._correction_frames(
                    correction_swept, span.occupied_tiles or []
                )
                vacated_frames = self._correction_vacated_frames(
                    correction_vacated, span.occupied_tiles or []
                )
                if frames or vacated_frames:
                    self._emit_span_updates(
                        controller, train, event_time, 0.0, frames, vacated_frames
                    )
                    return
            self._emit_final_no_boundary_callback(controller, train, event_time, 0.0)
            return

        # Maintenance counter: accumulate powered-on run time (see design_new.md
        # "Maintenance counters"). Reached only when powered on; the OFF branch
        # returned above.
        train.total_run_time_seconds += delta

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
        correction_frames = self._correction_frames(
            correction_swept, prev_occupied_tiles
        )
        correction_vacated_frames = self._correction_vacated_frames(
            correction_vacated, prev_occupied_tiles
        )

        # 3. Resolve speed for this tick from controller-owned storage
        # (canonical source per design).
        profile = self._get_speed_profile()
        previous_speed = self._read_current_speed(train)

        # Per design_new.md "Ownership split", the controller publishes a
        # `PhysicsCommand` each tick; physics dispatches on `kind` and never
        # inspects controller mode fields directly.
        command = self._read_physics_command(controller)
        new_speed = self._apply_physics_command(
            command, profile, previous_speed, delta
        )


        # 9. Calculate distance to move, then hold it inside what the block
        # detector confirms (design_new.md "Power", The calculated span never
        # runs ahead of the detector). The cap shortens the step only — it never
        # touches `new_speed`, so the controller keeps braking and stopping
        # against the capped span with no branch for having been capped.
        distance_cm = self._speed_to_distance(new_speed, delta)
        distance_cm = self.cap_distance_to_detected_occupancy(train, distance_cm)

        # 10. Move train position. _move_train / _advance_end may force a stop
        # when the train hits the end of track; that stop is captured in
        # self._force_stop and applied to new_speed before the callback.
        self._force_stop = False
        if self._work is not None and distance_cm > 0:
            self._move_train(train, distance_cm)
            # Maintenance counter: accumulate distance actually moved (see
            # design_new.md "Maintenance counters").
            train.total_distance_cm += distance_cm

            if self._force_stop:
                new_speed = 0.0

            # 11. Update block occupancy. Recomputes the scratchpad's
            # `occupied_blocks` from the current `occupied_tiles` so the `TrainSpan`
            # forwarded to `TrainController.on_physics_updated(...)` carries the live
            # block ownership view. The actual `Block.occupied_by` / `Block.caution`
            # writes still flow through Observe via
            # `PathDispatcher.on_controller_progress(...)` →
            # `BlockManager.on_block_tail_vacated(...)` / `occupy_blocks(...)`.
            self._update_block_occupancy(train)

            # 12. Build and emit per-boundary PhysicsSpanUpdate callbacks plus
            # one final no-boundary callback for this tick.
            new_occupied_tiles = (
                list(self._work.occupied_tiles) if self._work is not None else []
            )
            added_traversals = correction_frames + self._build_added_tile_traversals(
                prev_occupied_tiles, new_occupied_tiles
            )
            removed_traversals = (
                correction_vacated_frames
                + self._build_removed_tile_traversals(
                    prev_occupied_tiles, new_occupied_tiles, prev_tail_locator
                )
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
            # here anyway is that same final no-boundary one). The exception is
            # a correction that carried the head onto a switch, or the tail off
            # one, on this read: that entry or vacate still has to be reported.
            if (correction_frames or correction_vacated_frames) and (
                self._work is not None
            ):
                self._emit_span_updates(
                    controller,
                    train,
                    event_time,
                    new_speed,
                    correction_frames,
                    correction_vacated_frames,
                )
            else:
                self._emit_final_no_boundary_callback(
                    controller, train, event_time, new_speed
                )

        # 13. Periodic position-block sync diagnostic (no auto-correction now
        # that the controller owns occupied_blocks state via the span).
        self._tick_count += 1
        if self._tick_count >= self._sync_validation_interval:
            self._validate_position_sync(train)
            self._tick_count = 0

    def apply_track_voltage(self) -> None:
        """Set the track voltage that drives the next tick (the voltage half of
        the physics pass).

        Reads the controller's published `PhysicsCommand` (never physics-local
        movement state), so it may run at the back of the tick, after planning,
        depending on this tick's `integrate_movement(...)` only through the
        energized-block set that step computed.

        A powered-off (or unwired) train drives its blocks to 0 V.
        """
        train = self._get_train()
        if not train:
            return
        controller = self.controller
        if not controller:
            return
        if not controller.power_on:
            self._set_voltage_for_blocks(train, 0.0)
            return
        # Output voltage to occupied blocks (hardware-output path).
        self._update_voltage_output(train, controller)

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

        Per design_new.md "Ownership split", physics is an executor only and the
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
        """Compute the next-tick speed: the speed solution for the command kind,
        then the command's speed floor (design_new.md "TrainPhysics", An ATO
        train never runs below its ATO minimum speed)."""
        speed = self._solve_speed(command, profile, previous_speed, delta)
        return self._apply_speed_floor(command, speed)

    @staticmethod
    def _drives_forward(command: PhysicsCommand) -> bool:
        """Whether the tick's effective operating point drives the train
        forward: a positive commanded speed, or, in manual `simulation`
        submode, a power notch under a positive ATC limit.

        A `PHYSICS_KIND_TARGET_SPEED` command carries the capped commanded
        speed. An ATO handle command (`atc_limit_kmh is None`) carries the ATO
        output's commanded speed in `target_speed_kmh`. A manual `simulation`
        handle command carries the driver's notch and the live ATC limit.
        """
        if command.kind == PHYSICS_KIND_EMERGENCY_BRAKE:
            return False
        if command.kind == PHYSICS_KIND_TARGET_SPEED:
            return float(command.target_speed_kmh) > 0.0
        if command.atc_limit_kmh is None:
            return float(command.target_speed_kmh) > 0.0
        return (
            command.handle in POWER_MULTIPLIERS
            and float(command.atc_limit_kmh) > 0.0
        )

    def _apply_speed_floor(self, command: PhysicsCommand, speed: float) -> float:
        """design_new.md "TrainPhysics", An ATO train never runs below its ATO
        minimum speed: a speed above 0 and below the command's floor becomes the
        floor when the tick drives the train forward, and 0 otherwise. The
        controller sets the floor to the ATO minimum speed for an ATO train and
        to 0 for every other command. Below its ATO minimum voltage a locomotive
        may stall on a slope or a bad joint, so a speed ATO calculated there
        could describe motion the locomotive does not make."""
        floor = float(command.speed_floor_kmh or 0.0)
        if speed <= 0.0 or speed >= floor:
            return speed
        return floor if self._drives_forward(command) else 0.0

    def _solve_speed(
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

        # ATC ceiling for a directly-driven handle (design_new.md,
        # Driver/operator inputs). The raw driver notch may never raise the
        # integrated speed above the live distance-0 `atc_limit`, and the
        # controller does not rewrite `handle_position` to enforce it. When the
        # notch would push the speed above the limit while the train is not yet
        # over it, hold at the limit; when the train is already over it,
        # decelerate toward the limit at the service brake rate rather than
        # snapping the tick's speed down to it (a driver braking harder than the
        # service rate keeps their steeper deceleration). Reached only for
        # PHYSICS_KIND_HANDLE: `power_percent` is already capped in the
        # Propulsion cap step, and EB / pending power-off arrive as their own
        # command kinds.
        atc_limit = command.atc_limit_kmh
        if atc_limit is not None and new_speed > atc_limit:
            if previous_speed > atc_limit:
                atc_braked = previous_speed - operational_decel * delta
                new_speed = max(atc_limit, min(new_speed, atc_braked))
            else:
                new_speed = atc_limit

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
    # Track voltage (canonical home per design_new.md "Block power-controller role")
    # =========================================================================

    def compute_track_voltage(
        self,
        controller: Optional["TrainController"],
        speed_profile: Optional["SpeedProfile"],
    ) -> float:
        """Canonical home for `track_voltage`.

        Per design_new.md "Voltage behavior" and "Block power-controller role",
        `track_voltage` is per-tick derived state owned by `TrainPhysics`. The
        controller publishes the voltage source on its `PhysicsCommand`, so
        physics never inspects mode fields directly:
          - `voltage_source_track_voltage` — an exact magnitude: `0` under an
            emergency cut-out, or a speed calibration step's voltage;
          - `voltage_source_speed_kmh` — every other case, ATO and manual
            `power_percent` included, mapped through `voltage_speed_curve`.

        A powered train that nothing drives outputs `lighting_voltage`, or 0 V
        when the profile sets none (design_new.md "TrainPhysics"): a speed of 0
        is 0% power, whether or not the train holds a reservation and in either
        mode. Only an OFF train and an emergency cut-out output 0 V whatever the
        profile holds.

        `BlockPowerController` consumes this scalar without making any
        manual-mode policy decisions itself.
        """
        # OFF or no controller: 0V. `train_status` is derived from the stored
        # `powered` flag (design_new.md "Controller state machine"); an
        # unpowered train de-energizes its block.
        powered = bool(getattr(controller, "powered", False)) if controller is not None else False
        if controller is None or not powered:
            return 0.0

        # Read the controller's per-tick command once. Physics is an executor:
        # the controller decides the voltage source and hands it down here rather
        # than physics inspecting mode fields or the calibration run registry.
        command = self._read_physics_command(controller)

        # An exact magnitude wins. The emergency cut-out publishes `0` here
        # (design_new.md "Emergency cut-out"). A speed-calibration step no
        # ceiling lowered publishes its own voltage here (design_new.md "Speed
        # calibration", Controller owns calibration output). Nothing else about
        # emission changes: the value is still a magnitude reaching this train's
        # ordinary energized set, signed per block. The controller reads the
        # calibration accessor and hands the magnitude down; physics never
        # reaches into the run registry.
        #
        # An overrun is handled by the emergency cut-out, not here:
        # `compute_track_voltage` has no overrun test of its own (design_new.md
        # "TrainPhysics", A train that overran its authority is cut out). The one
        # overrun that raises no cut-out is a calibration leg's, whose overrun
        # block is kept dead by the separate emission-set exclusion
        # `blocks_past_authority`, not by this method.
        if command.voltage_source_track_voltage is not None:
            return float(command.voltage_source_track_voltage)

        voltage_speed = float(command.voltage_source_speed_kmh)
        if speed_profile is not None:
            # Speed to voltage reads the curve backward (design_new.md
            # "SpeedProfile"); a speed of 0 maps to `lighting_voltage`, or 0 V
            # when it is unset: standing, under a plain EB, under the power-off
            # ceiling, or in ATO while not engaged.
            return speed_profile.speed_to_voltage(voltage_speed)
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
                if track.block_id:
                    block_id = track.block_id
                if getattr(track, "is_switch", False) and track.switch_id:
                    resource_type = "switch"
                    resource_id = track.switch_id
                elif track.block_id:
                    resource_type = "block"
                    resource_id = track.block_id

        return TileTraversal(
            tile_xy=tile,
            entry_edge=entry_edge,
            exit_edge=exit_edge,
            resource_type=resource_type,  # type: ignore[arg-type]
            resource_id=resource_id,
            block_id=block_id,
        )

    def _correction_frames(
        self,
        swept: List[Tuple[Tuple[int, int], str, str]],
        covered_tiles: List[Tuple[int, int]],
    ) -> List[TileTraversal]:
        """Traversal frames for the switch tiles a detector correction carried
        the head across that the span still covers, in the order the head
        crossed them. Block entries come from the detector, so only switch
        tiles are reported; a tile the span no longer covers is left out."""
        covered = {tuple(tile) for tile in covered_tiles}
        frames: List[TileTraversal] = []
        seen: Set[Tuple[int, int]] = set()
        for tile, entry_edge, exit_edge in swept:
            tile = tuple(tile)
            if tile in seen or tile not in covered:
                continue
            seen.add(tile)
            frame = self._make_tile_traversal(tile, entry_edge, exit_edge)
            if frame.resource_type == "switch":
                frames.append(frame)
        return frames

    def _correction_vacated_frames(
        self,
        vacated: List[Tuple[Tuple[int, int], str, str]],
        covered_tiles: List[Tuple[int, int]],
    ) -> List[TileTraversal]:
        """Traversal frames for the switch tiles a detector correction carried
        the tail off, in the order the tail left them. A tile the span covers
        again is left out."""
        covered = {tuple(tile) for tile in covered_tiles}
        frames: List[TileTraversal] = []
        seen: Set[Tuple[int, int]] = set()
        for tile, entry_edge, exit_edge in vacated:
            tile = tuple(tile)
            if tile in seen or tile in covered:
                continue
            seen.add(tile)
            frames.append(self._make_tile_traversal(tile, entry_edge, exit_edge))
        return frames

    def _record_switch_tiles_left(
        self, controller: "TrainController", span_before: "TrainSpan"
    ) -> None:
        """Queue the switch tiles a correction carried the tail off — covered by
        the span before it, not after — for the next movement report, with the
        edges the train used through each, read from the span before the
        correction."""
        span_after = controller.physics_sync.current_span
        if span_after is None:
            return
        left = {tuple(tile) for tile in span_before.occupied_tiles or []} - {
            tuple(tile) for tile in span_after.occupied_tiles or []
        }
        if not left or self.network is None:
            return
        left = {
            tile
            for tile in left
            if getattr(self.network.tracks.get(tile), "switch_id", None)
        }
        if not left:
            return
        head_is_a = controller._head_end_label() == "end_a"
        self._work = _work_from_span(span_before, head_is_a)
        try:
            tail_tile, tail_entry = self._get_tail_locator()
            frames = self._walk_tile_frames(tuple(tail_tile), tail_entry, left)
        finally:
            self._work = None
        self._correction_vacated.extend(
            (frame.tile_xy, frame.entry_edge, frame.exit_edge)
            for frame in frames
            if frame.resource_type == "switch"
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
          - one final no-boundary callback every tick so Observe always has a
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

        Traces from tail to head and records all tiles. The trace follows the
        points as they are set, like `_update_tail_position`, not the path: the
        body lies on the track the points lead along, and a span a correction
        carried past the authority end has tiles the path does not have.
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
            exit_edge = self._calculate_exit_edge(
                current_tile, current_entry, follow_path=False
            )
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
          view (written into the new `TrainSpan` so Observe's tail-vacate
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
                bid = getattr(track, "block_id", None) if track else None
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
            if track and track.block_id:
                blocks.add(track.block_id)

        return blocks

    # =========================================================================
    # Physical-detection reconciliation (snap to the seam)
    # =========================================================================

    def reconcile_to_detected_occupancy(
        self, train: "Train", controller: "TrainController"
    ) -> bool:
        """Snap the calculated span forward to agree with the current detector.

        Physical detection outranks the calculated position (design_new.md
        "Block occupancy detection"). When the detector has confirmed a block
        entered for this train but the calculated span still disagrees, slide the
        rigid span forward one tile at a time until the head has reached every
        confirmed-entered block — "snap to the seam at least".

        A `vacate` moves the tail forward out of the vacated block, to its seam
        with the next block (design_new.md "Block occupancy detection", A
        vacated block moves the span end to the seam; `_move_tail_to_vacated_seam`).
        It runs after the entry correction, so a read that both enters and
        vacates first puts the head on the entered block. An unidentified
        train's span IS its confirmed-occupied blocks, so a clear shrinks it
        directly (see the unidentified-train branch below).

        One-directional: it only advances the train forward, never backward, and
        is a no-op when the calculated span already agrees. Under the v1
        simulated sensor (current derived from the span) the span always already
        agrees, so this never moves; it becomes active only with real hardware,
        or an early/late/fault-injecting sensor, that leads the calculated
        position. Returns True if it moved the span.
        """
        ps = controller.physics_sync
        entered = ps.pending_detected_entered_blocks
        vacated = ps.pending_detected_vacated_blocks

        if getattr(train, "is_unidentified", False):
            # Forward-only protects integrated motion from being snapped
            # backwards; an unidentified train integrates none of its own, so there is nothing
            # to protect and its corrected span is simply its confirmed-occupied
            # blocks (design_new.md "Unidentified trains", Span).
            # `UnidentifiedTrainTracker` owns that construction.
            tracker = self._unidentified_tracker()
            if tracker is None:
                return False
            return tracker.write_detected_span(
                train.train_id, set(entered), set(vacated)
            )

        if ps.current_span is None:
            return False
        span_before = ps.current_span
        moved = self._snap_head_to_entered(train, controller, set(entered))
        if vacated:
            moved = (
                self._move_tail_to_vacated_seam(train, controller, set(vacated))
                or moved
            )
        if moved:
            # Either move can carry the tail off a switch: the entry correction
            # slides the whole span, and a vacate's seam is also the far seam of
            # a 0 cm switch tile.
            self._record_switch_tiles_left(controller, span_before)
        return moved

    def _snap_head_to_entered(
        self, train: "Train", controller: "TrainController", entered: Set[str]
    ) -> bool:
        """Slide the rigid span forward until the head has reached every
        confirmed-entered block it can reach by going forward (design_new.md
        "Block occupancy detection", Physical detection outranks the calculated
        position). Returns True if it moved the span."""
        ps = controller.physics_sync
        span = ps.current_span
        if span is None or not entered:
            return False

        head_is_a = controller._head_end_label() == "end_a"
        self._work = _work_from_span(span, head_is_a)

        # Only a block the head can actually reach by going forward can be
        # reconciled by going forward. A block behind the span — the locomotive
        # is standing further back than the calculated position put it, and
        # attribution has just given that block to this train — would otherwise
        # keep `disagrees()` true for every step of the walk, and the span would
        # be marched across the layout hunting track it will never cover. Such a
        # block still belongs to the train; the span simply does not move for it.
        entered_ahead = self._entered_blocks_ahead(
            set(entered), allow_crossing_own_body=True
        )
        if not entered_ahead:
            self._work = None
            return False

        def disagrees() -> bool:
            occupied = set(self._work.occupied_blocks or [])
            return any(block_id not in occupied for block_id in entered_ahead)

        blocks_before = set(self._work.occupied_blocks or [])
        moved = False
        moved_cm = 0.0
        guard = 0
        swept: List[Tuple[Tuple[int, int], str, str]] = []
        while disagrees() and guard < 200:
            guard += 1
            step_cm = self._head_distance_to_next_tile()
            if not self._advance_head_to_next_tile(train, swept):
                break  # end of track / no forward tile — cannot reconcile further
            moved = True
            moved_cm += step_cm

        if moved:
            # The first tile left is the one the head started in, which it
            # entered earlier; every later one it entered during this walk.
            self._correction_swept.extend(swept[1:])
            ps.current_span = _span_from_work(self._work)
            gained = set(self._work.occupied_blocks or []) - blocks_before
            self._report_span_correction(train, gained, moved_cm, blocks_before)
        self._work = None
        return moved

    def _head_distance_to_next_tile(self) -> float:
        """How far the head end moves to sit on the next tile's entry seam — the
        track left in the tile it is on. Read before the step that consumes it,
        so a correction can report the distance it made up."""
        work = self._work
        if work is None:
            return 0.0
        if work.head_is_a:
            tile, distance = work.end_a_tile, work.end_a_distance_cm
        else:
            tile, distance = work.end_b_tile, work.end_b_distance_cm
        tile_length = self._get_tile_length_cm(tile)
        return max(0.0, tile_length - distance) if tile_length > 0 else 0.0

    def _report_span_correction(
        self,
        train: "Train",
        gained: Set[str],
        moved_cm: float,
        span_blocks_before: Set[str],
    ) -> None:
        """Report a forward span correction — the app agreeing the detector found
        the train further along the track than the model had it (design_new.md
        "Power", Every forward span correction is reported).

        A gain of one block is routine sensor latency: the cap parks the head on
        the entry seam of the block it is entering, so every ordinary block entry
        gains exactly that one block once its detector confirms. It is reported at
        info level so an operator can watch the detector lead the calculated span
        block by block, without it reading as a fault.

        A gain of `OVERRUN_REPORT_MIN_BLOCKS` or more blocks is not latency: the
        detector found the train a whole block past where the model had it, so the
        locomotive is running faster than its speed profile says, and every
        estimate built on that profile (arrival time, braking distance, where the
        train sits inside its current block) is wrong in the same direction. That
        gain is reported at warning level with an `ErrorRecord` naming the overrun.

        Both levels carry the same payload: the blocks the detector read the train
        onto, the blocks the calculated span held before the correction, and the
        distance the span was moved forward. The correction itself is unchanged
        either way; the report is what stops the app correcting silently.
        """
        if not gained:
            return
        is_overrun = len(gained) >= OVERRUN_REPORT_MIN_BLOCKS
        hardware_blocks = sorted(gained)
        span_before = sorted(span_blocks_before)
        center = self._notification_center
        if center is None:
            log = logger.warning if is_overrun else logger.info
            log(
                "[TrainPhysics %s] span corrected forward %.1fcm: detector led "
                "the calculated span onto %s (span held %s)",
                train.train_id,
                moved_cm,
                hardware_blocks,
                span_before,
            )
            return
        from notification_center import Kind, train_ref

        payload = {
            "train_id": train.train_id,
            "blocks_gained": hardware_blocks,
            "span_blocks_before": span_before,
            "correction_cm": round(float(moved_cm), 1),
        }
        try:
            if is_overrun:
                from runtime_payloads import ErrorRecord

                center.emit(
                    source="TrainPhysics",
                    operation="reconcile_to_detected_occupancy",
                    kind=Kind.WARNING,
                    message=(
                        f"train {train.train_id} was detected {len(gained)} blocks "
                        f"ahead of where the model had it "
                        f"({', '.join(hardware_blocks)}); the span was corrected "
                        f"forward {moved_cm:.1f}cm. The locomotive is running "
                        f"faster than its speed profile."
                    ),
                    entities=[train_ref(train.train_id)],
                    error=ErrorRecord(
                        "the detector led the calculated span by more than one block",
                        "span_overrun_correction",
                    ),
                    event_time=time.time(),
                    payload=payload,
                )
            else:
                center.emit(
                    source="TrainPhysics",
                    operation="reconcile_to_detected_occupancy",
                    kind=Kind.INFO,
                    message=(
                        f"detector read train {train.train_id} onto "
                        f"{', '.join(hardware_blocks)} ahead of the calculated span "
                        f"(span held {', '.join(span_before) or 'no block'}); the "
                        f"span was corrected forward {moved_cm:.1f}cm."
                    ),
                    entities=[train_ref(train.train_id)],
                    event_time=time.time(),
                    payload=payload,
                )
        except Exception:
            # A reporting failure must not undo a correction that already landed.
            logger.warning(
                "[TrainPhysics %s] span correction gained %d blocks",
                train.train_id,
                len(gained),
            )

    def _entered_blocks_ahead(
        self, entered: Set[str], *, allow_crossing_own_body: bool = False
    ) -> Set[str]:
        """Of `entered`, the blocks the head reaches by travelling forward.

        A pure look — it walks the rail ahead of the head and reports which of
        the given blocks lie along it, moving nothing. The walk is bounded by the
        same tile budget the seam cap uses, which is what keeps a block that is
        behind the span (or on track the points do not lead to) out of the
        result.

        The walk follows the points as they are set, not the train's path
        (design_new.md "Block occupancy detection", The correction walks through
        the train's own blocks only). The locomotive goes where the points send
        it, and the path ends at the authority end while the track does not, so
        a block a train overran onto is ahead of it like any other — whether or
        not that overrun raised a cut-out.

        The walk crosses only switch tiles, the given blocks, and blocks this
        train holds (its span's blocks and those tied to it by
        `Block.occupied_by`). It stops at the first block that is none of those.
        The points as they are set lead on without end round a loop, and with no
        such stop the walk would come round to a block behind the train and
        report it as ahead.

        The walk also stops at the first `detected block` the detector has not
        confirmed occupied, which is the same line `_head_entry_cap` holds the
        head at. A block on the far side of one is not somewhere this train can
        be: reaching it means having crossed track whose detector says it is
        clear, and a correction that drags the span over that track claims a
        position no detector supports. So the correction never crosses a
        boundary the cap would not let the train cross under power.

        The one exception, taken only when `allow_crossing_own_body` is set, is a
        clear detected block this train still holds — one its own span covers, or
        one still tied to it by `Block.occupied_by` because its confirmed clear
        has not committed yet. That block reading clear is the train's own body
        clearing from behind the head as it rolls forward, not foreign track: the
        detector already vouched for the train being there. So the walk may cross
        it to reach a block confirmed just beyond, which is what lets a badly
        lagging span catch up to a train that has pulled a block or more ahead
        instead of losing that confirmed block to an unidentified train. A clear
        block the train does not hold still stops the walk, exactly as before.
        """
        work = self._work
        if work is None or not entered:
            return set()

        detector = self._block_occupancy_detector()
        detected = detector.detected_block_ids() if detector is not None else set()
        confirmed = detector.confirmed_occupied if detector is not None else set()

        held: Set[str] = set(work.occupied_blocks or [])
        train = self._get_train()
        if train is not None and self.network is not None:
            held |= {
                block_id
                for block_id, block in self.network.blocks.items()
                if getattr(block, "occupied_by", None) == train.train_id
            }
        own_body: Set[str] = held if allow_crossing_own_body else set()

        if work.head_is_a:
            tile, distance, entry = (
                work.end_a_tile,
                work.end_a_distance_cm,
                work.end_a_entry_edge,
            )
        else:
            tile, distance, entry = (
                work.end_b_tile,
                work.end_b_distance_cm,
                work.end_b_entry_edge,
            )

        wanted = set(entered)
        found: Set[str] = set()
        for next_tile, _travelled in self._walk_forward_tiles(
            tile, distance, entry, float("inf"), follow_path=False
        ):
            block_id = self._block_of_tile(next_tile)
            if block_id in wanted:
                found.add(block_id)
                wanted.discard(block_id)
                if not wanted:
                    break
                continue
            if block_id is None:
                continue
            if block_id not in held:
                break
            if (
                block_id in detected
                and block_id not in confirmed
                and block_id not in own_body
            ):
                break
        return found

    # =========================================================================
    # Physical-detection reconciliation (hold at the seam)
    # =========================================================================

    @property
    def pending_entry_block(self) -> Optional[str]:
        """The detected block this tick's cap is holding the head out of.

        It carries this train's `track_voltage` for as long as the cap holds,
        because the locomotive has to be driven onto the very block whose
        detector releases the cap (design_new.md "Power", Block power-controller
        role). `SimulationController` also reads it so the de-energize drain
        leaves that block alone.
        """
        return self._pending_entry_block

    def cap_distance_to_detected_occupancy(
        self, train: "Train", distance_cm: float
    ) -> float:
        """Shorten this tick's step so the calculated span stays inside what the
        detector confirms (design_new.md "Power", The calculated span never runs
        ahead of the detector).

        The mirror of `reconcile_to_detected_occupancy`, which pulls a span
        forward to where the detector says the train already is. This holds a
        span back from where the detector has not yet said the train is: the head
        stops at the entry seam of the next detected block until that block
        confirms occupied, and the tail stops at the exit seam of the rearmost
        detected block the span holds until that block confirms clear. A span is
        rigid, so the tighter of the two caps decides the whole tick.

        Returns a distance in `[0, distance_cm]` — never negative, so a cap
        stops a train rather than dragging it backwards. Inert whenever the
        session detects no blocks (the simulated driver reports none, which is
        what keeps it from deadlocking against a current map it builds from this
        very span), and inert for an unidentified train, which integrates no motion of
        its own.
        """
        self._pending_entry_block = None
        if distance_cm < 0:
            return 0.0
        if self._work is None or self.network is None:
            return distance_cm
        if getattr(train, "is_unidentified", False):
            return distance_cm

        detector = self._block_occupancy_detector()
        if detector is None:
            return distance_cm
        detected = detector.detected_block_ids()
        if not detected:
            return distance_cm
        confirmed = detector.confirmed_occupied

        # Walk a hair past the requested distance so a train already standing on
        # a seam (parked there by an earlier tick's cap) still finds the block it
        # is waiting on, and keeps it energized.
        limit_cm = distance_cm + 2 * _SEAM_HOLD_EPSILON_CM

        allowed = distance_cm
        head_cap, pending = self._head_entry_cap(detected, confirmed, limit_cm)
        if head_cap is not None:
            allowed = min(allowed, max(0.0, head_cap - _SEAM_HOLD_EPSILON_CM))
            self._pending_entry_block = pending
        tail_cap = self._tail_exit_cap(detected, confirmed, limit_cm)
        if tail_cap is not None:
            allowed = min(allowed, max(0.0, tail_cap - _SEAM_HOLD_EPSILON_CM))
        if allowed > 0.0 and self._held_block_behind(train, detected, confirmed):
            return 0.0
        return allowed

    def _head_entry_cap(
        self, detected: Set[str], confirmed: Set[str], limit_cm: float
    ) -> Tuple[Optional[float], Optional[str]]:
        """Distance the head may travel before it would enter a detected block
        the detector has not confirmed, plus that block's id.

        `(None, None)` when no such block lies within `limit_cm`.

        **A held head always ends up inside a block.** Switch tiles belong to no
        block and carry no detection, so a run of them between two blocks is
        track nothing can vouch for. Stopping the head there would claim a
        position no detector supports, and it would strand the train: a head in
        no block has no current block to resolve, so `PathDispatcher.create_path`
        can no longer plan for it. The hold is therefore taken at the exit seam
        of the last block the head is actually in, not at the far block's edge.

        The one head this cannot hold is one already standing outside every
        block. Rather than pin it there for good, it is left free to run to the
        next block boundary, where the ordinary hold applies again.
        """
        work = self._work
        if work is None:
            return None, None
        if work.head_is_a:
            tile, distance, entry = (
                work.end_a_tile,
                work.end_a_distance_cm,
                work.end_a_entry_edge,
            )
        else:
            tile, distance, entry = (
                work.end_b_tile,
                work.end_b_distance_cm,
                work.end_b_entry_edge,
            )

        current_block = self._block_of_tile(tile)
        if current_block is None:
            return None, None

        # Where the head would leave the block it is in now. On a run straight
        # from one block into the next this is the next block's entry seam, so
        # the hold is unchanged; across a switch it is the earlier seam, which is
        # the last point the head is still somewhere a detector answers for.
        leaves_current_block_at: Optional[float] = None

        for next_tile, travelled in self._walk_forward_tiles(
            tile, distance, entry, limit_cm
        ):
            block_id = self._block_of_tile(next_tile)
            if block_id != current_block and leaves_current_block_at is None:
                leaves_current_block_at = travelled
            if block_id is None or block_id == current_block:
                continue
            if block_id in detected and block_id not in confirmed:
                return leaves_current_block_at, block_id
            current_block = block_id
            leaves_current_block_at = None
        return None, None

    def _held_block_behind(
        self, train: "Train", detected: Set[str], confirmed: Set[str]
    ) -> Optional[str]:
        """A confirmed detected block this train holds that its span neither
        covers nor can reach by going forward — the locomotive standing behind
        the calculated span, most plainly because reversed rail wiring drove it
        opposite to the commanded direction.

        Attribution hands such a block to the adjacent ordinary train rather
        than minting an unidentified train, and the forward-only snap deliberately does not
        move the span for it. The tail rule still applies to it: the tail may
        not run away from a block whose detector says this train is standing
        there, so while one exists the whole span is pinned (a span is rigid).
        The pin releases through the ordinary vacate path — the loco returns,
        the block's current stops, the confirmed clear releases `occupied_by`.
        """
        work = self._work
        network = self.network
        if work is None or network is None:
            return None
        span_blocks = set(work.occupied_blocks or [])
        stray: Set[str] = set()
        for block_id, block in network.blocks.items():
            if getattr(block, "occupied_by", None) != train.train_id:
                continue
            if block_id in span_blocks:
                continue
            if block_id in detected and block_id in confirmed:
                stray.add(block_id)
        if not stray:
            return None
        # A held confirmed block the head CAN reach forward is the snap's job,
        # not a pin — it only sits outside the span for the instants the
        # forward walk has not yet covered it.
        behind = stray - self._entered_blocks_ahead(stray)
        return min(behind) if behind else None

    def _tail_exit_cap(
        self, detected: Set[str], confirmed: Set[str], limit_cm: float
    ) -> Optional[float]:
        """Distance the tail may travel before it would leave a detected block
        the detector still confirms occupied, or None when nothing holds it.

        The span drops a block when its rearmost tile moves out of it, so the
        stop is the first tile ahead that belongs to any other block — including
        a switch tile, which belongs to none.
        """
        work = self._work
        if work is None:
            return None
        if work.head_is_a:
            tile, distance, entry = (
                work.end_b_tile,
                work.end_b_distance_cm,
                work.end_b_entry_edge,
            )
        else:
            tile, distance, entry = (
                work.end_a_tile,
                work.end_a_distance_cm,
                work.end_a_entry_edge,
            )

        tail_block = self._block_of_tile(tile)
        if tail_block is None or tail_block not in detected:
            return None
        if tail_block not in confirmed:
            return None

        for next_tile, travelled in self._walk_forward_tiles(
            tile, distance, entry, limit_cm
        ):
            if self._block_of_tile(next_tile) != tail_block:
                return travelled
        return None

    def block_list_rear_to_front(self) -> List[str]:
        """This train's block list — the blocks `Block.occupied_by` names it
        on — ordered rear to front (design_new.md "Block occupancy detection",
        The occupancy chain runs one way).

        The order is the connectivity walk forward from the span's tail end,
        following the points as they are set, so the span supplies which side
        is the tail, never where inside a block the train sits. Blocks the walk
        meets in order come first to last. A held block the walk never meets is
        either behind the tail — the locomotive standing behind the calculated
        span, which makes it the rearmost — or ahead of the head, which makes it
        the front; it is placed accordingly. Empty when the train holds no block
        or has no span.
        """
        return self._walk_block_list(set())[0]

    def head_side_block(self, gained: Iterable[str] = ()) -> Optional[str]:
        """The block on this train's head side: the first block the
        connectivity walk meets past the front of its block list, going away
        from the rear (design_new.md "Block occupancy detection", Touching).

        `gained` is what attribution has credited this train on the current
        read; each such block joins the list for the next pass, so a train
        that crossed several blocks between two reads has its head side past
        the last of them. None when the walk meets no further block.
        """
        return self._walk_block_list(set(gained))[1]

    def _walk_block_list(
        self, gained: Set[str]
    ) -> Tuple[List[str], Optional[str]]:
        """(block list rear to front, head-side block) for the blocks this
        train holds plus `gained`. See `block_list_rear_to_front` and
        `head_side_block`."""
        controller = self.controller
        network = self.network
        train = self._get_train()
        if controller is None or network is None or train is None:
            return [], None
        listed = {
            block_id
            for block_id, block in network.blocks.items()
            if getattr(block, "occupied_by", None) == train.train_id
        } | gained
        span = controller.physics_sync.current_span
        if not listed or span is None:
            return [], None

        head_is_a = controller._head_end_label() == "end_a"
        previous_work = self._work
        self._work = _work_from_span(span, head_is_a)
        try:
            tail = span.end_b if head_is_a else span.end_a
            tail_tile = tuple(tail.tile)
            ordered: List[str] = []
            head_side: Optional[str] = None
            tail_block = self._block_of_tile(tail_tile)
            if tail_block in listed:
                ordered.append(tail_block)
            for next_tile, _travelled in self._walk_forward_tiles(
                tail_tile,
                float(tail.distance_cm),
                tail.entry_edge,
                float("inf"),
                follow_path=False,
            ):
                block_id = self._block_of_tile(next_tile)
                if block_id is None or block_id in ordered:
                    continue
                if block_id in listed:
                    ordered.append(block_id)
                elif ordered:
                    # Past this train's last listed block: the list ends here
                    # and this is the block on its head side.
                    head_side = block_id
                    break
            unmet = listed - set(ordered)
            ahead = self._entered_blocks_ahead(unmet) if unmet else set()
            return sorted(unmet - ahead) + ordered + sorted(ahead), head_side
        finally:
            self._work = previous_work

    def _move_tail_to_vacated_seam(
        self, train: "Train", controller: "TrainController", vacated: Set[str]
    ) -> bool:
        """Move the span's tail out of the blocks this read vacated, to the seam
        with the next block (design_new.md "Block occupancy detection", A
        vacated block moves the span end to the seam). Returns True if it moved
        the span.

        Only the tail moves out of a vacated block, and only forward: a vacate
        is always the rearmost block of the list. The head moves with it by the
        same distance, because a span is rigid. The tail stops at the first tile
        ahead that is not in a vacated block — a switch tile included — so the
        move never carries the tail past the vacated block's exit seam; ordinary
        integration carries it over the switch. A 0 cm switch tile has both its
        seams on that point, so the tail has left it, and
        `reconcile_to_detected_occupancy` queues its tail-vacate for the next
        movement report.

        The head may move only over this train's own blocks: when the move
        would carry it onto a switch tile or into a block outside the list, the
        head stops at the exit seam of the block it is in, and the tail stops
        short of its seam by the same amount. That keeps the head off track the
        detector has not given this train.
        """
        span = controller.physics_sync.current_span
        if span is None or self.network is None:
            return False
        head_is_a = controller._head_end_label() == "end_a"
        self._work = _work_from_span(span, head_is_a)
        try:
            tail = span.end_b if head_is_a else span.end_a
            tail_tile = tuple(tail.tile)
            if self._block_of_tile(tail_tile) not in vacated:
                return False
            to_seam: Optional[float] = None
            for next_tile, travelled in self._walk_forward_tiles(
                tail_tile,
                float(tail.distance_cm),
                tail.entry_edge,
                float("inf"),
                follow_path=False,
            ):
                if self._block_of_tile(next_tile) not in vacated:
                    to_seam = travelled
                    break
            if to_seam is None:
                return False
            # The tail cap parks a tail `_SEAM_HOLD_EPSILON_CM` short of the
            # seam while the block still reads occupied. That is the tail at
            # the seam, and the vacate has to carry it across, or a standing
            # train keeps the vacated block in its span for good.
            to_seam = max(to_seam, 2 * _SEAM_HOLD_EPSILON_CM)

            own = {
                block_id
                for block_id, block in self.network.blocks.items()
                if getattr(block, "occupied_by", None) == train.train_id
            }
            own |= set(controller.physics_sync.pending_detected_entered_blocks)
            own -= vacated
            distance = min(to_seam, self._head_room_within(own, to_seam))
            if distance <= _SEAM_HOLD_EPSILON_CM:
                return False
            self._advance_head_by(train, distance)
            controller.physics_sync.current_span = _span_from_work(self._work)
            return True
        finally:
            self._work = None

    def _head_room_within(self, blocks: Set[str], limit_cm: float) -> float:
        """How far the head may move forward, up to `limit_cm`, before it would
        leave `blocks` — onto a switch tile or into any other block. It walks the
        points as they are set, like the entry correction."""
        work = self._work
        if work is None:
            return 0.0
        if work.head_is_a:
            tile, distance, entry = (
                work.end_a_tile,
                work.end_a_distance_cm,
                work.end_a_entry_edge,
            )
        else:
            tile, distance, entry = (
                work.end_b_tile,
                work.end_b_distance_cm,
                work.end_b_entry_edge,
            )
        if self._block_of_tile(tile) not in blocks:
            return 0.0
        for next_tile, travelled in self._walk_forward_tiles(
            tile, distance, entry, limit_cm, follow_path=False
        ):
            if self._block_of_tile(next_tile) not in blocks:
                return travelled
        return limit_cm

    def _advance_head_by(self, train: "Train", distance_cm: float) -> None:
        """Move the head forward by `distance_cm` and retrace the tail and
        occupied span. Like `_advance_head_to_next_tile`, it records no swept
        traversals, so it emits no movement events."""
        work = self._work
        remaining = distance_cm
        while work is not None and remaining > _SEAM_HOLD_EPSILON_CM:
            to_next = self._head_distance_to_next_tile()
            # A move that ends on the tile's exit seam leaves the head in this
            # tile, so the span does not list the block beyond it.
            if to_next >= remaining - _SEAM_HOLD_EPSILON_CM:
                if work.head_is_a:
                    work.end_a_distance_cm += remaining
                else:
                    work.end_b_distance_cm += remaining
                self._retrace_from_head(train)
                return
            if not self._advance_head_to_next_tile(train):
                return
            remaining -= to_next

    def _walk_forward_tiles(
        self,
        tile: Tuple[int, int],
        distance_cm: float,
        entry_edge: str,
        limit_cm: float,
        follow_path: bool = True,
    ) -> Iterator[Tuple[Tuple[int, int], float]]:
        """Walk forward from one span end, yielding `(tile, travelled_cm)` for
        each tile the end would reach, where `travelled_cm` is how far the end
        moves to sit exactly on that tile's entry seam.

        Stops at `limit_cm`, at the end of track, and at the tile budget. Records
        nothing and mutates nothing — the movement helpers own actually moving
        the train.
        """
        current_tile = tile
        current_entry = entry_edge
        tile_length = self._get_tile_length_cm(current_tile)
        travelled = max(0.0, tile_length - distance_cm) if tile_length > 0 else 0.0

        for _ in range(_CAP_WALK_MAX_TILES):
            if travelled > limit_cm:
                return
            exit_edge = self._calculate_exit_edge(
                current_tile, current_entry, follow_path
            )
            if not exit_edge:
                return
            next_tile = self._get_neighbor_tile(current_tile, exit_edge)
            if next_tile is None:
                return
            next_entry = self._entry_edge_on_next(current_tile, exit_edge, next_tile)
            yield next_tile, travelled
            current_tile = next_tile
            current_entry = next_entry
            length = self._get_tile_length_cm(current_tile)
            if length > 0:
                travelled += length

    def _block_of_tile(self, tile: Tuple[int, int]) -> Optional[str]:
        """The block a tile belongs to, or None for a tile that belongs to none
        (a switch tile carries no block and no detection)."""
        if self.network is None:
            return None
        track = self.network.tracks.get(tile)
        return getattr(track, "block_id", None) if track else None

    def _block_occupancy_detector(self):
        """The runtime's current `BlockOccupancyDetector`, or None in an
        isolated unit test, through the injected `occupancy_detector` call."""
        get = self._occupancy_detector
        return get() if get is not None else None

    def _unidentified_tracker(self):
        """The runtime's `UnidentifiedTrainTracker`, or None in an isolated
        unit test, as injected by `wire_dependencies(...)`."""
        return self._unidentified_trains

    def _advance_head_to_next_tile(
        self,
        train: "Train",
        swept: Optional[List[Tuple[Tuple[int, int], str, str]]] = None,
    ) -> bool:
        """Move the head end to the next forward tile (at its entry seam) and
        retrace the tail and occupied span. Returns False when there is no
        forward tile. Used only by reconcile_to_detected_occupancy, so it follows
        the points as they are set rather than the path, as every correction
        walk does (see `_entered_blocks_ahead`). It emits no
        movement events itself; when `swept` is given it appends the tile the
        head leaves, as (tile, entry_edge, exit_edge), for the caller to
        report."""
        work = self._work
        if work is None:
            return False
        if work.head_is_a:
            head_tile = work.end_a_tile
            head_entry = work.end_a_entry_edge
        else:
            head_tile = work.end_b_tile
            head_entry = work.end_b_entry_edge

        exit_edge = self._calculate_exit_edge(head_tile, head_entry, follow_path=False)
        if not exit_edge:
            return False
        next_tile = self._get_neighbor_tile(head_tile, exit_edge)
        if next_tile is None:
            return False
        next_entry = self._entry_edge_on_next(head_tile, exit_edge, next_tile)
        if swept is not None:
            swept.append((head_tile, head_entry, exit_edge))

        if work.head_is_a:
            work.end_a_tile = next_tile
            work.end_a_entry_edge = next_entry
            work.end_a_distance_cm = 0.0
        else:
            work.end_b_tile = next_tile
            work.end_b_entry_edge = next_entry
            work.end_b_distance_cm = 0.0

        self._retrace_from_head(train)
        return True

    def _retrace_from_head(self, train: "Train") -> None:
        """Retrace the tail and the occupied tiles and blocks from where the
        head now is.

        Rebuilds the ordered occupied_blocks from the new tiles, mirroring
        _update_block_occupancy but without its diagnostic transition
        bookkeeping (last_block_transitions / _last_occupied_blocks), which the
        reconciliation must not perturb.
        """
        work = self._work
        if work is None:
            return
        self._update_tail_position(train)
        self._update_occupied_tiles(train)
        ordered_blocks: List[str] = []
        seen: Set[str] = set()
        for tile in work.occupied_tiles or []:
            track = self.network.tracks.get(tile) if self.network else None
            bid = getattr(track, "block_id", None) if track else None
            if bid and bid not in seen:
                ordered_blocks.append(bid)
                seen.add(bid)
        work.occupied_blocks = ordered_blocks

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
            nc = self._notification_center
            emitted = False
            if nc is not None:
                try:
                    from notification_center import Kind as _Kind, train_ref as _tref
                    from runtime_payloads import ErrorRecord as _ErrRec

                    nc.emit(
                        source="TrainPhysics",
                        operation="_sync_block_tracker",
                        kind=_Kind.WARNING,
                        message=(
                            f"block tracker desync for {train.train_id}"
                        ),
                        entities=[_tref(train.train_id)],
                        error=_ErrRec(
                            "tile-derived blocks disagree with the local tracker",
                            "block_tracker_desync",
                        ),
                        event_time=time.time(),
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

        Per design_new.md "Ownership split", voltage emission delegates to
        `compute_track_voltage(...)` so physics does not inspect controller
        mode fields. The EB zero ceiling is already baked into the
        controller-published `PhysicsCommand.voltage_source_speed_kmh`, which
        maps to `lighting_voltage`; the cut-out's 0 V arrives as
        `voltage_source_track_voltage`.
        """
        power_controller = self._get_power_controller()
        if not power_controller:
            return

        voltage = self.compute_track_voltage(controller, self._get_speed_profile())
        self._set_voltage_for_blocks(train, voltage)

    def _set_voltage_for_blocks(self, train: "Train", voltage: float) -> None:
        """Energize every block this train stands on, plus the one block ahead it
        is about to enter, at the same `track_voltage` — signed per block by the
        way the train runs through it.

        The set is the train's occupied blocks, the blocks the detector confirms
        it is standing on, its `pending-entry block`, and the single next block
        of its reserved corridor (`_energized_blocks_for_train`). One block ahead
        is what it takes to cross an insulated boundary without a power gap, and
        it is also all that may be live: rail energized further ahead than the
        locomotive is authorized to be *this instant* is rail a train running
        faster than the model keeps accelerating over (design_new.md "Power",
        Only the next reserved block is pre-energized). A block that leaves this
        set is returned to 0V by SimulationController after every train has
        emitted, draining `BlockManager.take_blocks_to_deenergize()` together
        with this instance's `take_blocks_dropped_from_energized()`.

        `track_voltage` is a magnitude; a DC locomotive's direction is the
        polarity on its rails, so each block gets that magnitude signed by the
        block-relative direction the train runs through it — positive for
        `forward`, negative for `reverse` (design_new.md "Power", A block
        voltage carries a direction). The sign is resolved per block, not per
        train: each block's tile order is drawn on its own, so two blocks a
        train holds at once may run opposite ways against its line of travel.
        A block no source can fix a direction for is driven positive.
        """
        power_controller = self._get_power_controller()
        if not power_controller:
            return

        energized = self._energized_blocks_for_train(train)
        # Record what fell out of the set before driving the new one, so the
        # drain can zero a block this train stopped energizing for a reason
        # BlockManager never queued (see `_blocks_dropped_from_energized`).
        self._blocks_dropped_from_energized |= self._last_energized_blocks - energized
        self._blocks_dropped_from_energized -= energized
        self._last_energized_blocks = set(energized)

        directions = self._block_traversal_directions(energized)
        for block_id in energized:
            signed = -voltage if directions.get(block_id) == "reverse" else voltage
            power_controller.set_block_voltage(block_id, signed)

    def take_blocks_dropped_from_energized(self) -> Set[str]:
        """Return and clear the blocks this train stopped energizing since the
        last call, for `SimulationController`'s post-train de-energize drain.

        Reported rather than zeroed here because one block is one feed and the
        drain is the only place that can see every train: this instance knows
        only that *it* stopped driving the block, not whether another train
        picked it up in the same tick.
        """
        dropped = self._blocks_dropped_from_energized
        self._blocks_dropped_from_energized = set()
        return dropped

    def _block_traversal_directions(
        self, energized: Optional[Set[str]] = None
    ) -> Dict[str, str]:
        """Block-relative direction for each block this train runs through.

        Three sources, in this order. The active path's `resource_sequence` runs
        from the train's tail to its authority end, so its per-block directed
        tile slice covers the calculated span, the pending-entry block, and the
        whole reserved corridor in one read. A train driving without a path has
        no such record, so the directed span walk — ordered tail end to cab end
        — answers for the blocks it covers. A block in `energized` that neither
        covers is one the detector credits to the train beyond its calculated
        span; it takes the direction of the train's line of travel continued
        forward from the head into it (`_directions_along_line_of_travel`). A
        block none of the three can fix is absent from the mapping and the
        caller drives it positive (design_new.md "Power", A block voltage
        carries a direction).
        """
        directions: Dict[str, str] = {}
        if self.network is None:
            return directions
        blocks = self.network.blocks

        projection = getattr(self.controller, "path_projection", None)
        plan = getattr(projection, "received_path_plan", None)
        for entry in getattr(plan, "resource_sequence", None) or []:
            if getattr(entry, "resource_type", None) != "block":
                continue
            block = blocks.get(entry.resource_id)
            if block is None:
                continue
            resolved = block.block_direction_for_tile_walk(
                [(t.x, t.y) for t in (entry.path_tiles or [])]
            )
            if resolved is None:
                # One tile of the block on the walk fixes no direction — a
                # single-tile block always lands here. The path entry carries the
                # directed frame the walk could not supply, so the edge the
                # traversal leaves by decides it.
                resolved = block.block_direction_for_compass_exit(
                    getattr(entry, "exit_edge", None)
                )
            if resolved is not None:
                directions[entry.resource_id] = resolved

        work = self._work
        if work is None or not work.occupied_tiles:
            return directions
        # `occupied_tiles` is ordered end_a -> end_b and carries no head label;
        # the line of travel runs tail -> head, so the walk is that list
        # reversed when the cab selects end_a as the head.
        walk = list(work.occupied_tiles)
        if work.head_is_a:
            walk.reverse()
        head_tile = work.end_a_tile if work.head_is_a else work.end_b_tile
        head_entry_edge = (
            work.end_a_entry_edge if work.head_is_a else work.end_b_entry_edge
        )
        head_block_id = self.network.tile_to_block_id(*head_tile) if head_tile else None
        for block_id in work.occupied_blocks:
            if block_id in directions:
                continue
            block = blocks.get(block_id)
            if block is None:
                continue
            resolved = block.block_direction_for_tile_walk(walk)
            if resolved is None and block_id == head_block_id:
                # The same one-tile gap as the path branch, with no path to
                # supply the frame. The head's own entry edge supplies it, but
                # only for the block the head stands in — and the helper answers
                # only for single-tile blocks, which is the gap.
                resolved = block.block_direction_for_compass_entry(head_entry_edge)
            if resolved is not None:
                directions[block_id] = resolved

        leftover = {b for b in (energized or ()) if b not in directions}
        if leftover:
            directions.update(
                self._directions_along_line_of_travel(work, walk, leftover)
            )
        return directions

    def _directions_along_line_of_travel(
        self,
        work: _SpanWorkState,
        walk: List[Tuple[int, int]],
        block_ids: Set[str],
    ) -> Dict[str, str]:
        """Directions for `block_ids` read off the train's line of travel
        continued forward from its head: the tail-to-head span walk, extended by
        the tiles the head would reach next.

        A block the detector credits to a train just beyond its calculated span
        sits on that continuation, so the extended walk runs through it the way
        the locomotive is moving, and its sign keeps the locomotive going the
        way it was going. The extension stops once it has passed through
        `_LINE_OF_TRAVEL_BLOCKS` blocks beyond the head's own block. A block it
        never runs through two tiles of stays unresolved, and keeps the
        positive default.
        """
        if work.head_is_a:
            tile, distance, entry = (
                work.end_a_tile,
                work.end_a_distance_cm,
                work.end_a_entry_edge,
            )
        else:
            tile, distance, entry = (
                work.end_b_tile,
                work.end_b_distance_cm,
                work.end_b_entry_edge,
            )
        head_block_id = self._block_of_tile(tile) if tile else None
        extended = list(walk)
        passed: List[str] = []
        for next_tile, _travelled in self._walk_forward_tiles(
            tile, distance, entry, float("inf")
        ):
            block_id = self._block_of_tile(next_tile)
            if (
                block_id is not None
                and block_id != head_block_id
                and block_id not in passed
            ):
                if len(passed) == _LINE_OF_TRAVEL_BLOCKS:
                    break
                passed.append(block_id)
            extended.append(next_tile)

        out: Dict[str, str] = {}
        for block_id in block_ids:
            block = self.network.blocks.get(block_id) if self.network else None
            if block is None:
                continue
            resolved = block.block_direction_for_tile_walk(extended)
            if resolved is not None:
                out[block_id] = resolved
        return out

    def _standing_blocks_for_train(self, train: "Train") -> Set[str]:
        """The blocks this train is standing on: its calculated span, plus the
        blocks the detector confirms it is standing on.

        Span blocks come from the movement scratchpad (the physics-canonical view
        of where the train is this tick).

        The detector half exists because the two views can disagree about where
        the train is, and the rail must never go dead under a locomotive on
        either reading. A block the detector confirms this train is standing on —
        one attribution handed it because it neighbours the span (design_new.md
        "Power", Attribution) — is not in the calculated span and need not be
        reserved, so without this it would carry no voltage at all and the
        locomotive standing on it could not move. It is read by scanning
        `occupied_by` rather than from an index, because that field has writers
        outside `BlockManager` (adoption's re-key, initial placement) and a
        hand-maintained reverse index would drift from them.
        """
        standing = self._get_blocks_for_train(train)
        if self.network is None:
            return standing
        train_id = train.train_id
        standing |= {
            block_id
            for block_id, block in self.network.blocks.items()
            if getattr(block, "occupied_by", None) == train_id
        }
        return standing

    def _reserved_blocks_for_train(self, train: "Train") -> Set[str]:
        """This train's reserved-ahead corridor — the dispatcher-owned future
        authority, read from the BlockManager's per-train reserved index."""
        if self.network is None:
            return set()
        train_id = train.train_id
        block_manager = getattr(self.network, "block_manager", None)
        if block_manager is not None:
            return block_manager.reserved_blocks_for_train(train_id)
        # Fallback for a network with no BlockManager wired (isolated unit
        # tests). Production networks always create one (RailwayNetwork).
        return {
            block_id
            for block_id, block in self.network.blocks.items()
            if getattr(block, "reserved_for_train", None) == train_id
        }

    def _resolve_next_reserved_block(
        self, train: "Train", standing: Set[str]
    ) -> Optional[str]:
        """The first block of this train's reserved corridor the head reaches by
        going forward, or None when it has no reserved block ahead of it.

        The corridor is a set, not an order, so the order comes from the rail:
        the same forward tile walk the seam cap uses, which follows the points as
        they are actually aligned rather than as a plan describes them. The walk
        passes over the blocks the train is already standing on — a corridor
        block can still be occupied for the ticks a long consist straddles it —
        and answers with the first one ahead that is reserved and not yet stood
        on.

        A train that has run past its authority has nothing reserved ahead and
        gets None, which is exactly the intent: the rail in front of a train
        beyond its authority end is dead (design_new.md "Power", Only the next
        reserved block is pre-energized).
        """
        work = self._work
        if work is None or self.network is None:
            return None
        reserved = self._reserved_blocks_for_train(train)
        if not reserved:
            return None

        if work.head_is_a:
            tile, distance, entry = (
                work.end_a_tile,
                work.end_a_distance_cm,
                work.end_a_entry_edge,
            )
        else:
            tile, distance, entry = (
                work.end_b_tile,
                work.end_b_distance_cm,
                work.end_b_entry_edge,
            )

        for next_tile, _travelled in self._walk_forward_tiles(
            tile, distance, entry, float("inf")
        ):
            block_id = self._block_of_tile(next_tile)
            if block_id is None or block_id in standing:
                continue
            return block_id if block_id in reserved else None
        return None

    @property
    def next_reserved_block(self) -> Optional[str]:
        """The one reserved-ahead block this train energizes, or None.

        Resolved at emission and read back by `SimulationController` so the
        de-energize drain holds the same block the emission side drives
        (design_new.md "Power", De-energize against the same source).
        """
        return self._next_reserved_block

    def _energized_blocks_for_train(self, train: "Train") -> Set[str]:
        """Every block that carries this train's `track_voltage` this tick.

        Per design_new.md "Power", Block power-controller role: the train drives
        its one voltage onto the blocks it stands on and onto the one block in
        front of it. A block is one power feed — it owns the one `track voltage`
        its member segments share — so the set is:

          - the blocks it is standing on (`_standing_blocks_for_train`: the
            calculated span, plus what the detector confirms under it),
          - its `pending-entry block`, the detected block a cap is holding its
            head out of. Its calculated span stops short of that block, so
            without this the rail the locomotive has to roll onto to release its
            own cap would be dead,
          - the next block of its reserved corridor
            (`_resolve_next_reserved_block`), so it crosses the insulated
            boundary in front of it without a power gap.

        **Only the next reserved block, never the whole corridor.** One block
        ahead is all it takes to close the boundary gap, and it is also all that
        may be live. A train whose speed profile runs fast reaches the seam
        before the model says it has, and every further block of live rail in
        front of it is track it keeps accelerating over while the app still
        believes it is a block back. Energizing one block bounds that overrun to
        one block, and puts dead rail immediately beyond the authority end.

        An unidentified or ghosted (`presence_unknown`) train commands no
        `track_voltage` at all, so its set is empty and a block held only by such
        a train is driven to `0` by the de-energize drain. Putting voltage on the
        rail under a train the app cannot steer or stop — one it cannot identify,
        or one no detector can find — is the hazard this rule exists to contain
        (design_new.md "Power", A ghosted or unidentified train's emission set is
        empty for that tick).
        """
        if getattr(train, "is_unidentified", False) or getattr(
            self.controller, "presence_unknown", False
        ):
            self._next_reserved_block = None
            self._blocks_past_authority = set()
            return set()
        standing = self._standing_blocks_for_train(train)
        # The walk passes over only the blocks the train stands on. The
        # pending-entry block is the block in front of the head, not under it:
        # handing it to the walk as stood-on made the walk skip it and energize
        # the reserved block beyond as well, two blocks ahead of the head.
        self._next_reserved_block = self._resolve_next_reserved_block(
            train, standing
        )
        energized = set(standing)
        if self._pending_entry_block is not None:
            energized.add(self._pending_entry_block)
        if self._next_reserved_block is not None:
            energized.add(self._next_reserved_block)
        self._blocks_past_authority = self._detected_blocks_past_authority(
            train, energized
        )
        return energized - self._blocks_past_authority

    @property
    def blocks_past_authority(self) -> Set[str]:
        """The blocks this emission left unpowered because they lie past this
        train's authority end, read back by `SimulationController` so the
        de-energize drain does not hold them (design_new.md "Power", A block
        past the authority end is never energized)."""
        return set(self._blocks_past_authority)

    def _detected_blocks_past_authority(
        self, train: "Train", energized: Set[str]
    ) -> Set[str]:
        """Of `energized`, the blocks the detector credits to this train that lie
        past the end of its authority.

        A train past its authority end is one that holds a path plan but has no
        reserved block ahead of its head. The overrun block is the block on its
        head side, so attribution credits it to this train rather than to an
        unidentified train (design_new.md "Block occupancy detection",
        Touching). A block such a train stands on only by the
        detector's say-so, and that is not on its authorized path, is a block it
        overran onto. The rail there must stay dead: powering it keeps pushing
        a locomotive that has already run past where it may go, and the block's
        direction cannot be read off a path or a span that stop short of it, so
        whichever sign it got could drive the locomotive either way.
        """
        projection = getattr(self.controller, "path_projection", None)
        plan = getattr(projection, "received_path_plan", None)
        if plan is None or self._next_reserved_block is not None:
            return set()
        authorized = {
            entry.resource_id
            for entry in (plan.resource_sequence or [])
            if getattr(entry, "resource_type", None) == "block"
        }
        span_blocks = self._get_blocks_for_train(train)
        return {
            block_id
            for block_id in energized
            if block_id not in authorized and block_id not in span_blocks
        }

    # =========================================================================
    # Track Geometry Helpers
    # =========================================================================

    def _get_tile_length_cm(self, tile: Tuple[int, int]) -> float:
        """Get length of a tile in cm."""
        if not self.network:
            return 100.0  # Default fallback

        return self.network._get_tile_length_cm(tile[0], tile[1])

    def _calculate_exit_edge(
        self, tile: Tuple[int, int], entry_edge: str, follow_path: bool = True
    ) -> Optional[str]:
        """
        Calculate exit edge based on track geometry and entry edge.

        Uses the 6-bit track system to determine valid paths through the tile.
        With `follow_path` False the exit follows the points as they are set
        (`Switch.position`) and ignores the train's path, which is the
        connectivity walk attribution's adjacency uses (design_new.md "Block
        occupancy detection", Touching) and the walk every detector-driven span
        correction uses (The correction walks through the train's own blocks
        only). With `follow_path` True a train that holds a path follows it, and
        its last tile has no exit, so ordinary movement stops at the authority
        end.
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

        if not follow_path:
            return possible_exits[0]
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
