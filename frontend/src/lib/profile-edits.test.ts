import { describe, expect, it } from 'vitest';

import { changedProfileFields, handEditable } from './profile-edits';

const saved = {
  profile_id: 'P1',
  name: 'Loco 7',
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
    expect(changedProfileFields(saved, current, false)).toEqual({ acceleration_rate: 12 });
  });

  it('sends nothing when nothing changed', () => {
    expect(changedProfileFields(saved, { ...saved }, false)).toEqual({});
  });

  it('never sends calibrated_at, which only a calibration commit writes', () => {
    const current = { ...saved, calibrated_at: '2026-10-22T10:00:00Z' };
    expect(changedProfileFields(saved, current, false)).toEqual({});
  });

  it('sends a changed lighting voltage, and a cleared one as null', () => {
    expect(changedProfileFields(saved, { ...saved, lighting_voltage: 2.0 }, false)).toEqual({ lighting_voltage: 2.0 });
    expect(changedProfileFields(saved, { ...saved, lighting_voltage: null }, true)).toEqual({ lighting_voltage: null });
  });

  it('sends only the name, lighting voltage and curve from a locomotive own profile', () => {
    const curve = [
      [3, 12],
      [12, 60],
    ];
    const current = { ...saved, voltage_speed_curve: curve, lighting_voltage: 1.0, acceleration_rate: 12 };
    expect(changedProfileFields(saved, current, true)).toEqual({ voltage_speed_curve: curve, lighting_voltage: 1.0 });
  });
});

describe('handEditable', () => {
  it('keeps model fields off an instance profile', () => {
    expect(handEditable('voltage_speed_curve', true)).toBe(true);
    expect(handEditable('lighting_voltage', true)).toBe(true);
    expect(handEditable('name', true)).toBe(true);
    expect(handEditable('max_voltage', true)).toBe(false);
    expect(handEditable('max_voltage', false)).toBe(true);
    expect(handEditable('lighting_voltage', false)).toBe(true);
    expect(handEditable('calibrated_at', false)).toBe(false);
    expect(handEditable('derived_from', false)).toBe(false);
  });
});
