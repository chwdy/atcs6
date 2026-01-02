# OpenTTD Tile Bit System & Track Representation

## Overview
OpenTTD uses a highly optimized bitmask system to store track data directly within the map's tile structure. This allows for extremely fast lookups and efficient memory usage.

### Key File: `src/track_type.h`
This file defines the enums `Track`, `TrackBits`, `Trackdir`, and `TrackdirBits`.

## 1. The Core: `TrackBits` (Representing *What* is There)
`TrackBits` is a bitfield (uint8_t) that tells you exactly which rail pieces exist on a tile. A single tile can have multiple rail pieces (e.g., a crossing).

### The 6 Basic Indivisible Tracks
There are 6 possible "single" track pieces that can exist on a flat tile.
*   `TRACK_BIT_X` (Bit 0): A straight line from NE to SW.
*   `TRACK_BIT_Y` (Bit 1): A straight line from NW to SE.
*   `TRACK_BIT_UPPER` (Bit 2): A 90° corner in the North corner (North to East).
*   `TRACK_BIT_LOWER` (Bit 3): A 90° corner in the South corner (South to West).
*   `TRACK_BIT_LEFT` (Bit 4): A 90° corner in the West corner (West to North).
*   `TRACK_BIT_RIGHT` (Bit 5): A 90° corner in the East corner (East to South).

### Combinations (Junctions & Crossings)
Any valid combination of these bits creates junctions.
*   **Simple Cross**: `TRACK_BIT_X | TRACK_BIT_Y` (Bitmask: `0000 0011`)
*   **3-Way Junction**: `TRACK_BIT_X | TRACK_BIT_UPPER | TRACK_BIT_RIGHT` (Creates a junction pointing NE).
*   **Full 4-Way**: Theoretically `TRACK_BIT_ALL`, but usually limited by game rules to prevent impossible turns.

## 2. Directional Logic: `Trackdir` (Representing *Movement*)
While `TrackBits` describes the physical static rail, `Trackdir` describes a train's movement along that rail. 
Since every rail piece can be traversed in two directions, there are 12 `Trackdir`s for the 6 `Track`s.

*   `0` and `8`: Moving along `TRACK_X` (NE vs SW).
*   `1` and `9`: Moving along `TRACK_Y` (SE vs NW).
*   `...` and so on for the corners.

The relationship is simple: `ReverseTrackdir(td) = td ^ 8`.

## 3. Signal Interaction
Signals are NOT stored in the `TrackBits`. They are stored in a separate byte in the `RailTile` structure (accessed via `GetPresentSignals` and `GetSignalStates`).

### How Signals "Snap" to Track
Signals are conceptually located *between* tiles or at the entrance/exit of a `Trackdir`.
*   A tile with `TRACK_BIT_X` can have signals for `TRACKDIR_X_NE` and/or `TRACKDIR_X_SW`.
*   **Block Signals**: Stored as a bitmask relative to the available track direction.
*   **PBS (Path Based Signals)**: More complex, checking `Trackdir` reservation against the global pathfinder state.

### Code Example (Checking for Validity)
```cpp
// Check if a specific track piece exists on a tile
if (GetTrackBits(tile) & TRACK_BIT_X) {
    // There is an X-axis track here
}

// Check if a train can enter from the North-East
if (HasTrackdir(GetTrackBits(tile), TRACKDIR_X_SW)) {
    // Yes, a train coming from NE moving SW has a valid rail piece.
}
```

## 4. Special Cases
*   **Wormholes (Tunnels/Bridges)**: Uses `TRACK_BIT_WORMHOLE` (0x40). The tile doesn't have standard "bits" but points to an entrance/exit.
*   **Depots**: Uses `TRACK_BIT_DEPOT` (0x80).
