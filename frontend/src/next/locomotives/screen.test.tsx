// @vitest-environment happy-dom
import { act } from 'preact/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { CalibrationGroupSummary, SpeedProfilePanelHost } from '../../editor/editor-speed-profiles';
import { screen } from '../state';
import { mountLocomotivesScreen, openLocomotiveForTrain } from './screen';
import { calibration, models, profiles, selection, trains, updateFromNetwork } from './state';

const emergencyBrake = vi.fn(async (_trainId: string) => ({}));
const deleteSpeedProfile = vi.fn(async (_id: number) => undefined);
const deleteLocomotiveModel = vi.fn(async (_id: number) => ({}));
const renameLocomotiveModel = vi.fn(async (_id: number, _name: string) => ({}));
const updateSpeedProfile = vi.fn(async (_id: number, _data: unknown) => ({}));
const assignTrainSpeedProfile = vi.fn(async (_train: string, _id: number) => ({}));
const resetTrainSpeedProfile = vi.fn(async (_train: string) => ({}));
const updateTrain = vi.fn(async (_train: string, _data: unknown) => ({}));
const confirmModal = vi.fn(async (_opts: unknown) => true);
vi.mock('../../lib/api', () => ({
  api: {
    emergencyBrake: (id: string) => emergencyBrake(id),
    trainPowerOff: vi.fn(async () => ({})),
    deleteSpeedProfile: (id: number) => deleteSpeedProfile(id),
    deleteLocomotiveModel: (id: number) => deleteLocomotiveModel(id),
    renameLocomotiveModel: (id: number, name: string) => renameLocomotiveModel(id, name),
    updateSpeedProfile: (id: number, data: unknown) => updateSpeedProfile(id, data),
    assignTrainSpeedProfile: (t: string, id: number) => assignTrainSpeedProfile(t, id),
    resetTrainSpeedProfile: (t: string) => resetTrainSpeedProfile(t),
    updateTrain: (t: string, d: unknown) => updateTrain(t, d),
  },
}));
vi.mock('../../lib/toast', () => ({ showToast: vi.fn() }));
vi.mock('../../lib/helpers', () => ({ showConfirmModal: (opts: unknown) => confirmModal(opts) }));

/** Stands in for today's Speed Profile panel: draws a calibration group and
 *  tells its host, as editor-speed-profiles.ts does. */
function fakePanel() {
  let host: SpeedProfilePanelHost | null = null;
  const shows: Array<[number, string | null | undefined]> = [];
  const draw = (profileId: number) => {
    host!.el.innerHTML =
      '<div class="sp-editor"><div class="control-section" id="sp-cal-group">' +
      '<div class="control-section-header">Calibration</div>' +
      '<div class="input-small-row sp-cal-train-row"><select id="sp-cal-train"></select></div>' +
      '<div class="sp-cal-readiness" id="sp-cal-readiness"></div>' +
      '<div id="sp-lighting-row"></div><div id="sp-speed-row"></div><div id="sp-ato-row"></div></div></div>';
    host!.profileShown(profileId);
  };
  return {
    shows,
    host: () => host!,
    setHost(h: SpeedProfilePanelHost | null) {
      host = h;
    },
    async showProfileEditor(profileId: number | string, options: { trainId?: string | null } = {}) {
      shows.push([Number(profileId), options.trainId]);
      draw(Number(profileId));
    },
    close: vi.fn(() => {
      host!.el.innerHTML = '';
    }),
    /** The panel opening a profile on its own (naming a train, or another screen). */
    jumpTo: draw,
  };
}

const network = (overrides: { status?: string; speed?: number } = {}) => ({
  trains: [
    { id: 'T1', car_number: 'H100', speed_profile_id: 11, length_cm: 22 },
    { id: 'T_LOOSE', car_number: '', speed_profile_id: 12 },
  ],
  controllers_by_train_id: {
    T1: { span: {}, train_status: overrides.status ?? 'manual', is_stopped: true, current_speed: overrides.speed ?? 0 },
    T_LOOSE: { span: {}, train_status: 'off', is_stopped: true },
  },
  speed_profiles: {
    '0': { profile_id: 0, model_id: null, set_name: '', parent_profile_id: null, calibrated_at: null },
    '1': { profile_id: 1, model_id: 1, set_name: '1', parent_profile_id: null, calibrated_at: '2026-11-08T03:37:42+00:00' },
    '5': { profile_id: 5, model_id: 2, set_name: 'A', parent_profile_id: null, calibrated_at: null },
    '11': { profile_id: 11, model_id: null, set_name: '', parent_profile_id: 1, calibrated_at: '2026-11-08T03:37:42+00:00' },
    '12': { profile_id: 12, model_id: null, set_name: '', parent_profile_id: 0, calibrated_at: null },
  },
  locomotive_models: [
    { model_id: 2, name: 'Siemens Charger' },
    { model_id: 1, name: 'H100' },
  ],
});

const summary = (over: Partial<CalibrationGroupSummary> = {}): CalibrationGroupSummary => ({
  profileId: 11,
  trainId: 'T1',
  view: 'none',
  ready: true,
  unmet: [],
  passStage: null,
  speedCommitted: false,
  atoCommitted: false,
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
    trains.value = [];
    profiles.value = [];
    models.value = [];
    selection.value = null;
    calibration.value = null;
    screen.value = 'locomotives';
    emergencyBrake.mockClear();
    confirmModal.mockClear();
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

  it('lists the trains, then the Default and each model with its sets, by name', () => {
    const names = [...document.querySelectorAll('.nloco-row-name')].map((n) => n.textContent);
    expect(names).toEqual(['H100', 'T_LOOSE', 'Default', 'H100', 'Set 1', 'Siemens Charger', 'Set A']);
    // A train row says where its profile came from and whether it was measured.
    const t1 = [...document.querySelectorAll('.nloco-row')][0];
    expect(t1.querySelector('.nloco-row-detail')?.textContent).toBe('H100 · 1');
    expect(t1.textContent).toContain('MANUAL');
    expect(t1.textContent).toContain('measured');
    expect(rowNamed('T_LOOSE').querySelector('.nloco-row-detail')?.textContent).toBe('Default');
    // A set row says which trains came from it.
    expect(rowNamed('Set 1').querySelector('.nloco-row-detail')?.textContent).toBe('Used by H100');
    expect(rowNamed('Set A').querySelector('.nloco-row-detail')?.textContent).toBe('Used by no train');
  });

  const trainRow = () => [...document.querySelectorAll<HTMLButtonElement>('.nloco-row')][0];

  it('shows a picked train’s own profile with the train named, and the seven steps', () => {
    act(() => trainRow().click());
    expect(panel.shows).toEqual([[11, 'T1']]);
    expect(document.querySelector('.nloco-main h2')?.textContent).toBe('H100');
    expect(document.querySelector('.nloco-card')!.textContent).toContain('H100 · 1');

    const group = document.querySelector('#sp-cal-group')!;
    const steps = [...group.querySelectorAll('.nloco-step-title')].map((s) => s.textContent);
    expect(steps).toEqual([
      'Name the locomotive',
      'Name the measured block and start the speed pass',
      'Find the lighting voltage',
      'Find the starting voltage',
      'Let the sweep run',
      'Commit the curve',
      'Test and commit the ATO minimum voltage',
    ]);
    expect(group.children[1].querySelector('.nloco-steps')).not.toBeNull();

    act(() => panel.host().calibrationChanged(summary({ view: 'speed-running', passStage: 'sweep' })));
    expect(group.querySelector('[aria-current="step"]')!.textContent).toContain('Let the sweep run');
  });

  it('puts each of the group’s rows under the step it does; the speed row follows the pass', () => {
    act(() => trainRow().click());
    const group = document.querySelector('#sp-cal-group')!;
    const stepOf = (sel: string) => group.querySelector(sel)!.closest('.nloco-step')!.querySelector('.nloco-step-title')!.textContent;
    expect(stepOf('#sp-cal-train')).toBe('Name the locomotive');
    expect(stepOf('#sp-lighting-row')).toBe('Find the lighting voltage');
    expect(stepOf('#sp-speed-row')).toBe('Name the measured block and start the speed pass');
    expect(stepOf('#sp-ato-row')).toBe('Test and commit the ATO minimum voltage');
    act(() => panel.host().calibrationChanged(summary({ view: 'speed-held' })));
    expect(stepOf('#sp-speed-row')).toBe('Commit the curve');
  });

  it('does not reopen the panel when a poll changes only live values', () => {
    act(() => trainRow().click());
    act(() => updateFromNetwork(network({ speed: 12 })));
    act(() => updateFromNetwork(network({ status: 'calibrating', speed: 20 })));
    expect(panel.shows).toHaveLength(1);
  });

  it('asks the panel again when a load failed and left it blank', async () => {
    const draw = panel.showProfileEditor.bind(panel);
    let failNext = true;
    // The real panel shows a toast and draws nothing when the profile does not load.
    panel.showProfileEditor = async (profileId, options = {}) => {
      if (failNext) {
        failNext = false;
        panel.shows.push([Number(profileId), options.trainId]);
        return;
      }
      await draw(profileId, options);
    };
    act(() => trainRow().click());
    await settle();
    expect(document.querySelector('#sp-cal-group')).toBeNull();

    // A poll that changes nothing asks nothing; one that changes the train asks again.
    act(() => updateFromNetwork(network()));
    await settle();
    expect(panel.shows).toHaveLength(1);
    act(() => updateFromNetwork(network({ status: 'emergency' })));
    await settle();
    expect(panel.shows).toEqual([
      [11, 'T1'],
      [11, 'T1'],
    ]);
    expect(document.querySelector('#sp-cal-group')).not.toBeNull();
  });

  it('asks the panel again when the picked row is clicked while the panel is blank', async () => {
    act(() => trainRow().click());
    await settle();
    act(() => panel.close());
    act(() => trainRow().click());
    await settle();
    expect(panel.shows).toHaveLength(2);
    expect(document.querySelector('#sp-cal-group')).not.toBeNull();
  });

  it('shows the canvas only while a run is open, and only on this screen', () => {
    act(() => trainRow().click());
    expect(document.documentElement.dataset.locoCanvas).toBeUndefined();
    act(() => panel.host().calibrationChanged(summary({ view: 'speed-setup' })));
    expect(document.documentElement.dataset.locoCanvas).toBe('shown');
    act(() => {
      screen.value = 'operate';
    });
    expect(document.documentElement.dataset.locoCanvas).toBeUndefined();
  });

  it('lists the refused commands, disabled with the reason, while the train reads CALIBRATING', () => {
    act(() => trainRow().click());
    act(() => panel.host().calibrationChanged(summary({ view: 'speed-running' })));
    act(() => updateFromNetwork(network({ status: 'calibrating' })));
    for (const label of ['Assign a route', 'Manual / ATO', 'Start route', 'Go', 'Manual submode', 'Handle and power']) {
      expect(button(label).disabled).toBe(true);
      expect(button(label).title).toContain('Refused while H100 reads CALIBRATING');
    }
    act(() => button('Emergency brake').click());
    expect(emergencyBrake).toHaveBeenCalledWith('T1');
  });

  it('opens a train from another screen', () => {
    act(() => {
      screen.value = 'operate';
      openLocomotiveForTrain('T1');
    });
    expect(reveal).toHaveBeenCalledTimes(1);
    expect(selection.value).toEqual({ kind: 'train', trainId: 'T1' });
    expect(panel.shows).toEqual([[11, 'T1']]);
  });

  it('follows the panel when it opens a set itself, without reopening it', async () => {
    act(() => trainRow().click());
    await settle();
    act(() => panel.jumpTo(1));
    expect(selection.value).toEqual({ kind: 'set', profileId: 1, fromPanel: true });
    expect(rowNamed('Set 1').classList.contains('row-selected')).toBe(true);
    expect(document.querySelector('.nloco-main h2')?.textContent).toBe('H100 · 1');
    expect(panel.shows).toHaveLength(1);
  });

  it('picks the train when another part of the app opens its own profile', async () => {
    act(() => rowNamed('Set 1').click());
    await settle();
    act(() => panel.jumpTo(11));
    await settle();
    expect(selection.value).toEqual({ kind: 'train', trainId: 'T1' });
    expect(panel.shows[panel.shows.length - 1]).toEqual([11, 'T1']);
  });

  it('shows this screen when the profile list is asked for elsewhere', () => {
    panel.host().showList();
    expect(reveal).toHaveBeenCalledTimes(1);
  });

  it('closes the panel and shows the card when a model is picked', () => {
    act(() => trainRow().click());
    act(() => rowNamed('Siemens Charger').click());
    expect(panel.close).toHaveBeenCalledTimes(1);
    expect(document.querySelector('.nloco-card-kind')?.textContent).toBe('Model');
  });

  it('gives a train a set, and resets it', async () => {
    act(() => trainRow().click());
    const select = document.getElementById('nloco-give-profile') as HTMLSelectElement;
    expect([...select.options].map((o) => o.textContent)).toEqual([
      'Give it another profile…', 'Default', 'H100 · 1', 'Siemens Charger · A',
    ]);
    act(() => {
      select.value = '5';
      select.dispatchEvent(new Event('change'));
    });
    act(() => button('Copy in').click());
    await settle();
    expect(assignTrainSpeedProfile).toHaveBeenCalledWith('T1', 5);
    act(() => button('Reset').click());
    await settle();
    expect(resetTrainSpeedProfile).toHaveBeenCalledWith('T1');
  });

  it('deletes a set after asking, naming the trains that keep their copies', async () => {
    act(() => rowNamed('Set 1').click());
    act(() => button('Delete').click());
    await settle();
    expect(confirmModal).toHaveBeenCalledWith(expect.objectContaining({ detail: expect.stringContaining('H100 keep their own copies') }));
    expect(deleteSpeedProfile).toHaveBeenCalledWith(1);
    expect(selection.value).toBeNull();
  });

  it('renames a model and deletes it with its sets', async () => {
    act(() => rowNamed('Siemens Charger').click());
    const field = document.getElementById('nloco-model-name') as HTMLInputElement;
    act(() => {
      field.value = 'Siemens SC-44';
      field.dispatchEvent(new Event('input'));
    });
    act(() => {
      field.dispatchEvent(new Event('blur'));
    });
    await settle();
    expect(renameLocomotiveModel).toHaveBeenCalledWith(2, 'Siemens SC-44');
    act(() => button('Delete').click());
    await settle();
    expect(deleteLocomotiveModel).toHaveBeenCalledWith(2);
    expect(profiles.value.some((p) => p.profile_id === 5)).toBe(false);
  });
});
