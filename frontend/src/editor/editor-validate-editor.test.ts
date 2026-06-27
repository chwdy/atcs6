import { describe, expect, it, vi } from 'vitest';

import { Editor } from './editor';

// validateCurrentEditor() runs on every refreshNetwork(). It must close the
// sidebar only when the entity being edited was genuinely deleted — never for a
// tile / transient / list panel. Hiding one of those made the inspector vanish
// and the follow-up re-select reopen it, a visible flash (and a docked-canvas
// reflow) every time, e.g. right-clicking to throw a switch. The method only
// reads _currentEditorType / _currentEditorId / cachedNetwork and calls
// hideSecondarySidebar(), so a hand-built `this` exercises it without a DOM.
function run(state: Record<string, any>) {
  const hideSecondarySidebar = vi.fn();
  const ctx: any = { hideSecondarySidebar, cachedNetwork: {}, ...state };
  Editor.prototype.validateCurrentEditor.call(ctx);
  return { hideSecondarySidebar };
}

describe('validateCurrentEditor — does not hide non-entity panels on refresh', () => {
  // The reported bug: throwing a switch flashed the Tile Editor + layout.
  for (const editorType of ['tile', 'multi-select', 'station-group-list', 'tunnel']) {
    it(`keeps the '${editorType}' panel open`, () => {
      const { hideSecondarySidebar } = run({
        _currentEditorType: editorType,
        _currentEditorId: '4,5',
      });
      expect(hideSecondarySidebar).not.toHaveBeenCalled();
    });
  }

  it('keeps an unknown/future panel type open (safe default)', () => {
    const { hideSecondarySidebar } = run({
      _currentEditorType: 'some-future-panel',
      _currentEditorId: 'x',
    });
    expect(hideSecondarySidebar).not.toHaveBeenCalled();
  });
});

describe('validateCurrentEditor — still closes a truly deleted entity editor', () => {
  it('hides a switch editor when its switch is gone from the network', () => {
    const { hideSecondarySidebar } = run({
      _currentEditorType: 'switch',
      _currentEditorId: 'SW_gone',
      cachedNetwork: { switches: { SW_other: {} } },
    });
    expect(hideSecondarySidebar).toHaveBeenCalledTimes(1);
  });

  it('keeps a switch editor open when its switch still exists', () => {
    const { hideSecondarySidebar } = run({
      _currentEditorType: 'switch',
      _currentEditorId: 'SW_live',
      cachedNetwork: { switches: { SW_live: {} } },
    });
    expect(hideSecondarySidebar).not.toHaveBeenCalled();
  });
});
