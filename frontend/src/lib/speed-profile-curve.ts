/**
 * Pure speed-profile math — a 1:1 port of the conversion methods on
 * `SpeedProfile` (speed_profile.py). No DOM, no imports. The Speed Profile
 * panel's converter and curve chart use it, so what they show matches the
 * server's arithmetic.
 *
 * A profile stores one curve, `voltage_speed_curve`: [track_voltage, speed_kmh]
 * points, voltage ascending, the last at `max_voltage` with the top speed. It
 * is read from the profile's `starting_voltage` up (design_new.md
 * "SpeedProfile", The curve is read from the starting voltage up): a point at
 * the starting voltage and 0 km/h goes in front, and a stored point at or
 * below the starting voltage is not read. Power percent is not stored: 0% is
 * stopped, and any percent above 0 is a straight line in speed from 0 km/h to
 * the top speed.
 */

export type CurvePoint = [number, number];
export type Curve = CurvePoint[];

/** Linear interpolation between (x1,y1) and (x2,y2). Mirrors SpeedProfile._interpolate. */
export function interpolate(
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  x: number,
): number {
  if (x2 === x1) return y1;
  const t = (x - x1) / (x2 - x1);
  return y1 + t * (y2 - y1);
}

/** The points both readings use. Mirrors SpeedProfile.readable_curve:
 *  (startingVoltage, 0), then every stored point above the starting voltage. */
export function readableCurve(curve: Curve, startingVoltage: number): Curve {
  return [[startingVoltage, 0] as CurvePoint, ...curve.filter(([v]) => v > startingVoltage)];
}

/** The voltage of the curve's first point above the starting voltage, or
 *  `maxVoltage` when none lies above it. Mirrors SpeedProfile.first_point_voltage. */
export function firstPointVoltage(curve: Curve, startingVoltage: number, maxVoltage: number): number {
  const readable = readableCurve(curve, startingVoltage);
  return readable.length > 1 ? readable[1][0] : maxVoltage;
}

/** The speed of the curve's last point, at `max_voltage`. */
export function topSpeed(curve: Curve): number {
  return curve.length ? curve[curve.length - 1][1] : 0;
}

/** The lowest voltage ATO drives a moving train at. Mirrors
 *  SpeedProfile.ato_minimum_voltage: the set ATO minimum voltage, otherwise
 *  the curve's first point above the starting voltage. */
export function atoMinimumVoltage(
  curve: Curve,
  startingVoltage: number,
  maxVoltage: number,
  atoMinVoltage: number | null,
): number {
  return atoMinVoltage ?? firstPointVoltage(curve, startingVoltage, maxVoltage);
}

/** Read the curve forward. Mirrors SpeedProfile.voltage_to_speed: at or below
 *  the starting voltage is 0 km/h; above it interpolates; at or above the last
 *  point reads the top speed. */
export function voltageToSpeed(curve: Curve, voltage: number, startingVoltage: number): number {
  const c = readableCurve(curve, startingVoltage);
  if (c.length < 2 || voltage <= c[0][0]) return 0;
  if (voltage >= c[c.length - 1][0]) return c[c.length - 1][1];
  for (let i = 0; i < c.length - 1; i++) {
    const [v1, s1] = c[i];
    const [v2, s2] = c[i + 1];
    if (v1 <= voltage && voltage <= v2) return interpolate(v1, s1, v2, s2, voltage);
  }
  return c[c.length - 1][1];
}

/**
 * Read the curve backward. Mirrors SpeedProfile.speed_to_voltage: a speed of 0
 * is the lighting voltage (0 V when there is none); a speed above the top speed
 * is `maxVoltage`; otherwise it interpolates, taking the lowest voltage of a
 * flat stretch, so any speed above 0 is above the starting voltage.
 */
export function speedToVoltage(
  curve: Curve,
  speedKmh: number,
  maxVoltage: number,
  lightingVoltage: number | null,
  startingVoltage: number,
): number {
  if (speedKmh <= 0) return lightingVoltage ?? 0;
  const c = readableCurve(curve, startingVoltage);
  if (c.length < 2) return maxVoltage;
  if (speedKmh > c[c.length - 1][1]) return maxVoltage;
  for (let i = 0; i < c.length - 1; i++) {
    const [v1, s1] = c[i];
    const [v2, s2] = c[i + 1];
    if (s1 < speedKmh && speedKmh <= s2) {
      if (s2 === s1) return v1;
      return interpolate(s1, v1, s2, v2, speedKmh);
    }
  }
  return c[c.length - 1][0];
}

/** Power percent to speed. Mirrors SpeedProfile.power_percent_to_speed: 0% is
 *  0 km/h; above 0 it is the top speed times the percent. */
export function powerPercentToSpeed(curve: Curve, percent: number): number {
  if (percent <= 0) return 0;
  return topSpeed(curve) * Math.min(percent, 100) / 100;
}

/** Speed to power percent. Mirrors SpeedProfile.speed_to_power_percent. */
export function speedToPowerPercent(curve: Curve, speedKmh: number): number {
  if (speedKmh <= 0) return 0;
  const top = topSpeed(curve);
  if (top <= 0) return 100;
  return Math.max(0, Math.min(100, 100 * speedKmh / top));
}

/** Power percent to voltage, through speed. Mirrors SpeedProfile.power_percent_to_voltage. */
export function powerPercentToVoltage(
  curve: Curve,
  percent: number,
  maxVoltage: number,
  lightingVoltage: number | null,
  startingVoltage: number,
): number {
  return speedToVoltage(curve, powerPercentToSpeed(curve, percent), maxVoltage, lightingVoltage, startingVoltage);
}

/**
 * Every condition the curve breaks, as plain reasons; empty when it is valid.
 * Mirrors speed_profile.curve_problems, so the panel can say why a save would
 * be refused before sending it.
 */
export function curveProblems(curve: Curve | null | undefined, maxVoltage: number): string[] {
  if (!curve || curve.length < 2) return ['the curve needs at least two points'];
  const problems: string[] = [];
  const voltages = curve.map(([v]) => v);
  const speeds = curve.map(([, s]) => s);
  if (voltages.some((v, i) => i > 0 && v <= voltages[i - 1])) {
    problems.push('the points must be in order of rising voltage');
  }
  const [firstV, firstS] = curve[0];
  if (firstV <= 0 || firstS <= 0) {
    problems.push("the first point's voltage and speed must be above 0");
  }
  if (Math.abs(voltages[voltages.length - 1] - maxVoltage) > 1e-6) {
    problems.push(`the last point must be at max_voltage (${maxVoltage} V)`);
  }
  if (speeds.some((s, i) => i > 0 && s < speeds[i - 1])) {
    problems.push('speed must not fall as voltage rises');
  }
  if (speeds[speeds.length - 1] <= firstS) {
    problems.push("the top speed must be above the first point's speed");
  }
  return problems;
}

/** Why a lighting voltage would be refused, or null when it is fine. `null`
 *  (no lighting voltage) is always fine. Mirrors the service's check:
 *  design_new.md "SpeedProfile", A lighting voltage stays below the starting
 *  voltage. */
export function lightingVoltageProblem(
  lightingVoltage: number | null,
  startingVoltage: number,
): string | null {
  if (lightingVoltage == null) return null;
  if (!Number.isFinite(lightingVoltage)) return 'the lighting voltage must be a number';
  if (lightingVoltage < 0) return 'the lighting voltage must not be below 0 V';
  if (lightingVoltage >= startingVoltage) {
    return `the lighting voltage must be below the starting voltage (${startingVoltage} V)`;
  }
  return null;
}

/** Why a starting voltage would be refused, or null when it is fine
 *  (design_new.md "SpeedProfile", A starting voltage stays below `max_voltage`). */
export function startingVoltageProblem(
  startingVoltage: number,
  maxVoltage: number,
  lightingVoltage: number | null,
): string | null {
  if (!Number.isFinite(startingVoltage)) return 'the starting voltage must be a number';
  if (startingVoltage < 0) return 'the starting voltage must not be below 0 V';
  if (startingVoltage >= maxVoltage) return `the starting voltage must be below the max voltage (${maxVoltage} V)`;
  if (lightingVoltage != null && lightingVoltage >= startingVoltage) {
    return `the starting voltage must be above the lighting voltage (${lightingVoltage} V)`;
  }
  return null;
}

/** Why an ATO minimum voltage would be refused, or null when it is fine
 *  (design_new.md "SpeedProfile", An ATO minimum voltage stays on the measured
 *  part of the curve). `null` (unset) is always fine. */
export function atoMinimumVoltageProblem(
  atoMinVoltage: number | null,
  curve: Curve,
  startingVoltage: number,
  maxVoltage: number,
): string | null {
  if (atoMinVoltage == null) return null;
  if (!Number.isFinite(atoMinVoltage)) return 'the ATO minimum voltage must be a number';
  const low = firstPointVoltage(curve, startingVoltage, maxVoltage);
  if (atoMinVoltage < low - 1e-9 || atoMinVoltage > maxVoltage + 1e-9) {
    return `the ATO minimum voltage must lie from ${low} V (the curve's first point above the starting voltage) up to ${maxVoltage} V`;
  }
  return null;
}
