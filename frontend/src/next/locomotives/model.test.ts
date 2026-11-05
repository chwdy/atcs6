import { describe, expect, it } from 'vitest';

import type { CalibrationGroupSummary } from '../../editor/editor-speed-profiles';
import {
  calibrationRunShown,
  calibrationSteps,
  modelProfiles,
  panelTarget,
  rosterFromNetwork,
  rosterPayload,
  rosterFormFrom,
  selectionForProfile,
  selectionForTrain,
  trainsFromNetwork,
  trainsNotInRoster,
  type RosterEntry,
  type TrainSummary,
} from './model';

const entry = (over: Partial<RosterEntry> = {}): RosterEntry => ({
  entry_id: 'R1',
  name: 'Class 47 #2',
  model: 'Bachmann Class 47',
  purchase_date: null,
  purchase_price: null,
  consist_length_cm: 22,
  max_speed: null,
  min_radius_cm: null,
  max_slope: 0,
  speed_profile_id: 'inst_R1',
  ...over,
});

const train = (over: Partial<TrainSummary> = {}): TrainSummary => ({
  id: 'T1',
  name: 'T1',
  model: '',
  lengthCm: 22,
  speedProfileId: 'inst_T1',
  rosterEntryId: null,
  placed: true,
  status: 'MANUAL',
  stopped: true,
  ...over,
});

const summary = (over: Partial<CalibrationGroupSummary> = {}): CalibrationGroupSummary => ({
  profileId: 'inst_R1',
  trainId: 'T1',
  view: 'none',
  ready: true,
  unmet: [],
  lightingVoltageSet: false,
  speedCommitted: false,
  ...over,
});

describe('reading the network snapshot', () => {
  it('reads roster entries and trains, with train_status upper case and placement from the span', () => {
    const network = {
      train_roster: [{ entry_id: 'R1', name: 'A', model: 'M', purchase_price: 10, max_slope: 0, speed_profile_id: null }],
      trains: [
        { id: 'T1', car_number: '', speed_profile_id: 'p', roster_entry_id: 'R1' },
        { id: 'T2', car_number: '47 102', speed_profile_id: 'q', roster_entry_id: null },
      ],
      controllers_by_train_id: { T1: { span: { x: 1 }, train_status: 'calibrating', is_stopped: false }, T2: { span: null } },
    };
    expect(rosterFromNetwork(network)[0]).toMatchObject({ entry_id: 'R1', purchase_price: 10, speed_profile_id: null });
    const [t1, t2] = trainsFromNetwork(network);
    expect(t1).toMatchObject({ name: 'T1', placed: true, status: 'CALIBRATING', stopped: false, rosterEntryId: 'R1' });
    expect(t2).toMatchObject({ name: '47 102', placed: false, status: 'OFF' });
  });
});

describe('the left list', () => {
  it('lists placed trains whose roster entry is not listed, and no unplaced ones', () => {
    const trains = [
      train({ id: 'A', name: 'A', rosterEntryId: 'R1' }),
      train({ id: 'B', name: 'B', rosterEntryId: 'GONE' }),
      train({ id: 'C', name: 'C' }),
      train({ id: 'D', name: 'D', placed: false }),
    ];
    expect(trainsNotInRoster(trains, [entry()]).map((t) => t.id)).toEqual(['B', 'C']);
  });

  it('lists model profiles only, default first even when the snapshot leaves it out', () => {
    const profiles = [
      { profile_id: 'z_model', name: '', model: '', derived_from: null, calibrated_at: null },
      { profile_id: 'inst', name: '', model: '', derived_from: 'z_model', calibrated_at: null },
      { profile_id: 'a_model', name: '', model: '', derived_from: null, calibrated_at: null },
    ];
    expect(modelProfiles(profiles).map((p) => p.profile_id)).toEqual(['default', 'a_model', 'z_model']);
  });
});

describe('panelTarget', () => {
  it('shows a roster entry’s own profile and names the train placed from it', () => {
    const trains = [train({ id: 'T9', name: 'T9', rosterEntryId: 'R1', speedProfileId: 'inst_R1' })];
    expect(panelTarget({ kind: 'roster', entryId: 'R1' }, [entry()], trains)).toEqual({ profileId: 'inst_R1', trainId: 'T9' });
  });

  it('falls back to the placed train’s profile when the entry names none', () => {
    const trains = [train({ id: 'T9', rosterEntryId: 'R1', speedProfileId: 'seeded' })];
    expect(panelTarget({ kind: 'roster', entryId: 'R1' }, [entry({ speed_profile_id: null })], trains)).toEqual({
      profileId: 'seeded',
      trainId: 'T9',
    });
  });

  it('names the asked train when more than one was placed from the entry', () => {
    const trains = [
      train({ id: 'A', name: 'A', rosterEntryId: 'R1' }),
      train({ id: 'B', name: 'B', rosterEntryId: 'R1' }),
    ];
    expect(panelTarget({ kind: 'roster', entryId: 'R1' }, [entry()], trains).trainId).toBe('A');
    expect(panelTarget({ kind: 'roster', entryId: 'R1', trainId: 'B' }, [entry()], trains).trainId).toBe('B');
  });

  it('names no train for an unplaced entry, a model profile, or the Add form', () => {
    expect(panelTarget({ kind: 'roster', entryId: 'R1' }, [entry()], [])).toEqual({ profileId: 'inst_R1', trainId: null });
    expect(panelTarget({ kind: 'profile', profileId: 'm' }, [], [])).toEqual({ profileId: 'm', trainId: null });
    expect(panelTarget({ kind: 'new' }, [], [])).toEqual({ profileId: null, trainId: null });
  });

  it('opens a train through its roster entry when it came from one', () => {
    const trains = [train({ id: 'T1', rosterEntryId: 'R1' }), train({ id: 'T2' })];
    expect(selectionForTrain('T1', [entry()], trains)).toEqual({ kind: 'roster', entryId: 'R1', trainId: 'T1' });
    expect(selectionForTrain('T2', [entry()], trains)).toEqual({ kind: 'train', trainId: 'T2' });
  });
});

describe('selectionForProfile', () => {
  it('picks the locomotive a profile belongs to, and a model profile as itself', () => {
    const trains = [train({ id: 'T1', rosterEntryId: 'R1', speedProfileId: 'inst_R1' }), train({ id: 'T2', speedProfileId: 'inst_T2' })];
    expect(selectionForProfile('inst_R1', [entry()], trains)).toEqual({ kind: 'roster', entryId: 'R1' });
    expect(selectionForProfile('inst_T2', [entry()], trains)).toEqual({ kind: 'train', trainId: 'T2' });
    expect(selectionForProfile('class47', [entry()], trains)).toEqual({ kind: 'profile', profileId: 'class47', fromPanel: true });
  });
});

describe('calibrationSteps', () => {
  it('starts on naming the locomotive', () => {
    expect(calibrationSteps(null)).toEqual(['current', 'todo', 'todo', 'todo', 'todo']);
    expect(calibrationSteps(summary({ trainId: null }))).toEqual(['current', 'todo', 'todo', 'todo', 'todo']);
  });

  it('marks the step the open pass belongs to', () => {
    expect(calibrationSteps(summary())).toEqual(['done', 'current', 'todo', 'todo', 'todo']);
    expect(calibrationSteps(summary({ view: 'speed-setup' }))).toEqual(['done', 'todo', 'current', 'todo', 'todo']);
    expect(calibrationSteps(summary({ view: 'speed-running', lightingVoltageSet: true }))).toEqual([
      'done',
      'done',
      'done',
      'current',
      'todo',
    ]);
    expect(calibrationSteps(summary({ view: 'speed-held' }))).toEqual(['done', 'todo', 'done', 'done', 'current']);
  });

  it('moves on to the speed pass once the train runs on a lighting voltage, and ends done after a commit', () => {
    expect(calibrationSteps(summary({ lightingVoltageSet: true }))).toEqual(['done', 'done', 'current', 'todo', 'todo']);
    expect(calibrationSteps(summary({ speedCommitted: true }))).toEqual(['done', 'done', 'done', 'done', 'done']);
  });

  it('shows the canvas only while a run is open or being set up', () => {
    expect(calibrationRunShown(summary())).toBe(false);
    expect(calibrationRunShown(summary({ view: 'speed-setup' }))).toBe(true);
    expect(calibrationRunShown(summary({ view: 'speed-running' }))).toBe(true);
    expect(calibrationRunShown(summary({ view: 'speed-running', trainId: null }))).toBe(false);
  });
});

describe('rosterPayload', () => {
  it('sends blank fields as not given and parses numbers', () => {
    const form = { ...rosterFormFrom(entry()), purchase_price: '89.5', consist_length_cm: '', max_slope: '' };
    const { body, errors } = rosterPayload(form, null);
    expect(errors).toEqual([]);
    expect(body).toMatchObject({ name: 'Class 47 #2', purchase_price: 89.5, consist_length_cm: null, max_slope: 0 });
    expect(body).not.toHaveProperty('entry_id');
  });

  it('keeps the id and the profile link on an edit, because POST replaces the whole entry', () => {
    const { body } = rosterPayload(rosterFormFrom(entry()), entry());
    expect(body).toMatchObject({ entry_id: 'R1', speed_profile_id: 'inst_R1' });
  });

  it('refuses a form with neither name nor model, or a number that does not parse', () => {
    const blank = { ...rosterFormFrom(entry()), name: ' ', model: '' };
    expect(rosterPayload(blank, null).body).toBeNull();
    const bad = { ...rosterFormFrom(entry()), purchase_price: 'cheap' };
    const result = rosterPayload(bad, null);
    expect(result.body).toBeNull();
    expect(result.errors[0]).toContain('Purchase price');
  });
});
