"""
Canonical TrainSpan schema for v1.

`TrainSpan` is the controller-owned live physical span definition referenced
throughout `design_new.md` (TrainController section, TrainPhysics section,
PhysicsSpanUpdate payload, RouteExecutor span reads, PathDispatcher start-side
selection). It carries exactly four fields:

  - both physical end locators (`end_a`, `end_b`) — tile + distance_cm +
    entry_edge per the canonical `SpanEndLocator` schema below
  - the ordered occupied span (`occupied_tiles`, `occupied_blocks`) in physical
    order from `end_a` to `end_b`

`end_a` and `end_b` are stable, cab-independent physical ends; the span never
labels either as head/tail (design_new.md TrainSpan schema). Consumers that
need the forward/head side derive it from the controller's selected cab plus
travel state and `end_a` / `end_b` (`TrainController._head_span_locator()` /
`TrainController.travel_direction`).

This module owns the schema only. `TrainPhysics` produces the values and
`TrainController` stores them (`physics_sync.current_span`); both layers import
from here so they share exactly the same shape without circular imports.
"""

from dataclasses import dataclass, field
from typing import List, Tuple


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
class TrainSpan:
    """Canonical live physical span owned by
    `TrainController.physics_sync.current_span`.

    Producer: `TrainPhysics.tick(...)` (one new `TrainSpan` per movement step).
    Storage: `TrainController.physics_sync.current_span` (written by
    `TrainController.on_physics_updated(...)`).
    Consumers: `RouteExecutor` / `PathDispatcher` (via
    `TrainController.get_current_span()`) and `TrainController` derived reads
    (`_head_span_locator`, `travel_direction`, lookahead inputs).

    The schema is cab-independent: `end_a` / `end_b` are stable geometric labels
    for the two physical ends of the consist and do NOT imply head/tail.
    `occupied_tiles` / `occupied_blocks` are ordered from `end_a` to `end_b` and
    do not change when the operator changes cab.
    """

    end_a: SpanEndLocator
    end_b: SpanEndLocator
    occupied_tiles: List[Tuple[int, int]] = field(default_factory=list)
    occupied_blocks: List[str] = field(default_factory=list)
