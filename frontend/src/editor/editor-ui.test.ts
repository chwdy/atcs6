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

// Regression: closing the home inspector while it is FLOATING must actually
// hide the window. Its visibility in docked mode is the `.visible` class, but a
// floating window is shown by the `.floating-window` class (position: fixed;
// display: flex) which `.visible` does not gate — so hide must go through
// FloatingWindow.setHidden (the `.floating-window--hidden` lever), and reopen
// must clear it. The window-management refactor tested TabGroup.closeTab and
// FloatingWindow.setHidden in isolation but never this empty→hide wiring for
// the one dockable window, so a floating inspector's close/× left the frame on
// screen.
describe('home inspector hide/show routes through FloatingWindow.setHidden', () => {
  // Minimal stand-ins for the two collaborators setupSecondarySidebar would
  // build — enough surface for hide/showSecondarySidebar to run without a full
  // DOM. The real classes are covered by window-manager / tab-group tests.
  function makeUiHandler() {
    const homeWindow = {
      setHidden: vi.fn(),
      isHidden: vi.fn(() => false),
      isFloating: vi.fn(() => true),
      getGeometry: vi.fn(() => ({ left: null, top: null, width: null, height: null })),
    };
    const homeTabGroup = {
      cleanupPanelSubscriptions: vi.fn(),
      clear: vi.fn(),
      show: vi.fn(),
      serialize: vi.fn(() => ({ pinned: false, activeTabId: null, tabs: [] })),
      // Unpinned, so it is the floor of the active chain — see
      // EditorUIHandler.resolveTargetGroup.
      isPinned: vi.fn(() => false),
    };
    const editor = {
      $id: vi.fn(() => null), // no DOM: the class-list block is skipped
      renderer: null,
      _currentEditorType: 'tile',
      _currentEditorId: '0,0',
      _activeTabGroupForRestore: null,
    };
    const handler = new EditorUIHandler(editor as any);
    handler.homeWindow = homeWindow as any;
    handler.homeTabGroup = homeTabGroup as any;
    return { handler, homeWindow, homeTabGroup, editor };
  }

  it('hideSecondarySidebar hides the floating window (setHidden(true))', async () => {
    const { handler, homeWindow } = makeUiHandler();
    await handler.hideSecondarySidebar();
    expect(homeWindow.setHidden).toHaveBeenCalledWith(true);
  });

  it('showSecondarySidebar reveals a previously-hidden home window', () => {
    const { handler, homeWindow, homeTabGroup } = makeUiHandler();
    homeWindow.isHidden.mockReturnValue(true); // it was closed
    handler.showSecondarySidebar('Tile Editor', '<div></div>');
    expect(homeWindow.setHidden).toHaveBeenCalledWith(false);
    expect(homeTabGroup.show).toHaveBeenCalledWith('Tile Editor', '<div></div>');
  });

  it('showSecondarySidebar does not re-focus an already-visible window', () => {
    const { handler, homeWindow, homeTabGroup } = makeUiHandler();
    homeWindow.isHidden.mockReturnValue(false); // already open
    handler.showSecondarySidebar('Tile Editor', '<div></div>');
    expect(homeWindow.setHidden).not.toHaveBeenCalled();
    expect(homeTabGroup.show).toHaveBeenCalled();
  });
});
