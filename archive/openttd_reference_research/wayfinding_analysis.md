# OpenTTD Wayfinding Analysis

## Overview
OpenTTD uses **YAPF** (Yet Another Pathfinder) as its primary pathfinding engine for trains. The logic is highly modular, using C++ templates to define cost functions and node follow behaviors. The core files are located in `src/pathfinder/yapf/`.

## Key Logic: Cost Calculation
The heart of the wayfinding logic lies in how "cost" is assigned to every possible move. A train will always try to minimize this cost.

### File: `src/pathfinder/yapf/yapf_costrail.hpp`
The class `CYapfCostRailT` defines the cost parameters.

### Major Cost Factors

1.  **Distance (Base Cost)**
    *   **Straight**: `YAPF_TILE_CORNER_LENGTH`
    *   **Diagonal**: `YAPF_TILE_LENGTH` (Pythagorean approximation).

2.  **Curve Penalties**
    *   **90-degree turns**: Heavily penalized (`rail_curve90_penalty`) to encourage realistic, smooth turns.
    *   **45-degree turns**: Slight penalty (`rail_curve45_penalty`).

3.  **Slope Penalties**
    *   Climbing a slope adds `rail_slope_penalty`. This encourages trains to take longer, flat routes over shorter, steep ones.

4.  **Signal Penalties (Look-ahead)**
    *   The pathfinder looks ahead at future signals.
    *   **Red Signals**: Adds a cost relative to how far away the red signal is.
    *   **PBS (Path Based Signals)**: Checks if the specific track path is reserved by another train.

5.  **Station & Depot Penalties**
    *   Passing through a station platform (that is NOT the destination) adds a significant penalty (`rail_station_penalty`).
    *   Reversing in a depot adds `rail_depot_reverse_penalty`.

## Code Snippet Example
```cpp
/* Curve Cost Calculation */
inline int CurveCost(Trackdir td1, Trackdir td2)
{
    int cost = 0;
    if (TrackFollower::Allow90degTurns() && HasTrackdir(TrackdirCrossesTrackdirs(td1), td2)) {
        cost += Yapf().PfGetSettings().rail_curve90_penalty;
    } else if (td2 != NextTrackdir(td1)) {
        cost += Yapf().PfGetSettings().rail_curve45_penalty;
    }
    return cost;
}
```

## How It Works
1.  **Node Expansion**: The pathfinder expands "nodes" (track segments).
2.  **Cost Accumulation**: For each new node, it calls `PfCalcCost`.
3.  **Caching**: To improve performance, segment costs (straight runs of track) are cached so they don't need to be recalculated every tick.
4.  **Decision**: The train chooses the track direction (`Trackdir`) associated with the lowest total path cost.
