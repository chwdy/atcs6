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
        derived_from: null,
        own_fields: [],
        max_voltage: 12,
        lighting_voltage: 1.5,
        voltage_speed_curve: [
          [2, 10],
          [6, 40],
          [12, 100],
        ],
      })),
      getSpeedProfiles: vi.fn(async () => []),
      updateSpeedProfile: vi.fn(async () => ({})),
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

describe('SpeedProfileEditorHandler curve and lighting voltage', () => {
  // These tests let the panel finish wiring itself (one tick of the timer)
  // and then work its fields. The test page has no canvas drawing, and the
  // plot is never laid out, so it never draws: an empty context is enough.
  beforeEach(() => {
    vi.useFakeTimers();
    (HTMLCanvasElement.prototype as any).getContext = () => ({});
  });
  afterEach(() => {
    delete (HTMLCanvasElement.prototype as any).getContext;
    vi.useRealTimers();
    document.body.innerHTML = '';
  });

  async function openPanel() {
    const editor = fakeEditor();
    const handler = new SpeedProfileEditorHandler(editor);
    const host = fakeHost();
    document.body.appendChild(host.el);
    handler.setHost(host);
    await handler.showProfileEditor('p1');
    await vi.advanceTimersByTimeAsync(1);
    const $ = <T extends HTMLElement>(sel: string) => host.el.querySelector(sel) as T;
    return { editor, handler, host, $ };
  }

  /** Type into a field and leave it, as the operator does. */
  function typeInto(el: HTMLInputElement, value: string, event: 'change' | 'input') {
    el.value = value;
    el.dispatchEvent(new Event(event));
  }

  it('draws one voltage → speed chart, with no lighting run controls', async () => {
    const { $, host } = await openPanel();
    expect($('#sp-curve-canvas')).not.toBeNull();
    expect($('#sp-vp-canvas')).toBeNull();
    expect($('#sp-ps-canvas')).toBeNull();
    expect($('#sp-lighting-calibrate')).toBeNull();
    expect(host.el.querySelector('.sp-lighting-stepper')).toBeNull();
    expect(host.el.textContent).toContain('Voltage → speed curve');
  });

  it('shows the starting voltage, starting speed and top speed read off the curve', async () => {
    const { $ } = await openPanel();
    expect($('#sp-start-v').textContent).toBe('2');
    expect($('#sp-start-s').textContent).toBe('10');
    expect($('#sp-top-s').textContent).toBe('100');
    // The point list: the first point's voltage is editable (the starting
    // voltage); the last point's voltage is the max voltage, set in its own field.
    const xs = Array.from(document.querySelectorAll<HTMLInputElement>('#sp-curve-list .sp-px'));
    expect(xs.map((x) => x.value)).toEqual(['2', '6', '12']);
    expect(xs.map((x) => x.disabled)).toEqual([false, false, true]);
  });

  it('shows the lighting voltage in a plain field, with the hint that it stays below the starting voltage', async () => {
    const { $ } = await openPanel();
    expect($<HTMLInputElement>('#sp-lighting').value).toBe('1.5');
    expect($('#sp-lighting-hint').textContent).toContain('below the starting voltage (2 V)');
  });

  it('saves a changed lighting voltage, and only that field', async () => {
    const { editor, $ } = await openPanel();
    typeInto($<HTMLInputElement>('#sp-lighting'), '1.2', 'change');
    await vi.advanceTimersByTimeAsync(1);
    expect(editor.api.updateSpeedProfile).toHaveBeenCalledWith('p1', { lighting_voltage: 1.2 });
  });

  it('saves an empty lighting voltage as none', async () => {
    const { editor, $ } = await openPanel();
    typeInto($<HTMLInputElement>('#sp-lighting'), '', 'change');
    await vi.advanceTimersByTimeAsync(1);
    expect(editor.api.updateSpeedProfile).toHaveBeenCalledWith('p1', { lighting_voltage: null });
  });

  it('does not send a lighting voltage at or above the starting voltage, and says why', async () => {
    const { editor, $ } = await openPanel();
    typeInto($<HTMLInputElement>('#sp-lighting'), '2', 'change');
    await vi.advanceTimersByTimeAsync(1);
    expect(editor.api.updateSpeedProfile).not.toHaveBeenCalled();
    expect($('#sp-lighting-hint').textContent).toContain('Not saved');
  });

  it('saves a typed curve point as the curve, and only that field', async () => {
    const { editor } = await openPanel();
    const speed = document.querySelector<HTMLInputElement>('#sp-curve-list .sp-py[data-i="1"]')!;
    typeInto(speed, '45', 'input');
    await vi.advanceTimersByTimeAsync(1);
    expect(editor.api.updateSpeedProfile).toHaveBeenCalledWith('p1', {
      voltage_speed_curve: [
        [2, 10],
        [6, 45],
        [12, 100],
      ],
    });
  });

  it('moves the last point with the max voltage, and sends both together', async () => {
    const { editor, $ } = await openPanel();
    typeInto($<HTMLInputElement>('#sp-maxv'), '14', 'input');
    await vi.advanceTimersByTimeAsync(300);
    expect(editor.api.updateSpeedProfile).toHaveBeenCalledWith('p1', {
      max_voltage: 14,
      voltage_speed_curve: [
        [2, 10],
        [6, 40],
        [14, 100],
      ],
    });
  });

  it('converts voltage to speed, and a power percent to speed and voltage', async () => {
    const { $ } = await openPanel();
    typeInto($<HTMLInputElement>('#cv-vs-v'), '4', 'input');
    expect($<HTMLInputElement>('#cv-vs-s').value).toBe('25');
    typeInto($<HTMLInputElement>('#cv-vs-s'), '0', 'input');
    expect($<HTMLInputElement>('#cv-vs-v').value).toBe('1.5'); // 0 km/h is the lighting voltage
    typeInto($<HTMLInputElement>('#cv-ps-p'), '50', 'input');
    expect($<HTMLInputElement>('#cv-ps-s').value).toBe('55'); // halfway from 10 to 100 km/h
    expect($('#cv-ps-v').textContent).toBe('50% drives 7.5 V on the rails.');
    typeInto($<HTMLInputElement>('#cv-ps-s'), '100', 'input');
    expect($<HTMLInputElement>('#cv-ps-p').value).toBe('100');
  });
});
