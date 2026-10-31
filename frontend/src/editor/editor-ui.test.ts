// @vitest-environment happy-dom
import { describe, it, expect, vi, beforeEach } from 'vitest';

// editor-ui pulls in toast + helpers for their side-effecting DOM helpers; we
// only care about the track-bit commit logic here, so stub both. constants
// (FORBIDDEN_TRACK_BITS / isSquareCornerSwitch) are pure and used as-is.
vi.mock('../lib/toast', () => ({ showToast: vi.fn() }));
vi.mock('../lib/helpers', () => ({ showConfirmModal: vi.fn() }));

import { EditorUIHandler } from './editor-ui';
import { showToast } from '../lib/toast';

// Build an EditorUIHandler wired to a fake editor. `_commitTrackBits` only
// touches editor.api, editor.selectElement, and editor.host.refreshNetwork
// (the injected host — P3 replaced the window.app global with editor.host).
function makeHandler(api: Record<string, any>) {
  const selectElement = vi.fn().mockResolvedValue(undefined);
  const refreshNetwork = vi.fn().mockResolvedValue(undefined);
  const handler = new EditorUIHandler({ api, selectElement, host: { refreshNetwork } } as any);
  return { handler, selectElement, refreshNetwork };
}

describe('_commitTrackBits — removing the last segment', () => {
  beforeEach(() => {
    vi.mocked(showToast).mockClear();
  });

  it('removes the tile without re-fetching it or surfacing an error', async () => {
    // Server deletes the tile when the last segment goes (bits=0): the PATCH
    // response carries a message and NO `track`. There is nothing left to
    // re-fetch, so the commit must skip getTrack entirely.
    const api = {
      updateTrack: vi.fn().mockResolvedValue({ status: 'success', message: 'removed (empty)' }),
      // The real api.getTrack resolves null on 404 (expected miss).
      getTrack: vi.fn().mockResolvedValue(null),
      createSwitchAt: vi.fn(),
    };
    const { handler, selectElement, refreshNetwork } = makeHandler(api);

    await handler._commitTrackBits({ x: 3, y: 4 }, 0);

    expect(api.updateTrack).toHaveBeenCalledWith(3, 4, { track_bits: 0 });
    // The whole point of the fix: a removed tile is never re-fetched.
    expect(api.getTrack).not.toHaveBeenCalled();
    // Map refresh + reselect still run; no spurious failure toast.
    expect(refreshNetwork).toHaveBeenCalledTimes(1);
    expect(selectElement).toHaveBeenLastCalledWith({ x: 3, y: 4 });
    expect(showToast).not.toHaveBeenCalled();
  });

  it('still re-fetches the tile on a normal (non-removing) bit change', async () => {
    const api = {
      updateTrack: vi
        .fn()
        .mockResolvedValue({ status: 'success', track: { x: 3, y: 4, track_bits: 1, is_switch: false } }),
      getTrack: vi.fn().mockResolvedValue({ x: 3, y: 4, is_switch: false }),
      createSwitchAt: vi.fn(),
    };
    const { handler } = makeHandler(api);

    await handler._commitTrackBits({ x: 3, y: 4 }, 1);

    expect(api.updateTrack).toHaveBeenCalledWith(3, 4, { track_bits: 1 });
    expect(api.getTrack).toHaveBeenCalledTimes(1);
    expect(showToast).not.toHaveBeenCalled();
  });

  it('rejects a forbidden configuration before touching the server', async () => {
    const api = { updateTrack: vi.fn(), getTrack: vi.fn(), createSwitchAt: vi.fn() };
    const { handler, selectElement } = makeHandler(api);

    await handler._commitTrackBits({ x: 3, y: 4 }, 63); // 63 = full 6-path, forbidden

    expect(api.updateTrack).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('forbidden'), 'error', expect.any(Number));
    expect(selectElement).toHaveBeenCalledWith({ x: 3, y: 4 }); // resync revert
  });
});

// The one panel: today's panels draw into the three nodes index.html keeps
// for them (title, back link, body), and `visible` on #secondary-sidebar says
// a panel is open — the Editor screen's right panel watches that class.
describe('the panel', () => {
  function makePanel() {
    const root = document.createElement('div');
    root.innerHTML =
      '<div id="secondary-sidebar" hidden>' +
      '<button id="secondary-nav-parent" hidden></button>' +
      '<h3 id="secondary-sidebar-title"></h3>' +
      '<div id="secondary-sidebar-content"></div></div>';
    const editor: any = {
      $id: (id: string) => root.querySelector(`#${id}`),
      renderer: { clearBlockDirectionOverlay: vi.fn() },
      _currentEditorType: 'switch',
      _currentEditorId: 'SW_1',
      switchHandler: { _cleanup: vi.fn() },
      getPanelLineage: (panel: any) =>
        panel.editorType === 'switch'
          ? [{ label: 'Switches', editorType: 'switches-list' }, { label: panel.editorId }]
          : [{ label: 'Switches' }],
      restoreSecondaryPanel: vi.fn().mockResolvedValue(true),
    };
    const handler = new EditorUIHandler(editor);
    handler.setupSecondarySidebar();
    const el = (id: string) => root.querySelector(`#${id}`) as HTMLElement;
    return { handler, editor, el };
  }

  it('shows a panel: title, body, the open mark, and the back link to its list', () => {
    const { handler, el } = makePanel();
    handler.showSecondarySidebar('Switch SW_1', '<p id="body">x</p>');
    expect(el('secondary-sidebar-title').textContent).toBe('Switch SW_1');
    expect(el('secondary-sidebar-content').querySelector('#body')).not.toBeNull();
    expect(el('secondary-sidebar').classList.contains('visible')).toBe(true);
    expect(el('secondary-nav-parent').hidden).toBe(false);
    expect(el('secondary-nav-parent').textContent).toBe('← Switches');
  });

  it('the back link opens the parent list', async () => {
    const { handler, editor, el } = makePanel();
    handler.showSecondarySidebar('Switch SW_1', '<p></p>');
    el('secondary-nav-parent').click();
    await Promise.resolve();
    expect(editor.switchHandler._cleanup).toHaveBeenCalled();
    expect(editor.restoreSecondaryPanel).toHaveBeenCalledWith(
      expect.objectContaining({ editorType: 'switches-list' }),
    );
  });

  it('hiding clears the open mark, the back link and the editor type', async () => {
    const { handler, editor, el } = makePanel();
    handler.showSecondarySidebar('Switch SW_1', '<p></p>');
    await handler.hideSecondarySidebar();
    expect(el('secondary-sidebar').classList.contains('visible')).toBe(false);
    expect(el('secondary-nav-parent').hidden).toBe(true);
    expect(editor._currentEditorType).toBeNull();
    expect(editor.switchHandler._cleanup).toHaveBeenCalled();
  });

  it('closes for a missing entity only when that entity is the one showing', async () => {
    const { handler, el } = makePanel();
    handler.showSecondarySidebar('Switch SW_1', '<p></p>');
    await handler.closeStaleSecondaryTab('switch', 'SW_2');
    expect(el('secondary-sidebar').classList.contains('visible')).toBe(true);
    await handler.closeStaleSecondaryTab('switch', 'SW_1');
    expect(el('secondary-sidebar').classList.contains('visible')).toBe(false);
  });

  it('a rename updates the title only for the entity showing', () => {
    const { handler, el } = makePanel();
    handler.showSecondarySidebar('Switch SW_1', '<p></p>');
    handler.updatePanelTitle('switch', 'SW_2', 'Other');
    expect(el('secondary-sidebar-title').textContent).toBe('Switch SW_1');
    handler.updatePanelTitle('switch', 'SW_1', 'Yard throat');
    expect(el('secondary-sidebar-title').textContent).toBe('Yard throat');
  });
});
