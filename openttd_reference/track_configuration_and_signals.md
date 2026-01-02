# OpenTTD Track Configuration & Signal Logic

## 1. Track Configurations (Switches & Junctions)

### 6 Basic Paths -> Infinite Configurations
You are correctly realizing that the 6 `Track` bits (X, Y, Upper, Lower, Left, Right) represent the specific *physical* rail paths on a single tile. 
*   **Automatic Generation?** No. OpenTTD does **not** automatically generate complex switches like "Wyes" or "Scissors crossovers" from a high-level command like "Connect A to B".
*   **User Definition:** The user (or an algorithm) must explicitly place each individual piece. If you want a T-junction, you must place the horizontal track (`TRACK_BIT_X`) and then add the curve (`TRACK_BIT_LOWER`) on the same tile. 
*   **Logic:** The system simply logical-ORs the bits. If you add `TRACK_BIT_LOWER` to a tile that already has `TRACK_BIT_X`, it becomes a junction. 
*   **Wye / Crossover:** These are just specific combinations of bits on adjacent tiles. A crossover is just two diagonal tracks meeting at a tile with `TRACK_BIT_CROSS` (X + Y).

### Restrictions (Impossible Patterns)
The engine prevents certain "impossible" physical overlaps on a single tile to simplify rendering and logic:
*   You generally cannot have a "U-Turn" on a single tile (e.g., entering North implies exiting South, East, or West; never North).
*   Specific restrictions are checked in `CheckTrackCombination` in `rail_cmd.cpp`.

## 2. Signals

### Signal Types (in `signal_type.h`)
OpenTTD supports several types, defined in the `SignalType` enum:
1.  **Block (`SIGTYPE_BLOCK`)**: Standard model. The track between two signals is one "block". Only one train allowed per block.
2.  **Pre-Signals (`SIGTYPE_ENTRY`, `EXIT`, `COMBO`)**: Logic gates for trains. An Entry signal remains red unless there is a green Exit signal reachable.
3.  **PBS (Path Based Signals) (`SIGTYPE_PBS`)**: The modern standard. It allows *multiple* trains in a "block" as long as their specific requested paths do not overlap.

### Signal Control & "Snapping"
*   **Location:** Signals are conceptually placed on the *nodes* between tracks.
*   **Direction:** Signals are directional. A standard signal on `TRACK_BIT_X` controls trains moving NE -> SW. You need a second signal for the other direction.
*   **One-Way:** Defined by `SIGTYPE_PBS_ONEWAY` or by placing a standard signal and toggling its "back" to blocked.
*   **Control Range:** A signal does NOT "know" which track part it controls. Instead, the *Train* (via the Pathfinder) looks ahead. When it sees a signal, it asks "Is the path *after* this signal clear?".

## 3. Station Platforms

### Structure
*   **Tile-Based:** A station is just a collection of tiles with `MP_STATION` type.
*   **No "Link" Logic:** A platform doesn't inherently "know" it's connected to the track next to it.
*   **Pathfinding Constraint:** The Penalty system (`yapf_costrail.hpp`) adds a massive cost (`rail_station_penalty`) to passing through a station tile that isn't your destination. This effectively tells trains "Only go here if you are stopping."

## 4. One-Way Tracks
OpenTTD doesn't have a "One-way Track" type. It uses "Signals" to enforce direction.
*   **Mechanism:** If you place a "One-Way Signal" (or a standard signal facing only one way), the pathfinder treats the Reverse direction as `infinite cost` (Dead End).
*   **Implication for ATC:** You don't need to model "One-way rail". You model "Rail" + "Directional Constraints (Signals)".

## Summary for ATC Implementation
To replicate this:
1.  **Grid Data Structure:** Each cell needs a 6-bit integer for `TrackBits`.
2.  **Signal Layer:** A separate layer (or property on the cell) storing `SignalType` and `SignalDirection` for each of the 2 ends of each track bit.
3.  **Junctions:** Do not model "Switch Objects". Just model "Does this cell have >2 exits?". If yes, it's a switch.
4.  **Pathing:** The "Switch" is decided by the train's pathfinder choosing which bit to follow (e.g., arriving on `X` (NE), choosing to leave via `LOWER` (South) instead of continuing `X` (SW)).
