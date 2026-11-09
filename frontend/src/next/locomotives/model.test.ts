import { describe, expect, it } from 'vitest';

import type { CalibrationGroupSummary } from '../../editor/editor-speed-profiles';
import {
  calibrationRunShown,
  calibrationSteps,
  measuredAt,
  modelsFromNetwork,
  panelTarget,
  placedTrains,
  profilesFromNetwork,
  selectionForProfile,
  selectionForTrain,
  setsOf,
  trainSource,
  trainsFrom,
  trainsFromNetwork,
  type ProfileSummary,
  type TrainSummary,
} from './model';

const train = (over: Partial<TrainSummary> = {}): TrainSummary => ({
  id: 'T1',
  name: 'T1',
  carNumber: '',
  lengthCm: 22,
  speedProfileId: 11,
  placed: true,
  status: 'MANUAL',
  stopped: true,
  ...over,
});

const profiles: ProfileSummary[] = [
  { profile_id: 0, model_id: null, set_name: '', parent_profile_id: null, calibrated_at: null },
  { profile_id: 1, model_id: 1, set_name: '2', parent_profile_id: null, calibrated_at: '2026-11-08T03:37:42+00:00' },
  { profile_id: 3, model_id: 1, set_name: '1', parent_profile_id: null, calibrated_at: null },
  { profile_id: 11, model_id: null, set_name: '', parent_profile_id: 1, calibrated_at: '2026-11-08T03:37:42+00:00' },
  { profile_id: 12, model_id: null, set_name: '', parent_profile_id: 77, calibrated_at: null },
];
const models = [{ model_id: 1, name: 'H100' }];

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

describe('reading the network snapshot', () => {
  it('reads trains, profiles and models, with numeric ids', () => {
    const network = {
      trains: [
        { id: 'T1', car_number: '', speed_profile_id: 11 },
        { id: 'T2', car_number: '47 102', speed_profile_id: 12 },
      ],
      controllers_by_train_id: { T1: { span: { x: 1 }, train_status: 'calibrating', is_stopped: false }, T2: { span: null } },
      speed_profiles: { '1': { profile_id: 1, model_id: 1, set_name: '1', parent_profile_id: null } },
      locomotive_models: [{ model_id: 1, name: 'H100' }],
    };
    const [t1, t2] = trainsFromNetwork(network);
    expect(t1).toMatchObject({ name: 'T1', placed: true, status: 'CALIBRATING', stopped: false, speedProfileId: 11 });
    expect(t2).toMatchObject({ name: '47 102', placed: false, status: 'OFF' });
    expect(profilesFromNetwork(network)[0]).toMatchObject({ profile_id: 1, model_id: 1, set_name: '1' });
    expect(modelsFromNetwork(network)).toEqual([{ model_id: 1, name: 'H100' }]);
  });
});

describe('the left list', () => {
  it('lists only placed trains, by name', () => {
    const trains = [train({ id: 'B', name: 'B' }), train({ id: 'A', name: 'A' }), train({ id: 'C', name: 'C', placed: false })];
    expect(placedTrains(trains).map((t) => t.id)).toEqual(['A', 'B']);
  });

  it('lists a model’s sets by set name', () => {
    expect(setsOf(1, profiles).map((p) => p.set_name)).toEqual(['1', '2']);
  });

  it('names where a train’s profile came from, by model and set', () => {
    expect(trainSource(train(), profiles, models)).toBe('H100 · 2');
    expect(trainSource(train({ speedProfileId: 12 }), profiles, models)).toBe('Set deleted');
  });

  it('dates a profile by its own curve', () => {
    expect(measuredAt(11, profiles)).toBe('2026-11-08T03:37:42+00:00');
    expect(measuredAt(3, profiles)).toBeNull();
  });

  it('names the trains copied from a set', () => {
    expect(trainsFrom(1, [train({ name: 'H100' }), train({ id: 'T2', speedProfileId: 12 })], profiles).map((t) => t.name)).toEqual(['H100']);
  });
});

describe('panelTarget', () => {
  it('shows a train’s own profile with the train named, and a set as itself', () => {
    expect(panelTarget({ kind: 'train', trainId: 'T1' }, [train()])).toEqual({ profileId: 11, trainId: 'T1' });
    expect(panelTarget({ kind: 'set', profileId: 3 }, [train()])).toEqual({ profileId: 3, trainId: null });
  });

  it('shows no panel for a model or the Default profile', () => {
    expect(panelTarget({ kind: 'model', modelId: 1 }, [train()])).toEqual({ profileId: null, trainId: null });
    expect(panelTarget({ kind: 'default' }, [train()])).toEqual({ profileId: null, trainId: null });
  });
});

describe('selectionForProfile', () => {
  it('picks the train a profile belongs to, a set as itself, and the Default', () => {
    expect(selectionForProfile(11, [train()], profiles)).toEqual({ kind: 'train', trainId: 'T1' });
    expect(selectionForProfile(3, [train()], profiles)).toEqual({ kind: 'set', profileId: 3, fromPanel: true });
    expect(selectionForProfile(0, [train()], profiles)).toEqual({ kind: 'default' });
    expect(selectionForTrain('T1')).toEqual({ kind: 'train', trainId: 'T1' });
  });
});

describe('calibrationSteps', () => {
  const T = 'todo', C = 'current', D = 'done';

  it('starts on naming the locomotive', () => {
    expect(calibrationSteps(null)).toEqual([C, T, T, T, T, T, T]);
    expect(calibrationSteps(summary({ trainId: null }))).toEqual([C, T, T, T, T, T, T]);
  });

  it('marks the step the open run belongs to', () => {
    expect(calibrationSteps(summary())).toEqual([D, C, T, T, T, T, T]);
    expect(calibrationSteps(summary({ view: 'speed-setup' }))).toEqual([D, C, T, T, T, T, T]);
    expect(calibrationSteps(summary({ view: 'speed-running', passStage: 'lighting_trial' }))).toEqual([D, D, C, T, T, T, T]);
    expect(calibrationSteps(summary({ view: 'speed-running', passStage: 'starting_trial' }))).toEqual([D, D, D, C, T, T, T]);
    expect(calibrationSteps(summary({ view: 'speed-running', passStage: 'sweep' }))).toEqual([D, D, D, D, C, T, T]);
    expect(calibrationSteps(summary({ view: 'speed-held' }))).toEqual([D, D, D, D, D, C, T]);
    expect(calibrationSteps(summary({ view: 'ato-running' }))).toEqual([D, D, D, D, D, D, C]);
  });

  it('moves on to the ATO minimum test after a commit, and ends done after its commit', () => {
    expect(calibrationSteps(summary({ speedCommitted: true }))).toEqual([D, D, D, D, D, D, C]);
    expect(calibrationSteps(summary({ atoCommitted: true }))).toEqual([D, D, D, D, D, D, D]);
  });

  it('shows the canvas only while a run is open or being set up', () => {
    expect(calibrationRunShown(summary())).toBe(false);
    expect(calibrationRunShown(summary({ view: 'speed-setup' }))).toBe(true);
    expect(calibrationRunShown(summary({ view: 'speed-running' }))).toBe(true);
    expect(calibrationRunShown(summary({ view: 'speed-running', trainId: null }))).toBe(false);
  });
});
