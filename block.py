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
from entity_ids import segment_id_for_block
from segment import Segment, SegmentTileEntry


# Block-relative directions used in the static block-level travel policy.
_BLOCK_RELATIVE_DIRECTIONS: Set[str] = {"forward", "reverse"}


# `ordered_tiles` entries are `SegmentTileEntry` (x, y, entry_edge) records owned
# by `Segment`. `BlockTileEntry` remains as an alias so existing importers keep
# working.
BlockTileEntry = SegmentTileEntry



class CautionState(NamedTuple):
    """The v1 caution record; it goes with the other v1 runtime fields."""

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


class Block:
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
        occupied_seconds: float = 0.0,
        occupied_by: Optional[str] = None,
        caution: Optional[CautionState] = None,
        reservation: Optional[BlockReservation] = None,
        locked_by_path: Optional[str] = None,
        disabled: bool = False,
        manual: bool = False,
        allowed_travel_directions: Optional[Set[str]] = None,
        ordered_tiles: Optional[List[SegmentTileEntry]] = None,
        segments: Optional[List[Segment]] = None,
        segment_id: Optional[str] = None,
    ) -> None:
        self.block_id = block_id  # Unique ID (e.g., "BLK_001")
        self.name = name  # Human-readable name

        # Static config flag: operator-marked safe wait place (design_new.md
        # "Block" field schema). Never written by runtime; its only consumer is
        # the best-effort truncation holding-point preference (item 11 under
        # "Path And PathDispatcher").
        self.is_holding_point = is_holding_point

        # Rail polarity is NOT a block field. A block whose rails are wired the
        # opposite way round is corrected on its `block_track` hardware binding
        # (`TrackBinding.reversed`, hardware_config.py), which auto-persists
        # like every other wiring edit (design_new.md "Power", Rail polarity
        # lives on the rail binding).

        # Maintenance counter: total unscaled wall-clock seconds a train has
        # occupied this block (accrued from time.time() deltas, not scaled by
        # playback speed). Written once per tick by SimulationController.update(...)
        # and persisted with the layout (see design_new.md "Maintenance counters").
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
        # The braking-distance lock: the path_id holding this block hard, or
        # None. A peer to `reservation` (design_new.md "Block" field schema),
        # not derived from it: a block is locked only by the path that already
        # reserves it, so `locked_by_path == reserved_by_path` whenever set.
        # Sole mutator is `BlockManager`; `is_locked` is the convenience read.
        self.locked_by_path = locked_by_path
        self.disabled = disabled  # Static availability flag (physical-absence)
        self.manual = manual  # Virtual-disable flag (manual_override bypasses)

        # Track voltage is not a `Block` field. Its canonical home is the
        # per-tick derived `TrainPhysics.compute_track_voltage(...)`, and the
        # applied-output mirror (what reached the rail) is owned by
        # `BlockPowerController` (`get_recorded_voltage(block_id)`), which the
        # live frontend payload sources through `RailwayNetwork.to_dict`
        # (design_new.md "Power", Block power-controller role).

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
        #
        # `name` names the block and only the block: the auto segment starts
        # unnamed. A display name belongs to one record, and copying it here
        # made every un-grouped segment and its block read as the same entity
        # wherever both are listed. A caller that means to name the geometry
        # names the member segment (see SegmentGeometryManager.create_segment).
        #
        # The auto segment's id is derived from the block's, never equal to it
        # (see entity_ids): the two axes are separate entities and an id that
        # names both is ambiguous everywhere it is displayed or bound. A caller
        # that already knows the segment id — geometry creation, which mints the
        # segment first — passes `segment_id` to pin it.
        if segments is not None:
            self.segments: List[Segment] = list(segments)
        else:
            self.segments = [
                Segment(
                    segment_id=segment_id or segment_id_for_block(block_id),
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
    def ordered_tiles(self) -> List[SegmentTileEntry]:
        """Canonical local traversal order across all member segments.

        Concatenates each segment's `ordered_tiles`; `ordered_tiles[0]` is the
        forward-entry anchor for the block.
        """
        out: List[SegmentTileEntry] = []
        for seg in self.segments:
            out.extend(seg.ordered_tiles)
        return out

    @ordered_tiles.setter
    def ordered_tiles(self, value: List[SegmentTileEntry]) -> None:
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

    @property
    def is_locked(self) -> bool:
        """True iff a path holds this block's braking-distance lock.

        Convenience read for `locked_by_path is not None` (design_new.md
        "Block" field schema). A locked block is uncancellable: Preemption may
        take a reserved block from a lower-priority train only when it is not
        locked.
        """
        return self.locked_by_path is not None

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
            # Braking-distance lock (peer to reservation).
            "locked_by_path": self.locked_by_path,
            "is_locked": self.is_locked,
            "disabled": self.disabled,
            "manual": self.manual,
            # `current_voltage` is not a `Block` field: the live frontend payload
            # gets the applied-output mirror injected by `RailwayNetwork.to_dict`
            # from `BlockPowerController.get_recorded_voltage(...)`.
            "allowed_travel_directions": sorted(self.allowed_travel_directions),
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
        Runtime fields (occupied_by, caution, reservation, computed state) are
        excluded; track voltage is not a `Block` field at all.

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
            "ordered_tiles": [
                [int(x), int(y), str(edge)] for (x, y, edge) in self.ordered_tiles
            ],
            "segment_ids": self.segment_ids,
            "segments": [s.to_layout_dict() for s in self.segments],
        }

    @staticmethod
    def _loaded_name(stored_name: str, segments: List[Segment]) -> str:
        """The block's name as loaded, with a copied-down name dropped.

        A name belongs to one record (see design_new.md **Block grouping**, one
        name per record). A save written before that rule holds one string in
        both places: the block and its only member segment read identically
        everywhere both are listed, most visibly in a tile's right-click menu.

        A single-segment block whose stored name IS its member's name therefore
        loads unnamed and reads as its id — the segment keeps the name, since
        naming the geometry is what the operator did. This looks only at an
        exact match on a one-segment block, so a block the operator named in
        its own right, and every grouped block, load with their names intact.
        """
        if len(segments) == 1 and stored_name and stored_name == segments[0].name:
            return ""
        return stored_name

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Block":
        """Reconstruct block from a layout snapshot.

        Runtime fields (`occupied_by`, `caution`, `reservation`, computed
        `state`) are not loaded from disk — they default-initialize and the
        engine populates them at run time; track voltage is not a `Block` field.
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
            block_id = str(data["block_id"])
            segments = [Segment.from_dict(s) for s in raw_segments]
            # A save written when the two axes shared one id carries its member
            # segment under the block's own id. The block id is the one outside
            # records name — hardware bindings, saved routes, stored paths — so
            # it survives, and the segment takes the derived geometry-axis id
            # (`entity_ids`). Tiles still pointing at the shared id are
            # re-pointed by `RailwayNetwork._reconcile_segment_tile_membership`,
            # which runs once the tracks are in place.
            for segment in segments:
                if segment.segment_id == block_id:
                    segment.segment_id = segment_id_for_block(block_id)
            atd_nested = data.get("allowed_travel_directions")
            return cls(
                block_id=block_id,
                name=cls._loaded_name(data.get("name", ""), segments),
                is_holding_point=bool(data.get("is_holding_point", False)),
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
        ordered_tiles: List[SegmentTileEntry] = []
        for entry in raw_tiles:
            if isinstance(entry, dict):
                x = int(entry["x"])
                y = int(entry["y"])
                edge = str(entry["entry_edge"])
            else:
                x = int(entry[0])
                y = int(entry[1])
                edge = str(entry[2])
            ordered_tiles.append(SegmentTileEntry(x=x, y=y, entry_edge=edge))

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






    # ========================================================================
    # Mutators - direct primitive writes only.
    # `BlockManager` is the sole legitimate caller for physical and CAUTION
    # transitions; these helpers exist so the manager and serialization can
    # write through one named call rather than poking primitives in many sites.
    # ========================================================================


    def release(self) -> None:
        """Clear physical occupancy without touching reservation fields.

        This is a primitive-level helper; `BlockManager` decides whether
        a release should also raise CAUTION via `on_block_tail_vacated()`.
        """
        self.occupied_by = None


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



    # ------------------------------------------------------------------
    # Direction normalization
    # ------------------------------------------------------------------



