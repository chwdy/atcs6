"""
Block Management System

Definition:
This module provides the Block class for centralized block management. A Block
represents an electrically isolated track section with physical properties
(length, radius limits, speed). Blocks are the minimum control unit for train
movement in model railway layouts.

Key concepts:
- Block = electrically isolated track section (all tracks share voltage/current)
- Tracks reference blocks via track.block_section_id
- Block has physical properties set at block level, not per-tile
- Tiles are for UI layout; a block might have different physical length than tile count
- Signals mark block boundaries and monitor block occupancy
- Switches are block boundaries (connect blocks but not IN blocks)

v1 model:
- `state` is computed from underlying primitive fields. Priority order:
  OCCUPIED > CAUTION > RESERVED > FREE.
- Underlying primitives: `occupied_by`, `_is_caution`, `caution_exit_edge`,
  `reserved_by_path`, `reserved_direction`, `reserved_for_train`, `disabled`.
- `Block` is a data container; `BlockManager` is the sole mutator for physical
  and CAUTION transitions.
"""

from dataclasses import dataclass, field
from typing import List, NamedTuple, Tuple, Optional, Dict, Any, Set
from enums import BlockState
from track_resource import TrackResource


# Block-relative directions used in the static block-level travel policy.
_BLOCK_RELATIVE_DIRECTIONS: Set[str] = {"forward", "reverse"}


class BlockTileEntry(NamedTuple):
    """One tile entry in `Block.ordered_tiles`.

    Per design_new.md "Block model" — each entry carries `(x, y, entry_edge)`,
    where `entry_edge` is the external edge through which forward traversal
    enters that tile. `Block.ordered_tiles[0].entry_edge` is the canonical
    forward-entry anchor for the block.

    Defined as a NamedTuple so existing code that unpacks `(x, y, edge)` keeps
    working while typed callers can reach `.x`, `.y`, `.entry_edge` by name.
    """

    x: int
    y: int
    entry_edge: str


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


@dataclass
class Block(TrackResource):
    """
    Centralized block entity for track section management.

    A Block is an electrically isolated track section where all tracks
    share voltage/current. It represents the minimum control unit for
    train movement.
    """

    block_id: str  # Unique ID (e.g., "BLK_001")
    name: str = ""  # Human-readable name

    # Physical properties (set on BLOCK, not tiles)
    length_cm: float = 0.0  # Total track length in cm
    min_radius_cm: Optional[float] = None  # Tightest curve radius
    speed_limit_kmh: Optional[float] = None  # Speed limit for block

    # Underlying primitive state fields. `state` is a computed @property
    # derived from these (see below).
    occupied_by: Optional[str] = None  # Train ID currently physically present
    _is_caution: bool = False  # Physical safety-margin flag
    caution_exit_edge: Optional[str] = None  # Direction of departing train (N/E/S/W)
    # Single Optional reservation record. The reserved_by_path,
    # reserved_for_train, reserved_direction reads are derived @properties below.
    reservation: Optional[BlockReservation] = None
    disabled: bool = False  # Static availability flag

    # Voltage control (Phase 2 - DC analog control)
    current_voltage: float = 0.0

    # Static block-level travel policy. Defaults to both directions allowed.
    allowed_travel_directions: Set[str] = field(
        default_factory=lambda: {"forward", "reverse"}
    )

    # Canonical local traversal order. Each entry is a `BlockTileEntry`
    # (x, y, entry_edge); `ordered_tiles[0].entry_edge` is the forward-entry
    # anchor for the block.
    ordered_tiles: List[BlockTileEntry] = field(default_factory=list)

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
    # Computed state property
    # ========================================================================

    @property
    def state(self) -> BlockState:
        """Compute current block state from underlying primitives.

        Priority order (per spec): OCCUPIED > CAUTION > RESERVED > FREE.
        """
        if self.occupied_by is not None:
            return BlockState.OCCUPIED
        if self._is_caution:
            return BlockState.CAUTION
        if self.reservation is not None:
            return BlockState.RESERVED
        return BlockState.FREE

    # ========================================================================
    # Serialization
    # ========================================================================

    def to_dict(self) -> Dict[str, Any]:
        """Convert block to dictionary for serialization.

        `state` is included as a computed read-only convenience but is NOT
        written back by `from_dict()`; reconstruction relies on the underlying
        primitives instead.
        """
        return {
            "block_id": self.block_id,
            "name": self.name,
            "length_cm": self.length_cm,
            "min_radius_cm": self.min_radius_cm,
            "speed_limit_kmh": self.speed_limit_kmh,
            "state": self.state.value,
            "occupied_by": self.occupied_by,
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
            "current_voltage": self.current_voltage,
            "allowed_travel_directions": sorted(self.allowed_travel_directions),
            "ordered_tiles": [
                [int(x), int(y), str(edge)] for (x, y, edge) in self.ordered_tiles
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Block":
        """Reconstruct block from dictionary.

        Note: the `state` key, if present, is ignored. State is always
        recomputed from the underlying primitive fields.
        """
        atd = data.get("allowed_travel_directions")
        if atd is None:
            allowed = {"forward", "reverse"}
        else:
            allowed = {str(d) for d in atd}

        raw_tiles = data.get("ordered_tiles") or []
        ordered_tiles: List[BlockTileEntry] = []
        for entry in raw_tiles:
            if isinstance(entry, dict):
                x = int(entry["x"])
                y = int(entry["y"])
                edge = str(entry["entry_edge"])
            else:
                x = int(entry[0])
                y = int(entry[1])
                edge = str(entry[2])
            ordered_tiles.append(BlockTileEntry(x=x, y=y, entry_edge=edge))

        # Reconstruct reservation. Prefer the new "reservation" record; fall
        # back to the legacy three-field shape so older saved data still loads.
        reservation: Optional[BlockReservation] = None
        raw_reservation = data.get("reservation")
        if raw_reservation:
            reservation = BlockReservation.from_dict(raw_reservation)
        else:
            legacy_path = data.get("reserved_by_path")
            legacy_train = data.get("reserved_for_train")
            legacy_dir = data.get("reserved_direction")
            if legacy_path and legacy_train and legacy_dir:
                reservation = BlockReservation(
                    path_id=str(legacy_path),
                    train_id=str(legacy_train),
                    exit_edge=str(legacy_dir),
                )

        return cls(
            block_id=data["block_id"],
            name=data.get("name", ""),
            length_cm=data.get("length_cm", 0.0),
            min_radius_cm=data.get("min_radius_cm"),
            speed_limit_kmh=data.get("speed_limit_kmh", data.get("max_speed_kmh")),
            occupied_by=data.get("occupied_by"),
            _is_caution=bool(data.get("_is_caution", False)),
            caution_exit_edge=data.get("caution_exit_edge"),
            reservation=reservation,
            disabled=bool(data.get("disabled", False)),
            current_voltage=float(data.get("current_voltage", 0.0)),
            allowed_travel_directions=allowed,
            ordered_tiles=ordered_tiles,
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
        - It is RESERVED by this same path
        - It is OCCUPIED by the train that will use this path
        """
        if self.disabled:
            return False
        st = self.state
        if st == BlockState.FREE:
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
        self._is_caution = False
        self.caution_exit_edge = None
        return True

    def release(self) -> None:
        """Clear physical occupancy without touching reservation fields.

        This is a primitive-level helper; `BlockManager` decides whether
        a release should also raise CAUTION via `on_block_tail_vacated()`.
        """
        self.occupied_by = None

    def set_to_caution(self, caution_exit_edge: Optional[str] = None) -> None:
        """Mark this block as in CAUTION with an optional direction stamp.

        The state machine treats CAUTION as an independent safety state, so
        this writes the underlying flag directly instead of going through a
        state field.
        """
        self._is_caution = True
        if caution_exit_edge is not None:
            self.caution_exit_edge = caution_exit_edge

    def clear(self) -> None:
        """Clear the CAUTION flag and its direction.

        Does not touch `reserved_by_path` or `occupied_by` - those are
        independent concerns owned by separate manager calls.
        """
        self._is_caution = False
        self.caution_exit_edge = None

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

        if not self.ordered_tiles:
            return None

        if len(self.ordered_tiles) == 1:
            entry_edge = str(self.ordered_tiles[0][2]).upper()
            opposite = {"N": "S", "S": "N", "E": "W", "W": "E"}
            if lower == "forward":
                return opposite.get(entry_edge)
            return entry_edge

        last = self.ordered_tiles[-1]
        second_last = self.ordered_tiles[-2]
        first = self.ordered_tiles[0]
        second = self.ordered_tiles[1]
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
