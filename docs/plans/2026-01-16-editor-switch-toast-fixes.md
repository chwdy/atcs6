# Editor Switch & Toast Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the switch editor subscription crash and incorrect toast argument usage in editor panels.

**Architecture:** Update the switch editor to store and subscribe using the actual switch ID from fetched data. Align toast calls with the `showToast(message, type, duration)` signature so toast display/dismiss behavior is correct.

**Tech Stack:** Vanilla JavaScript (ES6), browser DOM, pytest (syntax/smoke/backend suites).

### Task 1: Reproduce switch subscription bug in test

**Files:**
- Create: `tests/test_editor_switch_subscription.py`

**Step 1: Write the failing test**

```python
import re
from pathlib import Path

def test_switch_editor_subscription_uses_switch_id():
    content = Path("static/js/editor-switches.js").read_text()
    assert "this._switchId = switchData.switch_id" in content
    assert "subscribe(\n            'switches',\n            switchData.switch_id" in content
```

**Step 2: Run test to verify it fails**

Run: `conda run -n general pytest tests/test_editor_switch_subscription.py -v`
Expected: FAIL because the file currently assigns `this._switchId = switchId`.

**Step 3: Write minimal implementation**

```javascript
this._switchId = switchData.switch_id;
this._unsubscribe = window.stateManager.subscribe(
    'switches',
    switchData.switch_id,
    (newSwitch, oldSwitch) => this._onSwitchUpdate(newSwitch, oldSwitch),
    { immediate: false }
);
```

**Step 4: Run test to verify it passes**

Run: `conda run -n general pytest tests/test_editor_switch_subscription.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/test_editor_switch_subscription.py static/js/editor-switches.js
git commit -m "fix: use switch_id in subscription"
```

### Task 2: Reproduce toast signature bug in tests

**Files:**
- Create: `tests/test_editor_toast_signature.py`

**Step 1: Write the failing test**

```python
from pathlib import Path

def test_editor_toast_signature_is_three_args():
    files = [
        "static/js/editor-signals.js",
        "static/js/editor-stations.js",
    ]
    for file_path in files:
        content = Path(file_path).read_text()
        assert "showToast('" in content
        assert "'success', 'error'" not in content
```

**Step 2: Run test to verify it fails**

Run: `conda run -n general pytest tests/test_editor_toast_signature.py -v`
Expected: FAIL because `showToast('...', 'success', 'error', 5000)` exists.

**Step 3: Write minimal implementation**

```javascript
window.showToast('Signal updated successfully', 'success', 5000);
window.showToast('Station properties saved', 'success', 5000);
window.showToast('Station deleted', 'success', 5000);
```

**Step 4: Run test to verify it passes**

Run: `conda run -n general pytest tests/test_editor_toast_signature.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/test_editor_toast_signature.py static/js/editor-signals.js static/js/editor-stations.js
git commit -m "fix: align toast signature in editor panels"
```

### Task 3: Run required validation suite

**Files:**
- Test: `tests/test_javascript_syntax.py`
- Test: `tests/test_app_smoke.py`
- Test: `tests/test_backend.py`

**Step 1: Run JavaScript syntax checks**

Run: `conda run -n general pytest tests/test_javascript_syntax.py -v`
Expected: PASS

**Step 2: Run app smoke tests**

Run: `conda run -n general pytest tests/test_app_smoke.py -v`
Expected: PASS

**Step 3: Run backend tests**

Run: `conda run -n general pytest tests/test_backend.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git status
```
Expected: Clean working tree
