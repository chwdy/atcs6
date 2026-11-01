// @vitest-environment happy-dom
import { act } from 'preact/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { CalibrationGroupSummary, SpeedProfilePanelHost } from '../../editor/editor-speed-profiles';
import { screen } from '../state';
import { mountLocomotivesScreen, openLocomotiveForTrain } from './screen';
import { calibration, profiles, roster, selection, trains, updateFromNetwork } from './state';

const request = vi.fn();
const emergencyBrake = vi.fn(async (_trainId: string) => ({}));
vi.mock('../../lib/api', () => ({
  api: {
    _request: (...args: unknown[]) => request(...args),
    emergencyBrake: (id: string) => emergencyBrake(id),
    trainPowerOff: vi.fn(async () => ({})),
  },
}));
vi.mock('../../lib/toast', () => ({ showToast: vi.fn() }));

/** Stands in for today's Speed Profile panel: draws a calibration group and
 *  tells its host, as editor-speed-profiles.ts does. */
function fakePanel() {
  let host: SpeedProfilePanelHost | null = null;
  const shows: Array<[string, string | null | undefined]> = [];
  const draw = (profileId: string) => {
    host!.el.innerHTML =
      '<div class="sp-editor"><div class="control-section" id="sp-cal-group">' +
      '<div class="control-section-header">Calibration</div>' +
      '<div class="input-small-row sp-cal-train-row"><select id="sp-cal-train"></select></div>' +
      '<div class="sp-cal-readiness" id="sp-cal-readiness"></div>' +
      '<div id="sp-lighting-row"></div><div id="sp-speed-row"></div></div></div>';
    host!.profileShown(profileId);
  };
  return {
    shows,
    host: () => host!,
    setHost(h: SpeedProfilePanelHost | null) {
      host = h;
    },
    async showProfileEditor(profileId: string, options: { trainId?: string | null } = {}) {
      shows.push([profileId, options.trainId]);
      draw(profileId);
    },
    close: vi.fn(() => {
      host!.el.innerHTML = '';
    }),
    /** The panel opening a profile on its own (Edit model →, Promote to model). */
    jumpTo: draw,
  };
}

const network = (overrides: { status?: string; speed?: number } = {}) => ({
  train_roster: [
    { entry_id: 'R1', name: 'Class 47 #2', model: 'Bachmann Class 47', consist_length_cm: 22, max_slope: 0, speed_profile_id: 'inst_R1' },
    { entry_id: 'R2', name: 'Shinkansen N700', model: 'Kato N700', max_slope: 0, speed_profile_id: null },
  ],
  trains: [
    { id: 'T1', car_number: '', speed_profile_id: 'inst_R1', roster_entry_id: 'R1' },
    { id: 'T_LOOSE', car_number: '', speed_profile_id: 'loose_inst', roster_entry_id: null },
  ],
  controllers_by_train_id: {
    T1: { span: {}, train_status: overrides.status ?? 'manual', is_stopped: true, current_speed: overrides.speed ?? 0 },
    T_LOOSE: { span: {}, train_status: 'off', is_stopped: true },
  },
  speed_profiles: {
    class47: { profile_id: 'class47', name: 'Class 47', model: 'Bachmann Class 47', derived_from: null, calibrated_at: '2026-10-01T10:00:00+00:00' },
    inst_R1: { profile_id: 'inst_R1', name: 'Class 47 #2', model: '', derived_from: 'class47', calibrated_at: null },
  },
});

const summary = (over: Partial<CalibrationGroupSummary> = {}): CalibrationGroupSummary => ({
  profileId: 'inst_R1',
  trainId: 'T1',
  view: 'none',
  ready: true,
  unmet: [],
  lightingSaved: false,
  speedCommitted: false,
  ...over,
});

const button = (text: string): HTMLButtonElement => {
  const found = [...document.querySelectorAll('button')].find((b) => b.textContent?.trim() === text);
  if (!found) throw new Error(`no button "${text}"`);
  return found;
};

/** Let pending promises settle (a show the screen asked for, a form submit). */
const settle = () => act(async () => {
  await new Promise((r) => setTimeout(r, 0));
});

const rowNamed = (text: string): HTMLButtonElement => {
  const found = [...document.querySelectorAll<HTMLButtonElement>('.nloco-row')].find((b) =>
    b.querySelector('.nloco-row-name')?.textContent?.trim() === text
  );
  if (!found) throw new Error(`no row "${text}"`);
  return found;
};

describe('Locomotives screen', () => {
  let panel: ReturnType<typeof fakePanel>;
  let reveal: ReturnType<typeof vi.fn>;
  let unmount: () => void;

  beforeEach(() => {
    document.body.innerHTML = '<div class="workspace"><div class="canvas-container"></div></div>';
    roster.value = [];
    trains.value = [];
    profiles.value = [];
    selection.value = null;
    calibration.value = null;
    screen.value = 'locomotives';
    request.mockReset();
    emergencyBrake.mockClear();
    panel = fakePanel();
    reveal = vi.fn();
    act(() => {
      unmount = mountLocomotivesScreen({ editor: { speedProfileHandler: panel } }, reveal);
      updateFromNetwork(network());
    });
  });

  afterEach(() => {
    act(() => unmount());
  });

  it('lists the roster, placed trains not in it, and the model profiles', () => {
    const names = [...document.querySelectorAll('.nloco-row-name')].map((n) => n.textContent);
    expect(names).toEqual(['Class 47 #2', 'Shinkansen N700', 'T_LOOSE', 'default', 'class47']);
    expect(rowNamed('Class 47 #2').textContent).toContain('T1 · MANUAL');
    expect(rowNamed('Shinkansen N700').textContent).toContain('not placed');
  });

  it('shows a picked locomotive’s profile with its placed train named, and the five steps', () => {
    act(() => rowNamed('Class 47 #2').click());
    expect(panel.shows).toEqual([['inst_R1', 'T1']]);
    expect(document.querySelector('.nloco-main')!.textContent).toContain('Speed Profile panel · inst_R1');

    const group = document.querySelector('#sp-cal-group')!;
    const steps = [...group.querySelectorAll('.nloco-step-title')].map((s) => s.textContent);
    expect(steps).toEqual([
      'Name the locomotive',
      'Find the lighting voltage',
      'Name the measured block and start the speed pass',
      'Let the pass run',
      'Commit the result',
    ]);
    // The steps sit right under the group's heading.
    expect(group.children[1].querySelector('.nloco-steps')).not.toBeNull();

    act(() => panel.host().calibrationChanged(summary({ view: 'speed-running' })));
    expect(group.querySelector('[aria-current="step"]')!.textContent).toContain('Let the pass run');
  });

  it('puts each of the group’s rows under the step it does; the speed row follows the pass', () => {
    act(() => rowNamed('Class 47 #2').click());
    const group = document.querySelector('#sp-cal-group')!;
    const stepOf = (sel: string) => group.querySelector(sel)!.closest('.nloco-step')!.querySelector('.nloco-step-title')!.textContent;
    expect(stepOf('#sp-cal-train')).toBe('Name the locomotive');
    expect(stepOf('#sp-cal-readiness')).toBe('Name the locomotive');
    expect(stepOf('#sp-lighting-row')).toBe('Find the lighting voltage');
    expect(stepOf('#sp-speed-row')).toBe('Name the measured block and start the speed pass');

    act(() => panel.host().calibrationChanged(summary({ view: 'speed-running' })));
    expect(stepOf('#sp-speed-row')).toBe('Let the pass run');
    act(() => panel.host().calibrationChanged(summary({ view: 'speed-held' })));
    expect(stepOf('#sp-speed-row')).toBe('Commit the result');
    act(() => panel.host().calibrationChanged(summary({ view: 'speed-setup' })));
    expect(stepOf('#sp-speed-row')).toBe('Name the measured block and start the speed pass');
  });

  it('shows the current step’s hint only while no run is open', () => {
    act(() => rowNamed('Class 47 #2').click());
    act(() => panel.host().calibrationChanged(summary()));
    expect(document.querySelector('[aria-current="step"] .nloco-step-hint')).not.toBeNull();
    act(() => panel.host().calibrationChanged(summary({ view: 'lighting' })));
    expect(document.querySelector('[aria-current="step"] .nloco-step-hint')).toBeNull();
  });

  it('does not reopen the panel when a poll changes only live values', () => {
    act(() => rowNamed('Class 47 #2').click());
    act(() => updateFromNetwork(network({ speed: 12 })));
    act(() => updateFromNetwork(network({ status: 'calibrating', speed: 20 })));
    expect(panel.shows).toHaveLength(1);
  });

  it('shows the canvas only while a run is open, and only on this screen', () => {
    act(() => rowNamed('Class 47 #2').click());
    expect(document.documentElement.dataset.locoCanvas).toBeUndefined();
    act(() => panel.host().calibrationChanged(summary({ view: 'speed-setup' })));
    expect(document.documentElement.dataset.locoCanvas).toBe('shown');
    act(() => {
      screen.value = 'operate';
    });
    expect(document.documentElement.dataset.locoCanvas).toBeUndefined();
  });

  it('lists the refused commands, disabled with the reason, while the train reads CALIBRATING', () => {
    act(() => rowNamed('Class 47 #2').click());
    act(() => panel.host().calibrationChanged(summary({ view: 'lighting' })));
    expect(document.querySelector('.nloco-refused')).toBeNull();

    act(() => updateFromNetwork(network({ status: 'calibrating' })));
    const refused = document.querySelector('.nloco-refused')!;
    expect(refused).not.toBeNull();
    for (const label of ['Assign a route', 'Manual / ATO', 'Start route', 'Go', 'Manual submode', 'Handle and power']) {
      expect(button(label).disabled).toBe(true);
      expect(button(label).title).toContain('Refused while T1 reads CALIBRATING');
    }
    expect(button('Emergency brake').disabled).toBe(false);
    act(() => button('Emergency brake').click());
    expect(emergencyBrake).toHaveBeenCalledWith('T1');
  });

  it('opens a train’s locomotive from another screen', () => {
    act(() => {
      screen.value = 'operate';
      openLocomotiveForTrain('T1');
    });
    expect(reveal).toHaveBeenCalledTimes(1);
    expect(selection.value).toEqual({ kind: 'roster', entryId: 'R1', trainId: 'T1' });
    expect(panel.shows).toEqual([['inst_R1', 'T1']]);

    act(() => openLocomotiveForTrain('T_LOOSE'));
    expect(panel.shows[panel.shows.length - 1]).toEqual(['loose_inst', 'T_LOOSE']);
  });

  it('follows the panel when it opens another profile itself, without reopening it', async () => {
    act(() => rowNamed('Class 47 #2').click());
    await settle();
    act(() => panel.jumpTo('class47'));
    expect(selection.value).toEqual({ kind: 'profile', profileId: 'class47', fromPanel: true });
    expect(rowNamed('class47').classList.contains('row-selected')).toBe(true);
    expect(panel.shows).toHaveLength(1);
    expect(reveal).toHaveBeenCalledTimes(1);
  });

  it('picks the locomotive when another part of the app opens its own profile', async () => {
    act(() => rowNamed('class47').click());
    await settle();
    act(() => panel.jumpTo('inst_R1'));
    await settle();
    expect(selection.value).toEqual({ kind: 'roster', entryId: 'R1' });
    expect(rowNamed('Class 47 #2').classList.contains('row-selected')).toBe(true);
    // Shown again so the locomotive row names the train placed from it.
    expect(panel.shows[panel.shows.length - 1]).toEqual(['inst_R1', 'T1']);
    expect(reveal).toHaveBeenCalledTimes(1);
  });

  it('shows this screen when the profile list is asked for elsewhere', () => {
    panel.host().showList();
    expect(reveal).toHaveBeenCalledTimes(1);
  });

  it('closes the panel when the pick has no profile', () => {
    act(() => rowNamed('Class 47 #2').click());
    act(() => rowNamed('Shinkansen N700').click());
    expect(panel.close).toHaveBeenCalledTimes(1);
    expect(document.querySelector('.nloco-main')!.textContent).toContain('no speed profile yet');
  });

  it('adds a locomotive and picks it', async () => {
    request.mockResolvedValue({
      status: 'success',
      entry: { entry_id: 'ROSTER-NEW', name: 'Class 66', model: '', max_slope: 0, speed_profile_id: null },
    });
    act(() => button('Add locomotive').click());
    const name = document.getElementById('nloco-name') as HTMLInputElement;
    act(() => {
      name.value = 'Class 66';
      name.dispatchEvent(new Event('input', { bubbles: true }));
    });
    act(() => (document.querySelector('.nloco-form') as HTMLFormElement).requestSubmit());
    await settle();
    expect(request).toHaveBeenCalledWith(
      '/roster',
      expect.objectContaining({ method: 'POST', body: expect.objectContaining({ name: 'Class 66', purchase_price: null }) }),
      'saveRosterEntry'
    );
    expect(selection.value).toEqual({ kind: 'roster', entryId: 'ROSTER-NEW' });
    expect(rowNamed('Class 66').classList.contains('row-selected')).toBe(true);
  });
});
