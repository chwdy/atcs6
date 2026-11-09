import { describe, expect, it } from 'vitest';

import { changedProfileFields, handEditable } from './profile-edits';

const saved = {
  profile_id: 3,
  model_id: 1,
  set_name: '1',
  parent_profile_id: null,
  lighting_voltage: 1.5,
  calibrated_at: null,
  voltage_speed_curve: [
    [2, 10],
    [12, 80],
  ],
  acceleration_rate: 10,
};

describe('changedProfileFields', () => {
  it('sends only the fields the operator changed', () => {
    const current = { ...saved, acceleration_rate: 12 };
    expect(changedProfileFields(saved, current)).toEqual({ acceleration_rate: 12 });
  });

  it('sends nothing when nothing changed', () => {
    expect(changedProfileFields(saved, { ...saved })).toEqual({});
  });

  it('never sends calibrated_at or an id', () => {
    const current = { ...saved, calibrated_at: '2026-10-22T10:00:00Z', profile_id: 9, parent_profile_id: 4, model_id: 2 };
    expect(changedProfileFields(saved, current)).toEqual({});
  });

  it('sends a changed lighting voltage, and a cleared one as null', () => {
    expect(changedProfileFields(saved, { ...saved, lighting_voltage: 2.0 })).toEqual({ lighting_voltage: 2.0 });
    expect(changedProfileFields(saved, { ...saved, lighting_voltage: null })).toEqual({ lighting_voltage: null });
  });
});

describe('handEditable', () => {
  it('takes every value field and no id', () => {
    expect(handEditable('voltage_speed_curve')).toBe(true);
    expect(handEditable('max_voltage')).toBe(true);
    expect(handEditable('acceleration_rate')).toBe(true);
    expect(handEditable('set_name')).toBe(false);
    expect(handEditable('calibrated_at')).toBe(false);
    expect(handEditable('parent_profile_id')).toBe(false);
  });
});
