# Reference: UI Interaction for Building

> **Note:** This document describes design patterns from Simutrans (C++). For actual implementation, see `static/js/editor.js`.
>
> **Last Verified:** 2026-01-16

## Context
This document explains how user interactions (clicks, drags, dialogs) translate into building actions, based on Simutrans patterns.
Key classes in Simutrans are defined in `src/simutrans/tool/simtool.h` and implemented in `simtool.cc`.

## 1. General Tool Architecture
All tools inherit from `tool_t`.
*   **`init(player)`:** Called when the tool is selected. Can open dialogs (like `signal_spacing_frame_t` if CTRL is pressed).
*   **`work(player, pos)`:** The core action method. Called when the user clicks on the map.
*   **`move(player, button_state, pos)`:** Called when the user drags the mouse. Used for previewing paths.

---

## 2. Building Ways (Tracks/Roads)
**Class:** `tool_build_way_t`
**Type:** `two_click_tool_t` (Requires Start and End points).

### Interaction Flow
1.  **Selection (`init`):**
    *   The `default_param` contains the `way_desc` name (e.g., "wooden_sleeper_track").
    *   It sets the current cursor image.
2.  **First Click (`work`):**
    *   Records `start` position.
    *   Sets `is_dragging = true`.
3.  **Dragging (`move`):**
    *   Calls `way_builder_t::calc_route(start, current_mouse_pos)` to find a path.
    *   Visualizes the preview using `mark_tiles` (showing ghost tracks).
4.  **Second Click (`work`):**
    *   Calls `do_work()`.
    *   `way_builder_t::build()` is executed to actually place the tiles.

### Deleting Routes
**Class:** `tool_wayremover_t`
*   Similar to building, but `do_work()` calls `way_builder_t::remove()`.
*   It checks `get_removal_error()` on every tile to ensure the player owns the track and no trains are currently on it.

---

## 3. Placing Signals
**Class:** `tool_build_roadsign_t`
**Type:** `two_click_tool_t` (Can be single click or drag for auto-spacing).

### Signal Selection
*   The signal type is determined by `default_param`.
*   **CTRL+Click** on the toolbar button opens `signal_spacing_frame_t`.
    *   Allows setting `signal_spacing` (e.g., every 5 tiles).
    *   Allows toggling "Replace existing signals".

### Interaction Flow
1.  **Single Click:**
    *   Places a single signal at `pos`.
    *   Direction is determined by the track's valid exits (via `track_bits` and edge masks).
2.  **Dragging (Signal Spacing):**
    *   User clicks Start and End.
    *   The tool calculates a route between them.
    *   It iterates over the route and places a signal every `spacing` tiles (e.g., every 10th tile).
    *   This is crucial for quickly signaling a long main line.

---

## 4. Building Stations
**Class:** `tool_build_station_t`
**Type:** `tool_t` (Single click) or `two_click_tool_t` (Drag for extent).

### Logic
*   **Rotation:** Checks `rotation` parameter or cycles through layouts.
*   **Validation:** `check_pos()` ensures the ground is flat and valid.
*   **Docking:** Special logic (`tool_station_dock_aux`) for placing docks on water slopes.
*   **Extension:** If placed next to an existing station, it merges (`haltestelle_t::add_grund`).

---

## German-to-English Keyword Translation

| German Term | English Meaning | Description |
| :--- | :--- | :--- |
| `werkzeug` | Tool | The base class for UI tools. |
| `zweiklick` | Two-Click | Tools requiring start/end (drag). |
| `zeiger` | Pointer | The mouse cursor or map marker. |
| `voreinstellungen`| Preferences | Settings like signal spacing. |
| `entfernen` | Remove | Deleting objects. |
