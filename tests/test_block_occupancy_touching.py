"""Which trains a newly occupied block touches (design_new.md "Block occupancy
detection", Touching).

A block touches a train when it is next to a block in that train's block list
(the blocks `Block.occupied_by` names it on), across undetected track with no
other block in between. A directional train — one that holds a live
reservation and is not stopped — is touched on its head side only: current
appearing behind a train driving away from it is not that train arriving. Any
other train is touched on either side, because a stationary locomotive may
bridge a joint the model shows it clear of. Nothing here reads the calculated
span, except the frozen span of a ghosted train, which stands in for its empty
block list.

These tests reuse the straight-line harness from the seam-cap suite: six 100cm
blocks BLK_0..BLK_5 on one E-W run, trains placed facing EAST.
"""

from tests.test_block_occupancy_cap import build_line, place, wire_detector


def rolling(controller, speed_kmh):
    controller.physics_sync.current_speed_kmh = speed_kmh


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


def holding_a_path(controller):
    controller.path_dispatcher = _DispatcherStub(_ActivePathStub(["BLK_2"]))


def train_on_blk_2():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=2, length_cm=40.0)
    wire_detector(sim, detected={f"BLK_{i}" for i in range(6)}, confirmed={"BLK_2"})
    assert network.blocks["BLK_2"].occupied_by == "T"
    return network, sim, controller, physics


# ---------------------------------------------------------------------------
# The head side
# ---------------------------------------------------------------------------


def test_the_head_side_is_the_first_block_past_the_front_of_the_list():
    _network, _sim, _controller, physics = train_on_blk_2()
    assert physics.head_side_block() == "BLK_3"


def test_a_block_gained_on_this_read_moves_the_head_side_on():
    """Each block a train gains on a read joins its list for the next pass, so
    a train that crossed two blocks between reads stays one train."""
    _network, _sim, _controller, physics = train_on_blk_2()
    assert physics.head_side_block({"BLK_3"}) == "BLK_4"


def test_the_head_side_follows_the_points_past_the_end_of_the_path():
    """The block a train overran onto past its authority end is on its head
    side, though its path stops short of it."""
    _network, _sim, controller, physics = train_on_blk_2()
    holding_a_path(controller)
    assert physics.head_side_block() == "BLK_3"


# ---------------------------------------------------------------------------
# Directional and not
# ---------------------------------------------------------------------------


def test_a_directional_train_is_touched_on_its_head_side_only():
    _network, sim, controller, _physics = train_on_blk_2()
    holding_a_path(controller)
    rolling(controller, 16.0)
    detector = sim.block_occupancy_detector

    assert detector.touching_trains_for_block("BLK_3") == ["T"]
    assert detector.touching_trains_for_block("BLK_1") == []


def test_a_stopped_train_is_touched_on_either_side():
    """A stationary locomotive may be bridging a joint the model shows it clear
    of, on either side."""
    _network, sim, controller, _physics = train_on_blk_2()
    holding_a_path(controller)
    rolling(controller, 0.0)
    detector = sim.block_occupancy_detector

    assert detector.touching_trains_for_block("BLK_1") == ["T"]
    assert detector.touching_trains_for_block("BLK_3") == ["T"]


def test_a_train_holding_no_reservation_is_touched_on_either_side():
    _network, sim, controller, _physics = train_on_blk_2()
    rolling(controller, 16.0)
    detector = sim.block_occupancy_detector

    assert detector.touching_trains_for_block("BLK_1") == ["T"]
    assert detector.touching_trains_for_block("BLK_3") == ["T"]


# ---------------------------------------------------------------------------
# Adjacency is the block list, not the span
# ---------------------------------------------------------------------------


def test_a_block_with_a_clear_block_between_it_and_the_train_does_not_touch():
    """Reaching BLK_4 means having crossed BLK_3, whose detector says it is
    clear. The train cannot be there."""
    _network, sim, controller, _physics = train_on_blk_2()
    rolling(controller, 16.0)
    assert sim.block_occupancy_detector.touching_trains_for_block("BLK_4") == []


def test_a_block_past_a_clear_block_the_train_still_holds_touches():
    """The train pulled ahead onto BLK_4 while the model still shows it on
    BLK_2. BLK_3 reads clear, but the train still holds it — its own body
    clearing behind the head — so BLK_4 is next to its list's front."""
    network, sim, controller, _physics = train_on_blk_2()
    network.blocks["BLK_3"].occupy("T")
    holding_a_path(controller)
    rolling(controller, 16.0)
    assert sim.block_occupancy_detector.touching_trains_for_block("BLK_4") == ["T"]


def test_a_span_alone_does_not_touch():
    """A train whose calculated span covers a block it holds no block next to
    is not credited the block: attribution reads the block list."""
    network, sim, controller, _physics = train_on_blk_2()
    network.blocks["BLK_2"].occupied_by = None
    rolling(controller, 0.0)
    detector = sim.block_occupancy_detector
    assert detector.touching_trains_for_block("BLK_2") == []
    assert detector.touching_trains_for_block("BLK_3") == []


# ---------------------------------------------------------------------------
# A ghosted train's retained span
# ---------------------------------------------------------------------------


def test_a_ghosted_trains_retained_span_stands_in_for_its_block_list():
    network, sim, controller, _physics = train_on_blk_2()
    network.blocks["BLK_2"].occupied_by = None
    controller.presence_unknown = True
    detector = sim.block_occupancy_detector

    assert detector.touching_trains_for_block("BLK_2") == ["T"]
    assert detector.touching_trains_for_block("BLK_1") == ["T"]
    assert detector.touching_trains_for_block("BLK_4") == []


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_an_unwired_head_side_credits_a_directional_train_nothing():
    """With no head-side answer, a directional train touches nothing — the same
    fail-safe reading an unwired reachability walk takes."""
    _network, sim, controller, _physics = train_on_blk_2()
    holding_a_path(controller)
    rolling(controller, 16.0)
    detector = sim.block_occupancy_detector
    detector.head_side = None

    assert detector.touching_trains_for_block("BLK_3") == []
