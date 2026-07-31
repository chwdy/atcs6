"""
Block — power + occupancy + reservation + availability unit.

Definition (design_new.md "block"):
A `Block` is the power + occupancy + reservation unit: a switch-free connected
run of one or more `segment`s that share one power feed (one `track voltage`)
and admit one train at a time. It is the unit the dispatcher reserves and the
train occupies. It groups its geometry through `segments` (its member
`Segment`s) and owns the occupancy, reservation, availability, and power state.

Key concepts:
- Geometry (`ordered_tiles`, `length_cm`, `min_radius_cm`, `grade_percent`,
  `speed_limit_kmh`) lives on the member `Segment`s. The block exposes those as
  derived reads (concatenate / sum / min across its segments) so callers can
  keep reading `block.<geom>`. A single-segment block reads through to that one
  segment unchanged.
- `state` is computed from underlying primitive fields. Priority order:
  OCCUPIED > CAUTION > RESERVED > FREE.
- Underlying primitives: `occupied_by`, `caution: Optional[CautionState]`,
  `reservation: Optional[BlockReservation]`, `disabled`. Convenience reads
  `_is_caution`, `caution_exit_edge`, `reserved_by_path`, `reserved_direction`,
  `reserved_for_train` are all derived `@property` reads.
- `Block` is a data container; `BlockManager` is the sole mutator for physical
  and CAUTION transitions.
"""

from dataclasses import dataclass
from typing import List, NamedTuple, Optional, Dict, Any, Set
from enums import BlockState
from track_resource import TrackResource
from segment import Segment, TileEntry


# Block-relative directions used in the static block-level travel policy.
_BLOCK_RELATIVE_DIRECTIONS: Set[str] = {"forward", "reverse"}


# `ordered_tiles` entries are `TileEntry` (x, y, entry_edge) records owned by
# `Segment`. `BlockTileEntry` remains as an alias so existing importers keep
# working.
BlockTileEntry = TileEntry


class CautionState(NamedTuple):
    """Block caution state — physical safety-margin record.

    Per design_new.md "Block model" — `block.caution` is a single Optional
    record so the all-or-nothing rule (the flag and the direction stamp are
    written together by `BlockManager.on_block_tail_vacated()` and cleared
    together by `_reevaluate_caution_blocks()` / `release_path_block_reservations(completed=True)`)
    is structural rather than enforced by prose.

    Convenience reads `_is_caution` (= `caution is not None`) and
    `caution_exit_edge` (= `caution.exit_edge` or `None`) are exposed as
    `@property` reads on `Block`.
    """

    exit_edge: str  # "N" | "E" | "S" | "W"


@dataclass
class BlockReservation:
    """One active path's dispatcher reservation on a Block.

    Per design_new.md "Block model" — the three fields are always written
    together by `BlockManager.reserve_blocks_for_path(...)` and cleared
    together by `Block.release_path_reservation()`, so they live as one
    Optional record rather than three parallel optional scalars. The
    convenience reads `reserved_by_path`, `reserved_for_train`, and
    `reserved_direction` are derived from this record.
    """

    path_id: str
    train_id: str
    exit_edge: str  # "N" | "E" | "S" | "W"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path_id": self.path_id,
            "train_id": self.train_id,
            "exit_edge": self.exit_edge,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BlockReservation":
        return cls(
            path_id=str(data["path_id"]),
            train_id=str(data["train_id"]),
            exit_edge=str(data["exit_edge"]),
        )


class Block(TrackResource):
    """
    Power + occupancy + reservation + availability unit (design_new.md "block").

    Groups one or more `Segment`s (its geometry) and owns the runtime state:
    occupancy, reservation, CAUTION, availability, and power. It represents the
    minimum control unit for train movement.
    """

    def __init__(
        self,
        block_id: str,
        name: str = "",
        length_cm: float = 0.0,
        min_radius_cm: Optional[float] = None,
        grade_percent: Optional[float] = None,
        speed_limit_kmh: Optional[float] = None,
        is_holding_point: bool = False,
        polarity_reversed: bool = False,
        occupied_seconds: float = 0.0,
        occupied_by: Optional[str] = None,
        caution: Optional[CautionState] = None,
        reservation: Optional[BlockReservation] = None,
        disabled: bool = False,
        manual: bool = False,
        current_voltage: float = 0.0,
        allowed_travel_directions: Optional[Set[str]] = None,
        ordered_tiles: Optional[List[TileEntry]] = None,
        segments: Optional[List[Segment]] = None,
    ) -> None:
        self.block_id = block_id  # Unique ID (e.g., "BLK_001")
        self.name = name  # Human-readable name

        # Static config flag: operator-marked safe wait place (design_new.md
        # "Block" field schema). Never written by runtime; its only consumer is
        # the best-effort truncation holding-point preference (item 11 under
        # "Path And PathDispatcher").
        self.is_holding_point = is_holding_point

        # Static config flag: this block's two rails are wired the opposite way
        # round from the nominal-positive sense its geometry implies, so a
        # positive commanded voltage must drive the other rail. Electrical only —
        # it is not `allowed_travel_directions`, which is a routing policy and
        # never reaches the hardware. Read by the track driver when it resolves
        # this block's rail binding (design_new.md "Power -> Track polarity"), so
        # a change takes effect on the next write with no session rebuild. The
        # block is the unit here: a block is fed as one, so the flag covers every
        # member segment and a segment has no polarity of its own.
        self.polarity_reversed = polarity_reversed

        # Maintenance counter: total simulated seconds a train has occupied this
        # block. Written once per tick by SimulationController.update(...) and
        # persisted with the layout (see design_new.md "Maintenance counters").
        self.occupied_seconds = occupied_seconds

        # Underlying primitive state fields. `state` is a computed @property
        # derived from these (see below).
        self.occupied_by = occupied_by  # Train ID currently physically present
        # Single Optional caution record. The `_is_caution` and
        # `caution_exit_edge` reads are derived @properties below.
        self.caution = caution
        # Single Optional reservation record. The reserved_by_path,
        # reserved_for_train, reserved_direction reads are derived @properties.
        self.reservation = reservation
        self.disabled = disabled  # Static availability flag (physical-absence)
        self.manual = manual  # Virtual-disable flag (manual_override bypasses)

        # Voltage control (DC analog control)
        self.current_voltage = current_voltage

        # Static block-level travel policy. Defaults to both directions allowed.
        self.allowed_travel_directions: Set[str] = (
            set(allowed_travel_directions)
            if allowed_travel_directions is not None
            else {"forward", "reverse"}
        )

        # Member segments own this block's geometry. A block created with
        # geometry keywords (length_cm / ordered_tiles / ...) gets one auto
        # segment carrying them; the geometry reads below aggregate across all
        # members so a multi-segment block just works.
        if segments is not None:
            self.segments: List[Segment] = list(segments)
        else:
            self.segments = [
                Segment(
                    segment_id=block_id,
                    name=name,
                    length_cm=length_cm,
                    min_radius_cm=min_radius_cm,
                    grade_percent=grade_percent,
                    speed_limit_kmh=speed_limit_kmh,
                    ordered_tiles=list(ordered_tiles) if ordered_tiles else [],
                )
            ]

    # ========================================================================
    # TrackResource identity
    # ========================================================================

    @property
    def resource_id(self) -> str:
        return self.block_id

    @property
    def resource_type(self) -> str:
        return "block"

    # ========================================================================
    # Derived geometry reads (canonical storage lives on member `Segment`s)
    # ========================================================================

    @property
    def segment_ids(self) -> List[str]:
        """Ids of this block's member segments, in order."""
        return [s.segment_id for s in self.segments]

    @property
    def ordered_tiles(self) -> List[TileEntry]:
        """Canonical local traversal order across all member segments.

        Concatenates each segment's `ordered_tiles`; `ordered_tiles[0]` is the
        forward-entry anchor for the block.
        """
        out: List[TileEntry] = []
        for seg in self.segments:
            out.extend(seg.ordered_tiles)
        return out

    @ordered_tiles.setter
    def ordered_tiles(self, value: List[TileEntry]) -> None:
        self._geometry_write_target("ordered_tiles").ordered_tiles = list(value)

    @property
    def length_cm(self) -> float:
        """Total track length: the sum of the member segments' lengths."""
        return sum(s.length_cm for s in self.segments)

    @length_cm.setter
    def length_cm(self, value: float) -> None:
        self._geometry_write_target("length_cm").length_cm = value

    @property
    def min_radius_cm(self) -> Optional[float]:
        """Tightest curve radius across member segments (None if unset)."""
        vals = [s.min_radius_cm for s in self.segments if s.min_radius_cm is not None]
        return min(vals) if vals else None

    @min_radius_cm.setter
    def min_radius_cm(self, value: Optional[float]) -> None:
        self._geometry_write_target("min_radius_cm").min_radius_cm = value

    @property
    def speed_limit_kmh(self) -> Optional[float]:
        """Lowest segment speed limit across member segments (None if unset)."""
        vals = [
            s.speed_limit_kmh for s in self.segments if s.speed_limit_kmh is not None
        ]
        return min(vals) if vals else None

    @speed_limit_kmh.setter
    def speed_limit_kmh(self, value: Optional[float]) -> None:
        self._geometry_write_target("speed_limit_kmh").speed_limit_kmh = value

    @property
    def grade_percent(self) -> Optional[float]:
        """Track gradient. First member segment's grade that is set (None if
        none). Single-segment blocks read through to that segment."""
        for seg in self.segments:
            if seg.grade_percent is not None:
                return seg.grade_percent
        return None

    @grade_percent.setter
    def grade_percent(self, value: Optional[float]) -> None:
        self._geometry_write_target("grade_percent").grade_percent = value

    def _geometry_write_target(self, field: str) -> Segment:
        """The member segment a block-level geometry write lands on.

        Geometry belongs to a `Segment`, so a write through the block's derived
        geometry property is only well-defined when the block has exactly one
        member: the block and that segment are then the same run. A block that
        groups several segments has no single place to put the value — the
        reads are a concatenation (`ordered_tiles`), a sum (`length_cm`) or a
        minimum (`speed_limit_kmh`, `min_radius_cm`), none of which invert — so
        the write is rejected and the caller must address the segment it means.
        """
        if len(self.segments) != 1:
            raise ValueError(
                f"Block {self.block_id} groups {len(self.segments)} segments, "
                f"so '{field}' has no single block-level value to write. "
                f"Geometry lives on the member segments "
                f"({', '.join(self.segment_ids) or 'none'}); write it there."
            )
        return self.segments[0]

    # ========================================================================
    # Derived reservation reads
    # ========================================================================

    @property
    def reserved_by_path(self) -> Optional[str]:
        """Path ID currently reserving this block, or None."""
        return self.reservation.path_id if self.reservation is not None else None

    @property
    def reserved_for_train(self) -> Optional[str]:
        """Train ID this reservation is held for, or None."""
        return self.reservation.train_id if self.reservation is not None else None

    @property
    def reserved_direction(self) -> Optional[str]:
        """Reserved exit edge ('N'|'E'|'S'|'W'), or None."""
        return self.reservation.exit_edge if self.reservation is not None else None

    # ========================================================================
    # Derived caution reads
    # ========================================================================

    @property
    def _is_caution(self) -> bool:
        """True iff this block is currently in physical CAUTION."""
        return self.caution is not None

    @property
    def caution_exit_edge(self) -> Optional[str]:
        """Departing train's exit edge ('N'|'E'|'S'|'W') while in CAUTION, else None."""
        return self.caution.exit_edge if self.caution is not None else None

    # ========================================================================
    # Computed state property
    # ========================================================================

    @property
    def state(self) -> BlockState:
        """Compute current block state from underlying primitives.

        Priority order (per spec): OCCUPIED > CAUTION > RESERVED > FREE.
        """
        if self.occupied_by is not None:
            return BlockState.OCCUPIED
        if self.caution is not None:
            return BlockState.CAUTION
        if self.reservation is not None:
            return BlockState.RESERVED
        return BlockState.FREE

    # ========================================================================
    # Serialization
    # ========================================================================

    def to_dict(self) -> Dict[str, Any]:
        """Convert block to dictionary for the live frontend payload.

        Includes runtime state (occupied_by, caution, reservation, voltage,
        computed `state`) for UI rendering. Persistence does NOT use this
        path; see `to_layout_dict()` instead.
        """
        return {
            "block_id": self.block_id,
            "name": self.name,
            "length_cm": self.length_cm,
            "min_radius_cm": self.min_radius_cm,
            "grade_percent": self.grade_percent,
            "is_holding_point": self.is_holding_point,
            "speed_limit_kmh": self.speed_limit_kmh,
            "occupied_seconds": self.occupied_seconds,
            "state": self.state.value,
            "occupied_by": self.occupied_by,
            "caution": (
                {"exit_edge": self.caution.exit_edge}
                if self.caution is not None
                else None
            ),
            # Convenience reads (derived from caution).
            "_is_caution": self._is_caution,
            "caution_exit_edge": self.caution_exit_edge,
            "reservation": (
                self.reservation.to_dict() if self.reservation is not None else None
            ),
            # Convenience reads (derived from reservation).
            "reserved_by_path": self.reserved_by_path,
            "reserved_direction": self.reserved_direction,
            "reserved_for_train": self.reserved_for_train,
            "disabled": self.disabled,
            "manual": self.manual,
            "current_voltage": self.current_voltage,
            "allowed_travel_directions": sorted(self.allowed_travel_directions),
            "polarity_reversed": self.polarity_reversed,
            "ordered_tiles": [
                [int(x), int(y), str(edge)] for (x, y, edge) in self.ordered_tiles
            ],
            # Member segments in `segment_ids` order. The block's geometry keys
            # above are the derived aggregate reads; these carry each member's
            # own length / speed limit / radius so a multi-segment block keeps
            # its per-segment geometry.
            "segment_ids": self.segment_ids,
            "segments": [s.to_dict() for s in self.segments],
        }

    def to_layout_dict(self) -> Dict[str, Any]:
        """Layout-only serialization used by persistence.

        Per design_new.md Out of Scope ("Persistence / save-load of runtime
        state | In-memory only"), this writes block geometry and config only.
        Runtime fields (occupied_by, caution, reservation, current_voltage,
        computed state) are excluded.

        Member segments nest under the block, so one `blocks` key carries the
        whole grouping — the block's power/occupancy identity plus each member
        segment's own geometry.
        """
        return {
            "block_id": self.block_id,
            "name": self.name,
            "length_cm": self.length_cm,
            "min_radius_cm": self.min_radius_cm,
            "grade_percent": self.grade_percent,
            "is_holding_point": self.is_holding_point,
            "speed_limit_kmh": self.speed_limit_kmh,
            "occupied_seconds": self.occupied_seconds,
            "disabled": self.disabled,
            "manual": self.manual,
            "allowed_travel_directions": sorted(self.allowed_travel_directions),
            "polarity_reversed": self.polarity_reversed,
            "ordered_tiles": [
                [int(x), int(y), str(edge)] for (x, y, edge) in self.ordered_tiles
            ],
            "segment_ids": self.segment_ids,
            "segments": [s.to_layout_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Block":
        """Reconstruct block from a layout snapshot.

        Runtime fields (`occupied_by`, `caution`, `reservation`,
        `current_voltage`, computed `state`) are not loaded from disk —
        they default-initialize and the engine populates them at run time.
        Legacy snapshots that carry those keys are silently ignored.

        Member segments come from the nested `segments` list. A snapshot
        written before segments were nested carries the block's geometry inline
        instead; that block loads as a one-segment block built from the inline
        geometry, which is the default one-feed-per-segment grouping.
        """
        raw_segments = data.get("segments")
        if raw_segments:
            # Same rule as the inline-geometry path below: length must be
            # supplied explicitly (0 is allowed), with no silent default. The
            # block's own `length_cm` is the sum across its members, so the
            # snapshot must carry it for the block as well as for each segment.
            if "length_cm" not in data:
                raise ValueError(
                    f"Block.from_dict: block '{data.get('block_id')}' is missing "
                    "required 'length_cm'. Length must be supplied explicitly (0 "
                    "is allowed); there is no silent default."
                )
            segments = [Segment.from_dict(s) for s in raw_segments]
            atd_nested = data.get("allowed_travel_directions")
            return cls(
                block_id=data["block_id"],
                name=data.get("name", ""),
                is_holding_point=bool(data.get("is_holding_point", False)),
                polarity_reversed=bool(data.get("polarity_reversed", False)),
                disabled=bool(data.get("disabled", False)),
                manual=bool(data.get("manual", False)),
                allowed_travel_directions=(
                    {str(d) for d in atd_nested}
                    if atd_nested is not None
                    else {"forward", "reverse"}
                ),
                occupied_seconds=float(data.get("occupied_seconds", 0.0)),
                segments=segments,
            )

        atd = data.get("allowed_travel_directions")
        if atd is None:
            allowed = {"forward", "reverse"}
        else:
            allowed = {str(d) for d in atd}

        raw_tiles = data.get("ordered_tiles") or []
        ordered_tiles: List[TileEntry] = []
        for entry in raw_tiles:
            if isinstance(entry, dict):
                x = int(entry["x"])
                y = int(entry["y"])
                edge = str(entry["entry_edge"])
            else:
                x = int(entry[0])
                y = int(entry[1])
                edge = str(entry[2])
            ordered_tiles.append(TileEntry(x=x, y=y, entry_edge=edge))

        if "length_cm" not in data:
            raise ValueError(
                f"Block.from_dict: block '{data.get('block_id')}' is missing "
                "required 'length_cm'. Length must be supplied explicitly (0 is "
                "allowed); there is no silent default."
            )
        return cls(
            block_id=data["block_id"],
            name=data.get("name", ""),
            length_cm=float(data["length_cm"]),
            min_radius_cm=data.get("min_radius_cm"),
            grade_percent=data.get("grade_percent"),
            is_holding_point=bool(data.get("is_holding_point", False)),
            polarity_reversed=bool(data.get("polarity_reversed", False)),
            speed_limit_kmh=data.get("speed_limit_kmh", data.get("max_speed_kmh")),
            disabled=bool(data.get("disabled", False)),
            manual=bool(data.get("manual", False)),
            allowed_travel_directions=allowed,
            ordered_tiles=ordered_tiles,
            occupied_seconds=float(data.get("occupied_seconds", 0.0)),
        )

    # ========================================================================
    # Predicates
    # ========================================================================

    def is_free(self) -> bool:
        """Check if block is free and available for use."""
        return self.state == BlockState.FREE and not self.disabled

    def is_reserved(self) -> bool:
        """Check if block is reserved (and not occupied / in caution)."""
        return self.state == BlockState.RESERVED

    def can_enter(self, train_id: str) -> bool:
        """Check if `train_id` may enter this block under the v1 state model.

        A block accepts entry when it is FREE, or RESERVED for the same train,
        or already OCCUPIED by the same train (idempotent re-entry). CAUTION
        blocks do not block reservation but they are not freely enterable
        until the cascade clears them.
        """
        if self.disabled:
            return False
        st = self.state
        if st == BlockState.FREE:
            return True
        if st == BlockState.RESERVED and self.reserved_for_train == train_id:
            return True
        if st == BlockState.OCCUPIED and self.occupied_by == train_id:
            return True
        return False

    def is_available_for_path(
        self, path_id: str, train_id: Optional[str] = None
    ) -> bool:
        """Check if block is available for a path (validation helper).

        A block is available when:
        - It is FREE
        - It is in CAUTION and is either unreserved or reserved by this same
          path (a physically-free CAUTION block has `occupied_by is None`)
        - It is RESERVED by this same path
        - It is OCCUPIED by the train that will use this path

        `CAUTION` by itself does not make a block unavailable. Per
        design_new.md "`CAUTION` vs reservation independence", caution is
        orthogonal to reservation: it caps the following train's speed via
        `TrainController.CAUTION_SPEED_LIMIT_KMH` but does not block an
        otherwise-legal reservation. This mirrors the eligibility predicate
        in `BlockManager.reserve_blocks_for_path(...)`, which never rejects
        on caution — only on occupancy or a reservation held by another path.
        A CAUTION block reserved by a *different* path is unavailable (it
        falls through to the reserved-by-other case).
        """
        if self.disabled:
            return False
        st = self.state
        if st == BlockState.FREE:
            return True
        if st == BlockState.CAUTION and (
            self.reserved_by_path is None or self.reserved_by_path == path_id
        ):
            return True
        if st == BlockState.RESERVED and self.reserved_by_path == path_id:
            return True
        if (
            st == BlockState.OCCUPIED
            and train_id is not None
            and self.occupied_by == train_id
        ):
            return True
        return False

    def excludes_opposing_traversal(self) -> bool:
        """True when no train coming the other way can reach this block, so a
        follower stopped just inside it cannot foul an opposing move:

        - the block is one-way (`allowed_travel_directions` restricted to a
          single direction), which structurally has no opposing traversal; or
        - the block is in CAUTION — a leader has just passed forward through it,
          committing the corridor to one-directional following use and still
          occupying the block ahead in the protect direction, so no opposing
          move can be routed up to it.

        Canonical home for that exemption: best-effort truncation
        (`PathDispatcher._terminal_fouls_two_way_switch_block`) consults this
        rather than re-checking `caution` / `allowed_travel_directions` at the
        call site, so both exemptions travel with the block.
        """
        if self.caution is not None:
            return True
        atd = self.allowed_travel_directions
        return atd is not None and len(atd) < 2

    # ========================================================================
    # Mutators - direct primitive writes only.
    # `BlockManager` is the sole legitimate caller for physical and CAUTION
    # transitions; these helpers exist so the manager and serialization can
    # write through one named call rather than poking primitives in many sites.
    # ========================================================================

    def occupy(self, train_id: str) -> bool:
        """Mark this block as physically occupied by `train_id`.

        Disabled blocks reject occupancy. Idempotent for the same train.
        """
        if self.disabled:
            return False
        if self.occupied_by is not None and self.occupied_by != train_id:
            return False
        self.occupied_by = train_id
        # Clean physical entry overrides any prior caution metadata.
        self.caution = None
        return True

    def release(self) -> None:
        """Clear physical occupancy without touching reservation fields.

        This is a primitive-level helper; `BlockManager` decides whether
        a release should also raise CAUTION via `on_block_tail_vacated()`.
        """
        self.occupied_by = None

    def set_to_caution(self, caution_exit_edge: Optional[str] = None) -> None:
        """Mark this block as in CAUTION with the departing-train exit edge.

        Per design_new.md "Block model" — caution is a single Optional record
        carrying exactly `exit_edge`. The state machine treats CAUTION as an
        independent safety state. When `caution_exit_edge` is None and no
        prior caution record exists, an empty-edge placeholder is written;
        `_reevaluate_caution_blocks()` clears any direction-less placeholder
        on the next pass.
        """
        if caution_exit_edge is not None:
            self.caution = CautionState(exit_edge=caution_exit_edge)
        elif self.caution is None:
            # No prior record and no direction supplied. Direction-less
            # CAUTION is a legacy admin/test path; the placeholder empty
            # edge keeps the composite record well-formed and is cleared
            # by the next caution reevaluation.
            self.caution = CautionState(exit_edge="")

    def clear(self) -> None:
        """Clear the CAUTION record.

        Does not touch `reserved_by_path` or `occupied_by` - those are
        independent concerns owned by separate manager calls.
        """
        self.caution = None

    # ========================================================================
    # Path Reservation Helpers (Internal)
    #
    # These are private helpers; BlockManager is the only legitimate caller.
    # They never check or mutate physical occupancy / caution fields.
    # ========================================================================

    def reserve_for_path(
        self,
        path_id: str,
        train_id: str,
        direction: Optional[str] = None,
    ) -> None:
        """Internal: write the path-reservation record atomically.

        Called only by `BlockManager.reserve_blocks_for_path(...)`. Stores
        a single `BlockReservation` (NOT touching `occupied_by`, which is
        physical-only). The `direction` argument may be a compass exit_edge
        ("N"/"E"/"S"/"W") or a block-relative "forward"/"reverse"; we
        normalize block-relative values into the corresponding compass exit
        edge using `ordered_tiles` adjacency.
        """
        exit_edge = self._resolve_reservation_direction(direction)
        if exit_edge is None:
            raise ValueError(
                f"Block {self.block_id} reservation requires a non-null direction"
            )
        self.reservation = BlockReservation(
            path_id=path_id, train_id=train_id, exit_edge=exit_edge
        )

    def release_path_reservation(self) -> None:
        """Internal: clear the path-reservation record.

        Called only by `BlockManager.release_path_block_reservations(...)`.
        Clears `block.reservation` with no state guard. MUST NOT touch
        `caution_exit_edge` or `_is_caution` (those are physical-lifecycle
        fields owned by `BlockManager`).
        """
        self.reservation = None

    # ------------------------------------------------------------------
    # Direction normalization
    # ------------------------------------------------------------------

    def block_direction_for_compass_exit(
        self, exit_edge: Optional[str]
    ) -> Optional[str]:
        """Inverse of `_resolve_reservation_direction`: given a compass exit
        edge (`"N"/"E"/"S"/"W"`), return this block's relative direction
        (`"forward"` or `"reverse"`). Returns `None` when the input is
        missing/unrecognized or the block lacks geometry to disambiguate.

        Used by `PathDispatcher.try_complete_active_path(...)` to verify
        that the terminal authority traversal direction matches
        `Path.requested_destination_direction` (block-relative).
        """
        if exit_edge is None:
            return None
        upper = str(exit_edge).strip().upper()
        if upper not in ("N", "E", "S", "W"):
            return None
        forward_compass = self._resolve_reservation_direction("forward")
        if forward_compass is not None and upper == forward_compass:
            return "forward"
        reverse_compass = self._resolve_reservation_direction("reverse")
        if reverse_compass is not None and upper == reverse_compass:
            return "reverse"
        return None

    def block_direction_for_compass_entry(
        self, entry_edge: Optional[str]
    ) -> Optional[str]:
        """Given a compass ENTRY edge (`"N"/"E"/"S"/"W"`), return this block's
        relative direction (`"forward"` or `"reverse"`), or `None` when the
        input is missing/unrecognized or the block lacks geometry.

        The entry-keyed companion to `block_direction_for_compass_exit`: a
        single tile entered through edge `E` exits through its opposite, so the
        verdict equals `block_direction_for_compass_exit(opposite(E))`. For a
        multi-tile block the entry edge alone does not fix the direction (the
        caller decides by entry tile instead), so this returns `None` there.
        Canonical home for the single-tile entry-edge-vs-anchor direction rule;
        `RailwayPathfinder` routes its single-tile branches through here rather
        than re-deriving the anchor.
        """
        if entry_edge is None:
            return None
        upper = str(entry_edge).strip().upper()
        if upper not in ("N", "E", "S", "W"):
            return None
        if not self.ordered_tiles or len(self.ordered_tiles) != 1:
            return None
        opposite = {"N": "S", "S": "N", "E": "W", "W": "E"}
        return self.block_direction_for_compass_exit(opposite[upper])

    def _resolve_reservation_direction(
        self, direction: Optional[str]
    ) -> Optional[str]:
        """Normalize a reservation `direction` argument to a compass exit edge.

        - None -> None
        - Compass "N"/"E"/"S"/"W" (any case) -> uppercase compass
        - Block-relative "forward"/"reverse" -> compass exit edge derived from
          `ordered_tiles`. For multi-tile blocks we use adjacency between the
          last and second-to-last entries; for single-tile blocks we use the
          forward-entry anchor (`ordered_tiles[0].entry_edge`) and its
          opposite. Falls back to None if geometry is missing.
        """
        if direction is None:
            return None
        d = str(direction).strip()
        upper = d.upper()
        if upper in ("N", "E", "S", "W"):
            return upper
        lower = d.lower()
        if lower not in _BLOCK_RELATIVE_DIRECTIONS:
            return upper if upper else None

        ordered = self.ordered_tiles
        if not ordered:
            return None

        if len(ordered) == 1:
            entry_edge = str(ordered[0][2]).upper()
            opposite = {"N": "S", "S": "N", "E": "W", "W": "E"}
            if lower == "forward":
                return opposite.get(entry_edge)
            return entry_edge

        last = ordered[-1]
        second_last = ordered[-2]
        first = ordered[0]
        second = ordered[1]
        if lower == "forward":
            return _compass_from_delta(
                last[0] - second_last[0], last[1] - second_last[1]
            )
        return _compass_from_delta(first[0] - second[0], first[1] - second[1])


def _compass_from_delta(dx: int, dy: int) -> Optional[str]:
    """Return the compass direction implied by a single-step (dx, dy) delta."""
    if dx == 0 and dy > 0:
        return "N"
    if dx == 0 and dy < 0:
        return "S"
    if dy == 0 and dx > 0:
        return "E"
    if dy == 0 and dx < 0:
        return "W"
    return None
