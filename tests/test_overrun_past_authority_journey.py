"""Multi-tick journey: a fast locomotive overruns its authority end.

Scenario (the "real/fake CH347 hardware, train faster than its speed profile,
past the authority end" case):

  - A straight line BLK_A .. BLK_E, one 200cm block per two tiles.
  - The train is authorized (a real `create_path`) only as far as BLK_C.
  - A fault-injecting detector plays the part of the physical locomotive: it
    reports current on whichever block the *real* loco sits on, marched forward
    by the test. This is what a real/early CH347 detector does when the loco
    runs faster than its calibrated speed profile -- the detector confirms the
    train somewhere the model's calculated span has not reached yet.

What the journey demonstrates, tick by tick, through the real
`SimulationController.update()` (Observe -> Plan -> Action):

  1. The model's calculated span SNAPS FORWARD to follow the detector
     (`reconcile_to_detected_occupancy`): physical detection outranks the model,
     and the span is seen tracking A -> B -> C as the detector leads it.
  2. A span correction that gains >= 2 blocks emits the "running faster than its
     speed profile" WARNING; a one-block correction is silent sensor latency.
  3. The block the detector reports PAST the authority end (BLK_D) is attributed
     to THIS train -- owned, not ghosted, no phantom train minted -- because a
     train at its authority end stays permissive (design_new.md, "Attribution is
     gated on plausibility").
  4. Detection alone does NOT teleport the span past the authority end: the head
     parks AT the authorized end with the overrun block owned-but-not-entered.
     The head reaches a block outside the authorized path only through real
     MOVEMENT (a loco driving faster than its profile), and THAT is the case the
     0-V cut catches -- pinned by
     tests/test_lighting_calibration.py::
       test_a_train_that_overran_its_authority_is_cut_to_0v.

This is not a unit test of one helper; it is the whole tick loop reacting to a
detector that leads the model over many ticks.
"""

from typing import Dict, List, Set

import pytest

from enums import Direction
from network import RailwayNetwork
from track import Tile, TRACK_EW
from train import Train, TrainCar
from block_occupancy import OccupancyInput
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from tests.train_test_helpers import set_train_span
from tests.segment_test_helpers import make_segment_block_with_tracks

BLOCKS = ["BLK_A", "BLK_B", "BLK_C", "BLK_D", "BLK_E"]
AUTHORITY_END = "BLK_C"  # create_path target -> resource_sequence covers A..C


class PhysicalLocoDetector(OccupancyInput):
    """Ground-truth detector for the *physical* loco, driven by the test.

    `physical` is the set of blocks the real locomotive sits on right now. The
    model has to find out where that is by reading this detector -- exactly the
    real/early-sensor path the default in-memory driver cannot exercise (it
    builds current from the model's own span)."""

    def __init__(self, all_blocks: List[str]) -> None:
        self.all = list(all_blocks)
        self.physical: Set[str] = set()

    def detected_block_ids(self) -> Set[str]:
        return set(self.all)

    def read_block_currents(self) -> Dict[str, bool]:
        return {b: (b in self.physical) for b in self.all}


def _build_line() -> RailwayNetwork:
    network = RailwayNetwork()
    for x in range(10):
        network.add_track(Tile(x, 0, track_bits=TRACK_EW))
    for i, bid in enumerate(BLOCKS):
        make_segment_block_with_tracks(
            network, bid, positions=[(2 * i, 0), (2 * i + 1, 0)],
            name=bid[-1], length_cm=200.0,
        )
    return network


def _head_block(controller) -> str:
    span = controller.physics_sync.current_span
    occ = span.occupied_blocks if span else []
    # Head is the forward-most block (EAST): the last in track order present.
    for bid in reversed(BLOCKS):
        if bid in (occ or []):
            return bid
    return "?"


def _voltage(network, bid: str) -> float:
    blk = network.blocks.get(bid)
    return float(getattr(blk, "current_voltage", 0.0)) if blk else 0.0


def _overrun_warnings(sim):
    return [
        n for n in sim.notification_center.peek_recent(400)
        if getattr(n, "operation", None) == "reconcile_to_detected_occupancy"
    ]


def _run_phase(sim, network, controller, detector, physical, ticks, log):
    """Hold the physical loco on `physical` for `ticks` ticks; capture each."""
    detector.physical = set(physical)
    for _ in range(ticks):
        sim.update(0.1)
        span = controller.physics_sync.current_span
        head = _head_block(controller)
        vphys = sim.physics_instances.get(controller.train_id)
        past = vphys._head_past_authority(controller) if vphys else None
        log.append({
            "phys": sorted(physical),
            "model_occ": list(span.occupied_blocks or []) if span else [],
            "head": head,
            "past_auth": past,
            "v_head": round(_voltage(network, head), 2),
            "speed": round(float(getattr(controller.physics_sync, "current_speed_kmh", 0.0) or 0.0), 1),
            "eb": controller.eb.active,
            "warnings": len(_overrun_warnings(sim)),
            "ntrains": len(network.trains),
        })


def _make_fixture():
    network = _build_line()
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=40.0)
    train = Train(train_id="FAST", consist=[car], max_speed=80.0)
    network.add_train(train)
    network.place_train(train, 0, 0, direction=Direction.EAST)

    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    controller = sim._get_or_create_controller(train)
    set_train_span(
        controller, (0, 0), facing_direction=Direction.EAST,
        occupied_blocks=["BLK_A"],
    )

    result = dispatcher.create_path(
        train_id="FAST",
        requested_destination_block_id=AUTHORITY_END,
        requested_destination_direction="forward",
    )
    assert result.success, "authority path A..C must be created"

    detector = PhysicalLocoDetector(BLOCKS)
    detector.physical = {"BLK_A"}
    sim.block_occupancy_detector.input_interface = detector
    sim.block_occupancy_detector.seed_occupied({"BLK_A"})

    controller.commands.enqueue_command("power_on")
    controller.commands.enqueue_command("set_cab", {"cab": "A"})
    controller.commands.enqueue_command("set_mode", {"mode": "manual"})
    controller.commands.enqueue_command("set_manual_power_percent", {"percent": 80.0})
    return network, sim, controller, detector


def test_fast_loco_overruns_authority_and_span_parks_at_authority_end(capsys):
    network, sim, controller, detector = _make_fixture()

    log: List[dict] = []
    # The physical loco rolls forward, briefly straddling each seam as a real
    # loco does. It reaches its authority end (BLK_C), then overruns onto BLK_D
    # and BLK_E.
    march = [
        {"BLK_A"}, {"BLK_A", "BLK_B"}, {"BLK_B"},
        {"BLK_B", "BLK_C"}, {"BLK_C"},
        {"BLK_C", "BLK_D"}, {"BLK_D"},            # crossing the AUTHORITY end
        {"BLK_D", "BLK_E"}, {"BLK_E"},
    ]
    for physical in march:
        _run_phase(sim, network, controller, detector, physical, 4, log)

    # --- print the tick-by-tick table for the human ---
    print("\n tick | physical | model span            | head  | past? | v_head | speed | warns | trains")
    print("-" * 100)
    for i, r in enumerate(log):
        print(f" {i:4d} | {','.join(b[-1] for b in r['phys']):8s} | "
              f"{str(r['model_occ']):22s}| {r['head']:5s} | "
              f"{str(r['past_auth']):5s} | {r['v_head']:6.2f} | {r['speed']:5.1f} | "
              f"{r['warnings']:5d} | {r['ntrains']}")

    # --- assertions on the mechanism ---
    # 1. The model's span SNAPPED FORWARD to follow the detector, tick by tick,
    #    all the way to the authority end (BLK_C). Physical detection outranks
    #    the calculated position (reconcile_to_detected_occupancy).
    assert log[-1]["head"] == "BLK_C", \
        f"the span should have tracked the detector to the authority end; got {log[-1]}"
    assert any(r["head"] == "BLK_A" for r in log) and any(r["head"] == "BLK_B" for r in log), \
        "the span should have been seen progressing A -> B -> C as the detector led it"

    # 2. A >= 2-block correction raised the "running faster than its speed
    #    profile" WARNING (OVERRUN_REPORT_MIN_BLOCKS = 2); a one-block snap is
    #    silent sensor latency.
    assert log[-1]["warnings"] >= 1, "expected at least one overrun warning"

    # 3. The block the detector reports PAST the authority end (BLK_D) is
    #    attributed to THIS train -- owned, not ghosted, and no phantom
    #    (unidentified) train is minted for it. This is the permissive-at-
    #    authority-end attribution (design_new.md, "Attribution is gated on
    #    plausibility": a train past its authority end stays permissive).
    assert network.blocks["BLK_D"].occupied_by == "FAST", \
        f"the overrun block must be owned by FAST, got {network.blocks['BLK_D'].occupied_by}"
    assert all(r["ntrains"] == 1 for r in log), \
        "no phantom (unidentified) train may be minted for the overrun block"

    # 4. Detection alone does NOT teleport the calculated span past the authority
    #    end: the head parks AT the authorized end (BLK_C) with the overrun block
    #    owned-but-not-entered. The head only reaches a block outside the
    #    authorized path by real MOVEMENT -- a locomotive driving faster than its
    #    profile -- and that is the case the 0-V cut catches. That
    #    movement-driven step is pinned by
    #    tests/test_lighting_calibration.py::
    #      test_a_train_that_overran_its_authority_is_cut_to_0v.
    assert all(r["past_auth"] is False for r in log), \
        "the span head must not cross past authority by detection reconciliation alone"


def test_the_block_overrun_past_authority_is_never_powered():
    """The block the loco overran onto past its authority end stays dead rail.
    It is still credited to the train — owned, not ghosted — but the emission
    leaves it out, and so does the de-energize drain, so nothing keeps it live
    (design_new.md "Power", A block past the authority end is never energized).

    Powering it was the bug: its direction cannot be read off the path or the
    span, which both stop at the authority end, so it was driven positive by
    default — pushing the locomotive backwards wherever that block was drawn
    against the travel direction — and any voltage there kept driving a
    locomotive that had already run past where it may go."""
    network, sim, controller, detector = _make_fixture()
    log: List[dict] = []
    march = [
        {"BLK_A"}, {"BLK_A", "BLK_B"}, {"BLK_B"},
        {"BLK_B", "BLK_C"}, {"BLK_C"},
        {"BLK_C", "BLK_D"}, {"BLK_D"},            # overruns the authority end
    ]
    for physical in march:
        _run_phase(sim, network, controller, detector, physical, 4, log)

    physics = sim.physics_instances["FAST"]
    train = network.trains["FAST"]
    assert network.blocks["BLK_D"].occupied_by == "FAST"
    assert physics.blocks_past_authority == {"BLK_D"}
    assert "BLK_D" not in physics._energized_blocks_for_train(train)
    assert "BLK_D" not in sim._calculated_hold_blocks()
    # The blocks it is authorized on are unaffected.
    assert "BLK_C" in physics._energized_blocks_for_train(train)
