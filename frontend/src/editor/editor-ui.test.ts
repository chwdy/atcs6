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
