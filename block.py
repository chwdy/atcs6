"""
Block — a switch-free run of one or more segments on one power feed.

A `Block` is a data container (design_v2.md "Block"): it stores no train
state. Which train holds a block, and which train claims it, is read from the
train runtime when the snapshot is built.

Key concepts:
- Geometry (`ordered_tiles`, `length_cm`, `min_radius_cm`, `grade_percent`,
  `speed_limit_kmh`) lives on the member `Segment`s. The block exposes those as
  derived reads (concatenate / sum / min across its segments) so callers can
  keep reading `block.<geom>`. A single-segment block reads through to that one
  segment unchanged.
"""

from typing import List, Optional, Dict, Any, Set
from entity_ids import segment_id_for_block
from segment import Segment, SegmentTileEntry


class Block:
    """A switch-free run of one or more `Segment`s on one power feed.

    Groups its member segments (the geometry) and the block's static settings.
    It stores no train state.
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
        disabled: bool = False,
        manual: bool = False,
        allowed_travel_directions: Optional[Set[str]] = None,
        ordered_tiles: Optional[List[SegmentTileEntry]] = None,
        segments: Optional[List[Segment]] = None,
        segment_id: Optional[str] = None,
    ) -> None:
        self.block_id = block_id  # Unique ID (e.g., "BLK_001")
        self.name = name  # Human-readable name

        # Static config flag: operator-marked wait place. Saved with the
        # layout and never written by the runtime. Nothing reads it now: its
        # only reader was v1 route planning, which is deleted.
        self.is_holding_point = is_holding_point

        # Rail polarity is NOT a block field. A block whose rails are wired the
        # opposite way round is corrected on its `block_track` hardware binding
        # (`TrackBinding.reversed`, hardware_config.py), which auto-persists
        # like every other wiring edit (design_v2.md "Hardware setup", A
        # block's rails).

        self.disabled = disabled  # Static availability flag (physical-absence)
        self.manual = manual  # Virtual-disable flag (manual_override bypasses)

        # Track voltage is not a `Block` field. The voltage commanded to the
        # rail is recorded by `BlockPowerController`
        # (`get_recorded_voltage(block_id)`), which the live page payload reads
        # through `RailwayNetwork.to_dict` (design_v2.md "Block and switch
        # power controllers", BlockPowerController).

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
    # Serialization
    # ========================================================================

    def to_dict(self) -> Dict[str, Any]:
        """Convert block to dictionary for the snapshot.

        The live fields are added by `add_train_runtime_fields`. Persistence
        does NOT use this path; see `to_layout_dict()` instead.
        """
        return {
            "block_id": self.block_id,
            "name": self.name,
            "length_cm": self.length_cm,
            "min_radius_cm": self.min_radius_cm,
            "grade_percent": self.grade_percent,
            "is_holding_point": self.is_holding_point,
            "speed_limit_kmh": self.speed_limit_kmh,
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

        Runtime state is not saved with the layout (design_v2.md "Block",
        Block model: a block stores no train state), so this writes block
        geometry and config only.
        Track voltage is not a `Block` field at all.

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

        A name belongs to one record (see design_v2.md "Block", Block grouping,
        One name per record). A save written before that rule holds one string in
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

        Keys this loader does not know, such as runtime fields in an older
        save, are skipped; track voltage is not a `Block` field.

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
        )
