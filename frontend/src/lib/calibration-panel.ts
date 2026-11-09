/**
 * The decisions behind the Speed Profile panel's calibration group, kept free
 * of the DOM so they can be tested on their own (design_new.md "Speed
 * calibration", The panel, and "Calibrating a locomotive").
 *
 * The group names one locomotive in its locomotive row, and the speed pass
 * drives that train; the pass never picks a train for itself. These helpers
 * answer which trains the row lists, which of the group's controls may be
 * pressed, what the train's own stored values are when the profile on screen
 * is the set its instance was copied from, and what the pass's progress,
 * points and crossings read as.
 */

import type { Curve, CurvePoint } from './speed-profile-curve';

/** What the calibration group is showing for the named train. */
export type CalibrationRunView =
  | 'none' // no run open: the Calibrate speed and Test controls
  | 'speed-setup' // the speed pass is being set up: measured block, both ends, Start
  | 'speed-running' // the speed pass is open: its trials, then the sweep
  | 'speed-held' // the pass measured every step and waits for a commit
  | 'ato-setup' // the ATO minimum test is being set up: measured block, Start
  | 'ato-running'; // the ATO minimum test is open

/**
 * Every block of both ends of a stretch — each outer block, then its run-off
 * blocks — from a stretch preview or an open run's payload (design_new.md
 * "Speed calibration", Each end of the stretch is an outer block and the
 * run-off blocks past it).
 */
export function stretchEndBlocks(stretch: any): string[] {
  const outer: string[] = stretch?.outer_block_ids || [];
  const runoff: string[][] = stretch?.runoff_block_ids || [];
  return outer.flatMap((id, i) => (id ? [id, ...(runoff[i] || [])] : []));
}

/**
 * The stretch line the panel shows before the pass starts: each end's blocks
 * in order out from B with its length, the train length the ends were sized
 * for, and B's measured length.
 */
export function stretchEndsText(stretch: any): string {
  const names: Record<string, string> = stretch?.block_names || {};
  const outer: string[] = stretch?.outer_block_ids || [];
  const runoff: string[][] = stretch?.runoff_block_ids || [];
  const lengths: number[] = stretch?.end_lengths_cm || [];
  const ends = outer.map((id, i) => {
    const blocks = [id, ...(runoff[i] || [])].map((b) => names[b] || b).join(' → ');
    const length = lengths[i];
    return typeof length === 'number' ? `${blocks} (${Math.round(length)} cm)` : blocks;
  });
  const train =
    typeof stretch?.train_length_cm === 'number' && stretch.train_length_cm > 0
      ? ` · sized for a ${Math.round(stretch.train_length_cm)} cm train`
      : '';
  return `Ends: ${ends.join(' ↔ ')}${train} · measured length ${stretch?.length_cm} cm`;
}

/**
 * The trains the locomotive row lists for the profile on screen
 * (design_new.md "Speed calibration", The panel): the train that owns it when
 * it is an instance profile, and every train whose instance profile was copied
 * from it when it is a set or the Default profile. Sorted by name so the list
 * reads the same each time.
 */
export function calibrationTrainChoices(trains: any[], profiles: any[], profileId: number | string): any[] {
  const parentOf = new Map<number, number | null>();
  for (const p of profiles || []) parentOf.set(Number(p?.profile_id), p?.parent_profile_id ?? null);
  const onScreen = Number(profileId);
  return (trains || [])
    .filter((t: any) => {
      const own = t?.speed_profile_id;
      if (own == null) return false;
      return Number(own) === onScreen || parentOf.get(Number(own)) === onScreen;
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
 * A field as the named train runs on it: the value on the train's own
 * instance profile, or the profile on screen's when that record is not in the
 * list yet.
 */
export function trainStoredValue(
  train: any,
  profiles: any[],
  profileOnScreen: Record<string, any>,
  field: string
): any {
  const named = train?.speed_profile_id;
  if (named != null && Number(named) !== Number(profileOnScreen.profile_id)) {
    const own = (profiles || []).find((p: any) => Number(p?.profile_id) === Number(named));
    if (own && field in own) return own[field];
  }
  return profileOnScreen[field];
}

/** Which of the group's controls may be pressed. */
export interface CalibrationControls {
  /** Calibrate speed, which opens the pass setup. */
  speedCalibrate: boolean;
  /** Start in the pass setup. */
  speedStart: boolean;
  /** Test in the ATO minimum group, which opens the test setup. */
  atoTest: boolean;
  /** Start in the test setup. */
  atoStart: boolean;
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
    atoTest: trainNamed && view === 'none',
    atoStart: trainNamed && view === 'ato-setup' && ready && stretchResolved,
  };
}

/** Whether a setup view is showing: the measured-block picker and Start. */
export function isSetupView(view: CalibrationRunView): boolean {
  return view === 'speed-setup' || view === 'ato-setup';
}

/** Whether the readiness line should be asked for at all: only while no run is open. */
export function readinessApplies(view: CalibrationRunView): boolean {
  return view === 'none' || isSetupView(view);
}

/**
 * What the group shows for a train it has just been told about, from the
 * pass read: an open speed pass, or a finished pass waiting for a commit. A
 * pass left open by a page reload is shown so it can be ended, rather than
 * left to block a new pass with nothing on screen.
 */
export function viewForOpenRun(speedRun: any): CalibrationRunView {
  // A run that is stopping the train has already been ended by the operator.
  if (speedRun?.ending) return 'none';
  if (speedRun?.open && speedRun.kind === 'ato_minimum_test') return 'ato-running';
  if (speedRun?.open && speedRun.phase === 'measured') return 'speed-held';
  if (speedRun?.open) return 'speed-running';
  return 'none';
}

/** Which trial a speed pass is in, or null once it is sweeping (design_new.md
 *  "Speed calibration", The pass starts with two trial stages). */
export function trialStage(run: any): 'lighting' | 'starting' | null {
  if (run?.kind === 'ato_minimum_test') return null;
  if (run?.pass_stage === 'lighting_trial') return 'lighting';
  if (run?.pass_stage === 'starting_trial') return 'starting';
  return null;
}

/** What the operator does in the trial now running. The starting trial
 *  names the lighting voltage Keep held, which the starting voltage must be
 *  above (design_new.md "SpeedProfile", A lighting voltage stays below the
 *  starting voltage). */
export function trialInstruction(stage: 'lighting' | 'starting', heldLighting: number | null = null): string {
  if (stage === 'lighting') {
    return 'Lighting voltage: raise or lower the voltage until the lamps light and the train stands still, then Keep, which saves it. A locomotive whose lamps light only once it moves gets No lamps.';
  }
  const kept =
    heldLighting == null
      ? 'No lighting voltage was kept (no lamps).'
      : `The lighting voltage you kept is ${volts(heldLighting)} V; the starting voltage must be above it.`;
  return `Starting voltage: raise the voltage until the train just moves, then It moves. ${kept} It moves saves the starting voltage and starts the sweep.`;
}

/** Why It moves would be refused at this trial voltage, or null when it may
 *  be pressed. */
export function itMovesProblem(trialVoltage: number, heldLighting: number | null): string | null {
  if (heldLighting != null && trialVoltage <= heldLighting) {
    return `The starting voltage (${volts(trialVoltage)} V) must be above the lighting voltage you kept (${volts(heldLighting)} V). Raise the voltage until the train just moves, then press It moves.`;
  }
  return null;
}

/** The ATO minimum test's progress line (design_new.md "Speed calibration",
 *  ATO minimum test): the voltage, a stall, and how close it is to steady. */
export function atoTestProgress(run: any): string {
  const names: Record<string, string> = run?.block_names || {};
  const named = (id: string) => names[id] || id;
  if (run.phase === 'moving') {
    return run.move_target_block_id
      ? `Moving the train into ${named(run.move_target_block_id)} before the first crossing`
      : 'Finding the way into the nearer outer block';
  }
  if (run.ending) return 'Stopping the train, then releasing it.';
  const at = `Testing ${volts(run.current_voltage)} V`;
  if (run.leg_stalled) return `${at} — the train stalled. Raise the voltage to get it moving again.`;
  const target = Number(run.steady_round_trips_target) || 0;
  if (run.is_steady) return `${at} — steady: ${run.steady_round_trips} round trips in a row with no stall.`;
  return `${at} — ${run.steady_round_trips} of ${target} round trips in a row with no stall.`;
}

/** A voltage as the panel shows it: at most two decimals, no trailing zeros. */
export function volts(v: number): string {
  return String(Number(Number(v).toFixed(2)));
}

/**
 * The measured points as a curve: each step that passed, at the voltage it
 * drove, in voltage order. The first point is the lowest voltage that ran
 * every crossing. A step that failed gives no point.
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
  /** The lowest step that passed: the curve's first point. */
  first: boolean;
}

/**
 * Every step the pass has finished, passed or failed, in voltage order, so
 * the operator sees where the search for the lowest voltage that runs every
 * crossing narrowed.
 */
export function pointRows(run: any): PointRow[] {
  const measured = measuredCurve(run?.samples || []);
  const rows: PointRow[] = measured.map(([voltage, speed], i) => ({ voltage, speed, first: i === 0 }));
  for (const v of run?.failed_voltages || []) {
    rows.push({ voltage: Number(v), speed: null, first: false });
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
  not_needed: 'not needed — this direction already had its crossings',
  failed_step: 'failed its step — the train did not cross the measured block in time',
  stalled: 'stalled — the train did not cross in time at this voltage',
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
      used: c.outcome === 'used' || c.outcome === 'stalled',
      firstOfStep,
    };
  });
}

/** Plain words for which part of the sweep a step belongs to. */
const STEP_KIND_WORDS: Record<string, string> = {
  coarse: 'coarse step',
  narrowing: 'narrowing in on the lowest voltage that runs every crossing',
  filled: 'in-between step, where the speed jumps',
};

/**
 * The pass's progress line: the outer block it is moving the train into before
 * the first crossing (design_new.md "Speed calibration", The pass first moves
 * the train into an outer block), that it is getting the train ready again
 * after a failed step, the trial running, the step being measured, or, once
 * held, how many points it measured and the lowest voltage that ran every
 * crossing. Blocks are named by name when the run carries one.
 */
export function speedPassProgress(run: any, held: boolean): string {
  const names: Record<string, string> = run?.block_names || {};
  const named = (id: string) => names[id] || id;
  if (held) {
    const curve = measuredCurve(run.samples || []);
    const start = curve.length
      ? ` The lowest voltage that ran every crossing is ${volts(curve[0][0])} V, at ${curve[0][1].toFixed(1)} km/h.`
      : '';
    return `Measured ${curve.length} points across ${named(run.measured_block_id)}.${start}`;
  }
  if (run.phase === 'moving') {
    return run.move_target_block_id
      ? `Moving the train into ${named(run.move_target_block_id)} before the first crossing`
      : 'Finding the way into the nearer outer block';
  }
  if (run.ending) return 'Stopping the train, then releasing it.';
  const trial = trialStage(run);
  if (trial) {
    return `${trial === 'lighting' ? 'Lighting trial' : 'Starting trial'}: driving ${volts(run.current_voltage)} V`;
  }
  if (run.phase === 'resetting') {
    return 'Getting the train ready for the next step';
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
 * failed says why, and nothing was written. A run that ended by writing — an
 * ATO minimum test's Commit — says what it wrote.
 */
export function speedPassEndedMessage(
  lastResult: { success?: boolean; reason?: string | null } | null | undefined
): string {
  const reason = String(lastResult?.reason || '').trim().replace(/[.\s]+$/, '');
  const sentence = reason.charAt(0).toUpperCase() + reason.slice(1);
  if (reason && lastResult?.success) return `${sentence}.`;
  if (!reason) return 'The pass ended. Nothing was written.';
  return `The pass ended: ${sentence}. Nothing was written.`;
}
