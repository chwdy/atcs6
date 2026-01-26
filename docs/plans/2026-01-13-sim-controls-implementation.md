# Simulation Controls Refactor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Combine Play/Pause into a single toggle button and replace the speed slider with preset buttons (0.5x, 1x, 2x, 3x) for better usability.

**Architecture:** Frontend-only refactor involves HTML structural changes, CSS styling for the new layout, and JavaScript logic updates to handle the toggle state and preset interactions.

**Tech Stack:** HTML5, CSS3 (Flexbox), Vanilla JavaScript (ES6+).

### Task 1: HTML Structure Update

**Files:**
- Modify: `static/index.html`

**Step 1: Locate Simulation Controls**
Find the `.simulation-controls` div in `static/index.html`.

**Step 2: Replace Content**
Replace the existing buttons (`btn-play`, `btn-pause`, `btn-reset`) and `.speed-control` slider with the new structure:

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

**Step 3: Verification**
- Open `static/index.html` and verify the structure matches.
- Run `tests/test_app_smoke.py` to ensure no broken tags.

### Task 2: CSS Styling

**Files:**
- Modify: `static/css/style.css`

**Step 1: Style Simulation Controls Container**
Ensure `.simulation-controls` uses flexbox alignment.

```css
.simulation-controls {
    display: flex;
    gap: 10px;
    align-items: center;
    padding-right: 20px;
    border-right: 1px solid var(--border-color);
}
```

**Step 2: Style Play/Pause Button**
Give it a fixed width to prevent layout shifts when text changes.

```css
#btn-play-pause {
    min-width: 80px;
    justify-content: center;
}
```

**Step 3: Style Speed Presets**
Style the group of buttons.

```css
.speed-presets {
    display: flex;
    gap: 2px;
    background: var(--bg-dark);
    padding: 2px;
    border-radius: 4px;
    border: 1px solid var(--border-color);
}

.speed-btn {
    padding: 4px 8px;
    font-size: 0.75rem;
    background: transparent;
    border: none;
    color: var(--text-secondary);
    border-radius: 2px;
    cursor: pointer;
    transition: all 0.2s;
}

.speed-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
}

.speed-btn.active {
    background: var(--accent-color);
    color: white;
    font-weight: 600;
}
```

**Step 4: Remove Old Styles**
Remove unused styles related to `.speed-control` slider if any.

### Task 3: JavaScript Logic Update

**Files:**
- Modify: `static/js/app.js`

**Step 1: Update `setupControls`**
- Remove old listeners for `btn-play`, `btn-pause`, `speed-slider`.
- Add listener for `btn-play-pause`.
- Add listener for `btn-reset`.
- Add listeners for `.speed-btn`.

**Step 2: Implement `togglePlayback` Logic**
Inside `setupControls`:

```javascript
const playPauseBtn = document.getElementById('btn-play-pause');
playPauseBtn.addEventListener('click', () => {
    if (this.isSimulationRunning) {
        // Pause
        window.api.controlSimulation('stop').catch(e => console.error('[App] Pause error:', e));
        this.stopSimulationPolling();
        this.updatePlaybackUI(false);
    } else {
        // Play
        window.api.controlSimulation('start').catch(e => console.error('[App] Play error:', e));
        this.startSimulationPolling();
        this.updatePlaybackUI(true);
    }
});
```

**Step 3: Implement `btn-reset` Logic**
Update reset handler to ensure UI resets to "Play" state.

```javascript
document.getElementById('btn-reset').addEventListener('click', () => {
    window.api.controlSimulation('reset').catch(e => console.error('[App] Reset error:', e));
    this.stopSimulationPolling();
    this.updatePlaybackUI(false); // Ensure button shows Play
    this.refreshNetwork();
});
```

**Step 4: Implement Speed Presets**
Add delegation or loop for speed buttons.

```javascript
document.querySelectorAll('.speed-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
        const speed = parseFloat(e.target.dataset.speed);
        window.api.controlSimulation('set_speed', speed);
        
        // Update active state
        document.querySelectorAll('.speed-btn').forEach(b => b.classList.remove('active'));
        e.target.classList.add('active');
        
        window.showToast(`Speed set to ${speed}x`, 'info', 1000);
    });
});
```

**Step 5: Add `updatePlaybackUI` Helper**
Add this method to `App` class.

```javascript
updatePlaybackUI(isPlaying) {
    const btn = document.getElementById('btn-play-pause');
    if (!btn) return;
    
    if (isPlaying) {
        btn.innerHTML = '<span class="icon">⏸</span> Pause';
        btn.classList.remove('btn-primary');
        btn.classList.add('btn-secondary'); // Or warning color
    } else {
        btn.innerHTML = '<span class="icon">▶</span> Play';
        btn.classList.remove('btn-secondary');
        btn.classList.add('btn-primary');
    }
}
```

**Step 6: Verify Syntax**
Run `tests/test_javascript_syntax.py` to check for syntax errors.

### Task 4: Verification & Cleanup

**Files:**
- Run: `tests/test_app_smoke.py`
- Run: `tests/test_javascript_syntax.py`

**Step 1: Run Syntax Tests**
Ensure no syntax errors in `app.js`.

**Step 2: Run Smoke Tests**
Ensure app structure is intact.

**Step 3: Manual Verification Checklist**
- [ ] Click Play -> Button changes to "Pause", Sim starts.
- [ ] Click Pause -> Button changes to "Play", Sim stops.
- [ ] Click Reset -> Button changes to "Play", Sim stops.
- [ ] Click Speed Buttons -> Active class updates, toast appears.
