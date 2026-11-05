/**
 * The decisions behind the Speed Profile panel's calibration group, kept free
 * of the DOM so they can be tested on their own (design_new.md "Speed
 * calibration", The panel, and "Calibrating a locomotive").
 *
 * The group names one locomotive in its locomotive row, and the speed pass
 * drives that train; the pass never picks a train for itself. These helpers
 * answer which trains the row lists, which of the group's controls may be
 * pressed, what the train's own stored values are when the profile on screen
 * is the model profile its instance derives from, and what the pass's progress,
 * points and crossings read as.
 */

import type { Curve, CurvePoint } from './speed-profile-curve';

/** What the calibration group is showing for the named train. */
export type CalibrationRunView =
  | 'none' // no pass open: the Calibrate speed control
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
    // Opening the setup only names the measured block; whether the pass may
    // open depends on that block, so the readiness check gates Start instead.
    speedCalibrate: trainNamed && view === 'none',
    speedStart: trainNamed && view === 'speed-setup' && ready && stretchResolved,
  };
}

/** Whether the readiness line should be asked for at all: only while no pass is open. */
export function readinessApplies(view: CalibrationRunView): boolean {
  return view === 'none' || view === 'speed-setup';
}

/**
 * What the group shows for a train it has just been told about, from the
 * pass read: an open speed pass, or a finished pass waiting for a commit. A
 * pass left open by a page reload is shown so it can be ended, rather than
 * left to block a new pass with nothing on screen.
 */
export function viewForOpenRun(speedRun: any): CalibrationRunView {
  if (speedRun?.open && speedRun.phase === 'measured') return 'speed-held';
  if (speedRun?.open) return 'speed-running';
  return 'none';
}

/** A voltage as the panel shows it: at most two decimals, no trailing zeros. */
export function volts(v: number): string {
  return String(Number(Number(v).toFixed(2)));
}

/**
 * The measured points as a curve: each step that passed, at the voltage it
 * drove, in voltage order. The first point is the starting voltage and the
 * starting speed. A step that failed gives no point.
 */
export function measuredCurve(samples: any[]): Curve {
  const points: CurvePoint[] = (samples || []).map(
    (s: any) => [Number(s.track_voltage), Number(s.speed_kmh)] as CurvePoint
  );
  points.sort((a, b) => a[0] - b[0]);
  return points;
}

/** One line of the pass's point list: a step that passed, or one that failed. */
export interface PointRow {
  voltage: number;
  /** The step's speed; null for a step that failed and gave no point. */
  speed: number | null;
  /** The lowest step that passed: the starting voltage. */
  starting: boolean;
}

/**
 * Every step the pass has finished, passed or failed, in voltage order, so
 * the operator sees where the search for the starting voltage narrowed.
 */
export function pointRows(run: any): PointRow[] {
  const measured = measuredCurve(run?.samples || []);
  const rows: PointRow[] = measured.map(([voltage, speed], i) => ({ voltage, speed, starting: i === 0 }));
  for (const v of run?.failed_voltages || []) {
    rows.push({ voltage: Number(v), speed: null, starting: false });
  }
  return rows.sort((a, b) => a.voltage - b.voltage);
}

/** One line of the speed pass's crossing list (design_new.md "Speed
 *  calibration", The panel lists every crossing). */
export interface CrossingRow {
  /** The voltage of the step the crossing belongs to. */
  voltage: number;
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
  dropped: 'dropped — its step failed',
  failed_step: 'failed its step — the train did not cross the measured block in time',
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
    const key = `${c.track_voltage}/${c.attempt}`;
    const firstOfStep = key !== lastKey;
    lastKey = key;
    const toward = String(c.toward_block_id || '');
    return {
      voltage: Number(c.track_voltage),
      toward: names[toward] || toward,
      seconds: c.seconds == null ? '—' : Number(c.seconds).toFixed(2),
      speed: c.speed_kmh == null ? '—' : Number(c.speed_kmh).toFixed(1),
      note: CROSSING_NOTES[c.outcome] ?? String(c.outcome || ''),
      used: c.outcome === 'used',
      firstOfStep,
    };
  });
}

/** Plain words for which part of the sweep a step belongs to. */
const STEP_KIND_WORDS: Record<string, string> = {
  coarse: 'coarse step',
  narrowing: 'narrowing in on the starting voltage',
  filled: 'in-between step, where the speed jumps',
};

/**
 * The pass's progress line: the outer block it is moving the train into before
 * the first crossing (design_new.md "Speed calibration", The pass first moves
 * the train into an outer block), that it is getting the train ready again
 * after a failed step, the step being measured, or, once held, how many points
 * it measured and the starting voltage. Blocks are named by name when the run
 * carries one.
 */
export function speedPassProgress(run: any, held: boolean): string {
  const names: Record<string, string> = run?.block_names || {};
  const named = (id: string) => names[id] || id;
  if (held) {
    const curve = measuredCurve(run.samples || []);
    const start = curve.length
      ? ` Starting voltage ${volts(curve[0][0])} V, at ${curve[0][1].toFixed(1)} km/h.`
      : '';
    return `Measured ${curve.length} points across ${named(run.measured_block_id)}.${start}`;
  }
  if (run.phase === 'moving') {
    return run.move_target_block_id
      ? `Moving the train into ${named(run.move_target_block_id)} before the first crossing`
      : 'Finding the way into the nearer outer block';
  }
  if (run.phase === 'resetting') {
    return 'A step failed — getting the train ready for the next step';
  }
  const kind = STEP_KIND_WORDS[run.current_step_kind] || `${run.current_step_kind} step`;
  return (
    `Measuring ${volts(run.current_voltage)} V (${kind}), attempt ${run.attempt} — crossing ` +
    `${Math.min(run.crossings_done + 1, run.crossings_target)} of ${run.crossings_target}`
  );
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
