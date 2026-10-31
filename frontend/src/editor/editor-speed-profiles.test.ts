// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SpeedProfileEditorHandler, type SpeedProfilePanelHost } from './editor-speed-profiles';

// Where the Speed Profile panel draws: the panel host without a host
// element, or the host's element (the Locomotives screen).

function fakeEditor() {
  return {
    _currentEditorType: null as string | null,
    _currentEditorId: null as string | null,
    showSecondarySidebar: vi.fn(),
    api: {
      getSpeedProfile: vi.fn(async (id: string) => ({
        profile_id: id,
        name: id,
        voltage_power_curve: [
          [1.5, 0],
          [12, 100],
        ],
        power_speed_curve: [
          [0, 0],
          [100, 100],
        ],
      })),
    },
  };
}

function fakeHost(): SpeedProfilePanelHost & { [k: string]: any } {
  return {
    el: document.createElement('div'),
    showList: vi.fn(),
    profileShown: vi.fn(),
    profileDeleted: vi.fn(),
    calibrationChanged: vi.fn(),
  };
}

describe('SpeedProfileEditorHandler host', () => {
  // The editor finishes wiring the panel on a timer (plots, calibration
  // group); these tests look only at where it draws, so the timer never runs.
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('draws in the secondary sidebar when no host is set', async () => {
    const editor = fakeEditor();
    const handler = new SpeedProfileEditorHandler(editor);
    await handler.showProfileEditor('p1');
    expect(editor.showSecondarySidebar).toHaveBeenCalledWith('p1', expect.stringContaining('sp-cal-group'));
    expect(editor._currentEditorType).toBe('speed-profile');
  });

  it('draws into the host instead, and is not one of the sidebar’s editors', async () => {
    const editor = fakeEditor();
    const handler = new SpeedProfileEditorHandler(editor);
    const host = fakeHost();
    handler.setHost(host);
    await handler.showProfileEditor('p1', { trainId: 'T1' });
    expect(editor.showSecondarySidebar).not.toHaveBeenCalled();
    expect(editor._currentEditorType).toBeNull();
    expect(host.el.querySelector('#sp-cal-group')).not.toBeNull();
    expect(host.profileShown).toHaveBeenCalledWith('p1');
  });

  it('sends a request for the profile list to the host', async () => {
    const editor = fakeEditor();
    const handler = new SpeedProfileEditorHandler(editor);
    const host = fakeHost();
    handler.setHost(host);
    await handler.showProfilesPanel();
    expect(host.showList).toHaveBeenCalledTimes(1);
    expect(editor.showSecondarySidebar).not.toHaveBeenCalled();
  });

  it('stays open when the sidebar closes; only close() closes a hosted panel', async () => {
    const editor = fakeEditor();
    const handler = new SpeedProfileEditorHandler(editor);
    const host = fakeHost();
    handler.setHost(host);
    await handler.showProfileEditor('p1');
    handler._cleanup();
    expect(host.el.innerHTML).not.toBe('');
    handler.close();
    expect(host.el.innerHTML).toBe('');
  });
});
