# Reference: Vehicle Pathfinding

> **Last Verified:** 2026-01-16

## Context
This document details the **Vehicle Pathfinding** system (`route_t`), used by convoys (trains, trucks, ships) to navigate the **existing** network.
**Crucial Distinction:** This is *not* the system used when you drag the mouse to build tracks. This system assumes the tracks already exist and finds the best valid route for a vehicle to travel.

## 1. The Core: `route_t` & A*
The pathfinding uses a standard A* (A-Star) algorithm.
*   **Goal:** Find a sequence of existing tiles from Start to Target.
*   **Graph:** The nodes are the tiles (`grund_t`). The edges are the existing connections.

### The Search Loop (`route_t::intern_calc_route`)
1.  **Open List:** A binary heap favors the node with the lowest `f` score (`cost + heuristic`).
2.  **Expansion:** For the current tile, look at neighbors.
3.  **Validity (`test_driver_t`):** The `check_next_tile` method determines if the specific vehicle can physically enter the next tile.
4.  **Cost:** The algorithm accumulates "G-Score" (Cost so far) based on the factors below.

---

## 2. Cost Factors (Why trains choose their path)
The cost function determines the "best" route. It is a combination of the generic A* logic and the vehicle-specific `get_cost` method.

### Base Costs (`rail_vehicle_t::get_cost`)
*   **Speed Penalty:**
    *   If `Max_Tile_Speed >= Convoi_Max_Speed`: **Cost = 1** (Best case).
    *   If `Max_Tile_Speed < Convoi_Max_Speed`: **Cost increases**. The formula is effectively `4 - (3 * Tile_Speed / Convoi_Speed)`.
    *   *Result:* Fast express trains will avoid slow depot tracks or sidings if a faster main line is available.
*   **Slopes (`cost_upslope`):**
    *   Climbing a slope adds a massive penalty: **+25 points**.
    *   *Result:* Trains will take significant detours to avoid climbing a hill.

### Turn Penalties (`route_t` logic)
The generic A* solver adds its own penalties to encourage straight lines:
*   **Turn:** **+3 points** (Changing direction, e.g., North to East).
*   **Zig-Zag (Double Curve):** **+10 points**.
    *   Occurs if you turn immediately after another turn (e.g., N -> E -> N).
    *   *Result:* This heavily penalizes "staircase" paths and encourages long straight diagonals.
*   **V-Turn:** **+25 points**.
    *   Turning 90 degrees/perpendicular.

---

## 3. Vehicle-Specific Logic (`test_driver_t`)
The `route_t` class is generic. It delegates vehicle-specific logic to a `test_driver_t` object (implemented by `vehicle_t`).

### Tile Validation (`check_next_tile`)
Before adding a tile to the path, the vehicle checks:
*   **Way Existence:** Is there a way of the correct type (Rail vs Road)?
*   **Electrification:** If the vehicle is electric, is the track electrified (`is_electrified`)?
*   **Weight Limits:** (If enabled) Is the bridge/track strong enough for the axle load?
*   **Clearance:** Is there vertical clearance?
*   **Signals/Signs:**
    *   **One-Way:** Checks `edge_mask` to see if the signal restricts direction.
    *   **Private Ways:** Checks ownership (cannot enter competitors' private roads).

### Edge Connectivity Check
When determining valid movement directions, the system uses EDGE_MASKS:
*   **N edge (mask 13):** track_bits containing NS(1), NE(4), or NW(8)
*   **E edge (mask 22):** track_bits containing EW(2), NE(4), or SE(16)
*   **S edge (mask 49):** track_bits containing NS(1), SE(16), or SW(32)
*   **W edge (mask 42):** track_bits containing EW(2), NW(8), or SW(32)

A vehicle can exit via edge X if: `(track_bits & EDGE_MASKS[X]) != 0`

---

## 4. Signals and Reservation
It is important to note the distinction between **Pathfinding** and **Movement (Block Reservation)**.

*   **Pathfinding (`route_t`):** Finds a *static* valid path. It essentially ignores whether a signal is currently "Red" or "Green". It only cares if the signal allows travel in that *direction* (edge-based one-way logic).
*   **Movement (`drive`):** As the convoy moves, it reserves blocks ahead. If a signal is red, the train stops *on the path*. It does NOT re-calculate the path unless it is stuck for a long time.

---

## 5. Keyword Reference

| Term | Meaning | Description |
| :--- | :--- | :--- |
| `route` | Route/Path | The calculated sequence of coordinates. |
| `target` | Target | The destination coordinate. |
| `tile` | Tile | A single grid cell in the game world. |
| `track_bits` | Track Segments | 6-bit value (0-63) indicating which track segments exist on a tile. |
| `edge_mask` | Edge Connectivity | Bitmask checking which edges (N/E/S/W) have track connections. |
| `test_driver` | Test Driver | Interface for vehicle capability checks. |
| `cost` | Costs | Abstract value for distance/difficulty. |
