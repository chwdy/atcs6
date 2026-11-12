"""The next block ahead on the rig layout (design_v2.md "Claiming the next
block", The next block ahead).

The rig is a loop of five blocks with a passing pair. With both switches set
to main, a train running Left forward goes Left -> Main -> Right -> Up -> Left.
"""

import logging

import pytest

import demo7_1
from enums import SwitchState
from next_block import (
    FORWARD,
    REVERSE,
    SWITCH_AGAINST,
    neighbour_blocks,
    next_block_ahead,
    switches_next_to,
)

LEFT_SWITCH = "SW_7_5"
RIGHT_SWITCH = "SW_11_9"


@pytest.fixture
def rig():
    logging.disable(logging.CRITICAL)
    try:
        network = demo7_1.create_comprehensive_demo()
    finally:
        logging.disable(logging.NOTSET)
    ids = {block.name: block_id for block_id, block in network.blocks.items()}
    return network, ids


def _set(network, left, right):
    network.switches[LEFT_SWITCH].position = SwitchState(left)
    network.switches[RIGHT_SWITCH].position = SwitchState(right)


def _walk(network, ids, name, direction):
    walk = next_block_ahead(network, ids[name], direction)
    names = {block_id: block_name for block_name, block_id in ids.items()}
    return names.get(walk.block_id), walk.direction, walk.reason, walk.switch_ids


def test_the_loop_with_both_switches_set_to_main(rig):
    network, ids = rig
    _set(network, 0, 0)
    assert _walk(network, ids, "Left", FORWARD) == ("Main", FORWARD, None, (LEFT_SWITCH,))
    assert _walk(network, ids, "Main", FORWARD) == ("Right", REVERSE, None, (RIGHT_SWITCH,))
    assert _walk(network, ids, "Right", REVERSE) == ("Up", REVERSE, None, ())
    assert _walk(network, ids, "Up", REVERSE) == ("Left", FORWARD, None, ())


def test_the_loop_the_other_way_round(rig):
    network, ids = rig
    _set(network, 0, 0)
    assert _walk(network, ids, "Left", REVERSE) == ("Up", FORWARD, None, ())
    assert _walk(network, ids, "Up", FORWARD) == ("Right", FORWARD, None, ())
    assert _walk(network, ids, "Right", FORWARD) == ("Main", REVERSE, None, (RIGHT_SWITCH,))
    assert _walk(network, ids, "Main", REVERSE) == ("Left", REVERSE, None, (LEFT_SWITCH,))


def test_a_switch_met_at_its_toe_leads_on_by_the_leg_it_is_set_to(rig):
    network, ids = rig
    _set(network, 1, 1)
    assert _walk(network, ids, "Left", FORWARD) == ("Sideline", FORWARD, None, (LEFT_SWITCH,))
    assert _walk(network, ids, "Right", FORWARD) == ("Sideline", REVERSE, None, (RIGHT_SWITCH,))


def test_a_switch_met_from_the_other_leg_ends_the_walk(rig):
    network, ids = rig
    _set(network, 0, 0)
    assert _walk(network, ids, "Sideline", FORWARD) == (None, None, SWITCH_AGAINST, (RIGHT_SWITCH,))
    assert _walk(network, ids, "Sideline", REVERSE) == (None, None, SWITCH_AGAINST, (LEFT_SWITCH,))
    _set(network, 1, 1)
    assert _walk(network, ids, "Main", FORWARD) == (None, None, SWITCH_AGAINST, (RIGHT_SWITCH,))
    assert _walk(network, ids, "Main", REVERSE) == (None, None, SWITCH_AGAINST, (LEFT_SWITCH,))


def test_a_switch_position_can_be_asked_about_without_throwing_it(rig):
    network, ids = rig
    _set(network, 0, 0)
    walk = next_block_ahead(network, ids["Left"], FORWARD, {LEFT_SWITCH: 1})
    assert walk.block_id == ids["Sideline"]
    assert int(network.switches[LEFT_SWITCH].position) == 0


def test_the_walk_ends_where_the_track_ends(rig):
    network, ids = rig
    del network.tracks[(7, 5)]  # take the left switch off the layout
    walk = next_block_ahead(network, ids["Left"], FORWARD)
    assert (walk.block_id, walk.reason) == (None, "track_ends")


def test_the_switches_next_to_each_block(rig):
    network, ids = rig
    assert switches_next_to(network, ids["Left"]) == {LEFT_SWITCH}
    assert switches_next_to(network, ids["Right"]) == {RIGHT_SWITCH}
    assert switches_next_to(network, ids["Up"]) == set()
    assert switches_next_to(network, ids["Main"]) == {LEFT_SWITCH, RIGHT_SWITCH}
    assert switches_next_to(network, ids["Sideline"]) == {LEFT_SWITCH, RIGHT_SWITCH}


def test_the_blocks_next_to_a_block_follow_the_switches_as_set(rig):
    network, ids = rig
    _set(network, 0, 0)
    assert neighbour_blocks(network, ids["Left"]) == {ids["Main"]: FORWARD, ids["Up"]: FORWARD}
    _set(network, 1, 0)
    assert neighbour_blocks(network, ids["Left"]) == {ids["Sideline"]: FORWARD, ids["Up"]: FORWARD}
    assert neighbour_blocks(network, ids["Sideline"]) == {ids["Left"]: REVERSE}
