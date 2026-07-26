"""Shared presentation helper: order tile positions in traversal order.

Used by both the block read serialization (``routes/blocks.py``) and the
segment ``end_tiles`` endpoint (``routes/segments.py``) so start/end and sorted
position lists read the same way regardless of which axis asked for them.
"""

from collections import Counter

from track import BIT_TO_GROUP


def sort_positions_traversal(positions, network):
    """Sort ``(x, y)`` positions in traversal order.

    Detects the dominant track orientation (NS / EW / diagonal) from the tiles'
    track bits and sorts along it; falls back to the input order for shapes with
    no dominant orientation.
    """
    if len(positions) <= 1:
        return positions

    orientation_counts: Counter = Counter()
    for x, y in positions:
        track = network.get_track(x, y)
        if track:
            for bit_idx, group in BIT_TO_GROUP.items():
                if track.track_bits & (1 << bit_idx):
                    orientation_counts[group] += 1

    if not orientation_counts:
        return positions

    dominant_orientation = orientation_counts.most_common(1)[0][0]

    if dominant_orientation == "NS":
        # North-South: sort by y descending (north to south)
        return sorted(positions, key=lambda pos: -pos[1])
    elif dominant_orientation == "EW":
        # East-West: sort by x ascending (west to east)
        return sorted(positions, key=lambda pos: pos[0])
    elif dominant_orientation == "NE_SW":
        # Northeast-Southwest diagonal
        return sorted(positions, key=lambda pos: (pos[0] - pos[1], pos[0]))
    elif dominant_orientation == "NW_SE":
        # Northwest-Southeast diagonal
        return sorted(positions, key=lambda pos: (pos[0] + pos[1], pos[0]))
    else:
        return positions
