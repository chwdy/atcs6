# API Cleanup Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove confirmed unused methods from `static/js/api.js` to clean up the codebase.

**Architecture:** Refactoring (Deletion).

**Tech Stack:** JavaScript.

### Task 1: Remove Unused Methods

**Files:**
- Modify: `static/js/api.js`
- Test: `tests/test_javascript_syntax.py`

**Step 1: Remove Legacy Train Methods**
Remove:
- `trainSetDirection`
- `trainClearDirection`
- `trainDepart`
- `trainArrive`
- `trainTurnAround`
- `trainEnableManual`
- `trainDisableManual`
- `trainEmergencyStop`
- `trainStop`

**Step 2: Remove Unused Schedule Methods**
Remove:
- `getSchedule`
- `createSchedule` (UI implementation missing)

**Step 3: Remove Unused Path/Dispatcher Methods**
Remove:
- `completePath`
- `findPaths`

**Step 4: Remove Other Unused Methods**
Remove:
- `removeTracksFromBlock` (Duplicate of `removeBlockTiles`)
- `setScalePreset`
- `getSwitches` (No UI list for switches)

**Step 5: Verify Syntax**
Run `tests/test_javascript_syntax.py` to ensure file integrity.
