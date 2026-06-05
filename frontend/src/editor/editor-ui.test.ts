import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// editor-ui pulls in toast + helpers for their side-effecting DOM helpers; we
// only care about the track-bit commit logic here, so stub both. constants
// (FORBIDDEN_TRACK_BITS / isSquareCornerSwitch) are pure and used as-is.
vi.mock('../lib/toast', () => ({ showToast: vi.fn() }));
vi.mock('../lib/helpers', () => ({ showConfirmModal: vi.fn() }));

import { EditorUIHandler } from './editor-ui';
import { showToast } from '../lib/toast';

// Build an EditorUIHandler wired to a fake host. `_commitTrackBits` only
// touches host.api, host.selectElement, and window.app.refreshNetwork.
function makeHandler(api: Record<string, any>) {
  const selectElement = vi.fn().mockResolvedValue(undefined);
  const handler = new EditorUIHandler({ api, selectElement } as any);
  return { handler, selectElement };
}

describe('_commitTrackBits — removing the last segment', () => {
  beforeEach(() => {
    (globalThis as any).window = { app: { refreshNetwork: vi.fn().mockResolvedValue(undefined) } };
    vi.mocked(showToast).mockClear();
  });
  afterEach(() => {
    delete (globalThis as any).window;
  });

  it('removes the tile without re-fetching it or surfacing an error', async () => {
    // Server deletes the tile when the last segment goes (bits=0): the PATCH
    // response carries a message and NO `track`. The old code then called
    // getTrack, which 404s and threw the commit into the failure path.
    const api = {
      updateTrack: vi.fn().mockResolvedValue({ status: 'success', message: 'removed (empty)' }),
      // The real api.getTrack throws on 404. If the fix regresses and this is
      // called for a just-removed tile, the reject reproduces the original bug
      // (commit lands in the failure path, error toast, no refresh).
      getTrack: vi.fn().mockRejectedValue(Object.assign(new Error('Track not found'), { status: 404 })),
      createSwitchAt: vi.fn(),
    };
    const { handler, selectElement } = makeHandler(api);

    await handler._commitTrackBits({ x: 3, y: 4 }, 0);

    expect(api.updateTrack).toHaveBeenCalledWith(3, 4, { track_bits: 0 });
    // The whole point of the fix: a removed tile is never re-fetched.
    expect(api.getTrack).not.toHaveBeenCalled();
    // Map refresh + reselect still run; no spurious failure toast.
    expect((globalThis as any).window.app.refreshNetwork).toHaveBeenCalledTimes(1);
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
