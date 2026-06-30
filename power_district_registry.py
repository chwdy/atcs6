"""PowerDistrictRegistry — power-district CRUD pulled out of `RailwayNetwork`.

Per design_new.md "Power district": blocks are grouped into electrically-fed
districts, and an ungrouped block is its own single-block district. This module
owns the lifecycle + connectivity logic and the block->district reverse index.
The `power_districts` dict and the `_block_to_district` index live on
`RailwayNetwork`; the registry reads / writes them through the back-reference
`self._n`. `RailwayNetwork.create_power_district` / `district_block_ids` / etc.
become thin forwarders so external callers (REST API, BlockManager, physics)
keep working.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from power_district import PowerDistrict

if TYPE_CHECKING:
    from network import RailwayNetwork


class PowerDistrictRegistry:
    """Owns power-district CRUD + connectivity for one `RailwayNetwork`."""

    def __init__(self, network: "RailwayNetwork") -> None:
        self._n = network

    def _rebuild_district_index(self) -> None:
        """Rebuild the block_id -> district_id reverse index from
        `power_districts`. A block may appear in at most one district; on a
        duplicate the last district listed wins."""
        self._n._block_to_district = {}
        for district in self._n.power_districts.values():
            for block_id in district.block_ids:
                self._n._block_to_district[block_id] = district.district_id

    def district_block_ids(self, block_id: str) -> List[str]:
        """All block ids sharing `block_id`'s power district.

        A block the operator has not grouped is its own single-block district
        (design_new.md "Power district"), so this returns ``[block_id]`` for an
        ungrouped block.
        """
        district_id = self._n._block_to_district.get(block_id)
        if district_id is None:
            return [block_id]
        district = self._n.power_districts.get(district_id)
        if district is None or not district.block_ids:
            return [block_id]
        return list(district.block_ids)

    def create_power_district(
        self, district_id: str, block_ids: List[str], name: str = ""
    ) -> PowerDistrict:
        """Create (or replace) a power district and rebuild the reverse index.

        Each listed block is detached from any other district first, so every
        block stays in exactly one district (design_new.md "Power district").
        A prior district left empty by that detach is dropped: an ungrouped
        block is its own single-block district and needs no stored record.
        """
        block_ids = list(block_ids)
        for block_id in block_ids:
            self._detach_block_from_its_district(block_id, except_id=district_id)
        district = PowerDistrict(
            district_id=district_id, block_ids=block_ids, name=name
        )
        self._n.power_districts[district_id] = district
        self._prune_empty_districts(keep=district_id)
        self._rebuild_district_index()
        return district

    def _detach_block_from_its_district(
        self, block_id: str, except_id: Optional[str] = None
    ) -> None:
        """Remove `block_id` from whatever district currently holds it, unless
        that district is `except_id`. Leaves index rebuild / empty pruning to the
        caller so a batch detach rebuilds once."""
        prior_id = self._n._block_to_district.get(block_id)
        if prior_id is None or prior_id == except_id:
            return
        prior = self._n.power_districts.get(prior_id)
        if prior is not None:
            prior.block_ids = [b for b in prior.block_ids if b != block_id]

    def _prune_empty_districts(self, keep: Optional[str] = None) -> None:
        """Drop districts with no member blocks. An empty district is just a set
        of ungrouped single-block districts, so it carries no information. `keep`
        is never pruned, so a district created empty in the same call survives."""
        self._n.power_districts = {
            did: d
            for did, d in self._n.power_districts.items()
            if d.block_ids or did == keep
        }

    def delete_power_district(self, district_id: str) -> bool:
        """Delete a power district; its member blocks become ungrouped (each its
        own single-block district per design_new.md "Power district"). Returns
        False when the id is unknown."""
        if district_id not in self._n.power_districts:
            return False
        del self._n.power_districts[district_id]
        self._rebuild_district_index()
        return True

    def add_block_to_power_district(self, district_id: str, block_id: str) -> bool:
        """Add `block_id` to `district_id`, detaching it from any other district
        first so every block stays in exactly one district. A prior district left
        empty is dropped. Raises ValueError if the district or block is unknown.
        Returns True when added, False when the block was already a member."""
        district = self._n.power_districts.get(district_id)
        if district is None:
            raise ValueError(f"Power district {district_id} not found")
        if block_id not in self._n.blocks:
            raise ValueError(f"Block {block_id} not found")
        if block_id in district.block_ids:
            return False
        self._detach_block_from_its_district(block_id, except_id=district_id)
        district.block_ids.append(block_id)
        self._prune_empty_districts(keep=district_id)
        self._rebuild_district_index()
        return True

    def remove_block_from_power_district(
        self, district_id: str, block_id: str
    ) -> bool:
        """Remove `block_id` from `district_id`; the block becomes its own
        single-block district. The district is deleted if it becomes empty
        (non-empty parent rule, mirroring station groups). Raises ValueError if
        the district is unknown. Returns True when removed, False when the block
        was not a member."""
        district = self._n.power_districts.get(district_id)
        if district is None:
            raise ValueError(f"Power district {district_id} not found")
        if block_id not in district.block_ids:
            return False
        district.block_ids = [b for b in district.block_ids if b != block_id]
        self._prune_empty_districts()
        self._rebuild_district_index()
        return True

    def power_district_blocks_connected(self, block_ids: List[str]) -> bool:
        """True if `block_ids` form ONE connected region under direct block
        adjacency (no switch between members) — the district analogue of a
        block's contiguous tiles. A single existing block is trivially connected;
        an unknown block, or a set split by a switch or a gap, is not."""
        ids = list(dict.fromkeys(block_ids))
        if any(b not in self._n.blocks for b in ids):
            return False
        if len(ids) <= 1:
            return True
        # Map each candidate block's tiles to its id, then link candidates whose
        # tiles share a track edge. An edge into a switch tile or a non-candidate
        # block is not a within-set link, so a switch between two members leaves
        # them in separate components (the strict "no switch between" rule).
        tile_owner: Dict[Tuple[int, int], str] = {}
        for b in ids:
            for pos in self._n.get_block_positions(b):
                tile_owner[tuple(pos)] = b
        adjacency: Dict[str, Set[str]] = {b: set() for b in ids}
        for b in ids:
            for pos in self._n.get_block_positions(b):
                track = self._n.get_track(*pos)
                if track is None:
                    continue
                for npos in self._n._get_block_connected_neighbors(track):
                    owner = tile_owner.get(npos)
                    if owner is not None and owner != b:
                        adjacency[b].add(owner)
                        adjacency[owner].add(b)
        seen: Set[str] = set()
        stack = [ids[0]]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(adjacency[cur] - seen)
        return seen == set(ids)
