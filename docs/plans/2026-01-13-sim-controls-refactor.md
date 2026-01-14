# Design: Simulation Controls Refactor

## Goal
Combine Play/Pause into a single toggle button and replace the speed slider with preset buttons (0.5x, 1x, 2x, 3x) for better usability and space efficiency.

## 1. HTML Structure (`static/index.html`)

Replace the existing `.simulation-controls` content with:

```html
<div class="simulation-controls">
    <button id="btn-play-pause" class="btn btn-primary" title="Start/Pause Simulation">
        <span class="icon">▶</span> Play
    </button>
    <button id="btn-reset" class="btn btn-danger" title="Reset Simulation">
        <span class="icon">↺</span>
    </button>
    <div class="speed-presets">
        <button class="btn btn-sm speed-btn" data-speed="0.5">0.5x</button>
        <button class="btn btn-sm speed-btn active" data-speed="1">1x</button>
        <button class="btn btn-sm speed-btn" data-speed="2">2x</button>
        <button class="btn btn-sm speed-btn" data-speed="3">3x</button>
    </div>
</div>
```

## 2. CSS Styling (`static/css/style.css`)

- **Simulation Controls Container:** Flexbox, aligned items.
- **Play/Pause Button:** Fixed width to prevent jumping when text changes.
- **Reset Button:** Icon only, compact.
- **Speed Presets:**
    - Flex container with small gap.
    - Buttons: `btn-sm`, styled to look grouped.
    - Active state: Distinct background color (e.g., accent color).

## 3. JavaScript Logic (`static/js/app.js`)

- **State Management:** Track `isSimulationRunning` (already exists).
- **Handlers:**
    - `btn-play-pause` click:
        - If running: Call `stopSimulationPolling()`, update UI to "Play".
        - If stopped: Call `startSimulationPolling()`, update UI to "Pause".
    - `btn-reset` click:
        - Call `stopSimulationPolling()`, update UI to "Play".
        - Call `refreshNetwork()`.
    - Speed buttons click:
        - Call `window.api.controlSimulation('set_speed', speed)`.
        - Update active class on buttons.
- **UI Update Helper:**
    - `updatePlaybackUI(isPlaying)`: Updates button text/icon/class.

## 4. Verification Plan

1.  **Play/Pause Toggle:**
    - Click Play -> Sim starts, button changes to Pause.
    - Click Pause -> Sim stops, button changes to Play.
2.  **Reset:**
    - While playing -> Sim stops, network resets, button changes to Play.
3.  **Speed Control:**
    - Click 2x -> Toast "Speed set to 2x", 2x button active.
    - Verify simulation actually speeds up (visual check).
