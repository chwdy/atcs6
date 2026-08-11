"""
Block Tests - Block state management, validation, and extensions

Run with: pytest tests/test_blocks.py -v
"""

import pytest

from track import (
    Tile,
    TRACK_NS,
    TRACK_EW,
    TRACK_NE,
    TRACK_NW,
    TRACK_SE,
    TRACK_SW,
    FORBIDDEN_TRACK_BITS,
)
from network import RailwayNetwork
from block import Block
from railway_signal import Signal, SignalType, SignalState
from enums import BlockState, PlatformSide, Direction, TrainState
from tests.train_test_helpers import set_train_span
from segment_validation import (
    is_crossing,
    crossing_disallowed_in_segment,
    validate_track_for_segment,
    validate_segment_assignment_continuity,
    get_adjacent_positions,
)
from signal_validation import (
    can_place_signal,
    get_valid_directions,
    is_valid_direction,
    validate_signal_placement,
    FORBIDDEN_SIGNAL_BITS,
    VALID_DETECTION_DIRECTIONS,
)
from entity_ids import segment_id_for_block
from tests.segment_test_helpers import make_segment_block, make_segment_block_with_tracks


@pytest.fixture
def network():
    return RailwayNetwork()


@pytest.fixture
def straight_ew_track(network):
    for x in range(5):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)
    return network


@pytest.fixture
def straight_ns_track(network):
    for y in range(5):
        track = Tile(x=0, y=y, track_bits=TRACK_NS)
        network.add_track(track)
    return network


@pytest.fixture
def y_switch(network):
    network.add_track(Tile(x=1, y=0, track_bits=TRACK_NS))
    network.add_track(Tile(x=1, y=1, track_bits=TRACK_NS | TRACK_NE))
    network.add_track(Tile(x=1, y=2, track_bits=TRACK_NS))
    network.add_track(Tile(x=2, y=0, track_bits=TRACK_SW))
    return network


@pytest.fixture
def diamond_crossing(network):
    network.add_track(Tile(x=0, y=1, track_bits=TRACK_EW))
    network.add_track(Tile(x=2, y=1, track_bits=TRACK_EW))
    network.add_track(Tile(x=1, y=0, track_bits=TRACK_NS))
    network.add_track(Tile(x=1, y=2, track_bits=TRACK_NS))
    network.add_track(Tile(x=1, y=1, track_bits=TRACK_NS | TRACK_EW))
    return network


class TestBlock:
    """Tests for Block class"""

    def test_state_transitions(self):
        block = Block(block_id="BLK_TEST")
        block.ordered_tiles = [(0, 0, "S")]
        assert block.state == BlockState.FREE
        assert block.is_free() == True

        # v1: reservation is one record.
        block.reserve_for_path("P1", "TRAIN_01", "forward")
        assert block.state == BlockState.RESERVED
        assert block.is_reserved() == True

        # Clear reservation before occupying so state is OCCUPIED.
        block.release_path_reservation()
        block.occupy(train_id="TRAIN_01")
        assert block.state == BlockState.OCCUPIED

        # CAUTION applies only when block is physically free.
        block.release()
        block.set_to_caution()
        assert block.state == BlockState.CAUTION

        block.clear()
        assert block.state == BlockState.FREE

    def test_occupy_block(self):
        block = Block(block_id="BLK_TEST")
        block.ordered_tiles = [(0, 0, "S")]
        # Pre-reserved for the same train, then physically enters.
        block.reserve_for_path("P1", "TRAIN_01", "forward")
        block.occupy(train_id="TRAIN_01")
        assert block.state == BlockState.OCCUPIED

    def test_release_block(self):
        block = Block(block_id="BLK_TEST")
        block.ordered_tiles = [(0, 0, "S")]
        block.reserve_for_path("P1", "TRAIN_01", "forward")
        block.occupy(train_id="TRAIN_01")
        block.release()
        block.release_path_reservation()
        assert block.state == BlockState.FREE

    def test_caution_state_cycle(self):
        block = Block(block_id="BLK_TEST")
        block.ordered_tiles = [(0, 0, "S")]
        assert block.state == BlockState.FREE
        block.reserve_for_path("P1", "TRAIN_01", "forward")
        assert block.state == BlockState.RESERVED
        block.occupy("TRAIN_01")
        assert block.state == BlockState.OCCUPIED
        block.release()
        block.release_path_reservation()
        block.set_to_caution()
        assert block.state == BlockState.CAUTION
        block.clear()
        assert block.state == BlockState.FREE

    def test_is_available_for_path_caution(self):
        """A physically-free CAUTION block is available for a path.

        Mirrors the reservation eligibility predicate in
        `BlockManager.reserve_blocks_for_path(...)`: per design_new.md
        "`CAUTION` vs reservation independence",
        `caution is not None` is not an eligibility failure by itself. A
        CAUTION block is available unless it is reserved by *another* path.
        Without this, any consumer that trusts the boolean would wrongly
        reject a reservable caution block.
        """
        from block import CautionState

        # Pure CAUTION (physically free, unreserved) → available.
        b = Block("B1", ordered_tiles=[(0, 0, "S")])
        b.caution = CautionState(exit_edge="E")
        assert b.state == BlockState.CAUTION
        assert b.is_available_for_path("THIS_PATH", "T1") is True

        # CAUTION reserved by THIS path → available (caution outranks RESERVED
        # in `Block.state`, but the reservation is ours).
        b = Block("B2", ordered_tiles=[(0, 0, "S")])
        b.reserve_for_path("THIS_PATH", "T1", "forward")
        b.caution = CautionState(exit_edge="E")
        assert b.state == BlockState.CAUTION
        assert b.is_available_for_path("THIS_PATH", "T1") is True

        # CAUTION reserved by ANOTHER path → NOT available.
        b = Block("B3", ordered_tiles=[(0, 0, "S")])
        b.reserve_for_path("OTHER_PATH", "T2", "forward")
        b.caution = CautionState(exit_edge="E")
        assert b.state == BlockState.CAUTION
        assert b.is_available_for_path("THIS_PATH", "T1") is False

    def test_is_available_for_path_non_caution(self):
        """Non-caution availability cases are unchanged by the caution fix."""
        # FREE → available.
        b = Block("B1", ordered_tiles=[(0, 0, "S")])
        assert b.is_available_for_path("THIS_PATH", "T1") is True

        # RESERVED by this path → available; by another path → not.
        b = Block("B2", ordered_tiles=[(0, 0, "S")])
        b.reserve_for_path("THIS_PATH", "T1", "forward")
        assert b.is_available_for_path("THIS_PATH", "T1") is True
        b = Block("B3", ordered_tiles=[(0, 0, "S")])
        b.reserve_for_path("OTHER_PATH", "T2", "forward")
        assert b.is_available_for_path("THIS_PATH", "T1") is False

        # OCCUPIED by this train → available; by another train → not.
        b = Block("B4", ordered_tiles=[(0, 0, "S")])
        b.occupy("T1")
        assert b.is_available_for_path("THIS_PATH", "T1") is True
        b = Block("B5", ordered_tiles=[(0, 0, "S")])
        b.occupy("T2")
        assert b.is_available_for_path("THIS_PATH", "T1") is False

        # Disabled → never available.
        b = Block("B6", ordered_tiles=[(0, 0, "S")], disabled=True)
        assert b.is_available_for_path("THIS_PATH", "T1") is False

    def test_serialization(self):
        block = Block(
            block_id="BLK_TEST", name="Test Block", length_cm=1000, speed_limit_kmh=80
        )
        block_dict = block.to_dict()
        assert block_dict["block_id"] == "BLK_TEST"
        assert block_dict["name"] == "Test Block"
        assert block_dict["length_cm"] == 1000
        assert block_dict["speed_limit_kmh"] == 80
        restored = Block.from_dict(block_dict)
        assert restored.block_id == block.block_id
        assert restored.name == block.name

    def test_load_drops_a_name_copied_onto_a_one_segment_block(self):
        """A save from before one-name-per-record loads with the copy gone.

        The block and its only member held one string; the segment keeps it and
        the block loads unnamed (design_new.md **Block grouping**, one name per
        record).
        """
        block = Block(block_id="BLK_LEFT", length_cm=100)
        block.segments[0].name = "Left"
        stored = block.to_layout_dict()
        stored["name"] = "Left"  # what a pre-rule save carried

        restored = Block.from_dict(stored)
        assert restored.name == ""
        assert restored.segments[0].name == "Left"

    def test_load_keeps_a_block_name_of_its_own(self):
        """Only an exact copy is dropped; a distinct block name survives."""
        block = Block(block_id="BLK_YARD", length_cm=100)
        block.segments[0].name = "Left"
        stored = block.to_layout_dict()
        stored["name"] = "Yard Feed"

        restored = Block.from_dict(stored)
        assert restored.name == "Yard Feed"
        assert restored.segments[0].name == "Left"


class TestDisabledBlock:
    """Tests for disabled block behavior."""

    def test_disabled_block_cannot_be_entered(self):
        block = Block(block_id="BLK_001", disabled=True)
        assert block.can_enter("TRAIN_01") == False

    def test_disabled_block_cannot_be_occupied(self):
        block = Block(block_id="BLK_001", disabled=True)
        result = block.occupy("TRAIN_01")
        assert result == False
        assert block.state == BlockState.FREE

    def test_disabled_block_is_not_free(self):
        block = Block(block_id="BLK_001", disabled=True)
        assert block.state == BlockState.FREE
        assert block.is_free() == False

    def test_enabled_block_works_normally(self):
        block = Block(block_id="BLK_001", disabled=False)
        assert block.is_free() == True
        assert block.can_enter("TRAIN_01") == True
        assert block.occupy("TRAIN_01") == True

    def test_disabled_block_serialization(self):
        block = Block(block_id="BLK_001", disabled=True)
        block_dict = block.to_dict()
        assert block_dict["disabled"] == True
        restored = Block.from_dict(block_dict)
        assert restored.disabled == True


class TestBlockExtension:
    """Tests for block extension functionality."""

    def test_extend_block_with_force_removes_signal_monitoring(self):
        network = RailwayNetwork()
        for i in range(4):
            network.add_track(Tile(0, i, TRACK_NS))
        make_segment_block(network, "BLOCK_A")
        network.assign_track_to_segment(0, 0, segment_id_for_block("BLOCK_A"))
        network.assign_track_to_segment(0, 1, segment_id_for_block("BLOCK_A"))
        signal = network.add_signal(0, 2, "N")
        signal.monitored_block_id = "BLOCK_A"

        block = network.extend_segment(segment_id_for_block("BLOCK_A"), [(0, 2)], force=True)
        positions = network.get_block_positions("BLOCK_A")
        assert (0, 2) in positions
        assert len(positions) == 3
        signal_after = network.signals.get(signal.signal_id)
        assert signal_after.monitored_block_id is None

    def test_blocks_can_exist_within_stations(self):
        network = RailwayNetwork()
        for x in range(4):
            network.add_track(Tile(x, 0, TRACK_EW))
        network.add_station(0, 0, station_id="STA_TEST", name="Test Station")
        network.extend_station("STA_TEST", [(1, 0), (2, 0), (3, 0)])
        make_segment_block(network, "PLATFORM_1")
        network.extend_segment(segment_id_for_block("PLATFORM_1"), [(0, 0), (1, 0)], force_overwrite=True)
        block = network.get_block("PLATFORM_1")
        assert block is not None
        assert len(network.get_block_positions("PLATFORM_1")) == 2


class TestBlockValidation:
    """Tests for segment_validation.py functions"""

    def test_is_crossing(self):
        assert is_crossing(TRACK_NS | TRACK_EW) == True
        assert is_crossing(TRACK_NW | TRACK_SE) == True
        assert is_crossing(TRACK_NE | TRACK_SW) == True
        assert is_crossing(TRACK_NS) == False
        assert is_crossing(TRACK_NS | TRACK_NE) == False

    def test_crossing_disallowed_in_segment(self):
        # The orthogonal NS+EW crossing (3) is allowed inside a block.
        assert crossing_disallowed_in_segment(TRACK_NS | TRACK_EW) == False
        # Diagonal crossings remain disallowed.
        assert crossing_disallowed_in_segment(TRACK_NW | TRACK_SE) == True
        assert crossing_disallowed_in_segment(TRACK_NE | TRACK_SW) == True
        # Non-crossings are unaffected.
        assert crossing_disallowed_in_segment(TRACK_NS) == False

    def test_validate_track_for_segment_allows_orthogonal_crossing(self, diamond_crossing):
        # The orthogonal NS+EW crossing (track_bits 3) may belong to a block.
        network = diamond_crossing
        track = network.get_track(1, 1)
        is_valid, _ = validate_track_for_segment(track, network)
        assert is_valid == True

    def test_validate_track_for_segment_rejects_diagonal_crossing(self, network):
        # Diagonal crossings (NW+SE, NE+SW) stay out of blocks.
        network.add_track(Tile(x=0, y=0, track_bits=TRACK_NW | TRACK_SE))
        track = network.get_track(0, 0)
        is_valid, msg = validate_track_for_segment(track, network)
        assert is_valid == False
        assert "crossing" in msg.lower()

    def test_validate_track_for_segment_rejects_switch(self, y_switch):
        network = y_switch
        track = network.get_track(1, 1)
        result = validate_track_for_segment(track, network)
        assert result == False or "switch" in str(result).lower()

    def test_get_adjacent_positions(self):
        adjacent = get_adjacent_positions(5, 5)
        assert len(adjacent) <= 8
        assert (5, 4) in adjacent
        assert (6, 5) in adjacent
        assert (5, 6) in adjacent
        assert (4, 5) in adjacent


class TestSignalValidation:
    """Tests for signal_validation.py functions"""

    def test_can_place_signal_on_simple_track(self):
        result = can_place_signal(TRACK_NS)
        assert result == True
        result = can_place_signal(TRACK_EW)
        assert result == True

    def test_cannot_place_signal_on_switch(self):
        switch_bits = TRACK_NS | TRACK_NE
        result = can_place_signal(switch_bits)
        assert result == False

    def test_cannot_place_signal_on_crossing(self):
        crossing_bits = TRACK_NS | TRACK_EW
        result = can_place_signal(crossing_bits)
        assert result == False

    def test_get_valid_directions(self):
        directions = get_valid_directions(TRACK_NS)
        assert "N" in directions
        assert "S" in directions
        assert len(directions) == 2
        directions = get_valid_directions(TRACK_EW)
        assert "E" in directions
        assert "W" in directions

    def test_forbidden_signal_bits(self):
        for forbidden_bits in FORBIDDEN_SIGNAL_BITS:
            result = can_place_signal(forbidden_bits)
            assert result == False


class TestMultiBlockOccupancy:
    """Tests for multi-block occupancy implementation."""

    def test_short_train_single_block(self):
        network = RailwayNetwork()
        track1 = Tile(0, 0, track_bits=TRACK_EW, length=1.0)
        track1.segment_id = segment_id_for_block("BLOCK_1")
        network.add_track(track1)
        block1 = Block(block_id="BLOCK_1", length_cm=1000.0)
        network.blocks["BLOCK_1"] = block1
        from train import Train, TrainCar
        from enums import Direction

        from train_controller import create_controller_for_train

        train = Train(
            train_id="T1",
            consist=[TrainCar("CAR1", "LOCOMOTIVE", 5.0)],
        )
        network.add_train(train)
        controller = create_controller_for_train("T1", network)
        set_train_span(
            controller,
            (0, 0),
            facing_direction=Direction.EAST,
            occupied_blocks=["BLOCK_1"],
        )

        # Occupied blocks now live on the controller's live span.
        occupied = controller.get_current_span().occupied_blocks
        assert len(occupied) == 1
        assert occupied[0] == "BLOCK_1"

    def test_long_train_two_blocks(self):
        network = RailwayNetwork()
        track1 = Tile(0, 0, track_bits=TRACK_EW, length=1.0)
        track1.segment_id = segment_id_for_block("BLOCK_1")
        network.add_track(track1)
        track2 = Tile(1, 0, track_bits=TRACK_EW, length=1.0)
        track2.segment_id = segment_id_for_block("BLOCK_2")
        network.add_track(track2)
        block1 = Block(block_id="BLOCK_1", length_cm=10000.0)
        block2 = Block(block_id="BLOCK_2", length_cm=10000.0)
        network.blocks["BLOCK_1"] = block1
        network.blocks["BLOCK_2"] = block2
        from train import Train, TrainCar
        from enums import Direction

        from train_controller import create_controller_for_train

        train = Train(
            train_id="T1",
            consist=[TrainCar("CAR1", "LOCOMOTIVE", 15.0)],
        )
        network.add_train(train)
        controller = create_controller_for_train("T1", network)
        set_train_span(
            controller,
            (1, 0),
            facing_direction=Direction.EAST,
            occupied_tiles=[(0, 0), (1, 0)],
            occupied_blocks=["BLOCK_1", "BLOCK_2"],
        )

        # Occupied blocks now live on the controller's live span.
        occupied = controller.get_current_span().occupied_blocks
        assert len(occupied) == 2
        assert "BLOCK_1" in occupied
        assert "BLOCK_2" in occupied


class TestBlockManager:
    """Tests for BlockManager class."""

    def test_block_manager_initialization(self):
        from block_manager import BlockManager

        blocks = {
            "B1": Block("B1", "Block 1"),
            "B2": Block("B2", "Block 2"),
        }
        manager = BlockManager(blocks)
        assert manager.blocks is blocks
        assert manager.get_block_state("B1") == BlockState.FREE
        assert manager.get_block_state("B2") == BlockState.FREE

    def test_occupy_block_from_free(self):
        from block_manager import BlockManager

        blocks = {"B1": Block("B1")}
        manager = BlockManager(blocks)
        result = manager.occupy_block("B1", "TRAIN_1")
        assert result == True
        assert blocks["B1"].state == BlockState.OCCUPIED
        assert blocks["B1"].occupied_by == "TRAIN_1"

    def test_occupy_block_idempotent(self):
        from block_manager import BlockManager

        blocks = {"B1": Block("B1")}
        manager = BlockManager(blocks)
        manager.occupy_block("B1", "TRAIN_1")
        result = manager.occupy_block("B1", "TRAIN_1")
        assert result == True
        assert blocks["B1"].state == BlockState.OCCUPIED

    def test_set_block_to_caution(self):
        from block_manager import BlockManager

        blocks = {"B1": Block("B1")}
        manager = BlockManager(blocks)
        # v1: set_block_to_caution() only transitions OCCUPIED -> CAUTION.
        blocks["B1"].occupied_by = "TRAIN_1"
        result = manager.set_block_to_caution("B1")
        assert result == True
        assert blocks["B1"].state == BlockState.CAUTION

    def test_clear_block_from_caution(self):
        from block_manager import BlockManager

        from block import CautionState

        blocks = {"B1": Block("B1")}
        manager = BlockManager(blocks)
        # v1: CAUTION is computed when block.caution is set and not occupied.
        blocks["B1"].caution = CautionState(exit_edge="E")
        result = manager.clear_block("B1")
        assert result == True
        assert blocks["B1"].state == BlockState.FREE
        assert blocks["B1"].occupied_by is None

    def test_is_block_available(self):
        from block_manager import BlockManager

        blocks = {
            "B1": Block("B1"),
            "B2": Block("B2"),
        }
        manager = BlockManager(blocks)
        assert manager.is_block_available("B1") == True
        manager.occupy_block("B2", "TRAIN_1")
        assert manager.is_block_available("B2") == False
        assert manager.is_block_available("B2", "TRAIN_1") == True

    def test_block_manager_integration_with_network(self):
        network = RailwayNetwork()
        assert network.block_manager is None
        block = make_segment_block(network, "B1", "Block 1")
        assert network.block_manager is not None
        assert network.block_manager.get_block_state("B1") == BlockState.FREE
        network.block_manager.occupy_block("B1", "TRAIN_1")
        assert block.state == BlockState.OCCUPIED
        assert block.occupied_by == "TRAIN_1"

    def test_occupy_block_rejects_wrong_train_on_reserved_block(self):
        from block_manager import BlockManager

        blocks = {"B1": Block("B1", ordered_tiles=[(0, 0, "S")])}
        # v1: reserve via the canonical record so reserved_for_train = TRAIN_A.
        blocks["B1"].reserve_for_path("P1", "TRAIN_A", "forward")
        manager = BlockManager(blocks)
        # Without a simulation_controller wired, the manager should fall back
        # to the silent-False path (fatal-conflict reporting needs the sim
        # controller).
        result = manager.occupy_block("B1", "TRAIN_B")
        assert result == False
        assert blocks["B1"].state == BlockState.RESERVED
        assert blocks["B1"].reserved_for_train == "TRAIN_A"

    def test_occupy_block_from_caution_consumes_same_train_reservation(self):
        """A reservation can live under a CAUTION because CAUTION outranks
        RESERVED in `Block.state` priority. Per design_new.md
        "Block-manager model", clean entry must "consume any same-train
        reservation by clearing block.reservation" — for entry from CAUTION
        as well as from RESERVED. Without this, the reservation survives the
        train's traversal and the block flips to RESERVED (green) instead of
        FREE (blue) once caution clears later, surfacing as a phantom
        reservation behind the departed train.
        """
        from block_manager import BlockManager
        from block import CautionState

        block = Block("B1", ordered_tiles=[(0, 0, "S")])
        # Prior train left → CAUTION. Our train's path then reserved this
        # block before our train physically arrived. Reservation sits
        # underneath the caution.
        block.caution = CautionState(exit_edge="E")
        block.reserve_for_path("P_OURS", "TRAIN_OURS", "forward")
        assert block.state == BlockState.CAUTION
        assert block.reserved_for_train == "TRAIN_OURS"

        manager = BlockManager({"B1": block})
        assert manager.occupy_block("B1", "TRAIN_OURS")
        assert block.state == BlockState.OCCUPIED
        assert block.reservation is None, (
            "Clean entry from CAUTION must consume the same-train "
            "reservation per design_new.md \"Block-manager model\"."
        )

        # Now simulate the rest of the lifecycle: train tail vacates,
        # caution sets directionally, then caution clears (e.g. switch
        # flip downstream). Block must end FREE, not RESERVED.
        block.occupied_by = None
        block.caution = CautionState(exit_edge="E")
        assert block.state == BlockState.CAUTION
        block.caution = None
        assert block.state == BlockState.FREE, (
            "After the train has gone and caution clears, the block "
            "must be FREE (blue). RESERVED (green) here means a phantom "
            "reservation survived the traversal."
        )

    def test_occupy_block_from_caution_rejects_cross_train_reservation(self):
        """If a CAUTION block holds a reservation for a DIFFERENT train,
        clean entry by another train must hard-reject — mirrors the
        RESERVED case in design_new.md "Block-manager model". Previously the
        CAUTION-entry branch silently overrode the conflicting reservation.
        """
        from block_manager import BlockManager
        from block import CautionState

        block = Block("B1", ordered_tiles=[(0, 0, "S")])
        block.caution = CautionState(exit_edge="E")
        block.reserve_for_path("P_OTHER", "TRAIN_OTHER", "forward")
        assert block.state == BlockState.CAUTION  # CAUTION outranks RESERVED

        manager = BlockManager({"B1": block})
        result = manager.occupy_block("B1", "TRAIN_OURS")
        assert result is False
        # State must be unchanged.
        assert block.occupied_by is None
        assert block.reserved_for_train == "TRAIN_OTHER"


class _FakeSimulationController:
    """Minimal simulation controller stand-in for fatal-conflict tests."""

    def __init__(self):
        self.failed_runtime = False
        self.failed_runtime_payload = None
        self.calls = 0

    def enter_failed_runtime(self, payload):
        self.failed_runtime = True
        self.failed_runtime_payload = payload
        self.calls += 1


class TestFatalConflict:
    """`BlockManager.occupy_block(...)` must route physical-entry conflicts
    through `SimulationController.enter_failed_runtime(...)` instead of
    silently returning False."""

    def test_reserved_for_other_train_emits_fatal(self):
        from block_manager import BlockManager
        from runtime_payloads import FatalConflictKind

        blocks = {"B1": Block("B1", ordered_tiles=[(0, 0, "S")])}
        blocks["B1"].reserve_for_path("P1", "TRAIN_A", "forward")
        sim = _FakeSimulationController()
        manager = BlockManager(blocks, simulation_controller=sim)

        ok = manager.occupy_block("B1", "TRAIN_B")
        assert ok is False
        assert sim.failed_runtime is True
        payload = sim.failed_runtime_payload
        assert payload is not None
        assert payload.conflict_kind == FatalConflictKind.RESERVED_FOR_OTHER_TRAIN
        assert payload.resource_type == "block"
        assert payload.resource_id == "B1"
        assert payload.incoming_train_id == "TRAIN_B"
        assert payload.prior_owner_train_id == "TRAIN_A"

    def test_occupied_by_other_train_emits_fatal(self):
        from block_manager import BlockManager
        from runtime_payloads import FatalConflictKind

        blocks = {"B1": Block("B1")}
        blocks["B1"].occupied_by = "TRAIN_A"
        sim = _FakeSimulationController()
        manager = BlockManager(blocks, simulation_controller=sim)

        ok = manager.occupy_block("B1", "TRAIN_B")
        assert ok is False
        assert sim.failed_runtime is True
        payload = sim.failed_runtime_payload
        assert payload.conflict_kind == FatalConflictKind.OCCUPIED_BY_OTHER_TRAIN
        assert payload.prior_owner_train_id == "TRAIN_A"

    def test_disabled_block_entry_emits_fatal_with_none_owner(self):
        from block_manager import BlockManager
        from runtime_payloads import FatalConflictKind

        blocks = {"B1": Block("B1")}
        blocks["B1"].disabled = True
        sim = _FakeSimulationController()
        manager = BlockManager(blocks, simulation_controller=sim)

        ok = manager.occupy_block("B1", "TRAIN_A")
        assert ok is False
        assert sim.failed_runtime is True
        payload = sim.failed_runtime_payload
        assert payload.conflict_kind == FatalConflictKind.DISABLED_RESOURCE_ENTRY
        assert payload.prior_owner_train_id is None

    def test_no_back_reference_silently_returns_false(self):
        """Without a wired controller, conflicts still return False without
        crashing — tests and editor sessions exercise this path."""
        from block_manager import BlockManager

        blocks = {"B1": Block("B1")}
        blocks["B1"].occupied_by = "TRAIN_A"
        manager = BlockManager(blocks)
        assert manager.occupy_block("B1", "TRAIN_B") is False


class TestDirectionalCaution:
    """Directional `_reevaluate_caution_blocks()` must only inspect the
    forward-adjacent block in `caution_exit_edge`, never the follower
    behind."""

    def _build_three_block_chain(self):
        """A -> B -> C laid out west-to-east on row y=0.

        Forward direction is east; ``caution_exit_edge`` for vacating B
        toward C is "E". The test ensures the walk inspects C, not A.
        """
        from network import RailwayNetwork

        network = RailwayNetwork()
        # Three single-tile blocks A, B, C at x=0, x=1, x=2.
        for x in range(3):
            network.add_track(Tile(x=x, y=0, track_bits=TRACK_EW))
        # Assign each tile to its own block.
        a = make_segment_block(network, "A", "A")
        b = make_segment_block(network, "B", "B")
        c = make_segment_block(network, "C", "C")
        network.assign_track_to_segment(0, 0, segment_id_for_block("A"))
        network.assign_track_to_segment(1, 0, segment_id_for_block("B"))
        network.assign_track_to_segment(2, 0, segment_id_for_block("C"))
        # ordered_tiles records (x, y, entry_edge) for forward traversal.
        a.ordered_tiles = [(0, 0, "W")]
        b.ordered_tiles = [(1, 0, "W")]
        c.ordered_tiles = [(2, 0, "W")]
        return network

    def test_forward_neighbor_occupied_keeps_caution(self):
        from block import CautionState

        network = self._build_three_block_chain()
        manager = network.block_manager
        # Setup: C is OCCUPIED. B becomes CAUTION with exit_edge = "E".
        network.blocks["C"].occupied_by = "TRAIN_FORWARD"
        network.blocks["B"].caution = CautionState(exit_edge="E")

        manager._reevaluate_caution_blocks()

        # Because the next forward block (C) is OCCUPIED, B's caution stays.
        assert network.blocks["B"]._is_caution is True
        assert network.blocks["B"].caution_exit_edge == "E"
        assert network.blocks["B"].state == BlockState.CAUTION

    def test_follower_behind_does_not_block_clear(self):
        """A follower behind B (entering A) must NOT keep B in CAUTION when
        the forward neighbor C is free."""
        from block import CautionState

        network = self._build_three_block_chain()
        manager = network.block_manager
        # B in CAUTION pointing east. C is FREE. A is OCCUPIED by a follower.
        network.blocks["B"].caution = CautionState(exit_edge="E")
        network.blocks["A"].occupied_by = "FOLLOWER"

        manager._reevaluate_caution_blocks()

        # B's caution should clear: forward (C) is free, A is irrelevant.
        assert network.blocks["B"]._is_caution is False
        assert network.blocks["B"].caution_exit_edge is None
        assert network.blocks["B"].state == BlockState.FREE

    def test_no_reachable_forward_block_clears_caution(self):
        """If walking forward runs off the network, caution clears because
        nothing protectable lies ahead."""
        from block import CautionState

        network = self._build_three_block_chain()
        manager = network.block_manager
        # Vacate C with exit_edge east — nothing is east of C.
        network.blocks["C"].caution = CautionState(exit_edge="E")

        manager._reevaluate_caution_blocks()

        assert network.blocks["C"]._is_caution is False
        assert network.blocks["C"].caution_exit_edge is None


if __name__ == "__main__":
    print("Run tests with: pytest tests/test_blocks.py -v")
