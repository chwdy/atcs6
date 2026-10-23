import { describe, expect, it } from 'vitest';

import { changedProfileFields, handEditable } from './profile-edits';

const saved = {
  profile_id: 'P1',
  name: 'Loco 7',
  lighting_voltage: 1.5,
  calibrated_at: null,
  power_speed_curve: [
    [0, 0],
    [100, 80],
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

  it('never sends the fields calibration owns', () => {
    const current = { ...saved, lighting_voltage: 2.0, calibrated_at: '2026-10-22T10:00:00Z' };
    expect(changedProfileFields(saved, current, false)).toEqual({});
  });

  it('sends only the curve from a locomotive own profile', () => {
    const curve = [
      [0, 0],
      [100, 60],
    ];
    const current = { ...saved, power_speed_curve: curve, acceleration_rate: 12 };
    expect(changedProfileFields(saved, current, true)).toEqual({ power_speed_curve: curve });
  });
});

describe('handEditable', () => {
  it('keeps model fields off an instance profile', () => {
    expect(handEditable('power_speed_curve', true)).toBe(true);
    expect(handEditable('max_voltage', true)).toBe(false);
    expect(handEditable('max_voltage', false)).toBe(true);
    expect(handEditable('lighting_voltage', false)).toBe(false);
  });
});
