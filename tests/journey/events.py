"""Event and Invariant value types for a Journey.

An Event has a `trigger` predicate evaluated each tick. The first time it
returns True, its `assertions` and `actions` run in order. Events are processed
in the order declared on the Journey; they are not re-checked once consumed.

An Invariant is a check that runs every tick on the current Snapshot.
"""

from dataclasses import dataclass, field
from typing import Callable, List

# Forward reference avoids a runtime import cycle (Snapshot is defined in
# harness.py, which imports nothing from this module).
SnapshotCheck = Callable[["Snapshot"], None]  # noqa: F821
SnapshotPredicate = Callable[["Snapshot"], bool]  # noqa: F821


@dataclass
class Event:
    name: str
    trigger: SnapshotPredicate
    assertions: List[SnapshotCheck] = field(default_factory=list)
    actions: List[SnapshotCheck] = field(default_factory=list)


@dataclass
class Invariant:
    name: str
    check: SnapshotCheck


@dataclass
class Journey:
    train_id: str
    events: List[Event]
    invariants: List[Invariant]
