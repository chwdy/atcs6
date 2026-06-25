"""C1 — Power district: one-train-per-district enforcement + persistence.

design_new.md "Power district": a power district groups blocks onto one power
feed and is a single-train power domain. The dispatcher must not reserve a path
into a district another train holds (`reserve_blocks_for_path`), and a second
train physically drawing power in a held district is a fatal
`POWER_DISTRICT_INTRUSION` (`occupy_block`/`occupy_blocks`). A block the operator
has not grouped is its own single-block district.
"""

from block_manager import ReservedBlockTraversal
from network import RailwayNetwork
from power_district import PowerDistrict
from runtime_payloads import FatalConflictKind


class _CapturingSim:
    """Captures `enter_failed_runtime` payloads instead of freezing a runtime."""

    def __init__(self) -> None:
        self.failed = []

    def enter_failed_runtime(self, payload) -> None:
        self.failed.append(payload)


def _net_with_district():
    net = RailwayNetwork()
    net.create_block("BLK_A", "A")
    net.create_block("BLK_B", "B")
    net.create_power_district("D", ["BLK_A", "BLK_B"])
    return net


# --- entity / persistence -------------------------------------------------


def test_power_district_roundtrip():
    d = PowerDistrict(district_id="D", block_ids=["BLK_A", "BLK_B"], name="yard")
    assert PowerDistrict.from_dict(d.to_dict()) == d


def test_network_persists_districts_and_rebuilds_index():
    net = _net_with_district()
    net2 = RailwayNetwork()
    net2.from_dict(net.to_dict())
    assert "D" in net2.power_districts
    assert set(net2.district_block_ids("BLK_A")) == {"BLK_A", "BLK_B"}
    assert set(net2.district_block_ids("BLK_B")) == {"BLK_A", "BLK_B"}


def test_ungrouped_block_is_singleton_district():
    net = RailwayNetwork()
    net.create_block("BLK_X", "X")
    assert net.district_block_ids("BLK_X") == ["BLK_X"]


# --- one-train-per-district helper ----------------------------------------


def test_district_holder_other():
    net = _net_with_district()
    bm = net.block_manager
    bm.occupy_block("BLK_A", "T1")
    assert bm._power_district_holder_other("BLK_B", "T2") == ("BLK_A", "T1")
    # same train holding the district is not a conflict
    assert bm._power_district_holder_other("BLK_B", "T1") is None
    # an ungrouped block has no district sibling
    net.create_block("BLK_Z", "Z")
    assert bm._power_district_holder_other("BLK_Z", "T2") is None


# --- planning reject (reserve_blocks_for_path) -----------------------------


def test_reserve_rejected_when_district_held_by_other_train():
    net = _net_with_district()
    bm = net.block_manager
    bm.occupy_block("BLK_A", "T1")
    result = bm.reserve_blocks_for_path(
        ["BLK_B"],
        "P1",
        "T2",
        directions={
            "BLK_B": ReservedBlockTraversal(exit_edge="N", block_direction="forward")
        },
    )
    assert result.success is False
    assert result.reason == "power_district_held_by_other_train"
    assert result.failed_block_id == "BLK_B"


# --- fatal intrusion (occupy_block) ---------------------------------------


def test_occupy_intrusion_reports_power_district_intrusion():
    net = _net_with_district()
    bm = net.block_manager
    sim = _CapturingSim()
    bm.simulation_controller = sim
    assert bm.occupy_block("BLK_A", "T1") is True
    assert bm.occupy_block("BLK_B", "T2") is False
    assert len(sim.failed) == 1
    assert sim.failed[0].conflict_kind == FatalConflictKind.POWER_DISTRICT_INTRUSION
    assert sim.failed[0].prior_owner_train_id == "T1"


def test_no_intrusion_for_ungrouped_blocks():
    """Two trains on independent single-block districts never conflict."""
    net = RailwayNetwork()
    net.create_block("BLK_A", "A")
    net.create_block("BLK_B", "B")  # no district grouping -> independent feeds
    bm = net.block_manager
    sim = _CapturingSim()
    bm.simulation_controller = sim
    assert bm.occupy_block("BLK_A", "T1") is True
    assert bm.occupy_block("BLK_B", "T2") is True
    assert sim.failed == []
