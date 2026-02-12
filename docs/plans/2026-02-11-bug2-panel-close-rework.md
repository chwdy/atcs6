# Bug 2 Panel Close Rework - Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** When path tools (Create Path / Extend Path) complete, restore the previously-open secondary panel instead of closing the sidebar, using the existing panel history stack -- not a manual save/restore hack.

**Architecture:** The existing `editor-ui.js` already has a panel history stack (`secondaryPanelHistoryBack`) that `showSecondarySidebar()` auto-pushes to when replacing panels (lines 246-252). Path tool reset functions should pop this stack via `goBackSecondaryPanel()` instead of calling `hideSecondarySidebar()`. The manual `_previousEditorType`/`_previousEditorId` save/restore pattern must be removed entirely.

**Tech Stack:** Vanilla JavaScript (ES6+), no build tools.

---

## Background

### The Problem

When a user has a secondary panel open (e.g., a block editor) and uses the Create Path or Extend Path tool:

1. `showRoutePrioritySelector()` / `showExtendRoutePanel()` opens a `path-setup` / `extend-path-setup` panel, replacing the current panel.
2. When the path operation completes (or is cancelled), `resetRouteToolState()` / `resetExtendRouteState()` is called.
3. These reset functions currently close the sidebar entirely via `hideSecondarySidebar()`, destroying all history.
4. The user loses their previously-open panel.

### Why the Current Fix is Bad

The current code manually saves `_previousEditorType`/`_previousEditorId` before the path tool opens, then manually restores them after. This is:
- Only 1 level deep (nested tools would lose state)
- Requires `refreshEditorIfNeeded()` to know about all editor types (it only handles 3)
- Duplicates the existing history stack mechanism
- Fragile and hard to maintain

### How the History Stack Already Works

`showSecondarySidebar()` (editor-ui.js:246-252) already pushes the current panel onto `secondaryPanelHistoryBack` when a new panel replaces it. So when `showRoutePrioritySelector()` calls `this.editor.showSecondarySidebar('Create Path', content)`, the previous panel is **automatically saved** to the history stack. We just need to pop it back instead of nuking it.

### Key Edge Cases

1. **No previous panel:** If no panel was open before the path tool, `goBackSecondaryPanel()` silently returns (history is empty). The path-setup panel stays visible. We must handle this by falling back to `hideSecondarySidebar()`.
2. **`validateCurrentEditor()` after `refreshNetwork()`:** After path creation, `refreshNetwork()` calls `validateCurrentEditor()`. The `path-setup` and `extend-path-setup` editor types are not in the switch statement, so `itemExists` stays `false`, causing the panel to close. We need to add these types (and `paths-list`) to the known-safe types list.
3. **Order matters:** `refreshNetwork()` is called BEFORE `resetRouteToolState()`. So `validateCurrentEditor()` runs while `_currentEditorType` is still `path-setup`. We must handle this correctly.

---

## Task 1: Remove the `_previousEditorType`/`_previousEditorId` save/restore pattern

**Files:**
- Modify: `static/js/editor-paths.js:34-66` (`resetRouteToolState`)
- Modify: `static/js/editor-paths.js:202-207` (`showRoutePrioritySelector`)
- Modify: `static/js/editor-paths.js:611-641` (`resetExtendRouteState`)
- Modify: `static/js/editor-paths.js:788-793` (`showExtendRoutePanel`)

**Step 1: Rewrite `showRoutePrioritySelector()` - remove manual save**

Remove lines 203-207 (the `_previousEditorType`/`_previousEditorId` save). Keep the `_currentEditorType = 'path-setup'` and `_currentEditorId = null` assignments, because `showSecondarySidebar()` reads these to build the `currentSecondaryPanel` snapshot (line 266-267). The auto-push to history happens at line 246-252 before those are read, so the previous panel is already captured.

Replace lines 202-209 of `showRoutePrioritySelector()` with:

```javascript
    showRoutePrioritySelector() {
        this.editor._currentEditorType = 'path-setup';
        this.editor._currentEditorId = null;
```

**Step 2: Rewrite `showExtendRoutePanel()` - remove manual save**

Replace lines 788-795 of `showExtendRoutePanel()` with:

```javascript
    showExtendRoutePanel() {
        this.editor._currentEditorType = 'extend-path-setup';
        this.editor._currentEditorId = this.extendPathState.selectedPathId;
```

**Step 3: Rewrite `resetRouteToolState()` - use `goBackSecondaryPanel()`**

Replace lines 50-66 (the `if (this.editor._currentEditorType === 'path-setup')` block) with:

```javascript
        if (this.editor._currentEditorType === 'path-setup') {
            this.editor.goBackSecondaryPanel();
            // If history was empty (no panel before path tool), goBack does nothing.
            // In that case _currentEditorType is still 'path-setup', so close the sidebar.
            if (this.editor._currentEditorType === 'path-setup') {
                this.editor.hideSecondarySidebar();
            }
        }
```

**Step 4: Rewrite `resetExtendRouteState()` - use `goBackSecondaryPanel()`**

Replace lines 625-641 (the `if (this.editor._currentEditorType === 'extend-path-setup')` block) with:

```javascript
        if (this.editor._currentEditorType === 'extend-path-setup') {
            this.editor.goBackSecondaryPanel();
            // If history was empty, goBack does nothing - close sidebar as fallback.
            if (this.editor._currentEditorType === 'extend-path-setup') {
                this.editor.hideSecondarySidebar();
            }
        }
```

**Step 5: Run JavaScript syntax tests**

Run: `source ~/.nvm/nvm.sh && nvm use 22 && conda run -n general pytest tests/test_javascript_syntax.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add static/js/editor-paths.js
git commit -m "fix: use panel history stack instead of manual save/restore for path tools"
```

---

## Task 2: Add path-tool editor types to `validateCurrentEditor()`

**Files:**
- Modify: `static/js/editor.js:272-316` (`validateCurrentEditor`)

### Why This Is Needed

After path creation, `handleRouteSecondClick()` calls `await window.app.refreshNetwork()` (line 187) BEFORE calling `resetRouteToolState()` (line 188). Inside `refreshNetwork()`, `validateCurrentEditor()` runs while `_currentEditorType` is still `'path-setup'`. Since `'path-setup'` is not in the switch, `itemExists` stays `false` and `hideSecondarySidebar()` is called -- nuking the history stack before `resetRouteToolState` can pop it.

**Step 1: Add path-tool types to the switch statement**

Add these cases to the switch in `validateCurrentEditor()`, before the closing `}`:

```javascript
            case 'path-setup':
            case 'extend-path-setup':
            case 'paths-list':
            case 'path':
            case 'routes-list':
            case 'route-editor':
                // These are transient or list panels - always considered valid
                itemExists = true;
                break;
```

Place them after the `case 'schedules-list':` block (after line 308).

**Step 2: Run JavaScript syntax tests**

Run: `source ~/.nvm/nvm.sh && nvm use 22 && conda run -n general pytest tests/test_javascript_syntax.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add static/js/editor.js
git commit -m "fix: prevent validateCurrentEditor from closing path-tool and list panels"
```

---

## Task 3: Run the full test suite

**Files:** None (verification only)

**Step 1: Run all three mandatory test suites**

Run:
```bash
conda run -n general pytest tests/test_javascript_syntax.py tests/test_app_smoke.py tests/test_backend.py -v --tb=short
```
Expected: ALL PASS

**Step 2: If any fail, fix and re-run**

---

## Task 4: Manual verification checklist

This is for the developer to manually verify in the browser. Not automated.

1. Open a block editor panel (click a block in the editor).
2. Activate "Create Path" tool, click a start block. Verify the path-setup panel opens. The block editor should be in history.
3. Click an end block (orange highlight). Path is created.
4. Verify: the block editor panel is restored (not a blank/closed sidebar).
5. Verify: the canvas viewport does NOT jump.
6. Repeat with "Extend Path" tool.
7. Test cancellation (ESC / right-click during path tool) -- panel should also restore.
8. Test with NO previous panel open: activate path tool from fresh state, complete it. Sidebar should close cleanly.

---

## Notes

- The viewport-jump fix in `renderer.js` (adjusting `panX`/`panY` in `resize()`) is separate and already correct. Do not touch it.
- The `_previousEditorType` / `_previousEditorId` properties on the editor object are not declared anywhere -- they were ad-hoc. After this rework they will no longer be referenced anywhere and can be ignored (JS doesn't error on unused properties).
- `goBackSecondaryPanel()` is async (returns a Promise). The current `resetRouteToolState()` is sync. Since `goBackSecondaryPanel` calls `restoreSecondaryHistoryPanel` which calls `restoreSecondaryPanel` which may call async `showBlockEditor()` etc., ideally `resetRouteToolState` should await it. However, since the panel restore is a UI operation and the function ends right after, fire-and-forget is acceptable here. If issues arise, the function can be made async later.
