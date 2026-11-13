"""Rail polarity lives on the `block_track` hardware binding.

design_new.md "Power", Rail polarity lives on the rail binding: a block whose
two rails are wired the opposite way round from the nominal-positive sense its
tile order implies is corrected with `TrackBinding.reversed`, stored WITH the
wiring in the hardware configuration — so a flip auto-persists like every other
binding edit instead of waiting on a layout save. The concrete track driver
swaps the two channels as it resolves the binding; the channels themselves are
never rebound, because one physical rail may carry the idle detection feed.

It is deliberately NOT `Block.allowed_travel_directions`, which is a routing
policy the pathfinder reads and which never reaches hardware — and it is no
longer a Block field at all: the block PATCH rejects `polarity_reversed` with a
pointer at the binding surface.
"""

import threading
from datetime import datetime

import pytest

import hardware_config
from app_state import AppState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from services import block_service
from services.exceptions import ValidationError
from simulation import SimulationController
from tests.segment_test_helpers import make_segment_block


@pytest.fixture(autouse=True)
def _isolated_hw_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hardware_config, "CONFIG_PATH", str(tmp_path / "hardware_config.json")
    )
    hardware_config.reset_to_defaults()
    yield
    hardware_config.reset_to_defaults()


def _bind_track(block_id: str = "BLK_P", **kwargs) -> None:
    hardware_config.apply_updates(
        {
            "chips": [{"chip_id": "pwm1", "kind": "pca9685", "i2c_address": "0x40"}],
            "block_track": {
                block_id: {"chip_id": "pwm1", "forward": 0, "reverse": 1, **kwargs}
            },
        },
        persist=False,
    )


# --- the binding flag -------------------------------------------------------


def test_binding_defaults_normal():
    _bind_track()
    binding = hardware_config.get_hardware_config().block_track["BLK_P"]
    assert binding.reversed is False


def test_flag_round_trips_through_the_persisted_dict():
    """`_persist_dict` and `_parse_track_bindings` serve both parse paths
    (section PATCH and whole-snapshot layout load), so one round trip covers
    the trap of a key added to one and dropped by the other."""
    _bind_track(reversed=True)
    snapshot = hardware_config.as_dict()
    hardware_config.reset_to_defaults()
    hardware_config.apply_snapshot(snapshot, persist=False)
    assert hardware_config.get_hardware_config().block_track["BLK_P"].reversed is True


def test_set_track_binding_writes_the_flag():
    _bind_track()
    hardware_config.set_track_binding(
        "BLK_P", "pwm1", 0, 1, polarity_reversed=True, persist=False
    )
    assert hardware_config.get_hardware_config().block_track["BLK_P"].reversed is True


def test_channel_edit_keeps_the_stored_flag():
    """Re-picking the two rail channels must not silently reset a corrected
    block back to normal wiring — omitted, the flag keeps its value."""
    _bind_track(reversed=True)
    hardware_config.set_track_binding("BLK_P", "pwm1", 2, 3, persist=False)
    binding = hardware_config.get_hardware_config().block_track["BLK_P"]
    assert (binding.forward, binding.reverse, binding.reversed) == (2, 3, True)


def test_track_polarity_reversed_reads_the_live_config():
    """The driver's `polarity_of` read: live, not a snapshot, and False for a
    block with no rail binding (no rails to be reversed)."""
    assert hardware_config.track_polarity_reversed("BLK_P") is False
    _bind_track(reversed=True)
    assert hardware_config.track_polarity_reversed("BLK_P") is True
    assert hardware_config.track_polarity_reversed("BLK_OTHER") is False


# --- the block PATCH surface ------------------------------------------------


@pytest.fixture
def state():
    network = RailwayNetwork()
    path_dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, path_dispatcher)
    persistence_manager = PersistenceManager(storage_dir="layouts")
    return AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=sim_controller,
        persistence_manager=persistence_manager,
        simulation_lock=threading.Lock(),
    )


def test_block_patch_rejects_polarity_with_a_pointer(state):
    make_segment_block(state.network, "BLK_P")
    with pytest.raises(ValidationError, match="rail binding"):
        block_service.update_block(state, "BLK_P", data={"polarity_reversed": True})


def test_travel_direction_still_edits_normally(state):
    """The routing-policy neighbour is untouched by the move."""
    make_segment_block(state.network, "BLK_P")
    block_service.update_block(
        state, "BLK_P", data={"allowed_travel_directions": ["forward"]}
    )
    assert state.network.get_block("BLK_P").allowed_travel_directions == {"forward"}


# --- legacy layout lift -----------------------------------------------------


def test_legacy_layout_polarity_lifts_onto_the_binding(tmp_path):
    """A layout saved when polarity was a Block field must not load a corrected
    block back to normal wiring: the loader lifts `polarity_reversed` from the
    snapshot's block dicts onto the hardware section's binding."""
    import json

    wrapper = {
        "name": "legacy",
        "snapshot": {
            "tracks": [],
            "trains": [],
            "blocks": {"BLK_P": {"block_id": "BLK_P", "length_cm": 0, "polarity_reversed": True}},
            "signals": {},
            "platforms": [],
            "station_groups": [],
            "switches": {},
            "tunnels": [],
        },
        "hardware": {
            "enabled": False,
            "chips": [{"chip_id": "pwm1", "kind": "pca9685", "i2c_address": "0x40"}],
            "block_track": {"BLK_P": {"chip_id": "pwm1", "forward": 0, "reverse": 1}},
        },
    }
    path = tmp_path / "legacy_layout.json"
    path.write_text(json.dumps(wrapper))

    pm = PersistenceManager(storage_dir=str(tmp_path))
    result = pm.load_layout_with_fallback("legacy_layout", RailwayNetwork())

    assert result.get("hardware") == "applied"
    assert hardware_config.get_hardware_config().block_track["BLK_P"].reversed is True


def test_new_schema_save_needs_no_lift(tmp_path):
    """A binding that already carries `reversed: false` is left alone even if a
    stray legacy block key says otherwise — the binding is the authority."""
    import json

    wrapper = {
        "name": "mixed",
        "snapshot": {
            "tracks": [],
            "trains": [],
            "blocks": {"BLK_P": {"block_id": "BLK_P", "length_cm": 0, "polarity_reversed": True}},
            "signals": {},
            "platforms": [],
            "station_groups": [],
            "switches": {},
            "tunnels": [],
        },
        "hardware": {
            "enabled": False,
            "chips": [{"chip_id": "pwm1", "kind": "pca9685", "i2c_address": "0x40"}],
            "block_track": {
                "BLK_P": {"chip_id": "pwm1", "forward": 0, "reverse": 1, "reversed": False}
            },
        },
    }
    path = tmp_path / "mixed_layout.json"
    path.write_text(json.dumps(wrapper))

    pm = PersistenceManager(storage_dir=str(tmp_path))
    result = pm.load_layout_with_fallback("mixed_layout", RailwayNetwork())

    assert result.get("hardware") == "applied"
    assert hardware_config.get_hardware_config().block_track["BLK_P"].reversed is False
