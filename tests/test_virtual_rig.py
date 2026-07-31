import hardware_config
from hardware.fake import FakeCh347Device
from hardware.interfaces import build_real_interfaces
from hardware.virtual_rig import VirtualRig


def _setup():
    cfg = hardware_config.config_from_payload({
        "chips": [
            {
                "chip_id": "in",
                "kind": "hc165",
                "spi_select": "cs1",
                "active_low": True,
            },
            {"chip_id": "cur", "kind": "ina3221", "i2c_address": "0x41"},
        ],
        "block_occupancy": {
            "B1": {"chip_id": "in", "port": 0},
            "B2": {"chip_id": "in", "port": 1},
        },
        "block_occupancy_current": {
            "B1": {"chip_id": "cur", "port": 0, "threshold_ma": 10},
            "B2": {"chip_id": "cur", "port": 1, "threshold_ma": 10},
        },
    })
    device = FakeCh347Device(present_addresses={0x41})
    occupancy, _track, _switch = build_real_interfaces(
        device,
        cfg.chips,
        block_track={},
        block_occupancy={"B1": ("in", 0), "B2": ("in", 1)},
        switch_output={},
        block_occupancy_current={
            "B1": ("cur", 0, 0.0, 0.010),
            "B2": ("cur", 1, 0.0, 0.010),
        },
    )
    return cfg, device, occupancy


def test_virtual_rig_places_advances_stalls_and_removes_a_train():
    cfg, device, occupancy = _setup()
    rig = VirtualRig()

    assert rig.start(cfg, device, ["B1", "B2"])["block_id"] == "B1"
    assert occupancy.read_block_currents() == {"B1": True, "B2": False}

    rig.set_stalled(True)
    assert rig.advance(cfg, device)["block_id"] == "B1"
    rig.set_stalled(False)
    assert rig.advance(cfg, device)["block_id"] == "B2"
    assert occupancy.read_block_currents() == {"B1": False, "B2": True}

    assert rig.remove(cfg, device)["active"] is False
    assert occupancy.read_block_currents() == {"B1": False, "B2": False}


def test_virtual_rig_rejects_a_block_without_detector_wiring():
    cfg, device, _occupancy = _setup()
    rig = VirtualRig()

    try:
        rig.start(cfg, device, ["NO_BINDING"])
    except ValueError as exc:
        assert "no occupancy binding" in str(exc)
    else:
        raise AssertionError("unwired scenario block was accepted")
