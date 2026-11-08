import { describe, expect, it } from 'vitest';

import {
  type Curve,
  atoMinimumVoltage,
  atoMinimumVoltageProblem,
  curveProblems,
  firstPointVoltage,
  interpolate,
  lightingVoltageProblem,
  powerPercentToSpeed,
  powerPercentToVoltage,
  readableCurve,
  speedToPowerPercent,
  speedToVoltage,
  startingVoltageProblem,
  topSpeed,
  voltageToSpeed,
} from './speed-profile-curve';

// Parity tests: every expected value is hand-computed from the matching
// SpeedProfile method in speed_profile.py, so the TS port gives the same
// numbers as the server.

// The seeded default for a 100 km/h top speed at 12 V.
const SEEDED: Curve = [
  [1.5, 100 * 1.5 / 12],
  [12, 100],
];

// A measured-looking curve with a kink and a flat stretch (6 V..8 V at 40 km/h),
// for a locomotive that starts to move at 2 V.
const MEASURED: Curve = [
  [3, 10],
  [6, 40],
  [8, 40],
  [12, 100],
];
const START = 2;

describe('interpolate', () => {
  it('interpolates linearly', () => {
    expect(interpolate(2, 0, 12, 100, 7)).toBeCloseTo(50, 9);
    expect(interpolate(0, 2, 100, 12, 30)).toBeCloseTo(5, 9);
  });
  it('returns y1 when x2 === x1 (divide-by-zero guard)', () => {
    expect(interpolate(5, 3, 5, 9, 5)).toBe(3);
  });
});

describe('the curve is read from the starting voltage up', () => {
  it('puts (starting voltage, 0) in front and drops points at or below it', () => {
    expect(readableCurve(MEASURED, START)).toEqual([[2, 0], ...MEASURED]);
    expect(readableCurve(MEASURED, 6)).toEqual([[6, 0], [8, 40], [12, 100]]);
  });
  it('reads the first point above the starting voltage and the top speed', () => {
    expect(firstPointVoltage(MEASURED, START, 12)).toBe(3);
    expect(firstPointVoltage(MEASURED, 4, 12)).toBe(6);
    expect(topSpeed(MEASURED)).toBe(100);
    expect(topSpeed([])).toBe(0);
  });
  it('runs ATO from the set ATO minimum voltage, or from the first point', () => {
    expect(atoMinimumVoltage(MEASURED, START, 12, null)).toBe(3);
    expect(atoMinimumVoltage(MEASURED, START, 12, 5)).toBe(5);
  });
});

describe('voltageToSpeed (mirrors SpeedProfile.voltage_to_speed)', () => {
  it('is 0 km/h at or below the starting voltage', () => {
    expect(voltageToSpeed(MEASURED, 2, START)).toBe(0);
    expect(voltageToSpeed(MEASURED, 0, START)).toBe(0);
  });
  it('runs a straight line from the starting voltage to the first point', () => {
    expect(voltageToSpeed(MEASURED, 2.5, START)).toBeCloseTo(5, 9);
    expect(voltageToSpeed(MEASURED, 3, START)).toBe(10);
  });
  it('interpolates between points', () => {
    expect(voltageToSpeed(MEASURED, 4.5, START)).toBeCloseTo(25, 9);
    expect(voltageToSpeed(MEASURED, 7, START)).toBe(40);
    expect(voltageToSpeed(MEASURED, 10, START)).toBeCloseTo(70, 9);
  });
  it('reads the top speed at and above the last point', () => {
    expect(voltageToSpeed(MEASURED, 12, START)).toBe(100);
    expect(voltageToSpeed(MEASURED, 15, START)).toBe(100);
  });
  it('returns 0 for an empty curve', () => {
    expect(voltageToSpeed([], 7, START)).toBe(0);
  });
});

describe('speedToVoltage (mirrors SpeedProfile.speed_to_voltage)', () => {
  it('reads the lighting voltage at 0 km/h, or 0 V when there is none', () => {
    expect(speedToVoltage(MEASURED, 0, 12, 1.5, START)).toBe(1.5);
    expect(speedToVoltage(MEASURED, -3, 12, null, START)).toBe(0);
  });
  it('reads a slow speed on the line above the starting voltage', () => {
    expect(speedToVoltage(MEASURED, 5, 12, null, START)).toBeCloseTo(2.5, 9);
    expect(speedToVoltage(MEASURED, 10, 12, null, START)).toBe(3);
  });
  it('interpolates between points', () => {
    expect(speedToVoltage(MEASURED, 25, 12, null, START)).toBeCloseTo(4.5, 9);
    expect(speedToVoltage(MEASURED, 70, 12, null, START)).toBeCloseTo(10, 9);
  });
  it('takes the lowest voltage of a flat stretch', () => {
    expect(speedToVoltage(MEASURED, 40, 12, null, START)).toBe(6);
  });
  it('reads max_voltage above the top speed', () => {
    expect(speedToVoltage(MEASURED, 150, 12.5, null, START)).toBe(12.5);
  });
  it('reads the last point at exactly the top speed', () => {
    expect(speedToVoltage(MEASURED, 100, 12, null, START)).toBe(12);
  });
});

describe('power percent (mirrors SpeedProfile.power_percent_to_speed / speed_to_power_percent)', () => {
  it('0% is stopped', () => {
    expect(powerPercentToSpeed(MEASURED, 0)).toBe(0);
    expect(powerPercentToSpeed(MEASURED, -10)).toBe(0);
  });
  it('runs straight from 0 km/h to the top speed, capped at 100%', () => {
    expect(powerPercentToSpeed(MEASURED, 1)).toBeCloseTo(1, 9);
    expect(powerPercentToSpeed(MEASURED, 50)).toBeCloseTo(50, 9);
    expect(powerPercentToSpeed(MEASURED, 100)).toBe(100);
    expect(powerPercentToSpeed(MEASURED, 150)).toBe(100);
  });
  it('reads a speed back as its share of the top speed', () => {
    expect(speedToPowerPercent(MEASURED, 0)).toBe(0);
    expect(speedToPowerPercent(MEASURED, 50)).toBeCloseTo(50, 9);
    expect(speedToPowerPercent(MEASURED, 200)).toBe(100);
  });
  it('turns a power percent into a voltage through speed', () => {
    expect(powerPercentToVoltage(MEASURED, 50, 12, null, START)).toBeCloseTo(8 + 4 * 10 / 60, 9);
    expect(powerPercentToVoltage(MEASURED, 1, 12, null, START)).toBeCloseTo(2.1, 9);
    expect(powerPercentToVoltage(SEEDED, 0, 12, 0.8, 1)).toBe(0.8);
    expect(powerPercentToVoltage(SEEDED, 100, 12, null, 1)).toBeCloseTo(12, 9);
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
      "the first point's voltage and speed must be above 0",
      "the top speed must be above the first point's speed",
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
    expect(lightingVoltageProblem(null, START)).toBeNull();
    expect(lightingVoltageProblem(0, START)).toBeNull();
    expect(lightingVoltageProblem(1.9, START)).toBeNull();
  });
  it('refuses one below 0, or at or above the starting voltage', () => {
    expect(lightingVoltageProblem(-0.1, START)).toMatch(/below 0/);
    expect(lightingVoltageProblem(2, START)).toMatch(/below the starting voltage \(2 V\)/);
    expect(lightingVoltageProblem(Number.NaN, START)).toMatch(/number/);
  });
});

describe('startingVoltageProblem', () => {
  it('accepts one between the lighting voltage and max_voltage', () => {
    expect(startingVoltageProblem(2, 12, 1.5)).toBeNull();
    expect(startingVoltageProblem(2, 12, null)).toBeNull();
  });
  it('refuses one at or above max_voltage, or at or below the lighting voltage', () => {
    expect(startingVoltageProblem(12, 12, null)).toMatch(/max voltage/);
    expect(startingVoltageProblem(1.5, 12, 1.5)).toMatch(/lighting voltage/);
    expect(startingVoltageProblem(-1, 12, null)).toMatch(/below 0/);
  });
});

describe('atoMinimumVoltageProblem', () => {
  it('accepts none, and one on the measured part of the curve', () => {
    expect(atoMinimumVoltageProblem(null, MEASURED, START, 12)).toBeNull();
    expect(atoMinimumVoltageProblem(3, MEASURED, START, 12)).toBeNull();
    expect(atoMinimumVoltageProblem(12, MEASURED, START, 12)).toBeNull();
  });
  it('refuses one below the first point or above max_voltage', () => {
    expect(atoMinimumVoltageProblem(2.5, MEASURED, START, 12)).toMatch(/from 3 V/);
    expect(atoMinimumVoltageProblem(12.5, MEASURED, START, 12)).toMatch(/up to 12 V/);
  });
});
