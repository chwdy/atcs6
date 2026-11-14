"""A switch marked manual (design_v2.md "Switches", A switch marked manual):
the runtime never throws it for a train, the operator throws it from the page,
and the layout saves the mark. Old layout files that still carry the deleted v1
switch and block settings load without them.
"""

from block import Block
from switch import Switch, SwitchState
from switch_manager import SwitchManager


def _manual_switch() -> Switch:
    switch = Switch(switch_id="SW_M", x=0, y=0)
    switch.manual = True
    return switch


def test_the_runtime_throw_is_refused_on_a_manual_switch():
    switch = _manual_switch()
    manager = SwitchManager({"SW_M": switch})
    ok, _ = manager.request_position_change("SW_M", 1)
    assert ok is False
    assert switch.position == SwitchState.MAIN


def test_the_operator_throw_moves_a_manual_switch():
    switch = _manual_switch()
    manager = SwitchManager({"SW_M": switch})
    ok, _ = manager.request_position_change("SW_M", 1, manual_override=True)
    assert ok is True
    assert switch.position == SwitchState.DIVERGE


def test_the_manual_mark_is_saved_with_the_layout():
    saved = _manual_switch().to_layout_dict()
    assert saved["manual"] is True
    assert Switch.from_dict(saved).manual is True


def test_an_old_switch_record_loads_without_its_v1_settings():
    switch = Switch.from_dict({
        "switch_id": "SW_OLD", "x": 1, "y": 2,
        "main_length_cm": 0.0, "diverge_length_cm": 0.0,
        "disabled": True, "manual": False, "flip_count": 7,
    })
    saved = switch.to_layout_dict()
    assert "disabled" not in saved and "flip_count" not in saved
    assert not hasattr(switch, "disabled")


def test_an_old_block_record_loads_without_its_v1_settings():
    block = Block.from_dict({
        "block_id": "BLK_OLD", "name": "Old", "length_cm": 10.0,
        "is_holding_point": True, "disabled": True, "manual": True,
        "allowed_travel_directions": ["forward"],
        "ordered_tiles": [[0, 0, "W"]],
    })
    assert block.name == "Old"
    saved = block.to_layout_dict()
    for key in ("is_holding_point", "disabled", "manual", "allowed_travel_directions"):
        assert key not in saved
