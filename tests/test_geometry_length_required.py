"""Phase 3 (geometry enforcement): track-geometry length must be supplied
explicitly on deserialization — no silent default. An explicit ``0`` is allowed
(per the chosen policy); only a *missing* key is rejected.

This closes the silent-substitution class of bug (a block loaded with no length
became 0, then was masked by `DEFAULT_TILE_LENGTH_CM=50` in geometry while the
raw 0 fed the buggy ATC clearance walk — the phantom-EB root).
"""

import pytest


def test_block_from_dict_rejects_missing_length_allows_zero():
    from block import Block

    d = Block(block_id="B1").to_dict()
    assert "length_cm" in d
    # explicit 0 is allowed
    d["length_cm"] = 0.0
    assert Block.from_dict(d).length_cm == 0.0
    # missing key is rejected
    d.pop("length_cm")
    with pytest.raises(ValueError):
        Block.from_dict(d)


def test_switch_from_dict_rejects_missing_lengths_allows_zero():
    from switch import Switch

    d = Switch(switch_id="S1", x=0, y=0).to_dict()
    d["main_length_cm"] = 0.0
    d["diverge_length_cm"] = 0.0
    sw = Switch.from_dict(d)
    assert sw.main_length_cm == 0.0 and sw.diverge_length_cm == 0.0
    for missing in ("main_length_cm", "diverge_length_cm"):
        d2 = dict(d)
        d2.pop(missing)
        with pytest.raises(ValueError):
            Switch.from_dict(d2)
