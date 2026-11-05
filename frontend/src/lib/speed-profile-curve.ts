/**
 * Pure speed-profile math — a 1:1 port of the conversion methods on
 * `SpeedProfile` (speed_profile.py). No DOM, no imports. The Speed Profile
 * panel's converter and curve chart use it, so what they show matches the
 * server's arithmetic.
 *
 * A profile stores one curve, `voltage_speed_curve`: [track_voltage, speed_kmh]
 * points, voltage ascending. Its first point is the starting voltage (the
 * lowest voltage at which the locomotive moves steadily) and the starting
 * speed (its speed there). Its last point sits at `max_voltage` and its speed
 * is the top speed. Power percent is not stored: 0% is stopped, and any
 * percent above 0 is a straight line in speed from the starting speed to the
 * top speed (design_new.md "SpeedProfile").
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

/** The lowest voltage at which the locomotive moves steadily: the curve's first point. */
export function startingVoltage(curve: Curve): number {
  return curve.length ? curve[0][0] : 0;
}

/** The locomotive's speed at its starting voltage: the curve's first point. */
export function startingSpeed(curve: Curve): number {
  return curve.length ? curve[0][1] : 0;
}

/** The speed of the curve's last point, at `max_voltage`. */
export function topSpeed(curve: Curve): number {
  return curve.length ? curve[curve.length - 1][1] : 0;
}

/** Read the curve forward. Mirrors SpeedProfile.voltage_to_speed: below the
 *  starting voltage is 0 km/h; between points interpolates; at or above the
 *  last point reads the top speed. */
export function voltageToSpeed(curve: Curve, voltage: number): number {
  if (!curve.length) return 0;
  if (voltage < curve[0][0]) return 0;
  if (voltage >= curve[curve.length - 1][0]) return curve[curve.length - 1][1];
  for (let i = 0; i < curve.length - 1; i++) {
    const [v1, s1] = curve[i];
    const [v2, s2] = curve[i + 1];
    if (v1 <= voltage && voltage <= v2) return interpolate(v1, s1, v2, s2, voltage);
  }
  return curve[curve.length - 1][1];
}

/**
 * Read the curve backward. Mirrors SpeedProfile.speed_to_voltage: a speed of 0
 * is the lighting voltage (0 V when there is none); a speed up to the starting
 * speed is the starting voltage; a speed above the top speed is `maxVoltage`;
 * otherwise it interpolates, taking the lowest voltage of a flat stretch.
 */
export function speedToVoltage(
  curve: Curve,
  speedKmh: number,
  maxVoltage: number,
  lightingVoltage: number | null,
): number {
  if (speedKmh <= 0) return lightingVoltage ?? 0;
  if (!curve.length) return 0;
  if (speedKmh <= curve[0][1]) return curve[0][0];
  if (speedKmh > curve[curve.length - 1][1]) return maxVoltage;
  for (let i = 0; i < curve.length - 1; i++) {
    const [v1, s1] = curve[i];
    const [v2, s2] = curve[i + 1];
    if (s1 <= speedKmh && speedKmh <= s2) {
      if (s2 === s1) return v1;
      return interpolate(s1, v1, s2, v2, speedKmh);
    }
  }
  return curve[curve.length - 1][0];
}

/** Power percent to speed. Mirrors SpeedProfile.power_percent_to_speed: 0% is
 *  0 km/h; above 0 it runs straight from the starting speed to the top speed. */
export function powerPercentToSpeed(curve: Curve, percent: number): number {
  if (percent <= 0) return 0;
  const p = Math.min(percent, 100);
  const start = startingSpeed(curve);
  return start + (topSpeed(curve) - start) * p / 100;
}

/** Speed to power percent. Mirrors SpeedProfile.speed_to_power_percent. */
export function speedToPowerPercent(curve: Curve, speedKmh: number): number {
  if (speedKmh <= 0) return 0;
  const start = startingSpeed(curve);
  const span = topSpeed(curve) - start;
  if (span <= 0) return 100;
  return Math.max(0, Math.min(100, 100 * (speedKmh - start) / span));
}

/** Power percent to voltage, through speed. Mirrors SpeedProfile.power_percent_to_voltage. */
export function powerPercentToVoltage(
  curve: Curve,
  percent: number,
  maxVoltage: number,
  lightingVoltage: number | null,
): number {
  return speedToVoltage(curve, powerPercentToSpeed(curve, percent), maxVoltage, lightingVoltage);
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
    problems.push('the first point (the starting voltage and speed) must be above 0');
  }
  if (Math.abs(voltages[voltages.length - 1] - maxVoltage) > 1e-6) {
    problems.push(`the last point must be at max_voltage (${maxVoltage} V)`);
  }
  if (speeds.some((s, i) => i > 0 && s < speeds[i - 1])) {
    problems.push('speed must not fall as voltage rises');
  }
  if (speeds[speeds.length - 1] <= firstS) {
    problems.push('the top speed must be above the starting speed');
  }
  return problems;
}

/** Why a lighting voltage would be refused, or null when it is fine. `null`
 *  (no lighting voltage) is always fine. */
export function lightingVoltageProblem(
  lightingVoltage: number | null,
  curve: Curve,
): string | null {
  if (lightingVoltage == null) return null;
  if (!Number.isFinite(lightingVoltage)) return 'the lighting voltage must be a number';
  if (lightingVoltage < 0) return 'the lighting voltage must not be below 0 V';
  if (curve.length && lightingVoltage >= startingVoltage(curve)) {
    return `the lighting voltage must be below the starting voltage (${startingVoltage(curve)} V)`;
  }
  return null;
}
