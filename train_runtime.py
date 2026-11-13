"""The v2 trains and the runtime step (design_v2.md "Trains", "Claiming the
next block", "Switches", "When a train waits", "When the runtime stops a
train", "When a train overruns", "Track power", "Runtime step").

A train is a label the runtime puts on blocks that read occupied. It has no
position inside a block and no speed. `TrainRuntime` holds every train and
runs one runtime step at a time from what the detectors read:

1. the caller reads the detectors and hands in the blocks that read occupied;
2. update each train's blocks;
3. stop trains;
4. end claims;
5. give claims;
6. set every block's voltage;
7. publish events.

`TrainRuntime` reads the track model through `next_block.py` and writes to the
rig through two callables it is given: `set_block_voltage(block_id, volts)`
and `emit(operation, kind, train_id, message, payload)`. It never touches a
device or a detector itself.

This first build leaves out destinations, marking two trains as one,
splitting, and renaming.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from next_block import (
    FORWARD,
    SWITCH_AGAINST,
    TRACK_ENDS,
    WalkResult,
    neighbour_blocks,
    next_block_ahead,
    opposite_direction,
    switches_next_to,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from network import RailwayNetwork

MAX_TRACK_VOLTAGE: float = 12.0
"""The top of the speed knob, in volts."""

DEFAULT_HOLD_TIME_S: float = 5.0
"""How long a block that reads empty stays its train's block."""

# Why a train cannot be given a claim (design_v2.md "When a train waits").
BLOCK_TAKEN = "block_taken"
OWN_BLOCK = "own_block"
SWITCH_HELD = "switch_held"

Result = Tuple[bool, Optional[str]]
"""An operator request's answer: `(success, reason)`."""

EmitFn = Callable[[str, str, str, str, Dict[str, Any]], None]
SetVoltageFn = Callable[[str, float], Any]


@dataclass
class TimedStops:
    """The blocks where one train stops and when it can leave."""
    blocks: Set[str]
    seconds: float
    volts: float
    arrived_at_block: Optional[str] = None
    resume_at: Optional[float] = None


@dataclass
class RunningTrain:
    """One train (design_v2.md "Trains", What a train holds)."""

    seq: int
    """The counter value this train was made from. `train_id` is built from
    it, and it orders trains that began to wait on the same step."""
    train_id: str
    name: str
    blocks: List[str]
    """The train's blocks, from its rear block to its front block."""
    directions: Dict[str, str]
    """For each of the train's blocks, the direction the train runs it in,
    `forward` or `reverse` along that block's tile order."""
    knob_volts: float = 0.0
    destination_block_id: Optional[str] = None
    length_cm: Optional[float] = None
    lost: bool = False
    stopped_at: Optional[float] = 0.0
    waiting_since: Optional[float] = None

    # The claim: the block, the direction the train will run it in, and the
    # switches on the way to it.
    claim_block_id: Optional[str] = None
    claim_direction: Optional[str] = None
    claim_switch_ids: Tuple[str, ...] = ()
    alternating_switch: Optional[Tuple[str, int]] = None

    # First come, first served: what the train is asking for and the step on
    # which it first asked.
    asked_for: Optional[str] = None
    asked_step: Optional[int] = None

    waiting_reason: Optional[str] = None
    """Why the train cannot be given a claim, kept while it is waiting."""

    empty_since: Dict[str, float] = field(default_factory=dict)
    """For each of the train's blocks that reads empty, when it began to."""
    empty_while_moving: Dict[str, bool] = field(default_factory=dict)
    """For each of those blocks, whether the train was running, or had
    stopped less than the hold time before, when the block began to read
    empty."""

    end_of_track_block: Optional[str] = None
    """The front block the runtime has already stopped this train in for End
    of track, so it is not stopped there a second time."""

    timed_stops: Optional[TimedStops] = None

    @property
    def front_block(self) -> str:
        return self.blocks[-1]

    @property
    def direction(self) -> str:
        """Which way the train faces in its front block."""
        return self.directions.get(self.front_block, FORWARD)

    @property
    def running(self) -> bool:
        return self.knob_volts > 0.0 and not self.lost

    @property
    def stopped(self) -> bool:
        return self.knob_volts <= 0.0


class TrainRuntime:
    """Every train, and the runtime step that moves them."""

    def __init__(
        self,
        network: "RailwayNetwork",
        set_block_voltage: SetVoltageFn,
        emit: Optional[EmitFn] = None,
        hold_time_s: float = DEFAULT_HOLD_TIME_S,
        throw_switch: Optional[Callable[[str, int], bool]] = None,
        outputs_active: Callable[[], bool] = lambda: True,
    ) -> None:
        self._network = network
        self._set_block_voltage = set_block_voltage
        self._emit_fn = emit
        self._throw_switch = throw_switch
        self._outputs_active = outputs_active
        self.alternate_next: Dict[str, int] = {}
        self.hold_time_s = float(hold_time_s)
        self.trains: Dict[str, RunningTrain] = {}
        self._next_seq = 1
        self._step_count = 0
        self.occupied: Set[str] = set()
        """The blocks that read occupied on the last step."""
        self._now = 0.0
        self._left_at: Dict[str, float] = {}
        """For each block a train's rear has just left, when it was taken
        off. The runtime does not throw a switch next to such a block for the
        hold time."""

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def train_of_block(self, block_id: str) -> Optional[RunningTrain]:
        """The train whose block this is, or None."""
        for train in self.trains.values():
            if block_id in train.blocks:
                return train
        return None

    def claimer_of_block(self, block_id: str) -> Optional[RunningTrain]:
        """The train that holds a claim on this block, or None."""
        for train in self.trains.values():
            if train.claim_block_id == block_id:
                return train
        return None

    def block_is_taken(self, block_id: str) -> bool:
        """A block is taken when it reads occupied, is one of a train's
        blocks, or is claimed."""
        return (
            block_id in self.occupied
            or self.train_of_block(block_id) is not None
            or self.claimer_of_block(block_id) is not None
        )

    def wholly_in_front_block(self, train: RunningTrain) -> bool:
        """Whether the front block is the only one of the train's blocks that
        reads occupied."""
        reading = [b for b in train.blocks if b in self.occupied]
        return reading == [train.front_block]

    def walk_ahead(self, train: RunningTrain) -> WalkResult:
        """The walk from the train's front block in its direction."""
        return next_block_ahead(self._network, train.front_block, train.direction)

    def state_of(self, train: RunningTrain) -> str:
        """`"lost"`, `"stopped"`, `"waiting"` or `"moving"`."""
        if train.lost:
            return "lost"
        if train.stopped:
            return "stopped"
        if train.waiting_since is not None:
            return "waiting"
        return "moving"

    def held_switch_ids(self) -> Set[str]:
        """Every switch that is held (design_v2.md "Switches", A held switch
        is not thrown)."""
        held: Set[str] = set()
        for train in self.trains.values():
            # On the way between a train's front block and its claim.
            held.update(train.claim_switch_ids)
            # Between two blocks of the same train.
            for rear, ahead in zip(train.blocks, train.blocks[1:]):
                walk = next_block_ahead(
                    self._network, rear, train.directions.get(rear, FORWARD)
                )
                if walk.block_id == ahead:
                    held.update(walk.switch_ids)
            # Next to one of a train's blocks that reads empty.
            for block_id in train.blocks:
                if block_id not in self.occupied:
                    held |= switches_next_to(self._network, block_id)
        return held

    def train_entry(self, train: RunningTrain) -> Dict[str, Any]:
        """One train's entry in the snapshot (design_v2.md "Live fields", A
        train entry)."""
        state = self.state_of(train)
        stops = train.timed_stops
        walk = self.walk_ahead(train)
        return {
            "train_id": train.train_id,
            "name": train.name,
            "length_cm": train.length_cm,
            "direction": train.direction,
            "knob_volts": train.knob_volts,
            "timed_stops": {
                "block_ids": sorted(stops.blocks),
                "seconds": stops.seconds,
                "resume_in_s": (
                    max(0, math.ceil(stops.resume_at - self._now))
                    if stops.resume_at is not None else None
                ),
            } if stops is not None else None,
            "destination_block_id": train.destination_block_id,
            "lost": train.lost,
            "stopped_at": train.stopped_at,
            "waiting_since": train.waiting_since,
            "blocks": [
                {"block_id": b, "reads_occupied": b in self.occupied}
                for b in train.blocks
            ],
            "state": state,
            "waiting_reason": train.waiting_reason if state == "waiting" else None,
            "next_block_id": walk.block_id,
            "claimed_block_id": train.claim_block_id,
        }

    def trains_payload(self) -> Dict[str, Dict[str, Any]]:
        """The snapshot's `trains` map."""
        return {
            train.train_id: self.train_entry(train)
            for train in sorted(self.trains.values(), key=lambda t: t.seq)
        }

    # ------------------------------------------------------------------
    # The runtime step
    # ------------------------------------------------------------------

    def step(self, occupied: Iterable[str], now: float, live: bool) -> None:
        """One runtime step.

        `occupied` is the blocks that read occupied after the debounce, `now`
        is this pass's wall time, and `live` is whether the session state is
        Live. While it is not Live the step only updates each train's blocks:
        nothing is claimed and no voltage is written.
        """
        self._step_count += 1
        self._now = now
        self.occupied = {b for b in occupied if b in self._network.blocks}
        self._give_trains_to_occupied_blocks(now)
        self._take_off_rear_blocks(now)
        self._note_empty_blocks(now)
        self._take_off_empty_blocks(now)
        if not live:
            return
        self._stop_trains(now)
        self._apply_timed_stops(now)
        self._end_claims(now)
        self._give_claims(now)
        if self._outputs_active():
            self._set_voltages()

    # --- item 2: update each train's blocks ---------------------------

    def _give_trains_to_occupied_blocks(self, now: float) -> None:
        """An occupied block with no train gets one (design_v2.md "Trains")."""
        for block_id in sorted(self.occupied):
            owner = self.train_of_block(block_id)
            if owner is not None:
                if owner.lost:
                    # A lost train's own block reads occupied again.
                    self._find_lost_train(owner, block_id, owner.direction)
                continue

            # 1. The train that claimed it.
            claimer = self.claimer_of_block(block_id)
            if claimer is not None:
                direction = claimer.claim_direction or FORWARD
                passage = claimer.alternating_switch
                if passage is not None:
                    switch_id, position = passage
                    if switch_id in self.alternate_next:
                        self.alternate_next[switch_id] = 1 - position
                self._end_claim(claimer)
                self._add_front_block(claimer, block_id, direction)
                continue

            # 2. A lost train it belongs to.
            found = self._lost_train_next_to(block_id)
            if found is not None:
                lost_train, direction = found
                self._find_lost_train(lost_train, block_id, direction)
                continue

            # 3. A train that just overran.
            overrun = self._train_that_overran_into(block_id, now)
            if overrun is not None:
                train, direction = overrun
                self._add_front_block(train, block_id, direction)
                self._stop(train, now)
                train.destination_block_id = None
                self._emit(
                    "train_overran",
                    "warning",
                    train,
                    f"{train.name} stopped one block past where it was held.",
                    {"block_id": block_id},
                )
                continue

            # 4. A new train.
            self._make_train(block_id)

    def _lost_train_next_to(self, block_id: str) -> Optional[Tuple[RunningTrain, str]]:
        """The lost train whose block is next to `block_id` across switches as
        currently set, with the direction it would face there."""
        for train in sorted(self.trains.values(), key=lambda t: t.seq):
            if not train.lost:
                continue
            neighbours = neighbour_blocks(self._network, train.front_block)
            if block_id in neighbours:
                return train, neighbours[block_id]
        return None

    def _train_that_overran_into(
        self, block_id: str, now: float
    ) -> Optional[Tuple[RunningTrain, str]]:
        """The train that stopped or began waiting less than the hold time
        ago and whose next block ahead is `block_id`."""
        for train in sorted(self.trains.values(), key=lambda t: t.seq):
            if train.lost:
                continue
            since = train.stopped_at if train.stopped else train.waiting_since
            if since is None or now - since >= self.hold_time_s:
                continue
            walk = self.walk_ahead(train)
            if walk.block_id == block_id and walk.direction:
                return train, walk.direction
        return None

    def _make_train(self, block_id: str) -> RunningTrain:
        seq = self._next_seq
        self._next_seq += 1
        train_id = f"T{seq}"
        train = RunningTrain(
            seq=seq,
            train_id=train_id,
            name=train_id,
            blocks=[block_id],
            directions={block_id: FORWARD},
        )
        self.trains[train_id] = train
        self._emit(
            "train_appeared",
            "info",
            train,
            f"{train.name} appeared on {self._block_name(block_id)}.",
            {"block_id": block_id},
        )
        return train

    def _add_front_block(self, train: RunningTrain, block_id: str, direction: str) -> None:
        stops = train.timed_stops
        if stops is not None:
            if stops.resume_at is not None:
                train.timed_stops = None
            else:
                stops.arrived_at_block = block_id if block_id in stops.blocks else None
        train.blocks.append(block_id)
        train.directions[block_id] = direction
        train.end_of_track_block = None
        self._clear_asking(train)
        self._emit(
            "train_block_added",
            "info",
            train,
            f"{train.name} entered {self._block_name(block_id)}.",
            {"block_id": block_id},
        )

    def _find_lost_train(self, train: RunningTrain, block_id: str, direction: str) -> None:
        train.blocks = [block_id]
        train.directions = {block_id: direction}
        train.lost = False
        train.empty_since.clear()
        train.empty_while_moving.clear()
        train.end_of_track_block = None
        self._emit(
            "train_found",
            "info",
            train,
            f"{train.name} was found on {self._block_name(block_id)}.",
            {"block_id": block_id},
        )

    def _take_off_rear_blocks(self, now: float) -> None:
        """Take off each rear block that reads empty while a block further
        forward in its train reads occupied (design_v2.md "Trains", A block
        that reads empty is taken off at once or after the hold time)."""
        for train in sorted(self.trains.values(), key=lambda t: t.seq):
            if train.lost or self._keeps_its_blocks(train):
                continue
            while (
                len(train.blocks) > 1
                and train.blocks[0] not in self.occupied
                and any(b in self.occupied for b in train.blocks[1:])
            ):
                rear = train.blocks[0]
                self._take_off_block(train, rear)
                self._left_at[rear] = now
        for block_id, left_at in list(self._left_at.items()):
            if now - left_at >= self.hold_time_s:
                del self._left_at[block_id]

    def switches_just_left(self) -> Set[str]:
        """Every switch next to a block a train's rear left less than the
        hold time ago. The runtime does not throw these; the operator may
        (design_v2.md "Switches", The runtime does not throw a switch a train
        has just left)."""
        found: Set[str] = set()
        for block_id, left_at in self._left_at.items():
            if self._now - left_at < self.hold_time_s:
                found |= switches_next_to(self._network, block_id)
        return found

    def _keeps_its_blocks(self, train: RunningTrain) -> bool:
        """Whether the train is not moving while one of its blocks reads
        occupied. Such a train keeps every block: at 0 V it cannot have left
        one, and a block under cars that only have lights reads empty at 0 V
        (design_v2.md "Trains", A train that is not moving keeps its
        blocks)."""
        return self.state_of(train) != "moving" and any(
            b in self.occupied for b in train.blocks
        )

    def _note_empty_blocks(self, now: float) -> None:
        """Start the hold-time count for each block that began to read empty,
        and drop it for each block that reads occupied again."""
        for train in self.trains.values():
            if train.lost:
                continue
            if self._keeps_its_blocks(train):
                train.empty_since.clear()
                train.empty_while_moving.clear()
                continue
            for block_id in train.blocks:
                if block_id in self.occupied:
                    train.empty_since.pop(block_id, None)
                    train.empty_while_moving.pop(block_id, None)
                elif block_id not in train.empty_since:
                    train.empty_since[block_id] = now
                    train.empty_while_moving[block_id] = train.running or (
                        train.stopped_at is not None
                        and now - train.stopped_at < self.hold_time_s
                    )

    def _take_off_empty_blocks(self, now: float) -> None:
        """Take off each block that has read empty for the hold time; a train
        with no blocks left is lost or removed."""
        for train in sorted(self.trains.values(), key=lambda t: t.seq):
            if train.lost:
                continue
            due = [
                b
                for b in train.blocks
                if b in train.empty_since
                and now - train.empty_since[b] >= self.hold_time_s
            ]
            if not due:
                continue
            if len(due) < len(train.blocks):
                for block_id in due:
                    self._take_off_block(train, block_id)
                continue

            # Every block left has read empty for the hold time. The block
            # the train would keep is its front block.
            kept = train.front_block
            was_moving = train.empty_while_moving.get(kept, False)
            for block_id in list(train.blocks):
                if block_id != kept:
                    self._take_off_block(train, block_id)
            if was_moving:
                train.timed_stops = None
                train.lost = True
                train.knob_volts = 0.0
                train.stopped_at = now
                train.waiting_since = None
                train.waiting_reason = None
                self._end_claim(train)
                train.empty_since.clear()
                train.empty_while_moving.clear()
                self._emit(
                    "train_lost",
                    "warning",
                    train,
                    f"{train.name} was lost on {self._block_name(kept)}.",
                    {"block_id": kept},
                )
            else:
                self._remove(train)

    def _take_off_block(self, train: RunningTrain, block_id: str) -> None:
        if block_id == train.front_block and len(train.blocks) > 1:
            train.timed_stops = None
            # The claim was made from a block the train is no longer in; it
            # asks again from its new front block.
            self._end_claim(train)
            self._clear_asking(train)
            train.end_of_track_block = None
        train.blocks.remove(block_id)
        train.directions.pop(block_id, None)
        train.empty_since.pop(block_id, None)
        train.empty_while_moving.pop(block_id, None)
        self._emit(
            "train_block_removed",
            "info",
            train,
            f"{train.name} left {self._block_name(block_id)}.",
            {"block_id": block_id},
        )

    def _remove(self, train: RunningTrain) -> None:
        self._end_claim(train)
        self.trains.pop(train.train_id, None)
        self._emit(
            "train_removed",
            "info",
            train,
            f"{train.name} was removed.",
            {},
        )

    # --- item 3: stop trains ------------------------------------------

    def _stop_trains(self, now: float) -> None:
        """End of track (design_v2.md "When the runtime stops a train")."""
        for train in sorted(self.trains.values(), key=lambda t: t.seq):
            if not train.running:
                continue
            if train.end_of_track_block == train.front_block:
                continue
            if not self.wholly_in_front_block(train):
                continue
            walk = self.walk_ahead(train)
            if walk.block_id is None and walk.reason == TRACK_ENDS:
                train.end_of_track_block = train.front_block
                self._stop(train, now)
                self._emit(
                    "end_of_track",
                    "warning",
                    train,
                    f"{train.name} stopped where the track ends.",
                    {"block_id": train.front_block},
                )

    def _apply_timed_stops(self, now: float) -> None:
        for train in self.trains.values():
            stops = train.timed_stops
            if stops is None or stops.arrived_at_block != train.front_block:
                continue
            if not self.wholly_in_front_block(train):
                continue
            if stops.resume_at is None and train.running:
                self._stop(train, now)
                train.timed_stops = stops
                stops.resume_at = now + stops.seconds
            elif stops.resume_at is not None and now >= stops.resume_at:
                train.knob_volts = stops.volts
                train.stopped_at = None
                stops.resume_at = None
                stops.arrived_at_block = None

    def _stop(self, train: RunningTrain, now: float) -> None:
        """Set the knob to 0 V."""
        if train.knob_volts > 0.0 or train.stopped_at is None:
            train.stopped_at = now
        train.knob_volts = 0.0
        train.timed_stops = None
        train.waiting_since = None
        train.waiting_reason = None
        self._clear_asking(train)

    # --- item 4: end claims -------------------------------------------

    def _end_claims(self, now: float) -> None:
        """A claim ends when its train has been stopped for the hold time
        without entering the block."""
        for train in self.trains.values():
            if train.claim_block_id is None or not train.stopped:
                continue
            if train.stopped_at is not None and now - train.stopped_at >= self.hold_time_s:
                self._end_claim(train)

    def _end_claim(self, train: RunningTrain) -> None:
        train.claim_block_id = None
        train.claim_direction = None
        train.claim_switch_ids = ()
        train.alternating_switch = None

    def _clear_asking(self, train: RunningTrain) -> None:
        train.asked_for = None
        train.asked_step = None

    # --- item 5: give claims ------------------------------------------

    def disable_alternation(self, switch_id: str) -> None:
        self.alternate_next.pop(switch_id, None)
        for train in self.trains.values():
            if train.alternating_switch and train.alternating_switch[0] == switch_id:
                train.alternating_switch = None

    def _claim_walk(self, train: RunningTrain) -> Tuple[WalkResult, Optional[Tuple[str, int]]]:
        walk = self.walk_ahead(train)
        if len(walk.switch_ids) != 1:
            return walk, None
        switch_id = walk.switch_ids[0]
        position = self.alternate_next.get(switch_id)
        switch = self._network.switches.get(switch_id)
        if position is None or switch is None or switch.disabled or switch.manual or switch.motor_unavailable:
            return walk, None
        # For this layout, each leg leads directly to a different block.
        # Approaching from a leg cannot produce two different onward blocks.
        main, siding = (
            next_block_ahead(self._network, train.front_block, train.direction, {switch_id: side})
            for side in (0, 1)
        )
        if (
            main.switch_ids != (switch_id,) or siding.switch_ids != (switch_id,)
            or not main.block_id or not siding.block_id or main.block_id == siding.block_id
        ):
            return walk, None
        return (main, siding)[position], (switch_id, position)

    def _give_claims(self, now: float) -> None:
        """Serve the running trains in first-come order (design_v2.md
        "Claiming the next block")."""
        asking: List[RunningTrain] = []
        for train in self.trains.values():
            if not train.running or train.claim_block_id is not None:
                continue
            if train.timed_stops and train.timed_stops.arrived_at_block == train.front_block:
                # Keep power on the train's own blocks until its rear clears.
                continue
            walk, _ = self._claim_walk(train)
            wanted = walk.block_id or walk.reason or ""
            if train.asked_for != wanted or train.asked_step is None:
                train.asked_for = wanted
                train.asked_step = self._step_count
            asking.append(train)

        asking.sort(key=lambda train: (train.asked_step, train.seq))
        for train in asking:
            # Earlier trains on this step may have thrown or claimed a switch.
            walk, passage = self._claim_walk(train)
            reason = self._why_no_claim(train, walk)
            if reason is None and passage is not None:
                switch_id, position = passage
                switch = self._network.switches[switch_id]
                needs_throw = int(switch.position) != position or not switch.position_known
                if switch_id in self.held_switch_ids() or (
                    needs_throw and switch_id in self.switches_just_left()
                ):
                    reason = SWITCH_HELD
                elif needs_throw and (
                    self._throw_switch is None or not self._throw_switch(switch_id, position)
                ):
                    if not self._outputs_active():
                        return
                    reason = SWITCH_HELD
            if reason is None:
                train.alternating_switch = passage
                train.claim_block_id = walk.block_id
                train.claim_direction = walk.direction
                train.claim_switch_ids = walk.switch_ids
                train.waiting_since = None
                train.waiting_reason = None
                self._clear_asking(train)
                self._emit(
                    "claim_given",
                    "info",
                    train,
                    f"{train.name} claimed {self._block_name(walk.block_id)}.",
                    {"block_id": walk.block_id, "switch_ids": list(walk.switch_ids)},
                )
                continue
            if reason == TRACK_ENDS:
                # Not waiting: End of track stops it, and after that the
                # train is the operator's to drive.
                train.waiting_since = None
                train.waiting_reason = None
                continue
            if train.waiting_since is None and not self.wholly_in_front_block(train):
                # Keeps moving on its own blocks until the block behind
                # reads empty. A train that is already waiting stays
                # waiting: it stands at 0 V, so a front block that reads
                # empty for a moment must not put voltage back on it.
                train.waiting_since = None
                train.waiting_reason = None
                continue
            began = train.waiting_since is None
            if began:
                train.waiting_since = now
            if began or train.waiting_reason != reason:
                train.waiting_reason = reason
                self._emit(
                    "train_waiting",
                    "info",
                    train,
                    f"{train.name} is waiting ({reason}).",
                    {"reason": reason, "block_id": walk.block_id},
                )

    def _why_no_claim(self, train: RunningTrain, walk: WalkResult) -> Optional[str]:
        """None when the train can be given the claim, else why not."""
        if walk.block_id is None:
            return SWITCH_AGAINST if walk.reason == SWITCH_AGAINST else TRACK_ENDS
        if walk.block_id in train.blocks:
            return OWN_BLOCK
        if self.block_is_taken(walk.block_id):
            return BLOCK_TAKEN
        return None

    # --- item 6: set every block's voltage ----------------------------

    def block_voltages(self) -> Dict[str, float]:
        """The voltage each block gets on this step (design_v2.md "Track
        power"): a moving train's blocks and its claim get its knob voltage,
        signed by its line of travel, and every other block gets 0 V."""
        volts: Dict[str, float] = {block_id: 0.0 for block_id in self._network.blocks}
        for train in self.trains.values():
            if self.state_of(train) != "moving":
                continue
            for block_id in train.blocks:
                volts[block_id] = self._signed(train.knob_volts, train.directions.get(block_id))
            if train.claim_block_id is not None:
                volts[train.claim_block_id] = self._signed(
                    train.knob_volts, train.claim_direction
                )
        return volts

    @staticmethod
    def _signed(volts: float, direction: Optional[str]) -> float:
        return volts if direction != "reverse" else -volts

    def _set_voltages(self) -> None:
        for block_id, volts in sorted(self.block_voltages().items()):
            self._set_block_voltage(block_id, volts)

    # ------------------------------------------------------------------
    # Operator requests (design_v2.md "Running a train", "Trains")
    # ------------------------------------------------------------------

    def set_timed_stops(self, train_id: str, block_ids: Any, seconds: Any, live: bool) -> Result:
        train = self.trains.get(train_id)
        if train is None:
            return False, "unknown_train"
        if not live:
            return False, "not_live"
        if block_ids == []:
            train.timed_stops = None
            return True, None
        if train.lost:
            return False, "train_lost"
        if not train.running:
            return False, "start_train_first"
        if not isinstance(block_ids, list) or any(
            not isinstance(block_id, str) or block_id not in self._network.blocks
            for block_id in block_ids
        ):
            return False, "unknown_block"
        if type(seconds) not in (int, float) or not 1 <= seconds <= 3600:
            return False, "invalid_stop_duration"
        train.timed_stops = TimedStops(set(block_ids), float(seconds), train.knob_volts)
        return True, None

    def set_knob(self, train_id: str, volts: Any, now: float, live: bool) -> Result:
        train = self.trains.get(train_id)
        if train is None:
            return False, "unknown_train"
        if not live:
            return False, "not_live"
        if train.lost:
            return False, "train_lost"
        try:
            volts = float(volts)
        except (TypeError, ValueError):
            return False, "out_of_range"
        if not 0.0 <= volts <= MAX_TRACK_VOLTAGE:
            return False, "out_of_range"
        if volts <= 0.0:
            self._stop(train, now)
            return True, None
        train.timed_stops = None
        train.knob_volts = volts
        train.stopped_at = None
        return True, None

    def stop(self, train_id: str, now: float) -> Result:
        train = self.trains.get(train_id)
        if train is None:
            return False, "unknown_train"
        self._stop(train, now)
        return True, None

    def reverse(self, train_id: str, live: bool) -> Result:
        train = self.trains.get(train_id)
        if train is None:
            return False, "unknown_train"
        if not live:
            return False, "not_live"
        if train.lost:
            return False, "train_lost"
        if not train.stopped:
            return False, "not_stopped"
        if train.claim_block_id is not None:
            return False, "still_claiming"
        train.timed_stops = None
        train.blocks.reverse()
        train.directions = {
            block_id: opposite_direction(direction)
            for block_id, direction in train.directions.items()
        }
        train.destination_block_id = None
        train.end_of_track_block = None
        self._clear_asking(train)
        return True, None

    def stop_all(self, now: float, live: bool) -> Result:
        """Stop every train at once. While Live, every block is commanded to
        0 V in the same request."""
        for train in self.trains.values():
            self._stop(train, now)
        if live:
            for block_id in sorted(self._network.blocks):
                self._set_block_voltage(block_id, 0.0)
        return True, None

    def remove(self, train_id: str) -> Result:
        train = self.trains.get(train_id)
        if train is None:
            return False, "unknown_train"
        if train.running:
            return False, "not_stopped"
        self._remove(train)
        return True, None

    def leave_live(self, now: float) -> None:
        """Leaving Live stops every train and ends every claim. The trains
        themselves stay."""
        self.alternate_next.clear()
        for train in self.trains.values():
            self._stop(train, now)
            self._end_claim(train)

    def clear(self) -> None:
        """Start with no trains, as an activation does."""
        self.trains.clear()
        self.alternate_next.clear()
        self.occupied = set()
        self._left_at.clear()

    # ------------------------------------------------------------------

    def _block_name(self, block_id: Optional[str]) -> str:
        block = self._network.blocks.get(block_id) if block_id else None
        return (getattr(block, "name", "") or block_id or "?") if block else (block_id or "?")

    def _emit(
        self,
        operation: str,
        kind: str,
        train: RunningTrain,
        message: str,
        payload: Dict[str, Any],
    ) -> None:
        if self._emit_fn is not None:
            self._emit_fn(operation, kind, train.train_id, message, dict(payload))
