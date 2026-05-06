"""
Canonical TrainSpan schema for v1.

`TrainSpan` is the controller-owned live physical span definition referenced
throughout `design_new.md` (TrainController section, TrainPhysics section,
PhysicsSpanUpdate payload, RouteExecutor span reads, PathDispatcher start-side
selection). It carries:

  - both physical end locators (`end_a`, `end_b`) — tile + distance_cm +
    entry_edge per the canonical SpanEndLocator schema below
  - the ordered occupied span (`occupied_tiles`, `occupied_blocks`,
    `occupied_resources`) — block/resource-level ownership view in physical
    order from `end_a` to `end_b`
  - the optional active cab indicator (`leading_end`) so derived "head"
    helpers can return the cab-side end without needing the controller

The schema is cab-independent: occupancy and end locators do NOT shift when
the operator changes cab. Cab-derived "head"/"tail" helpers branch on
`leading_end` at read time.

This module owns the schema only. `TrainPhysics` writes the values and
`TrainController` stores them; both layers import from here so they share
exactly the same shape without circular imports.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from enums import LeadingEnd


@dataclass
class SpanEndLocator:
    """One physical end of a train span — precise tile + offset.

    `tile` is the (x, y) coordinate of the tile the end currently sits on.
    `distance_cm` is how far along that tile the end is, measured from
    `entry_edge`. `entry_edge` is the compass edge ("N"/"E"/"S"/"W") through
    which forward traversal entered this tile when this end last advanced.
    Together these three fields fully specify where the end is on the network
    geometry.
    """

    tile: Tuple[int, int]
    distance_cm: float
    entry_edge: str


@dataclass
class SpanTile:
    """One ordered tile entry inside `TrainSpan.occupied_tiles`.

    Tiles are listed from `end_a` to `end_b` in physical order. Each entry
    carries the tile coordinate plus optional resource resolution so callers
    that need a tile→block / tile→resource mapping do not have to re-resolve
    against `RailwayNetwork`. `block_id` and `resource_id` are populated by
    the producer (TrainPhysics) when the tile resolves cleanly to one block
    or one switch resource; they may be `None` for tiles that span no block
    membership (rare in v1 — every tile owned by a block resolves
    deterministically).
    """

    tile: Tuple[int, int]
    block_id: Optional[str] = None
    resource_id: Optional[str] = None
    resource_type: Optional[str] = None  # "block" | "switch"


@dataclass
class TrainSpan:
    """Canonical live physical span owned by `TrainController.current_span`.

    Producer: `TrainPhysics.tick(...)` (one new TrainSpan per movement step).
    Storage: `TrainController.current_span` (written by
    `TrainController.on_physics_updated(...)`).
    Consumers: `RouteExecutor` (start-side selection, covered-task index
    advance), `PathDispatcher.create_path` (start-side selection),
    `TrainController` derived properties (`travel_direction`, lookahead
    inputs).

    The schema is cab-independent — occupancy fields do not change when the
    operator changes cab. `leading_end` reflects the controller's currently
    selected cab and is updated only by cab-selection commands.
    """

    end_a: SpanEndLocator
    end_b: SpanEndLocator
    occupied_tiles: List[SpanTile] = field(default_factory=list)
    occupied_blocks: List[str] = field(default_factory=list)
    occupied_resources: List[str] = field(default_factory=list)
    leading_end: Optional[LeadingEnd] = None

    @property
    def head(self) -> Optional[SpanEndLocator]:
        """The cab-side end locator, or None when no cab is selected."""
        if self.leading_end == LeadingEnd.A:
            return self.end_a
        if self.leading_end == LeadingEnd.B:
            return self.end_b
        return None

    @property
    def tail(self) -> Optional[SpanEndLocator]:
        """The non-cab end locator, or None when no cab is selected."""
        if self.leading_end == LeadingEnd.A:
            return self.end_b
        if self.leading_end == LeadingEnd.B:
            return self.end_a
        return None

    @classmethod
    def from_train_position(cls, position: "TrainPositionLike") -> "TrainSpan":
        """Build a TrainSpan from a TrainPosition-shaped object.

        TrainPosition (defined in train.py) is the legacy in-memory position
        record used by TrainPhysics and other layers. This factory adapts it
        into the canonical TrainSpan schema during the migration. Once every
        producer/consumer reads/writes TrainSpan directly, this helper can be
        removed.

        The mapping:
          - `position.end_a_tile` + `end_a_distance_cm` + `end_a_entry_edge`
            → `SpanEndLocator` for `end_a`
          - `position.end_b_*` → `SpanEndLocator` for `end_b`
          - `position.occupied_tiles` (List[Tuple[int, int]]) → list of
            `SpanTile` with `block_id`/`resource_id` left None (callers that
            need them resolve through `RailwayNetwork`)
          - `position.occupied_blocks` → forwarded
          - `occupied_resources` is empty in this adapter; v1 callers that
            need switch ownership read it from `Switch.occupied_by` directly
            (the resource list is producer-built once Phase 2c lands)
          - `position.leading_end` → forwarded
        """
        end_a = SpanEndLocator(
            tile=position.end_a_tile,
            distance_cm=position.end_a_distance_cm,
            entry_edge=position.end_a_entry_edge,
        )
        end_b = SpanEndLocator(
            tile=position.end_b_tile,
            distance_cm=position.end_b_distance_cm,
            entry_edge=position.end_b_entry_edge,
        )
        tiles = [SpanTile(tile=t) for t in position.occupied_tiles]
        return cls(
            end_a=end_a,
            end_b=end_b,
            occupied_tiles=tiles,
            occupied_blocks=list(position.occupied_blocks),
            occupied_resources=[],
            leading_end=position.leading_end,
        )


# A structural alias for the legacy TrainPosition shape used by
# `from_train_position`. Defined separately to avoid importing TrainPosition
# from train.py (which would create a cycle: train.py → train_span.py →
# train.py). The fields below match TrainPosition's public attributes.
class TrainPositionLike:
    end_a_tile: Tuple[int, int]
    end_a_distance_cm: float
    end_a_entry_edge: str
    end_b_tile: Tuple[int, int]
    end_b_distance_cm: float
    end_b_entry_edge: str
    occupied_tiles: List[Tuple[int, int]]
    occupied_blocks: List[str]
    leading_end: Optional[LeadingEnd]
