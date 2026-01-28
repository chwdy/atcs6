# Frontend Code Simplification Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Simplify and standardize JavaScript codebase (`api.js` and `helpers.js`) to improve maintainability, reduce boilerplate, and enforce consistent error handling patterns.

**Architecture:** 
- **API:** Introduce a central `_request()` method to handle `fetch` calls, JSON serialization, and error logging/throwing.
- **Helpers:** Use modern ES6+ features (lookup maps, template literals) to reduce verbosity in utility functions.

**Tech Stack:** Vanilla JavaScript (ES6+)

### Task 1: Refactor API Class (`api.js`)

**Files:**
- Modify: `static/js/api.js`
- Test: `tests/test_javascript_syntax.py`, `tests/test_api.js` (if available), manual verification

**Step 1: Create `_request` helper method**
Add a private-like method `_request(endpoint, options)` to the `API` class that handles:
- URL construction
- Default headers
- Body JSON serialization
- Response status checking (204, 404, generic errors)
- Error logging

**Step 2: Refactor network and track methods**
Update `getNetwork`, `getTrack`, `updateTrack`, `deleteTrack` to use `_request`.

**Step 3: Refactor train methods**
Update `createTrain`, `getTrains`, `deleteTrain`, `updateTrain` to use `_request`.

**Step 4: Refactor simulation control methods**
Update `controlSimulation` to use `_request`.

**Step 5: Refactor block and signal methods**
Update all block and signal CRUD methods to use `_request`.

**Step 6: Refactor station and schedule methods**
Update station and schedule methods to use `_request`.

**Step 7: Verify functionality**
Run syntax tests to ensure no regressions.

### Task 2: Refactor Helpers (`helpers.js`)

**Files:**
- Modify: `static/js/helpers.js`
- Test: `tests/test_javascript_syntax.py`

**Step 1: Simplify `getTrackExits`**
Refactor to use a lookup map or cleaner conditional logic.

**Step 2: Simplify `render...` functions**
Standardize string interpolation and remove redundant variables.

**Step 3: Verify functionality**
Run syntax tests.
