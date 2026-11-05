import { describe, expect, it } from 'vitest';

import {
  type Curve,
  curveProblems,
  interpolate,
  lightingVoltageProblem,
  powerPercentToSpeed,
  powerPercentToVoltage,
  speedToPowerPercent,
  speedToVoltage,
  startingSpeed,
  startingVoltage,
  topSpeed,
  voltageToSpeed,
} from './speed-profile-curve';

// Parity tests: every expected value is hand-computed from the matching
// SpeedProfile method in speed_profile.py, so the TS port gives the same
// numbers as the server.

// The seeded default for a 100 km/h top speed at 12 V: starting voltage 2 V.
const SEEDED: Curve = [
  [2, 100 * 2 / 12],
  [12, 100],
];

// A measured-looking curve with a kink and a flat stretch (6 V..8 V at 40 km/h).
const MEASURED: Curve = [
  [3, 10],
  [6, 40],
  [8, 40],
  [12, 100],
];

describe('interpolate', () => {
  it('interpolates linearly', () => {
    expect(interpolate(2, 0, 12, 100, 7)).toBeCloseTo(50, 9);
    expect(interpolate(0, 2, 100, 12, 30)).toBeCloseTo(5, 9);
  });
  it('returns y1 when x2 === x1 (divide-by-zero guard)', () => {
    expect(interpolate(5, 3, 5, 9, 5)).toBe(3);
  });
});

describe('curve landmarks', () => {
  it('reads the starting voltage and speed from the first point, the top speed from the last', () => {
    expect(startingVoltage(MEASURED)).toBe(3);
    expect(startingSpeed(MEASURED)).toBe(10);
    expect(topSpeed(MEASURED)).toBe(100);
  });
  it('reads 0 from an empty curve', () => {
    expect(startingVoltage([])).toBe(0);
    expect(startingSpeed([])).toBe(0);
    expect(topSpeed([])).toBe(0);
  });
});

describe('voltageToSpeed (mirrors SpeedProfile.voltage_to_speed)', () => {
  it('is 0 km/h below the starting voltage', () => {
    expect(voltageToSpeed(MEASURED, 2.9)).toBe(0);
    expect(voltageToSpeed(MEASURED, 0)).toBe(0);
  });
  it('reads the starting speed at the starting voltage', () => {
    expect(voltageToSpeed(MEASURED, 3)).toBe(10);
  });
  it('interpolates between points', () => {
    expect(voltageToSpeed(MEASURED, 4.5)).toBeCloseTo(25, 9);
    expect(voltageToSpeed(MEASURED, 7)).toBe(40);
    expect(voltageToSpeed(MEASURED, 10)).toBeCloseTo(70, 9);
  });
  it('reads the top speed at and above the last point', () => {
    expect(voltageToSpeed(MEASURED, 12)).toBe(100);
    expect(voltageToSpeed(MEASURED, 15)).toBe(100);
  });
  it('returns 0 for an empty curve', () => {
    expect(voltageToSpeed([], 7)).toBe(0);
  });
});

describe('speedToVoltage (mirrors SpeedProfile.speed_to_voltage)', () => {
  it('reads the lighting voltage at 0 km/h, or 0 V when there is none', () => {
    expect(speedToVoltage(MEASURED, 0, 12, 1.5)).toBe(1.5);
    expect(speedToVoltage(MEASURED, -3, 12, null)).toBe(0);
  });
  it('reads the starting voltage for any speed up to the starting speed', () => {
    expect(speedToVoltage(MEASURED, 4, 12, null)).toBe(3);
    expect(speedToVoltage(MEASURED, 10, 12, null)).toBe(3);
  });
  it('interpolates between points', () => {
    expect(speedToVoltage(MEASURED, 25, 12, null)).toBeCloseTo(4.5, 9);
    expect(speedToVoltage(MEASURED, 70, 12, null)).toBeCloseTo(10, 9);
  });
  it('takes the lowest voltage of a flat stretch', () => {
    expect(speedToVoltage(MEASURED, 40, 12, null)).toBe(6);
  });
  it('reads max_voltage above the top speed', () => {
    expect(speedToVoltage(MEASURED, 150, 12.5, null)).toBe(12.5);
  });
  it('reads the last point at exactly the top speed', () => {
    expect(speedToVoltage(MEASURED, 100, 12, null)).toBe(12);
  });
});

describe('power percent (mirrors SpeedProfile.power_percent_to_speed / speed_to_power_percent)', () => {
  it('0% is stopped', () => {
    expect(powerPercentToSpeed(MEASURED, 0)).toBe(0);
    expect(powerPercentToSpeed(MEASURED, -10)).toBe(0);
  });
  it('runs straight from the starting speed to the top speed, capped at 100%', () => {
    expect(powerPercentToSpeed(MEASURED, 1)).toBeCloseTo(10.9, 9);
    expect(powerPercentToSpeed(MEASURED, 50)).toBeCloseTo(55, 9);
    expect(powerPercentToSpeed(MEASURED, 100)).toBe(100);
    expect(powerPercentToSpeed(MEASURED, 150)).toBe(100);
  });
  it('reads a speed back as its share of the speed range', () => {
    expect(speedToPowerPercent(MEASURED, 0)).toBe(0);
    expect(speedToPowerPercent(MEASURED, 55)).toBeCloseTo(50, 9);
    expect(speedToPowerPercent(MEASURED, 5)).toBe(0);
    expect(speedToPowerPercent(MEASURED, 200)).toBe(100);
  });
  it('reads 100% when the curve has no speed range', () => {
    expect(speedToPowerPercent([[3, 10], [12, 10]], 10)).toBe(100);
  });
  it('turns a power percent into a voltage through speed', () => {
    expect(powerPercentToVoltage(MEASURED, 50, 12, null)).toBeCloseTo(8 + 4 * 15 / 60, 9);
    expect(powerPercentToVoltage(SEEDED, 0, 12, 1.2)).toBe(1.2);
    expect(powerPercentToVoltage(SEEDED, 100, 12, null)).toBeCloseTo(12, 9);
  });
});

describe('curveProblems (mirrors speed_profile.curve_problems)', () => {
  it('accepts a valid curve', () => {
    expect(curveProblems(MEASURED, 12)).toEqual([]);
    expect(curveProblems(SEEDED, 12)).toEqual([]);
  });
  it('refuses fewer than two points', () => {
    expect(curveProblems([[3, 10]], 12)).toEqual(['the curve needs at least two points']);
    expect(curveProblems(null, 12)).toEqual(['the curve needs at least two points']);
  });
  it('names every broken condition', () => {
    expect(curveProblems([[0, 0], [12, 0]], 12)).toEqual([
      'the first point (the starting voltage and speed) must be above 0',
      'the top speed must be above the starting speed',
    ]);
    expect(curveProblems([[3, 10], [3, 20], [10, 15]], 12)).toEqual([
      'the points must be in order of rising voltage',
      'the last point must be at max_voltage (12 V)',
      'speed must not fall as voltage rises',
    ]);
  });
});

describe('lightingVoltageProblem', () => {
  it('accepts no lighting voltage, and one below the starting voltage', () => {
    expect(lightingVoltageProblem(null, MEASURED)).toBeNull();
    expect(lightingVoltageProblem(0, MEASURED)).toBeNull();
    expect(lightingVoltageProblem(2.9, MEASURED)).toBeNull();
  });
  it('refuses one below 0, or at or above the starting voltage', () => {
    expect(lightingVoltageProblem(-0.1, MEASURED)).toMatch(/below 0/);
    expect(lightingVoltageProblem(3, MEASURED)).toMatch(/below the starting voltage \(3 V\)/);
    expect(lightingVoltageProblem(Number.NaN, MEASURED)).toMatch(/number/);
  });
});
