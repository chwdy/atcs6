import { describe, expect, it } from 'vitest';

import { profileChoices, profileSourceLabel, setLabel } from './profile-choices';

const models = [
  { model_id: 2, name: 'Siemens Charger' },
  { model_id: 1, name: 'H100' },
];

const profiles = [
  { profile_id: 0, model_id: null, parent_profile_id: null },
  { profile_id: 4, model_id: 2, set_name: '#2101' },
  { profile_id: 3, model_id: 1, set_name: '2' },
  { profile_id: 1, model_id: 1, set_name: '1' },
  { profile_id: 7, model_id: null, parent_profile_id: 1 },
];

describe('profileChoices', () => {
  it('offers the Default profile and every set, grouped by model, never an instance', () => {
    expect(profileChoices(profiles, models)).toEqual([
      { id: '0', label: 'Default' },
      { id: '1', label: 'H100 · 1' },
      { id: '3', label: 'H100 · 2' },
      { id: '4', label: 'Siemens Charger · #2101' },
    ]);
  });

  it('leaves the Default out when the form already offers it', () => {
    expect(profileChoices(profiles, models, false).map((c) => c.id)).toEqual(['1', '3', '4']);
  });
});

describe('profileSourceLabel', () => {
  it('names where a train profile came from', () => {
    expect(profileSourceLabel({ parent_profile_id: 1 }, profiles, models)).toBe('H100 · 1');
    expect(profileSourceLabel({ parent_profile_id: 0 }, profiles, models)).toBe('Default');
    expect(profileSourceLabel({ parent_profile_id: 99 }, profiles, models)).toBe('Set deleted');
    expect(profileSourceLabel({ parent_profile_id: null }, profiles, models)).toBe('Scene profile');
  });

  it('names a set whose model is gone', () => {
    expect(setLabel({ model_id: 9, set_name: '1' }, models)).toBe('No model · 1');
  });
});
