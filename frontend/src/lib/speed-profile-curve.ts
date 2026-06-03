/**
 * Pure piecewise-linear speed-profile math — a 1:1 port of the conversion
 * methods on `SpeedProfile` (speed_profile.py). No DOM, no imports. Used by the
 * speed-profile editor's converter and live plot previews so they stay
 * byte-consistent with the server's interpolation.
 *
 * Curve conventions (matching the backend):
 *   - voltage_power_curve: [track_voltage, power_percent] pairs, voltage ascending.
 *   - power_speed_curve:   [power_percent, speed_kmh]      pairs, power ascending.
 * Every lookup clamps at the curve endpoints, exactly like the Python methods,
 * and reproduces their guard order (e.g. powerPercentToSpeed returns 0 for a
 * non-positive percent before the lower-endpoint check).
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

/** track_voltage -> power_percent (stage 1). Mirrors SpeedProfile.voltage_to_power. */
export function voltageToPower(vpCurve: Curve, voltage: number): number {
  const curve = vpCurve;
  if (!curve.length) return 0;
  if (voltage <= curve[0][0]) return curve[0][1];
  if (voltage >= curve[curve.length - 1][0]) return curve[curve.length - 1][1];
  for (let i = 0; i < curve.length - 1; i++) {
    const [v1, p1] = curve[i];
    const [v2, p2] = curve[i + 1];
    if (v1 <= voltage && voltage <= v2) return interpolate(v1, p1, v2, p2, voltage);
  }
  return curve[curve.length - 1][1];
}

/** power_percent -> track_voltage (stage 1, inverted). Mirrors SpeedProfile.power_to_voltage. */
export function powerToVoltage(vpCurve: Curve, powerPercent: number): number {
  const curve = vpCurve;
  if (!curve.length) return 0;
  if (powerPercent <= curve[0][1]) return curve[0][0];
  if (powerPercent >= curve[curve.length - 1][1]) return curve[curve.length - 1][0];
  for (let i = 0; i < curve.length - 1; i++) {
    const [v1, p1] = curve[i];
    const [v2, p2] = curve[i + 1];
    if (p1 <= powerPercent && powerPercent <= p2) {
      return interpolate(p1, v1, p2, v2, powerPercent);
    }
  }
  return curve[curve.length - 1][0];
}

/** power_percent -> speed_kmh (stage 2). Mirrors SpeedProfile.power_percent_to_speed. */
export function powerPercentToSpeed(psCurve: Curve, percent: number): number {
  const curve = psCurve;
  if (!curve.length) return 0;
  if (percent <= 0) return 0;
  if (percent <= curve[0][0]) return curve[0][1];
  if (percent >= curve[curve.length - 1][0]) return curve[curve.length - 1][1];
  for (let i = 0; i < curve.length - 1; i++) {
    const [p1, s1] = curve[i];
    const [p2, s2] = curve[i + 1];
    if (p1 <= percent && percent <= p2) return interpolate(p1, s1, p2, s2, percent);
  }
  return curve[curve.length - 1][1];
}

/** speed_kmh -> power_percent (stage 2, inverted). Mirrors SpeedProfile._speed_to_power. */
export function speedToPower(psCurve: Curve, speedKmh: number): number {
  const curve = psCurve;
  if (!curve.length) return 0;
  if (speedKmh <= 0) return 0;
  if (speedKmh <= curve[0][1]) return curve[0][0];
  if (speedKmh >= curve[curve.length - 1][1]) return curve[curve.length - 1][0];
  for (let i = 0; i < curve.length - 1; i++) {
    const [p1, s1] = curve[i];
    const [p2, s2] = curve[i + 1];
    if (s1 <= speedKmh && speedKmh <= s2) return interpolate(s1, p1, s2, p2, speedKmh);
  }
  return curve[curve.length - 1][0];
}

/** speed_kmh -> track_voltage via both stages. Mirrors SpeedProfile.speed_to_voltage_two_stage. */
export function speedToVoltageTwoStage(
  vpCurve: Curve,
  psCurve: Curve,
  speedKmh: number,
): number {
  if (speedKmh <= 0) return 0;
  const power = speedToPower(psCurve, speedKmh);
  return powerToVoltage(vpCurve, power);
}

/** track_voltage -> speed_kmh via both stages. Mirrors SpeedProfile.voltage_to_speed_two_stage. */
export function voltageToSpeedTwoStage(
  vpCurve: Curve,
  psCurve: Curve,
  voltage: number,
): number {
  const power = voltageToPower(vpCurve, voltage);
  return powerPercentToSpeed(psCurve, power);
}

/** Calibrated speed at 100% power = the top of power_speed_curve. */
export function topSpeed(psCurve: Curve): number {
  if (!psCurve.length) return 0;
  return psCurve[psCurve.length - 1][1];
}

/** Top of the usable voltage range (100%-power point) = the top of voltage_power_curve. */
export function maxVoltage(vpCurve: Curve, fallback: number): number {
  if (!vpCurve.length) return fallback;
  return vpCurve[vpCurve.length - 1][0];
}

/** Lighting voltage = 0%-power point = the bottom of voltage_power_curve. */
export function lightingVoltage(vpCurve: Curve, fallback: number): number {
  if (!vpCurve.length) return fallback;
  return vpCurve[0][0];
}
