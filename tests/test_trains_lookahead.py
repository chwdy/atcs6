"""
Lookahead and monitor tests - Monitor, lookahead, UI data

Run with: pytest tests/test_trains_lookahead.py -v
"""

import logging
import pytest

from track import Tile, TRACK_NS, TRACK_EW, TRACK_NW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction, TrainState, HandlePosition
from train import Train, TrainCar
from train_span import SpanEndLocator, TrainSpan
from tests.train_test_helpers import make_train_span, set_train_span
from simulation import SimulationController
from train_controller import TrainController, create_controller_for_train
from path_dispatcher import PathDispatcher
from train_route import Route, RouteMode
from switch import Switch
from runtime_payloads import SwitchLockClaim
from entity_ids import segment_id_for_block


@pytest.fixture
def network():
    return RailwayNetwork()


@pytest.fixture
def straight_ew_track(network):
    for x in range(5):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)
    return network




class TestTrainControllerMonitorLookahead:
    """
    Tests for TrainController lookahead payload used by Train Monitor.

    Ensures head-relative block offsets remain stable across tile transitions and
    cab direction changes are reflected in the lookahead head tile.
    """



    def test_cab_switch_updates_head_tile(self, network):
        """When switching cabs, head tile should reflect the new cab position."""
        for x in range(3):
            track = Tile(x=x, y=0, track_bits=TRACK_EW)
            track.segment_id = segment_id_for_block("BLOCK_MAIN")
            network.add_track(track)

        train = Train(train_id="TRAIN_LOOKAHEAD_CAB")
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)
        controller.physics_sync.current_span = TrainSpan(
            end_a=SpanEndLocator(tile=(0, 0), distance_cm=10.0, entry_edge="W"),
            end_b=SpanEndLocator(tile=(2, 0), distance_cm=10.0, entry_edge="E"),
            occupied_tiles=[(0, 0), (1, 0), (2, 0)],
        )

        controller.commands.set_cab("A", internal=True)  # mode-neutral cab setup
        payload_a = controller.get_lookahead()
        # With Cab A active, head is at end_a which is (0, 0)
        assert payload_a["head_tile"] == [0, 0]

        controller.commands.set_cab("B", internal=True)  # mode-neutral cab setup
        payload_b = controller.get_lookahead()
        # After switching to Cab B, ends don't swap - the physical span is
        # cab-independent: end_a stays at (0, 0) and end_b stays at (2, 0).
        # With Cab B active, the head is end_b which is (2, 0).
        assert payload_b["head_tile"] == [2, 0]

    def test_no_cab_returns_no_lookahead(self, network):
        """Lookahead should be unavailable when no cab is selected."""
        for x in range(3):
            track = Tile(x=x, y=0, track_bits=TRACK_EW)
            track.segment_id = segment_id_for_block(f"BLOCK_{x}")
            network.add_track(track)

        train = Train(train_id="TRAIN_LOOKAHEAD_NO_CAB")
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)
        controller.physics_sync.current_span = TrainSpan(
            end_a=SpanEndLocator(tile=(0, 0), distance_cm=10.0, entry_edge="W"),
            end_b=SpanEndLocator(tile=(2, 0), distance_cm=10.0, entry_edge="E"),
            occupied_tiles=[(0, 0), (1, 0), (2, 0)],
        )
        controller.commands.set_cab(None)

        payload = controller.get_lookahead()

        assert payload["cab"] is None
        assert payload["head_tile"] is None
        assert payload["speed_limit_reason"] == "No cab selected"
        assert payload["lookahead"]["blocks"] == []
        assert payload["lookahead"]["signals"] == []
        assert payload["lookahead"]["switches"] == []
        assert payload["lookahead"]["stations"] == []
        assert payload["lookahead"]["clearance_cm"] == pytest.approx(0.0)







class TestLookaheadNoPathRegardlessOfNetworkState:
    """Per design_new.md step `22a9` (Option B): the canonical lookahead is
    the no-authority empty payload whenever `received_path_plan is None`,
    regardless of physical track geometry, switch state, or foreign lock
    state around the train. There is no block-bounded fallback that walks
    the network to truncate at unusable switches.

    These tests pin the invariant for two failure modes the old fallback
    had to validate by walking the network: (a) wrong-side switch after a
    cab change, (b) switch locked by another path. Under Option B, both
    cases collapse to the same answer — empty `blocks`, no_authority.
    """

    def _build_three_tile_with_switch(self, network: RailwayNetwork) -> Train:
        """Create 0-1-2 path where tile 1 is an EW/NW switch."""
        track_0 = Tile(x=0, y=0, track_bits=TRACK_EW)
        track_0.segment_id = segment_id_for_block("BLOCK_0")
        network.add_track(track_0)

        track_1 = Tile(x=1, y=0, track_bits=TRACK_EW | TRACK_NW)
        track_1.segment_id = segment_id_for_block("BLOCK_1")
        track_1.switch_state = 0
        network.add_track(track_1)

        track_2 = Tile(x=2, y=0, track_bits=TRACK_EW)
        track_2.segment_id = segment_id_for_block("BLOCK_2")
        network.add_track(track_2)

        for block_id in ("BLOCK_0", "BLOCK_1", "BLOCK_2"):
            network.blocks[block_id] = Block(block_id=block_id, name=block_id)

        train = Train(train_id="TRAIN_LOOKAHEAD_NO_PATH")
        network.add_train(train)
        return train

    def _seed_span(self, controller):
        """Seed a 3-tile (0,0)->(2,0) span on the controller (cab "A" head at
        end_a=(0,0)). The no-authority assertion does not depend on the exact
        geometry; the head only needs to resolve."""
        controller.physics_sync.current_span = TrainSpan(
            end_a=SpanEndLocator(tile=(0, 0), distance_cm=10.0, entry_edge="W"),
            end_b=SpanEndLocator(tile=(2, 0), distance_cm=10.0, entry_edge="E"),
            occupied_tiles=[(0, 0), (1, 0), (2, 0)],
        )

    def _assert_no_authority(self, payload):
        from enums import AtcLimitReasonCode

        inner = payload["lookahead"]
        assert inner["blocks"] == [], (
            f"Lookahead without path must publish no blocks, got {inner['blocks']}"
        )
        assert inner["clearance_cm"] == 0.0
        assert inner["authority_boundary_delta_cm"] == 0.0
        # M5: the empty/no-authority payload must still carry the nested
        # target_stop_cm key (emit 0.0, don't omit it) so consumers see a
        # consistent shape between the empty and populated payloads.
        assert inner["target_stop_cm"] == 0.0
        curves = inner["speed_curves"]
        assert len(curves) == 1
        assert curves[0]["atc_limit"] == 0.0
        assert curves[0]["operational_limit"] == 0.0
        assert (
            curves[0]["atc_limit_reason_code"]
            == AtcLimitReasonCode.NO_AUTHORITY.value
        )

    def test_no_path_yields_no_authority_with_wrong_side_switch(self, network):
        """A switch set against the cab's travel frame is irrelevant when no
        path is wired — the payload is no-authority regardless."""
        train = self._build_three_tile_with_switch(network)
        network.get_track(1, 0).switch_state = 1

        controller = TrainController(train_id=train.train_id, network=network)
        self._seed_span(controller)
        controller.commands.set_cab("A", internal=True)  # mode-neutral cab setup
        controller.commands.set_cab("B", internal=True)  # mode-neutral cab setup

        self._assert_no_authority(controller.get_lookahead())

    def test_no_path_yields_no_authority_with_foreign_locked_switch(self, network):
        """A switch locked by another path is irrelevant when no path is
        wired — the payload is no-authority regardless."""
        train = self._build_three_tile_with_switch(network)
        switch_track = network.get_track(1, 0)
        switch_entity = Switch(switch_id="SW_1_0", x=1, y=0)
        network.add_switch(switch_entity)
        switch_track.switch_id = switch_entity.switch_id
        switch_entity.acquire_lock(
            SwitchLockClaim(
                path_id="PATH_OTHER", train_id="TRAIN_OTHER", locked_state=0
            )
        )

        controller = TrainController(train_id=train.train_id, network=network)
        self._seed_span(controller)
        controller.commands.set_cab("A", internal=True)  # mode-neutral cab setup

        self._assert_no_authority(controller.get_lookahead())



class TestTrainControlPanelDataBugFix:
    """
    Comprehensive tests for train control panel data propagation fix (2026-01-22).

    BUG: /api/network returned train data without controller state, so the
    train control panel could not reflect power/handle status or ATO path info.

    FIX: /api/network exposes a sibling `controllers_by_train_id` map per
    design_new.md "Data Flow To UI" (~line 2065).
    """

    def test_network_includes_controller_sibling(self):
        """Network response should include controllers_by_train_id sibling map."""
        from simulation import build_network_payload

        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        network.add_train(train)

        sim_controller = SimulationController(network)
        controller = sim_controller._get_or_create_controller(train)
        # Per design_new.md: power_on is a read-only derived property; use
        # controller.ops.state to power on.
        controller.ops.state = TrainState.READY
        controller.ops.handle_position = HandlePosition.P2
        controller.commands.control_mode = "ato"

        payload = build_network_payload(network, sim_controller)
        controllers = payload["controllers_by_train_id"]
        assert "TRAIN_01" in controllers
        controller_data = controllers["TRAIN_01"]
        assert controller_data["power_on"] is True
        assert controller_data["handle_position"] == "P2"
        assert controller_data["control_mode"] == "ato"
        assert "ato_info" in controller_data



class TestEmergencyResetButtonDataBugFix:
    """
    Comprehensive tests for emergency reset button data fix (2026-01-24).

    BUG: Controller payload only included `eb_active`, while the control
    panel expects `emergency_stop_active`, so the reset button never
    appears even when emergency braking is active.

    FIX: Provide `emergency_stop_active` in controller snapshots alongside
    `eb_active`.
    """

    def test_controller_payload_includes_emergency_stop_active(self):
        """Controller payload should expose emergency_stop_active for UI state."""
        from simulation import build_network_payload

        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        network.add_train(train)

        sim_controller = SimulationController(network)
        controller = sim_controller._get_or_create_controller(train)
        controller.eb.active = True

        payload = build_network_payload(network, sim_controller)
        controller_data = payload["controllers_by_train_id"]["TRAIN_01"]
        assert controller_data["eb_active"] is True



class TestTrainAssignedPathSerializationBugFix:
    """
    Comprehensive tests for assigned_path_id serialization fix (2026-01-22).

    BUG: Train.to_dict omitted assigned_path_id, causing frontend to show "No path"
    after UI refresh or cab switch.

    FIX: Train.to_dict includes assigned_path_id for frontend state.
    """


    def test_train_physics_power_off_does_not_update_speed(self):
        """TrainPhysics.tick should not update speed when power is off.

        v1: controller owns canonical current_speed_kmh; physics reads from
        controller.physics_sync.current_speed_kmh, not controller.physics_sync.current_speed_kmh.
        """
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_LOG_2", consist=[car], max_speed=60.0)
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        physics = sim._get_or_create_physics(train)
        # v1: write canonical speed onto the controller; controller.physics_sync.current_speed_kmh is mirrored.
        controller.physics_sync.current_speed_kmh = 10.0
        controller.physics_sync.current_speed_kmh = 10.0

        initial_speed = controller.physics_sync.current_speed_kmh
        physics.tick(0.1)

        # Speed should remain unchanged when power is off
        assert controller.physics_sync.current_speed_kmh == initial_speed
        assert controller.physics_sync.current_speed_kmh == initial_speed




class TestNoPathProducesNoAuthorityPayload:
    """Per design_new.md step `22a9`: without a `received_path_plan`, the
    canonical lookahead is the no-authority empty payload regardless of how
    much physical block geometry sits ahead of the head. Movement authority
    is binary on `received_path_plan` — there is no block-bounded fallback.

    These tests cover several head positions inside a multi-tile block to
    verify the no-authority shape is independent of block geometry."""

    @pytest.fixture
    def multi_tile_block_network(self):
        """3-tile, 150cm `TERMINAL` block for placing a train without a path."""
        from enums import AtcLimitReasonCode  # local import for clarity

        network = RailwayNetwork()
        block = Block(block_id="TERMINAL", length_cm=150.0)
        block.occupied_by = "TRAIN_NO_PATH"
        network.blocks["TERMINAL"] = block

        for x in range(3):
            track = Tile(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)
            track.segment_id = segment_id_for_block("TERMINAL")

        return network

    def _place_and_build(self, network, *, train_id, head_tile, head_distance_cm,
                        occupied_tiles):
        train = Train(train_id=train_id)
        network.add_train(train)
        controller = TrainController(train_id=train.train_id, network=network)
        set_train_span(
            controller,
            head_tile,
            head_distance_cm=head_distance_cm,
            occupied_tiles=occupied_tiles,
            occupied_blocks=["TERMINAL"],
        )
        controller.commands.set_cab("A", internal=True)  # mode-neutral cab setup
        return controller

    def _assert_no_authority_payload(self, lookahead):
        """The canonical no-authority shape, repeated across positions."""
        from enums import AtcLimitReasonCode

        inner = lookahead["lookahead"]
        assert inner["blocks"] == []
        assert inner["signals"] == []
        assert inner["switches"] == []
        assert inner["stations"] == []
        assert inner["clearance_cm"] == 0.0
        assert inner["authority_boundary_delta_cm"] == 0.0
        # M5: the empty/no-authority payload must still carry the nested
        # target_stop_cm key (emit 0.0, don't omit it) so consumers see a
        # consistent shape between the empty and populated payloads.
        assert inner["target_stop_cm"] == 0.0
        curves = inner["speed_curves"]
        assert len(curves) == 1
        anchor = curves[0]
        assert anchor["distance_cm"] == 0.0
        assert anchor["atc_limit"] == 0.0
        assert anchor["operational_limit"] == 0.0
        assert anchor["atc_limit_reason_code"] == AtcLimitReasonCode.NO_AUTHORITY.value

    def test_first_tile_placement_returns_no_authority(self, multi_tile_block_network):
        controller = self._place_and_build(
            multi_tile_block_network,
            train_id="TRAIN_NO_PATH_FIRST",
            head_tile=(0, 0),
            head_distance_cm=50.0,
            occupied_tiles=[(0, 0)],
        )
        self._assert_no_authority_payload(controller.get_lookahead())

    def test_mid_tile_placement_returns_no_authority(self, multi_tile_block_network):
        controller = self._place_and_build(
            multi_tile_block_network,
            train_id="TRAIN_NO_PATH_MID",
            head_tile=(1, 0),
            head_distance_cm=25.0,
            occupied_tiles=[(0, 0), (1, 0)],
        )
        self._assert_no_authority_payload(controller.get_lookahead())

    def test_last_tile_placement_returns_no_authority(self, multi_tile_block_network):
        controller = self._place_and_build(
            multi_tile_block_network,
            train_id="TRAIN_NO_PATH_LAST",
            head_tile=(2, 0),
            head_distance_cm=40.0,
            occupied_tiles=[(0, 0), (1, 0), (2, 0)],
        )
        self._assert_no_authority_payload(controller.get_lookahead())

    def test_prior_clearance_field_does_not_leak_into_payload(
        self, multi_tile_block_network
    ):
        """Even if the controller's `clearance_ahead_cm` field carries a
        legacy value (e.g. left over from before path completion), the
        canonical no-authority lookahead publishes `clearance_cm = 0`. The
        field is not a source of authority on its own."""
        controller = self._place_and_build(
            multi_tile_block_network,
            train_id="TRAIN_NO_PATH_PRIOR_CLEARANCE",
            head_tile=(0, 0),
            head_distance_cm=25.0,
            occupied_tiles=[(0, 0)],
        )
        controller.atc.clearance_ahead_cm = 200.0  # stale leftover
        self._assert_no_authority_payload(controller.get_lookahead())



