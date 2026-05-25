# Reference: Construction Pathfinding in Simutrans

> **Note:** This document describes design patterns from Simutrans (C++). For actual implementation, see `static/js/editor.js` (JavaScript construction pathfinder).
>
> **Last Verified:** 2026-01-16

## Context
This document explains the "Construction Pathfinding" algorithm used in Simutrans (`src/simutrans/builder/wegbauer.cc`).
**Crucial Distinction:** This is the system used **when the user drags the mouse** to build new tracks or roads. It determines how to connect Point A to Point B by placing *new* tiles.

## 1. The Core Loop
The builder uses an **A* (A-Star)** algorithm implementation in `way_builder_t::intern_calc_route`.

### How it works:
1.  **Start:** Determine the valid starting ground tile(s).
2.  **Iterate:** For the current lowest-cost node, look at all 4 cardinal neighbors.
3.  **Validate:** Call `is_allowed_step` to see if a normal track segment is physically possible.
4.  **Cost:** Calculate the cost based on the "Cost Factors" below.
5.  **Queue:** Push valid next steps to the priority queue.

---

## 2. Cost Factors (The "Personality" of the Builder)
The builder follows the path of least resistance, defined by `settings_t`. These values heavily influence the shape of the resulting track.

### Base Costs (defined in `settings.h/simuconf.tab`)
*   **Straight:** `1` (Cheapest).
*   **Slope:** `20` (Very expensive to build on slopes).
*   **Tunnel:** `16` (Expensive).
*   **No Way (Grass):** `3` (Building on virgin land).
*   **Existing Way:** `1` (Building over existing track = Reuse).
    *   *Result:* The builder is "lazy"; it prefers to reuse an existing track segment rather than building a new one parallel to it.

### Turn Penalties (Smoothing)
Simutrans grid is orthogonal, but the algorithm mimics "smooth" lines via Cost Penalties.
*   **Curve:** `5` (Simple turn).
*   **Double Curve (Zig-Zag):** `10`
    *   *Logic:* If we turned last tile, and are turning again.
    *   *Result:* A "Staircase" pattern (1 North, 1 East) triggers this constantly. The builder strongly prefers long straight segments (2 North, 2 East).
*   **90-Degree Curve:** `30`
    *   Extremely expensive to prevent sharp right angles unless absolutely necessary.
*   **Leaving Way:** `50`
    *   Penalty for branching *off* an existing way. This prevents the builder from uselessly creating temporary sidings or shortcuts through fields.

---

## 3. Tile Validation (`is_allowed_step`)
Before placing a "ghost" track piece during the search, the algorithm verifies the tile is valid via `is_allowed_step`.

*   **Slopes:** `check_slope(from, to)` ensures the way type (rail vs road) can handle the incline. For example, some rails can't climb double-height slopes.
*   **Obstacles:** `check_building` ensures we aren't plowing through a City Hall or a Depot.
*   **Ownership:** `check_owner` ensures we aren't building on a competitor's private land.
*   **Clearance:** Checks for vertical clearance (e.g., is there a bridge directly above us with insufficient headroom?).
*   **Crossings:** `check_crossing` verifies intersection rules (e.g., can't build a rail crossing through a complex road intersection or switch).

---

## 4. Bridges & Verticality Logic
How does it decide between a Tunnel, a Bridge, or staying On-Grade?

The algorithm doesn't "decide" in a single `if` statement. It explores **all** valid options as expanding branches of the search tree and picks the winner based on total cost.

### Scenario: A deep valley between A and B.
1.  **Branch 1 (On-Grade):** Walk down the slope.
    *   Cost accumulates `way_count_slope` (20) for every tile. High total cost.
2.  **Branch 2 (Bridge):**
    *   `check_for_bridge` triggers. It finds the other side of the valley.
    *   It adds a single "Jump" node to the queue.
    *   Cost = `Bridge_Maintenance_Cost` * Length.
    *   If the bridge is shorter/cheaper than walking down and up, the A* picks the bridge.

---


---

## 5. From Path to World (The Build Phase)
Once `intern_calc_route` returns a valid list of coordinates (`route_t`), the builder must apply this to the game world. This happens in specific methods like `build_track`, `build_road`, or `build_river`.

### The `track_bits` Calculation
The route is just a list of tiles: `(x,y), (x,y+1), (x+1,y+1)`.
For each tile, we calculate `track_bits` by determining which **segment** connects the previous and next tiles:

| Prev Tile | Next Tile | Segment | track_bits |
|:----------|:----------|:--------|:-----------|
| North | South | NS | 1 |
| East | West | EW | 2 |
| North | East | NE | 4 |
| North | West | NW | 8 |
| South | East | SE | 16 |
| South | West | SW | 32 |

**Example:**
*   If `prev` is at (0,0) and `next` is at (1,1) (moving diagonally SE), the segment is **SE** (track_bits = 16).
*   If `prev` is North and `next` is South, the segment is **NS** (track_bits = 1).

### Building Switches
When a new segment is added to an existing tile:
1. **Check current track_bits** of the tile
2. **OR the new segment**: `new_bits = old_bits | segment_bit`
3. **Validate**: Ensure `new_bits` is not in `FORBIDDEN_TRACK_BITS`
4. **Update switch_points** if the tile now has edges with 2+ exits

### Way Extension (`propagate_from_tile`)
The system calls `propagate_from_tile` to update neighbor connectivity:
*   **Merge:** If a tile has NS track (1) and we build NE across it, the result is `1 | 4 = 5` (a switch).
*   **Validate:** The 23 forbidden configurations prevent impossible 3-way switches.
*   **Dead-ends:** Single-segment tiles at route ends become dead-end buffer stops.

---

## 6. JavaScript Implementation Notes

The actual implementation in `static/js/editor.js` differs slightly from Simutrans:

### Cost Factors in JavaScript
```javascript
costs = {
    useExisting: 0.2, // Using existing track is cheapest
    extend: 0.5,      // Extending existing track direction
    diagonal: 1,      // Alternating direction (zigzag) is preferred
    straight: 2,      // Going straight costs more
    sameTurn: 3,      // Turning same direction twice in a row
    createSwitch: 5,  // Creating a Y-junction
    createCrossover: 7, // Creating a crossover
    reverse: 100      // Going back (180 degree)
};
```

### Key Classes
- `ConstructionPathfinder`: A* pathfinder for building new tracks
- `ExistingTrackPathfinder`: Follows existing tracks only (for block assignment, eraser)

---

## German-to-English Keyword Translation

| German Term | English Meaning | Description |
| :--- | :--- | :--- |
| `wegbauer` | Way Builder | The class name (`wegbauer`). |
| `bautyp` | Build Type | The type of way being built (road, rail, etc). |
| `bruecke` | Bridge | Bridge structure. |
| `tunnel` | Tunnel | Underground structure. |
| `terraforming` | Terraforming | Modifying terrain height. |
| `leitung` | Line/Conduit | Powerlines. |
| `fundament` | Foundation | Concrete base for buildings/ways. |
| `luft` | Air | Runways/Airport logic. |
| `fluss` | River | Waterway logic. |
