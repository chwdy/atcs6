"""Pin the frontend and backend "square corner switch" validity rule to agree.

`track.is_square_corner_switch(bits)` (Python) and `isSquareCornerSwitch(bits)`
(static/js/constants.js) encode the same geometry rule: a switch placement is
rejected when it joins a horizontal segment (NW|SE) and a vertical segment
(NE|SW) with no 45-degree diagonal (NS|EW) to ease the branch. The server uses
it in `TrackSegment.is_valid` / `routes/tracks.py`; the client uses it in the
editor to grey out invalid placements. They live in different languages, so
this test pins a line-for-line Python port of the JS function against the
backend for every `track_bits` value (0..63) — a future change on either side
trips CI before the two implementations drift.

If `isSquareCornerSwitch` in static/js/constants.js changes, mirror it in
`js_is_square_corner_switch` below; that is the whole point of the file.
"""

from track import is_square_corner_switch


# Direct translation of `isSquareCornerSwitch` in static/js/constants.js.
# NW=8, SE=16 (horizontal); NE=4, SW=32 (vertical); NS=1, EW=2 (diagonal).
def js_is_square_corner_switch(bits: int) -> bool:
    has_horizontal = (bits & (8 | 16)) != 0
    has_vertical = (bits & (4 | 32)) != 0
    has_diagonal = (bits & (1 | 2)) != 0
    return has_horizontal and has_vertical and not has_diagonal


def test_square_corner_rule_matches_frontend():
    """For every track_bits 0..63, the backend `is_square_corner_switch` and the
    frontend `isSquareCornerSwitch` port must return the same verdict."""
    for bits in range(64):
        assert is_square_corner_switch(bits) == js_is_square_corner_switch(bits), (
            f"square-corner rule drift at bits={bits}: "
            f"backend={is_square_corner_switch(bits)} "
            f"frontend={js_is_square_corner_switch(bits)}"
        )
