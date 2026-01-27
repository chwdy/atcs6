# OpenTTD Track Display Analysis

## Overview
Rendering in OpenTTD is tile-based. Each tile type (Rail, Road, House, etc.) registers a "Draw Procedure". For rail tiles, this is `DrawTile_Track`.

### File: `src/rail_cmd.cpp`
The rendering workflow is defined in `DrawTile_Track`.

## Rendering Pipeline (`DrawTile_Track`)

### 1. Data Retrieval
The function first gathers state about the tile:
*   `GetRailType(tile)`: Is it electric, maglev, or steam?
*   `GetTrackBits(tile)`: Which specific rail pieces are present (N-S, NE-SW, etc.)?
*   `GetTileOwner(tile)`: Determines which company color to apply to fences/depots.

### 2. Layered Drawing
OpenTTD draws tiles in specific layers to handle depth correctly (2.5D isometric).

#### A. Ground & Foundation
*   If the track is on a slope, `DrawFoundation` is called to draw the leveled concrete/earth base.
*   `DrawGroundSprite` draws the base grass/snow/desert texture.

#### B. The Tracks (`DrawTrackBits`)
This is the core visual component.
*   It looks up the correct sprite from the `RailTypeInfo`.
*   It combines the `TrackBits` (e.g., `TRACK_BIT_X | TRACK_BIT_Y`) to find the correct sprite index for junctions or crossovers.

#### C. Details & Overlay
*   **`DrawRailCatenary`**: If the rail type is electrified, this function draws the overhead wires/pylons on top of the tracks.
*   **`DrawSignals`**: Checks for signals and draws the correct semaphore or light sprite (Green/Red state is checked here).
*   **`DrawBridgeMiddle`**: If the tile is part of a bridge, the bridge floor/railings are drawn here to ensure they appear "above" the track.

## The "Procs" Structure
OpenTTD uses a virtual-table-like structure for C-style polymorphism:
```cpp
extern const TileTypeProcs _tile_type_rail_procs = {
    DrawTile_Track,           // Rendering
    GetSlopePixelZ_Track,     // Height calculation
    ClearTile_Track,          // Demolition
    GetTileTrackStatus_Track, // Logic/State
    // ...
};
```

## Key Concept: Sprites
The system relies heavily on **NewGRF** and Sprite Tables. `DrawTile_Track` calculates a `SpriteID`. The engine then looks up this ID in the loaded graphics set (GRF) to find the actual pixel data to put on screen. This abstraction allows for texture packs and mods to easily replace the visuals without changing the code.
