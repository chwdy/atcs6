# Reference: Signals and Block Logic

## Context
This document explains how **Signals** work, how **Blocks** are defined, and how vehicles interact with them.
**Core Concept:** This system uses a **Vehicle-Driven Reservation System**. Signals are largely passive markers; the *Vehicle* calculates whether a block is clear, not the signal itself.

## 1. Signal Installation
When a user places a signal, a `Signal` object is placed on a tile.
*   **Direction:** Signals are directional. They apply to vehicles moving *in* that direction.
*   **Edge Alignment:** The signal aligns with a specific edge (N/E/S/W) of the tile based on the track segment it monitors.
*   **track_bits context:** Signals reference which segment(s) they control (e.g., a signal on edge N affects trains entering/exiting via the north edge).

## 2. Block Definition
In Simutrans, a "Block" is **not** a static region of the map.
*   **Dynamic Definition:** A block is simply "The segment of track between the Train's current front and the *next* signal (or end of line)."
*   **Reservation:** Trains do not strictly "occupy" blocks; they "reserve" them. A fast train might reserve 10 blocks ahead, while a slow one reserves 2.

## 3. The Interaction Loop
The interaction is defined in `rail_vehicle_t::block_reserver` and `is_signal_clear`.

### Step 1: Scanning Ahead
A moving train constantly tries to "reserve" tiles ahead of it to ensure safety. It scans properties of the `route_t` it calculated earlier.

### Step 2: Encountering a Signal
When the reservation logic encounters a tile with a `roadsign_t` (Signal):
1.  **Stop Check:** The vehicle calls `is_signal_clear()`.
2.  **Recursive Check:** The vehicle tentatively "looks past" the signal.
    *   It checks the *next* block (up to the signal after that).
    *   It asks: "Is the track in that next segment free?"
3.  **Decision:**
    *   **If Free:** The signal state updates to `STATE_GREEN` (visual only). The vehicle reserves the tiles and continues.
    *   **If Occupied:** The signal state updates to `STATE_RED`. The vehicle **halts** before entering the signal tile.

### Step 3: Preshignals & Chains
Complex signals (Pre-signals, Choose-signals) add logic to Step 2.
*   **Pre-signal:** "Is the block after the *next* signal also free?" (Recursive depth = 2).
*   **Choose-signal:** "If the default path is blocked, can I find an alternative path to a free platform?" (Triggers a mini-pathfinding search).

## 4. Summary of Control Flow
1.  **Vehicle** wants to move.
2.  **Vehicle** checks `can_enter_tile` for next tile.
3.  **Vehicle** sees Signal on next tile.
4.  **Vehicle** checks validity of track *behind* signal.
5.  **Vehicle** updates Signal State (Red/Green) based on result.
6.  **Vehicle** moves or stops.

*Note: The Signal does not stop the train. The Train stops itself because the Signal "told" it the path ahead is blocked.*

---

## German-to-English Keyword Translation

| German Term | English Meaning | Description |
| :--- | :--- | :--- |
| `signal` | Signal | The visual traffic light object. |
| `blockstrecke` | Block Section | A segment of track. |
| `reserver` | Reserver | Logic to lock a tile for a specific train. |
| `zustand` | State | Properties (e.g., Red/Green state). |
| `vor_signal` | Pre-signal | Signal that checks 2 blocks ahead. |
| `wahl_signal` | Choose-signal | Signal that allows routing into multiple station platforms. |
