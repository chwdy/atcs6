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
- Underlying primitives: `occupied_by`, `_is_caution`, `caution_direction`,
  `reserved_by_path`, `reserved_direction`, `reserved_for_train`, `disabled`.
- `Block` is a data container; `BlockManager` is the sole mutator for physical
  and CAUTION transitions.
"""

from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, Any, Set
from enums import BlockState


# Block-relative directions used in the static block-level travel policy.
_BLOCK_RELATIVE_DIRECTIONS: Set[str] = {"forward", "reverse"}


@dataclass
class Block:
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
    max_speed_kmh: Optional[float] = None  # Speed limit for block

    # Underlying primitive state fields. `state` is a computed @property
    # derived from these (see below).
    occupied_by: Optional[str] = None  # Train ID currently physically present
    _is_caution: bool = False  # Physical safety-margin flag
    caution_direction: Optional[str] = None  # Direction of departing train (N/E/S/W)
    reserved_by_path: Optional[str] = None  # Path ID that holds the reservation
    reserved_direction: Optional[str] = None  # Reserved exit edge (N/E/S/W)
    reserved_for_train: Optional[str] = None  # Train ID the reservation is intended for
    disabled: bool = False  # Static availability flag

    # Voltage control (Phase 2 - DC analog control)
    current_voltage: float = 0.0

    # Static block-level travel policy. Defaults to both directions allowed.
    allowed_travel_directions: Set[str] = field(
        default_factory=lambda: {"forward", "reverse"}
    )

    # Canonical local traversal order. Each entry is (x, y, entry_edge) where
    # entry_edge is the external edge through which forward traversal enters
    # that tile. ordered_tiles[0].entry_edge is the forward-entry anchor.
    ordered_tiles: List[Tuple[int, int, str]] = field(default_factory=list)

    # ========================================================================
    # Computed state property
    # ========================================================================

    @property
    def state(self) -> BlockState:
        """Compute current block state from underlying primitives.

        Priority order (per spec): OCCUPIED > CAUTION > RESERVED > FREE.

        - OCCUPIED: a train is physically present (`occupied_by is not None`)
        - CAUTION: physically free and `_is_caution` is True
        - RESERVED: physically free, not in CAUTION, and `reserved_by_path` is set
        - FREE: otherwise
        """
        if self.occupied_by is not None:
            return BlockState.OCCUPIED
        if self._is_caution:
            return BlockState.CAUTION
        if self.reserved_by_path is not None:
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
            "max_speed_kmh": self.max_speed_kmh,
            "state": self.state.value,
            "occupied_by": self.occupied_by,
            "_is_caution": self._is_caution,
            "caution_direction": self.caution_direction,
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
        ordered_tiles: List[Tuple[int, int, str]] = []
        for entry in raw_tiles:
            if isinstance(entry, dict):
                x = int(entry["x"])
                y = int(entry["y"])
                edge = str(entry["entry_edge"])
            else:
                x = int(entry[0])
                y = int(entry[1])
                edge = str(entry[2])
            ordered_tiles.append((x, y, edge))

        return cls(
            block_id=data["block_id"],
            name=data.get("name", ""),
            length_cm=data.get("length_cm", 0.0),
            min_radius_cm=data.get("min_radius_cm"),
            max_speed_kmh=data.get("max_speed_kmh"),
            occupied_by=data.get("occupied_by"),
            _is_caution=bool(data.get("_is_caution", False)),
            caution_direction=data.get("caution_direction"),
            reserved_by_path=data.get("reserved_by_path"),
            reserved_direction=data.get("reserved_direction"),
            reserved_for_train=data.get("reserved_for_train"),
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
        self.caution_direction = None
        return True

    def release(self) -> None:
        """Clear physical occupancy without touching reservation fields.

        This is a primitive-level helper; `BlockManager` decides whether
        a release should also raise CAUTION via `on_block_tail_vacated()`.
        """
        self.occupied_by = None

    def set_to_caution(self, caution_direction: Optional[str] = None) -> None:
        """Mark this block as in CAUTION with an optional direction stamp.

        The state machine treats CAUTION as an independent safety state, so
        this writes the underlying flag directly instead of going through a
        state field.
        """
        self._is_caution = True
        if caution_direction is not None:
            self.caution_direction = caution_direction

    def clear(self) -> None:
        """Clear the CAUTION flag and its direction.

        Does not touch `reserved_by_path` or `occupied_by` - those are
        independent concerns owned by separate manager calls.
        """
        self._is_caution = False
        self.caution_direction = None

    # ========================================================================
    # Path Reservation Helpers (Internal)
    #
    # These are private helpers; BlockManager is the only legitimate caller.
    # They never check or mutate physical occupancy / caution fields.
    # ========================================================================

    def _reserve_for_path(
        self,
        train_id: str,
        direction: Optional[str] = None,
    ) -> None:
        """Internal: write the path-reservation primitives.

        Called only by `BlockManager.reserve_blocks_for_path(...)`. Stores
        `train_id` in `reserved_for_train` (NOT in `occupied_by`, which is
        physical-only). The `direction` argument may be a compass exit_edge
        ("N"/"E"/"S"/"W") or a block-relative "forward"/"reverse"; we
        normalize block-relative values into the corresponding compass exit
        edge using `ordered_tiles` adjacency.

        `path_id` is recorded separately by `BlockManager` writing
        `block.reserved_by_path` directly.
        """
        self.reserved_for_train = train_id
        self.reserved_direction = self._resolve_reservation_direction(direction)

    def release_path_reservation(self) -> None:
        """Internal: clear path-reservation primitives.

        Called only by `BlockManager.release_path_block_reservations(...)`.
        Clears `reserved_by_path`, `reserved_direction`, and
        `reserved_for_train` with no state guard. MUST NOT touch
        `caution_direction` or `_is_caution` (those are physical-lifecycle
        fields owned by `BlockManager`).
        """
        self.reserved_by_path = None
        self.reserved_direction = None
        self.reserved_for_train = None

    # ------------------------------------------------------------------
    # Direction normalization
    # ------------------------------------------------------------------

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
