"""The v2 runtime step on the rig layout, driven by scripted detector
readings (design_v2.md "Runtime step", "Trains", "Claiming the next block",
"When a train waits", "When the runtime stops a train", "When a train
overruns", "Track power").

The rig is a loop of five blocks. With both switches set to main, a train
running Left forward goes Left -> Main -> Right -> Up -> Left. Right and Up
are run in `reverse` that way round, so their voltage is negative.
"""

import logging

import pytest

import demo7_1
from enums import SwitchState
from train_runtime import TrainRuntime

LEFT_SWITCH = "SW_7_5"
RIGHT_SWITCH = "SW_11_9"
STEP_S = 0.1


class Rig:
    """The rig layout, a `TrainRuntime`, and what it wrote."""

    def __init__(self):
        logging.disable(logging.CRITICAL)
        try:
            self.network = demo7_1.create_comprehensive_demo()
        finally:
            logging.disable(logging.NOTSET)
        self.ids = {b.name: block_id for block_id, b in self.network.blocks.items()}
        self.names = {block_id: name for name, block_id in self.ids.items()}
        self.volts = {}
        self.writes = 0
        self.events = []
        self.now = 1000.0
        self.live = True
        self.runtime = TrainRuntime(self.network, self._set_voltage, self._emit)
        self.set_switches(0, 0)

    def _set_voltage(self, block_id, volts):
        self.writes += 1
        self.volts[self.names[block_id]] = volts

    def _emit(self, operation, kind, train_id, message, payload):
        self.events.append((operation, kind, train_id, payload))

    def set_switches(self, left, right):
        self.network.switches[LEFT_SWITCH].position = SwitchState(left)
        self.network.switches[RIGHT_SWITCH].position = SwitchState(right)

    def step(self, *occupied, seconds=STEP_S):
        """Run steps for `seconds` with these blocks reading occupied."""
        steps = max(1, round(seconds / STEP_S))
        for _ in range(steps):
            self.now += STEP_S
            self.runtime.step({self.ids[n] for n in occupied}, self.now, self.live)

    def knob(self, train_id, volts):
        return self.runtime.set_knob(train_id, volts, self.now, self.live)

    def train(self, train_id):
        return self.runtime.trains[train_id]

    def blocks(self, train_id):
        return [self.names[b] for b in self.train(train_id).blocks]

    def state(self, train_id):
        return self.runtime.state_of(self.train(train_id))

    def claim(self, train_id):
        return self.names.get(self.train(train_id).claim_block_id)

    def operations(self):
        return [event[0] for event in self.events]


@pytest.fixture
def rig():
    return Rig()


# --- trains appear and disappear ------------------------------------------


def test_an_occupied_block_with_no_train_gets_one(rig):
    rig.step("Left")
    assert list(rig.runtime.trains) == ["T1"]
    assert rig.blocks("T1") == ["Left"]
    assert rig.state("T1") == "stopped"
    assert rig.train("T1").direction == "forward"
    assert rig.operations() == ["train_appeared"]


def test_setup_shows_trains_and_writes_no_voltage(rig):
    rig.live = False
    rig.step("Left", "Right")
    assert sorted(rig.blocks(t) for t in rig.runtime.trains) == [["Left"], ["Right"]]
    assert rig.writes == 0


def test_a_stopped_train_lifted_off_is_removed_after_the_hold_time(rig):
    rig.step("Left")
    rig.step(seconds=4.8)
    assert "T1" in rig.runtime.trains
    rig.step(seconds=0.4)
    assert rig.runtime.trains == {}
    assert rig.operations()[-1] == "train_removed"


def test_a_block_that_reads_occupied_again_drops_the_count(rig):
    rig.step("Left")
    rig.step(seconds=4.0)
    rig.step("Left")
    rig.step(seconds=4.0)
    assert "T1" in rig.runtime.trains


def test_a_train_id_is_never_used_twice(rig):
    rig.step("Left")
    rig.step(seconds=6.0)
    rig.step("Left")
    assert list(rig.runtime.trains) == ["T2"]


def test_a_stopped_train_across_a_boundary_keeps_the_block_under_its_lit_cars(rig):
    """At 0 V only the motor car is seen, so the block under the lit cars
    reads empty while the train stands in it."""
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    rig.step("Left", "Main")
    rig.runtime.stop("T1", rig.now)
    rig.step("Main", seconds=8.0)  # Left reads empty at 0 V
    assert rig.blocks("T1") == ["Left", "Main"]
    assert rig.runtime.block_is_taken(rig.ids["Left"])
    assert LEFT_SWITCH in rig.runtime.held_switch_ids()
    # It runs again: now the detectors say the rear really has left.
    rig.knob("T1", 6.0)
    rig.step("Main")
    assert rig.blocks("T1") == ["Main"]


def test_a_stopped_train_across_a_boundary_that_is_lifted_off_is_removed(rig):
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    rig.step("Left", "Main")
    rig.runtime.stop("T1", rig.now)
    rig.step("Left", "Main", seconds=6.0)
    rig.step(seconds=5.5)
    assert rig.runtime.trains == {}


# --- one train round the loop ---------------------------------------------


def test_turning_the_knob_claims_the_next_block_and_powers_both(rig):
    rig.step("Left")
    assert rig.knob("T1", 6.0) == (True, None)
    rig.step("Left")
    assert rig.state("T1") == "moving"
    assert rig.claim("T1") == "Main"
    assert rig.volts == {"Left": 6.0, "Main": 6.0, "Right": 0.0, "Up": 0.0, "Sideline": 0.0}


def test_a_claimed_block_that_reads_occupied_becomes_the_front_block(rig):
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    rig.step("Left", "Main")
    assert rig.blocks("T1") == ["Left", "Main"]
    assert rig.claim("T1") == "Right"
    # Right is run in reverse this way round, so its voltage is negative.
    assert rig.volts == {"Left": 6.0, "Main": 6.0, "Right": -6.0, "Up": 0.0, "Sideline": 0.0}


def test_the_rear_block_is_taken_off_at_once_when_the_train_reads_further_forward(rig):
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    rig.step("Left", "Main")
    assert rig.blocks("T1") == ["Left", "Main"]
    rig.step("Main")
    assert rig.blocks("T1") == ["Main"]
    assert rig.volts["Left"] == 0.0
    assert not rig.runtime.block_is_taken(rig.ids["Left"])


def test_a_train_that_reads_on_no_block_keeps_its_block_and_voltage_for_the_hold_time(rig):
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    rig.step(seconds=4.5)  # on the switch: no block reads occupied
    assert rig.blocks("T1") == ["Left"]
    assert rig.state("T1") == "moving"
    assert rig.volts["Left"] == 6.0 and rig.volts["Main"] == 6.0
    rig.step("Main")
    assert rig.blocks("T1") == ["Main"]
    assert "train_lost" not in rig.operations()


def test_a_front_block_that_reads_empty_stays_for_the_hold_time_and_then_ends_the_claim(rig):
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    rig.step("Left", "Main")  # the nose touches Main
    assert rig.claim("T1") == "Right"
    rig.step("Left", seconds=4.5)  # Main reads empty again; Left still reads occupied
    assert rig.blocks("T1") == ["Left", "Main"]
    rig.step("Left", seconds=1.0)
    assert rig.blocks("T1") == ["Left"]
    # The claim on Right was made from Main. The train asks again from Left.
    assert rig.claim("T1") == "Main"


def test_three_laps_without_waiting_for_its_own_block(rig):
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    lap = [("Left", "Main"), ("Main", "Right"), ("Right", "Up"), ("Up", "Left")]
    for behind, ahead in lap * 3:
        rig.step(behind, ahead, seconds=0.5)
        rig.step(ahead, seconds=1.0)
        assert rig.blocks("T1") == [ahead]
    assert "train_waiting" not in rig.operations()
    assert list(rig.runtime.trains) == ["T1"]


def test_reverse_after_a_stop_leaves_no_old_block_at_the_front(rig):
    """The rig run of 2026-11-11: a train stopped and reversed while an old
    block was still on its list, and the old block became its front."""
    rig.set_switches(1, 0)
    rig.step("Sideline")
    rig.runtime.reverse("T1", rig.live)  # Sideline reverse -> Left
    rig.knob("T1", 6.0)
    rig.step("Sideline")
    rig.step("Sideline", "Left", seconds=0.5)
    rig.step("Left", seconds=0.5)
    rig.step("Left", "Up", seconds=0.5)
    rig.runtime.stop("T1", rig.now)
    rig.step("Left", "Up", seconds=5.2)
    assert rig.blocks("T1") == ["Left", "Up"]
    assert rig.runtime.reverse("T1", rig.live) == (True, None)
    assert rig.blocks("T1") == ["Up", "Left"]
    rig.knob("T1", 6.0)
    rig.step("Left", "Up")
    assert rig.claim("T1") == "Sideline"
    rig.step("Left", "Sideline", seconds=0.5)
    assert rig.blocks("T1") == ["Left", "Sideline"]
    assert list(rig.runtime.trains) == ["T1"]


def test_one_train_runs_all_the_way_round(rig):
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    for behind, ahead in [("Left", "Main"), ("Main", "Right"), ("Right", "Up"), ("Up", "Left")]:
        rig.step(behind, ahead, seconds=1.0)
        rig.step(ahead, seconds=6.0)
        assert rig.blocks("T1") == [ahead]
        assert rig.state("T1") == "moving"
    assert list(rig.runtime.trains) == ["T1"]
    assert "train_lost" not in rig.operations()


def test_the_knob_changes_the_voltage_on_the_next_step(rig):
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    rig.knob("T1", 9.0)
    rig.step("Left")
    assert rig.volts["Left"] == 9.0 and rig.volts["Main"] == 9.0


# --- two trains ------------------------------------------------------------


def test_a_train_waits_while_the_next_block_is_taken_and_starts_by_itself(rig):
    rig.step("Main")  # T1
    rig.step("Main", "Left")  # T2
    assert rig.blocks("T1") == ["Main"] and rig.blocks("T2") == ["Left"]
    rig.knob("T2", 6.0)
    rig.step("Main", "Left")
    assert rig.state("T2") == "waiting"
    assert rig.train("T2").waiting_reason == "block_taken"
    assert rig.volts["Left"] == 0.0 and rig.volts["Main"] == 0.0

    # T1 runs on to Right; Main stays T1's block for the hold time.
    rig.knob("T1", 6.0)
    rig.step("Main", "Left")
    rig.step("Main", "Right", "Left", seconds=1.0)
    assert rig.state("T2") == "waiting"
    rig.step("Right", "Left")  # T1's tail has left Main
    assert rig.blocks("T1") == ["Right"]
    assert rig.state("T2") == "moving"
    assert rig.claim("T2") == "Main"
    assert rig.volts["Left"] == 6.0 and rig.volts["Main"] == 6.0


def test_a_train_keeps_moving_until_it_is_wholly_in_its_front_block(rig):
    rig.step("Right")  # T1, the train in the way
    rig.step("Right", "Left")  # T2
    rig.knob("T2", 6.0)
    rig.step("Right", "Left")
    rig.step("Right", "Left", "Main")  # T2's front is now Main; Right is taken
    assert rig.blocks("T2") == ["Left", "Main"]
    assert rig.state("T2") == "moving"
    assert rig.volts["Left"] == 6.0 and rig.volts["Main"] == 6.0 and rig.volts["Right"] == 0.0
    rig.step("Right", "Main")  # its rear has left Left
    assert rig.state("T2") == "waiting"
    assert rig.volts["Left"] == 0.0 and rig.volts["Main"] == 0.0


def test_claims_are_given_first_come_first_served(rig):
    rig.step("Up")  # T1 stands in Up
    rig.step("Up", "Left")  # T2 on Left
    rig.step("Up", "Left", "Right")  # T3 on Right
    assert rig.blocks("T2") == ["Left"] and rig.blocks("T3") == ["Right"]
    assert rig.runtime.reverse("T2", rig.live) == (True, None)  # Left reverse -> Up
    assert rig.runtime.reverse("T3", rig.live) == (True, None)  # Right reverse -> Up
    rig.knob("T3", 6.0)
    rig.step("Up", "Left", "Right")  # T3 asks first
    rig.knob("T2", 6.0)
    rig.step("Up", "Left", "Right")
    assert rig.state("T2") == "waiting" and rig.state("T3") == "waiting"
    rig.step("Left", "Right", seconds=6.0)  # T1 is lifted off
    assert rig.claim("T3") == "Up"
    assert rig.claim("T2") is None and rig.state("T2") == "waiting"


# --- switches ---------------------------------------------------------------


def test_a_train_waits_at_a_switch_set_against_it(rig):
    rig.step("Sideline")
    rig.knob("T1", 6.0)
    rig.step("Sideline")
    assert rig.state("T1") == "waiting"
    assert rig.train("T1").waiting_reason == "switch_against"
    rig.set_switches(0, 1)
    rig.step("Sideline")
    assert rig.state("T1") == "moving" and rig.claim("T1") == "Right"


def test_held_switches(rig):
    rig.step("Left")  # T1
    rig.step("Left", "Up")  # T2
    assert rig.runtime.held_switch_ids() == set()
    rig.knob("T1", 6.0)
    rig.step("Left", "Up")
    assert rig.claim("T1") == "Main"
    assert rig.runtime.held_switch_ids() == {LEFT_SWITCH}
    rig.step("Left", "Main", "Up")  # stands across the left switch
    assert LEFT_SWITCH in rig.runtime.held_switch_ids()
    rig.step("Main", "Up")  # the rear has left Left
    assert rig.blocks("T1") == ["Main"]
    # The claim on Right holds the right switch. The left switch is no longer
    # held, but the runtime does not throw it for the hold time.
    assert rig.runtime.held_switch_ids() == {RIGHT_SWITCH}
    assert rig.runtime.switches_just_left() == {LEFT_SWITCH}
    rig.step("Main", "Up", seconds=5.2)
    assert rig.runtime.switches_just_left() == set()


# --- stopping ---------------------------------------------------------------


def test_stop_keeps_the_claim_for_the_hold_time_at_zero_volts(rig):
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    assert rig.runtime.stop("T1", rig.now) == (True, None)
    rig.step("Left")
    assert rig.claim("T1") == "Main"
    assert rig.volts["Left"] == 0.0 and rig.volts["Main"] == 0.0
    assert rig.runtime.reverse("T1", rig.live) == (False, "still_claiming")
    rig.step("Left", seconds=5.2)
    assert rig.claim("T1") is None
    assert rig.runtime.reverse("T1", rig.live) == (True, None)


def test_a_stopped_train_that_rolls_into_its_claim_has_entered_its_own_block(rig):
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    rig.runtime.stop("T1", rig.now)
    rig.step("Left", "Main")
    assert rig.blocks("T1") == ["Left", "Main"]
    assert "train_overran" not in rig.operations()


def test_stop_all_zeroes_every_block_in_the_same_request(rig):
    rig.step("Left", "Up")
    rig.knob("T1", 6.0)
    rig.knob("T2", 6.0)
    rig.step("Left", "Up")
    assert rig.volts["Left"] == 6.0
    rig.runtime.stop_all(rig.now, rig.live)
    assert set(rig.volts.values()) == {0.0}
    assert rig.state("T1") == "stopped" and rig.state("T2") == "stopped"


def test_stop_all_writes_nothing_while_not_live(rig):
    rig.live = False
    rig.step("Left")
    rig.runtime.stop_all(rig.now, rig.live)
    assert rig.writes == 0


def test_leaving_live_stops_every_train_and_ends_every_claim(rig):
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    rig.runtime.leave_live(rig.now)
    assert rig.state("T1") == "stopped" and rig.claim("T1") is None
    assert list(rig.runtime.trains) == ["T1"]


def test_the_runtime_stops_a_train_where_the_track_ends_once(rig):
    del rig.network.tracks[(7, 5)]  # take the left switch off the layout
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    assert rig.state("T1") == "stopped"
    assert rig.operations()[-1] == "end_of_track"
    # The operator turns the knob up again: the train is theirs to drive.
    rig.knob("T1", 4.0)
    rig.step("Left", seconds=1.0)
    assert rig.state("T1") == "moving"
    assert rig.volts["Left"] == 4.0
    assert rig.operations().count("end_of_track") == 1


# --- reverse ----------------------------------------------------------------


def test_reverse_turns_the_train_round(rig):
    rig.step("Left")
    assert rig.runtime.reverse("T1", rig.live) == (True, None)
    assert rig.train("T1").direction == "reverse"
    rig.knob("T1", 6.0)
    rig.step("Left")
    assert rig.claim("T1") == "Up"
    assert rig.volts["Left"] == -6.0 and rig.volts["Up"] == 6.0


def test_reverse_swaps_the_front_and_rear_blocks(rig):
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    rig.step("Left", "Main")
    assert rig.runtime.reverse("T1", rig.live) == (False, "not_stopped")
    rig.runtime.stop("T1", rig.now)
    rig.step("Left", "Main", seconds=5.2)
    assert rig.runtime.reverse("T1", rig.live) == (True, None)
    assert rig.blocks("T1") == ["Main", "Left"]
    rig.knob("T1", 6.0)
    rig.step("Left", "Main")
    assert rig.claim("T1") == "Up"
    assert rig.volts["Main"] == -6.0 and rig.volts["Left"] == -6.0 and rig.volts["Up"] == 6.0


# --- overrun, lost, found ---------------------------------------------------


def test_a_train_that_rolls_past_where_it_stopped_takes_the_next_block(rig):
    rig.step("Left")
    rig.step("Left", seconds=6.0)
    rig.knob("T1", 6.0)
    rig.runtime.stop("T1", rig.now)  # stopped before any claim was given
    rig.step("Left", "Main")
    assert rig.blocks("T1") == ["Left", "Main"]
    assert rig.state("T1") == "stopped"
    assert "train_overran" in rig.operations()
    assert list(rig.runtime.trains) == ["T1"]


def test_a_moving_train_whose_blocks_all_read_empty_becomes_a_lost_train(rig):
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    rig.step(seconds=4.5)
    assert rig.state("T1") == "moving" and rig.volts["Left"] == 6.0
    rig.step(seconds=1.0)
    assert rig.state("T1") == "lost"
    assert rig.blocks("T1") == ["Left"]
    assert rig.claim("T1") is None
    assert rig.volts["Left"] == 0.0
    assert rig.runtime.block_is_taken(rig.ids["Left"])
    assert rig.knob("T1", 6.0) == (False, "train_lost")
    assert rig.runtime.reverse("T1", rig.live) == (False, "train_lost")
    assert rig.runtime.held_switch_ids() == {LEFT_SWITCH}


def test_a_lost_train_is_found_on_its_own_block(rig):
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    rig.step(seconds=6.0)
    rig.step("Left")
    assert rig.state("T1") == "stopped"
    assert list(rig.runtime.trains) == ["T1"]
    assert rig.operations()[-1] == "train_found"


def test_a_lost_train_is_found_on_the_block_next_to_its_own(rig):
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    rig.step(seconds=6.0)
    rig.step("Main")
    assert rig.blocks("T1") == ["Main"]
    assert rig.state("T1") == "stopped"
    assert rig.train("T1").direction == "forward"
    assert list(rig.runtime.trains) == ["T1"]


def test_no_other_train_can_claim_a_lost_trains_block(rig):
    rig.step("Main")  # T1
    rig.step("Main", "Left")  # T2
    rig.knob("T1", 6.0)
    rig.step("Main", "Left")
    rig.step("Left", seconds=6.0)  # T1 is lost on Main
    assert rig.state("T1") == "lost"
    rig.knob("T2", 6.0)
    rig.step("Left")
    assert rig.state("T2") == "waiting"
    assert rig.train("T2").waiting_reason == "block_taken"


# --- requests ---------------------------------------------------------------


def test_the_knob_is_refused(rig):
    rig.step("Left")
    assert rig.knob("nobody", 6.0) == (False, "unknown_train")
    assert rig.knob("T1", -1.0) == (False, "out_of_range")
    assert rig.knob("T1", 12.5) == (False, "out_of_range")
    rig.live = False
    assert rig.knob("T1", 6.0) == (False, "not_live")


def test_removing_a_train(rig):
    rig.step("Left")
    rig.knob("T1", 6.0)
    assert rig.runtime.remove("T1") == (False, "not_stopped")
    rig.runtime.stop("T1", rig.now)
    assert rig.runtime.remove("T1") == (True, None)
    rig.step("Left")
    assert list(rig.runtime.trains) == ["T2"]


def test_a_train_entry_has_exactly_the_documented_fields(rig):
    rig.step("Left")
    rig.knob("T1", 6.0)
    rig.step("Left")
    entry = rig.runtime.trains_payload()["T1"]
    assert set(entry) == {
        "train_id", "name", "length_cm", "direction", "knob_volts", "timed_stops",
        "destination_block_id", "lost", "stopped_at", "waiting_since",
        "blocks", "state", "waiting_reason", "next_block_id", "claimed_block_id",
    }
    assert entry["state"] == "moving"
    assert entry["blocks"] == [{"block_id": rig.ids["Left"], "reads_occupied": True}]
    assert entry["next_block_id"] == rig.ids["Main"]
    assert entry["claimed_block_id"] == rig.ids["Main"]


def test_timed_stop_waits_for_rear_and_departs_once(rig):
    rig.step("Left")
    rig.knob("T1", 6)
    assert rig.runtime.set_timed_stops("T1", [rig.ids["Main"]], 2, True) == (True, None)
    rig.step("Left")
    rig.step("Left", "Main")
    assert rig.state("T1") == "moving"
    assert rig.claim("T1") is None
    assert rig.volts["Right"] == 0

    rig.step("Main")
    assert rig.state("T1") == "stopped"
    assert rig.volts["Main"] == 0
    assert rig.runtime.train_entry(rig.train("T1"))["timed_stops"]["resume_in_s"] == 2
    rig.step(seconds=2.1)
    assert rig.state("T1") == "stopped"
    rig.step("Main")
    assert rig.state("T1") == "moving"
    assert rig.claim("T1") == "Right"
    assert abs(rig.volts["Main"]) == 6
    rig.step("Main")
    assert rig.state("T1") == "moving"


@pytest.mark.parametrize("cancel", ["knob", "stop", "stop_all", "reverse", "leave_live"])
def test_manual_actions_cancel_a_pending_restart(rig, cancel):
    rig.step("Left")
    rig.knob("T1", 6)
    rig.runtime.set_timed_stops("T1", [rig.ids["Main"]], 2, True)
    rig.step("Left")
    rig.step("Main")
    assert rig.train("T1").timed_stops.resume_at is not None
    actions = {
        "knob": lambda: rig.knob("T1", 0),
        "stop": lambda: rig.runtime.stop("T1", rig.now),
        "stop_all": lambda: rig.runtime.stop_all(rig.now, True),
        "reverse": lambda: rig.runtime.reverse("T1", True),
        "leave_live": lambda: rig.runtime.leave_live(rig.now),
    }
    actions[cancel]()
    rig.step("Main", seconds=3)
    assert rig.train("T1").timed_stops is None
    assert rig.state("T1") == "stopped"
    assert rig.volts["Main"] == 0
