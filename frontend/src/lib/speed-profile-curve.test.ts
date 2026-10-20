import { describe, expect, it } from 'vitest';

import {
  type Curve,
  interpolate,
  voltageToPower,
  powerToVoltage,
  powerPercentToSpeed,
  speedToPower,
  speedToVoltageTwoStage,
  voltageToSpeedTwoStage,
  topSpeed,
  maxVoltage,
  lightingVoltage,
  reanchorVoltagePowerCurve,
} from './speed-profile-curve';

// Parity tests: every expected value is hand-computed from the corresponding
// SpeedProfile method in speed_profile.py so the TS port stays byte-consistent
// with the server's piecewise-linear interpolation.

// Default calibration curves (post-merge defaults).
const VP: Curve = [
  [2, 0],
  [12, 100],
]; // [track_voltage, power_percent]
const PS: Curve = [
  [0, 0],
  [100, 100],
]; // [power_percent, speed_kmh]

// A kinked, non-linear power_speed_curve to exercise multi-segment lookups.
const PS_KINKED: Curve = [
  [0, 0],
  [50, 20],
  [100, 100],
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

describe('voltageToPower (stage 1)', () => {
  it('interpolates inside the curve', () => {
    expect(voltageToPower(VP, 7)).toBeCloseTo(50, 9);
    expect(voltageToPower(VP, 4.5)).toBeCloseTo(25, 9);
  });
  it('clamps at the endpoints', () => {
    expect(voltageToPower(VP, 2)).toBe(0);
    expect(voltageToPower(VP, 1)).toBe(0);
    expect(voltageToPower(VP, 12)).toBe(100);
    expect(voltageToPower(VP, 20)).toBe(100);
  });
  it('returns 0 for an empty curve', () => {
    expect(voltageToPower([], 7)).toBe(0);
  });
});

describe('powerToVoltage (stage 1, inverted)', () => {
  it('interpolates inside the curve', () => {
    expect(powerToVoltage(VP, 50)).toBeCloseTo(7, 9);
    expect(powerToVoltage(VP, 25)).toBeCloseTo(4.5, 9);
  });
  it('clamps at the endpoints', () => {
    expect(powerToVoltage(VP, 0)).toBe(2);
    expect(powerToVoltage(VP, -5)).toBe(2);
    expect(powerToVoltage(VP, 100)).toBe(12);
    expect(powerToVoltage(VP, 150)).toBe(12);
  });
});

describe('powerPercentToSpeed (stage 2)', () => {
  it('returns 0 for non-positive percent (guard runs before endpoint check)', () => {
    expect(powerPercentToSpeed(PS, 0)).toBe(0);
    expect(powerPercentToSpeed(PS, -10)).toBe(0);
  });
  it('interpolates on the linear default', () => {
    expect(powerPercentToSpeed(PS, 40)).toBeCloseTo(40, 9);
    expect(powerPercentToSpeed(PS, 100)).toBe(100);
    expect(powerPercentToSpeed(PS, 150)).toBe(100);
  });
  it('interpolates across a kinked curve', () => {
    expect(powerPercentToSpeed(PS_KINKED, 25)).toBeCloseTo(10, 9);
    expect(powerPercentToSpeed(PS_KINKED, 75)).toBeCloseTo(60, 9);
  });
});

describe('speedToPower (stage 2, inverted)', () => {
  it('returns 0 for non-positive speed', () => {
    expect(speedToPower(PS, 0)).toBe(0);
    expect(speedToPower(PS, -5)).toBe(0);
  });
  it('inverts the linear default', () => {
    expect(speedToPower(PS, 40)).toBeCloseTo(40, 9);
  });
  it('inverts a kinked curve', () => {
    expect(speedToPower(PS_KINKED, 10)).toBeCloseTo(25, 9);
    expect(speedToPower(PS_KINKED, 60)).toBeCloseTo(75, 9);
  });
});

describe('speedToVoltageTwoStage', () => {
  it('returns 0 V for zero/negative speed (explicit rule)', () => {
    expect(speedToVoltageTwoStage(VP, PS, 0)).toBe(0);
    expect(speedToVoltageTwoStage(VP, PS, -10)).toBe(0);
  });
  it('matches the Python calibration points', () => {
    expect(speedToVoltageTwoStage(VP, PS, 30)).toBeCloseTo(5, 9);
    expect(speedToVoltageTwoStage(VP, PS, 60)).toBeCloseTo(8, 9);
    expect(speedToVoltageTwoStage(VP, PS, 45)).toBeCloseTo(6.5, 9);
    expect(speedToVoltageTwoStage(VP, PS, 150)).toBeCloseTo(12, 9);
  });
});

describe('voltageToSpeedTwoStage', () => {
  it('composes both stages', () => {
    expect(voltageToSpeedTwoStage(VP, PS, 7)).toBeCloseTo(50, 9);
    expect(voltageToSpeedTwoStage(VP, PS, 2)).toBe(0);
    expect(voltageToSpeedTwoStage(VP, PS, 12)).toBe(100);
  });
});

describe('curve summary helpers', () => {
  it('topSpeed reads the last power_speed point', () => {
    expect(topSpeed([[0, 0], [100, 80]])).toBe(80);
    expect(topSpeed(PS)).toBe(100);
    expect(topSpeed([])).toBe(0);
  });
  it('maxVoltage reads the last voltage_power point with a fallback', () => {
    expect(maxVoltage(VP, 99)).toBe(12);
    expect(maxVoltage([], 99)).toBe(99);
  });
  it('lightingVoltage reads the first voltage_power point with a fallback', () => {
    expect(lightingVoltage(VP, 99)).toBe(2);
    expect(lightingVoltage([], 99)).toBe(99);
  });
});

describe('reanchorVoltagePowerCurve (mirrors speed_profile.reanchor_voltage_power_curve)', () => {
  const CURVE: Curve = [
    [1.5, 0],
    [4, 30],
    [12, 100],
  ];

  it('moves the 0% point up and drops the points at or below it', () => {
    expect(reanchorVoltagePowerCurve(CURVE, 4, 12)).toEqual([
      [4, 0],
      [12, 100],
    ]);
  });

  it('moves the 0% point down and keeps exactly one 0% point', () => {
    expect(reanchorVoltagePowerCurve(CURVE, 1, 12)).toEqual([
      [1, 0],
      [4, 30],
      [12, 100],
    ]);
  });
});
