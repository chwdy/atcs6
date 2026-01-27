# OpenTTD Pathfinding: Construction vs. Operation

## Overview
It is critical to distinguish between the "Pathfinding" used by a train to get from A to B, and the "Validation" used by the game engine when a player tries to build a track. They are two completely separate systems.

## 1. Construction Validation ("Can I fit this here?")
When a user drags a track tool, the game does NOT run A* pathfinding. It performs **Static Local Validation**.

### Mechanism: `CheckTrackCombination`
Located in `src/rail_cmd.cpp`, this function takes the *existing* `TrackBits` on the tile and performs a bitwise check against the *proposed* `TrackBits`.

**The Logic:**
1.  **Bitwise OR**: It temporarily calculates `Result = ExistingBits | NewBits`.
2.  **Table Lookup**: It checks `Result` against a table of "Valid Configurations".
    *   *Example*: You cannot have `TRACK_BIT_X` (Horizontal) AND `TRACK_BIT_LEFT` (West corner) on the same tile if it would create an ambiguous or physically impossible junction (like a localized loop or self-intersection that the engine forbids).
3.  **Slope Check**: It calls `GetRailFoundation`. If the new combination requires a foundation that conflicts with the existing slope (e.g., trying to put a flat cross on a steep slope), it fails.

**Key Difference**: This is purely *geometry* and *physics*. It doesn't care if the track leads anywhere. It just cares if the pieces fit together on the 64x64 grid.

## 2. Operational Pathfinding ("Where do I go?")
This happens *after* the track is built, while the game is running.

### Mechanism: YAPF (in `yapf_rail.cpp`)
This uses the **Global Graph**.

**The Logic:**
1.  **Segments**: It treats the `TrackBits` validated above as mere nodes in a graph.
2.  **Cost Evaluation**: It doesn't ask "Is this valid?" (because it already is). It asks "How expensive is this?".
3.  **Dynamic State**: It checks things construction doesn't care about:
    *   Is another train blocking this `Trackdir`?
    *   Is the signal Red?
    *   Is the station platform long enough?

## Comparison Table

| Feature | Construction (`rail_cmd.cpp`) | Operation (`yapf_rail.cpp`) |
| :--- | :--- | :--- |
| **Question** | "Is this shape physically possible?" | "Is this route efficient?" |
| **Scope** | Single Tile (mostly) | Global Network |
| **Input** | `TrackBits` (Geometry) | `Trackdir` + `SignalState` (Graph) |
| **Speed** | Instant (Lookup Table) | Complex (A* Search) |
| **Conflicts** | Geometry (e.g., Rails crossing weirdly) | Traffic (e.g., Train in way) |

## Implementation Note for Engineers
When replicating this:
*   **Do not** use your A* engine to validate building placement. It's too slow and checks the wrong things.
*   **Do** create a simple bitmask lookup table for valid track combinations on a single tile.
*   **Do** ensure that every successful Build Command triggers a "Graph Dirty" event so the A* engine knows to re-scan the map.
