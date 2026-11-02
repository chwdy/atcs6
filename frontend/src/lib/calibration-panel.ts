/**
 * The decisions behind the Speed Profile panel's calibration group, kept free
 * of the DOM so they can be tested on their own (design_new.md "Speed
 * calibration", The panel, and "Lighting-voltage calibration", The panel).
 *
 * The group names one locomotive in its locomotive row, and both runs drive
 * that train; neither run picks a train for itself. These helpers answer which
 * trains the row lists, which of the group's controls may be pressed, and what
 * the train's own stored values are when the profile on screen is the model
 * profile its instance derives from.
 */

import type { Curve, CurvePoint } from './speed-profile-curve';

/** What the calibration group is showing for the named train. */
export type CalibrationRunView =
  | 'none' // no run open: the two Calibrate controls
  | 'lighting' // a lighting run is open on its row
  | 'speed-setup' // the speed pass is being set up: measured block, outer blocks, Start
  | 'speed-running' // the speed pass is open and measuring
  | 'speed-held'; // the pass measured every step and waits for a commit

/**
 * Every placed train whose resolved profile is the profile on screen: each
 * train naming it directly, and each train naming an instance profile that
 * derives from it. Sorted by car number so the list reads the same each time.
 */
export function calibrationTrainChoices(trains: any[], profiles: any[], profileId: string): any[] {
  const derivedFrom = new Map<string, string | null>();
  for (const p of profiles || []) derivedFrom.set(String(p?.profile_id), p?.derived_from ?? null);
  return (trains || [])
    .filter((t: any) => {
      const named = t?.speed_profile_id;
      if (!named) return false;
      return named === profileId || derivedFrom.get(String(named)) === profileId;
    })
    .sort((a: any, b: any) =>
      trainName(a).localeCompare(trainName(b), undefined, { numeric: true })
    );
}

/** The train's car number, or its id when it has none. */
export function trainName(train: any): string {
  return String(train?.car_number || train?.id || '');
}

/** One row of the locomotive picker: car number and state (left off until it is known). */
export function trainChoiceLabel(train: any, status: string | null): string {
  return status ? `${trainName(train)} · ${status}` : trainName(train);
}

/**
 * A field as the named train runs on it. Every train the row lists names the
 * profile on screen or an instance profile deriving from it, so the value is
 * the instance's own when it carries one and the profile on screen's
 * otherwise — `SpeedProfileRegistry.resolve` for that one field.
 */
export function trainStoredValue(
  train: any,
  profiles: any[],
  profileOnScreen: Record<string, any>,
  field: string
): any {
  const named = train?.speed_profile_id;
  if (named && named !== profileOnScreen.profile_id) {
    const own = (profiles || []).find((p: any) => p?.profile_id === named);
    if (own?.[field] != null) return own[field];
  }
  return profileOnScreen[field];
}

/** Which of the group's controls may be pressed. */
export interface CalibrationControls {
  /** Calibrate on the lighting-voltage row. */
  lightingCalibrate: boolean;
  /** Calibrate speed, which opens the pass setup. */
  speedCalibrate: boolean;
  /** Start in the pass setup. */
  speedStart: boolean;
}

export function calibrationControls(input: {
  trainNamed: boolean;
  ready: boolean;
  view: CalibrationRunView;
  stretchResolved: boolean;
}): CalibrationControls {
  const { trainNamed, ready, view, stretchResolved } = input;
  return {
    lightingCalibrate: trainNamed && view === 'none' && ready,
    // Opening the setup only names the measured block; whether the pass may
    // open depends on that block, so the readiness check gates Start instead.
    speedCalibrate: trainNamed && view === 'none',
    speedStart: trainNamed && view === 'speed-setup' && ready && stretchResolved,
  };
}

/**
 * Which readiness check the line asks for. While the pass setup is open it is
 * the speed check, which adds the condition that the train stands in one of
 * the measured block's two outer blocks; otherwise it is the conditions both
 * runs share.
 */
export function readinessKind(view: CalibrationRunView): 'speed' | 'lighting' {
  return view === 'speed-setup' ? 'speed' : 'lighting';
}

/** Whether the readiness line should be asked for at all: only while no run is open. */
export function readinessApplies(view: CalibrationRunView): boolean {
  return view === 'none' || view === 'speed-setup';
}

/**
 * What the group shows for a train it has just been told about, from the two
 * run reads: an open lighting run, an open speed pass, or a finished pass
 * waiting for a commit. A run left open by a page reload is shown so it can be
 * ended, rather than left to block a new run with nothing on screen.
 */
export function viewForOpenRuns(speedRun: any, lightingRun: any): CalibrationRunView {
  if (lightingRun?.open) return 'lighting';
  if (speedRun?.open && speedRun.phase === 'measured') return 'speed-held';
  if (speedRun?.open) return 'speed-running';
  return 'none';
}

/**
 * The measured power/speed points as a curve: the `(0%, 0 km/h)` origin, which
 * the pass never measures because `0%` power is the lighting voltage, then
 * each measured step in power order.
 */
export function measuredCurve(samples: any[]): Curve {
  const points: CurvePoint[] = (samples || []).map(
    (s: any) => [Number(s.power_percent), Number(s.speed_kmh)] as CurvePoint
  );
  points.sort((a, b) => a[0] - b[0]);
  return [[0, 0], ...points];
}

/** One line of the speed pass's crossing list (design_new.md "Speed
 *  calibration", The panel lists every crossing). */
export interface CrossingRow {
  power: number;
  toward: string;
  seconds: string;
  speed: string;
  /** Plain words for what happened to the crossing; empty when it was used. */
  note: string;
  /** Whether the crossing counts in its step's point. */
  used: boolean;
  /** Whether this row starts a new step (or a re-measure of one). */
  firstOfStep: boolean;
}

const CROSSING_NOTES: Record<string, string> = {
  used: '',
  discarded: 'discarded — the ATC limit lowered the drive',
  replaced: 'replaced — the step was measured again',
  dropped: 'dropped — a later crossing of this step stalled',
  ended_sweep: 'stalled — this and every lower step read 0 km/h',
};

/**
 * The pass's crossings in the order they ran, each with the outer block it ran
 * toward (by name when the run carries one), its time, its speed, and what
 * happened to it.
 */
export function crossingRows(run: any): CrossingRow[] {
  const names: Record<string, string> = run?.block_names || {};
  let lastKey = '';
  return (run?.crossings || []).map((c: any) => {
    const key = `${c.power_percent}/${c.attempt}`;
    const firstOfStep = key !== lastKey;
    lastKey = key;
    const toward = String(c.toward_block_id || '');
    return {
      power: Number(c.power_percent),
      toward: names[toward] || toward,
      seconds: c.seconds == null ? '—' : Number(c.seconds).toFixed(2),
      speed: c.speed_kmh == null ? '—' : Number(c.speed_kmh).toFixed(1),
      note: CROSSING_NOTES[c.outcome] ?? String(c.outcome || ''),
      used: c.outcome === 'used',
      firstOfStep,
    };
  });
}

/** The top of the speed axis that holds both curves, with some headroom. */
export function compareAxisTop(stored: Curve, measured: Curve): number {
  let top = 0;
  for (const [, s] of [...(stored || []), ...(measured || [])]) top = Math.max(top, s);
  return Math.max(30, Math.ceil(top * 1.15));
}

/**
 * What the speed row says once an open pass is gone. The status reply's
 * `last_result` is the pass's own ending, read from the command-result stream
 * (design_new.md "Speed calibration", A leg that overruns the stretch fails the
 * pass: the reason goes out on the command-result stream), so a pass that
 * failed says why. Nothing was written either way: a commit keeps the result
 * on screen until it lands.
 */
export function speedPassEndedMessage(
  lastResult: { success?: boolean; reason?: string | null } | null | undefined
): string {
  const reason = lastResult && !lastResult.success ? String(lastResult.reason || '').trim() : '';
  if (!reason) return 'The pass ended. Nothing was written.';
  const sentence = reason.charAt(0).toUpperCase() + reason.slice(1);
  return `The pass ended: ${sentence.replace(/[.\s]+$/, '')}. Nothing was written.`;
}
