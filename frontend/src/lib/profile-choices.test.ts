import { describe, expect, it } from 'vitest';

import { pickableProfileChoices } from './profile-choices';

const profiles = [
  { profile_id: 'default', name: 'Default HO Scale', derived_from: null },
  { profile_id: 'default_UNIDENT_1', name: 'Default (UNIDENT_1)', derived_from: null },
  { profile_id: 'default_UNIDENT_1__instance', name: 'Default (UNIDENT_1) instance', derived_from: 'default_UNIDENT_1' },
  { profile_id: 'loco_7', name: 'Loco 7', derived_from: 'class_37' },
  { profile_id: 'loco_8', name: 'Loco 8', derived_from: 'class_37' },
  { profile_id: 'class_37', name: 'Class 37', derived_from: null },
];

const trains = [
  { train_id: 'UNIDENT_1', is_unidentified: true, speed_profile_id: 'default_UNIDENT_1__instance' },
  { train_id: 'T8', speed_profile_id: 'loco_8' },
];

describe('pickableProfileChoices', () => {
  it('offers only instance profiles no train names', () => {
    expect(pickableProfileChoices(profiles, trains).map((c) => c.id)).toEqual(['loco_7']);
  });

  it("hides an unidentified train's blank pair", () => {
    const ids = pickableProfileChoices(profiles, trains).map((c) => c.id);
    expect(ids).not.toContain('default_UNIDENT_1');
    expect(ids).not.toContain('default_UNIDENT_1__instance');
  });

  it('keeps the profile the train on screen names, even a model profile', () => {
    expect(pickableProfileChoices(profiles, trains, 'loco_8').map((c) => c.id)).toEqual(['loco_7', 'loco_8']);
    expect(pickableProfileChoices(profiles, trains, 'default').map((c) => c.id)).toEqual(['default', 'loco_7']);
  });

  it('shows an unknown current profile by its id', () => {
    expect(pickableProfileChoices([], [], 'gone')).toEqual([{ id: 'gone', label: 'gone' }]);
  });
});
