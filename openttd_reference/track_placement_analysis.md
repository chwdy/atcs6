# OpenTTD Track Placement Analysis

## Overview
Track placement is handled via the **Command Pattern**. Every action in OpenTTD (building, demolishing, changing settings) is a "Command" that includes validation and execution steps.

### File: `src/rail_cmd.cpp`
The primary function for building normal path placement is `CmdBuildSingleRail`.

## The process of `CmdBuildSingleRail`

### 1. Input Validation
Before any changes are made, the code verifies the move is legal:
*   **`ValParamRailType` & `ValParamTrackOrientation`**: Ensures arguments are valid enum values.
*   **`CheckTileOwnership`**: Ensures the player has rights to modify this tile (not owned by a competitor or town authority).
*   **`IsCompatibleRail`**: Checks if the new rail type can overlap with existing rail types (e.g., standard vs. electrified).

### 2. Physical & Logic Checks
*   **`CheckTrackCombination`**: Ensures the new track bit doesn't conflict physically with existing tracks (e.g., crossing 'impossibly').
*   **`EnsureNoTrainOnTrack`**: Critically, this iterates through vehicles to ensure a train isn't currently sitting on the tile where the user wants to build.
*   **`CheckRailSlope`**: Validates that the terrain slope supports the track (e.g., you can't build track on a cliff edge without a foundation).

### 3. Execution (Map Modification)
If `flags` contains `DoCommandFlag::Execute`, the map is updated:
1.  **`MarkTileDirtyByTile`**: Marks the tile for redraw.
2.  **`Command<CMD_LANDSCAPE_CLEAR>::Do`**: Clears trees or farm fields if present.
3.  **`SetTrackBits`**: This is the core operation. It performs a bitwise OR operation on the tile's data to add the new track direction.
4.  **`YapfNotifyTrackLayoutChange`**: Identifies that the track graph has changed, invalidating pathfinding caches so trains know about the new route immediately.

## Signal Removal
The command also handles logic for auto-removing signals if the new track overlaps with them (`auto_remove_signals` parameter), adding the cost of signal removal to the total build cost.

## Code Flow Summary
```cpp
CommandCost CmdBuildSingleRail(...) {
    // 1. Validate
    if (Failed(CheckTileOwnership(tile))) return Error;
    if (Failed(CheckTrackCombination(tile))) return Error;
    if (Failed(EnsureNoTrainOnTrack(tile))) return Error;

    // 2. Prepare
    if (Overlap) RemoveSignals();

    // 3. Execute
    if (ExecuteFlag) {
        SetTrackBits(tile, existing_bits | new_bits);
        YapfNotifyTrackLayoutChange(tile); // Update Pathfinder
    }

    return Cost;
}
```
