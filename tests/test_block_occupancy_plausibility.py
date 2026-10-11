"""A train is not entering a block it is being driven away from
(design_new.md "Power", Attribution).

Adjacency says two things are next to each other and nothing about which way
either is going. On its own it reads current appearing *behind* a train driving
away from it as that train arriving there — which hands the app a position the
train is travelling away from, and quietly absorbs a second object on the track
into a train the app believes it is steering.

`TrainPhysics.could_be_entering` is the gate: a moving train could only have
entered a block in front of it. A train that is not moving is not being driven
away from anything, so every neighbouring block stays plausible for it — the
model never knows where inside a block a train sits, so it cannot rule out a
locomotive bridging an insulated joint it shows the train well clear of.

These tests reuse the straight-line harness from the seam-cap suite: six 100cm
blocks BLK_0..BLK_5 on one E-W run, trains placed facing EAST.
"""

from tests.test_block_occupancy_cap import build_line, place, wire_detector


def rolling(controller, speed_kmh):
    controller.physics_sync.current_speed_kmh = speed_kmh


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def test_a_moving_train_could_be_entering_the_block_in_front_of_it():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    wire_detector(sim, detected=set(), confirmed=set())
    rolling(controller, 16.0)

    assert physics.could_be_entering("BLK_3") is True


def test_a_moving_train_is_not_entering_the_block_behind_it():
    """The one the user named: voltage is driving the train away from BLK_1, so
    current there is not this train arriving."""
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    wire_detector(sim, detected=set(), confirmed=set())
    rolling(controller, 16.0)

    assert physics.could_be_entering("BLK_1") is False


def test_a_stopped_train_could_be_entering_either_neighbour():
    """A stationary locomotive may be bridging a joint the model shows it clear
    of, on either side. Nothing the app holds rules that out, so nothing here
    may call it a second object."""
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    wire_detector(sim, detected=set(), confirmed=set())
    rolling(controller, 0.0)

    assert physics.could_be_entering("BLK_1") is True
    assert physics.could_be_entering("BLK_3") is True


def test_a_block_the_span_already_covers_is_always_plausible():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    wire_detector(sim, detected=set(), confirmed=set())
    rolling(controller, 16.0)

    assert physics.could_be_entering("BLK_2") is True


def test_the_forward_walk_stops_at_an_unconfirmed_detected_block():
    """Reaching BLK_4 means having crossed BLK_3, whose detector says it is
    clear. The train cannot be there, however far forward it lies."""
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    wire_detector(
        sim,
        detected={f"BLK_{i}" for i in range(6)},
        confirmed={"BLK_2", "BLK_4"},
    )
    rolling(controller, 16.0)

    assert physics.could_be_entering("BLK_4") is False


def test_a_train_that_crossed_two_confirmed_blocks_stays_one_train():
    """A genuinely fast train has both blocks confirmed in the same read, so the
    walk reaches both and neither is read as a second object."""
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    wire_detector(
        sim,
        detected={f"BLK_{i}" for i in range(6)},
        confirmed={"BLK_2", "BLK_3", "BLK_4"},
    )
    rolling(controller, 16.0)

    assert physics.could_be_entering("BLK_3") is True
    assert physics.could_be_entering("BLK_4") is True


class _ActivePathStub:
    """Minimal active path so `received_path_plan` resolves non-None."""

    def __init__(self, resource_sequence):
        self.path_id = "P"
        self.resource_sequence = list(resource_sequence)
        self.zero_reservation_end_standoff = False


class _DispatcherStub:
    def __init__(self, path):
        self._path = path

    def get_active_path(self, train_id):
        return self._path


def test_a_train_past_its_authority_end_is_permissive_like_a_stopped_train():
    """A train at or past the end of its authority is being brought to a stop,
    not driven onward, so — like a stopped train — its travel direction no longer
    reliably says which side it is leaving, and every neighbour stays plausible.
    This is what lets a block a train overran past its authority end attribute to
    it (owned, not ghosted); the drive is cut to 0 V separately the moment the
    head is past authority (see test_lighting_calibration)."""
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    wire_detector(sim, detected={f"BLK_{i}" for i in range(6)}, confirmed={"BLK_2"})
    rolling(controller, 16.0)

    # No authority record: a plain moving train still vetoes the block behind it.
    assert physics.could_be_entering("BLK_1") is False

    # It holds a path plan but nothing is reserved ahead of its head — it has run
    # to its authority end, so the block behind (one it may have overrun onto)
    # becomes plausible and attributes to it.
    controller.path_dispatcher = _DispatcherStub(_ActivePathStub(["BLK_2"]))
    assert physics.could_be_entering("BLK_1") is True
    assert sim.block_occupancy_detector.touching_trains_for_block("BLK_1") == ["T"]


def test_the_walk_crosses_a_clear_block_the_train_still_holds():
    """The train pulled ahead onto BLK_4 while the model still shows it on BLK_2.
    BLK_3 between them reads clear, but the train still holds it
    (`Block.occupied_by`, its confirmed clear not yet committed) — its own body
    clearing behind the head, not foreign track. The walk crosses it, so BLK_4
    stays this train's rather than becoming a second object the app then loses
    the real train to."""
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    wire_detector(
        sim,
        detected={f"BLK_{i}" for i in range(6)},
        confirmed={"BLK_2", "BLK_4"},
    )
    network.blocks["BLK_3"].occupy("T")
    rolling(controller, 16.0)

    assert physics.could_be_entering("BLK_4") is True


# ---------------------------------------------------------------------------
# The gate as attribution sees it
# ---------------------------------------------------------------------------


def test_attribution_refuses_a_block_behind_a_moving_train():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    wire_detector(sim, detected={f"BLK_{i}" for i in range(6)}, confirmed={"BLK_2"})
    rolling(controller, 16.0)

    detector = sim.block_occupancy_detector
    assert detector.touching_trains_for_block("BLK_1") == []


def test_attribution_accepts_a_block_behind_a_stopped_train():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    wire_detector(sim, detected={f"BLK_{i}" for i in range(6)}, confirmed={"BLK_2"})
    rolling(controller, 0.0)

    detector = sim.block_occupancy_detector
    assert detector.touching_trains_for_block("BLK_1") == ["T"]


def test_an_unwired_gate_credits_nothing():
    """With no plausibility gate, no entry is credited to an adjacent train —
    the same fail-safe reading an unwired reachability walk takes."""
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    wire_detector(sim, detected={f"BLK_{i}" for i in range(6)}, confirmed={"BLK_2"})
    rolling(controller, 0.0)

    detector = sim.block_occupancy_detector
    detector.can_enter = None

    assert detector.touching_trains_for_block("BLK_1") == []
