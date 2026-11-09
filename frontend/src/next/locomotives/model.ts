/**
 * The decisions behind the Locomotives screen (docs/ui-layout-rewrite-plan.md
 * §6), kept free of the DOM so they can be tested on their own.
 *
 * design_new.md "SpeedProfile", Models, sets and instance profiles: the
 * screen lists the trains on the layout, each running on its own instance
 * profile, then the models with their sets, and the fixed Default profile.
 * Picking a train or a set shows its Speed Profile panel, whose calibration
 * group walks the seven steps of design_new.md "Calibrating a locomotive".
 */
import type { CalibrationGroupSummary } from '../../editor/editor-speed-profiles';
import { profileSourceLabel } from '../../lib/profile-choices';

/** What the screen reads about one train from the network snapshot. */
export interface TrainSummary {
  id: string;
  /** The locomotive's name: its `car_number`, or the id when it has none
   *  (design_new.md "Train Entity", A locomotive has one name). */
  name: string;
  /** The `car_number` itself, blank when the train has none. */
  carNumber: string;
  lengthCm: number | null;
  /** The number of the train's own instance profile. */
  speedProfileId: number | null;
  /** The train stands on the layout (its controller has a span). */
  placed: boolean;
  /** `train_status`, upper case as the spec writes it (`OFF`, `MANUAL`, …). */
  status: string;
  stopped: boolean;
}

/** What the screen reads about one speed profile. */
export interface ProfileSummary {
  profile_id: number;
  /** On a set, its model; null on the Default profile and an instance. */
  model_id: number | null;
  set_name: string;
  /** On an instance, the profile it was copied from. */
  parent_profile_id: number | null;
  calibrated_at: string | null;
}

/** A locomotive model (design_new.md "SpeedProfile", `LocomotiveModel`). */
export interface ModelSummary {
  model_id: number;
  name: string;
}

/** What the left list has picked. A pick the Speed Profile panel made itself
 *  carries `fromPanel`, so the screen does not ask the panel to show it again. */
export type Selection =
  | { kind: 'train'; trainId: string }
  | { kind: 'set'; profileId: number; fromPanel?: boolean }
  | { kind: 'model'; modelId: number }
  | { kind: 'default' }
  | null;

/** The fixed Default profile's number. */
export const DEFAULT_PROFILE_ID = 0;

const byText = (a: string, b: string) => a.localeCompare(b, undefined, { numeric: true });

export function trainsFromNetwork(network: any): TrainSummary[] {
  const controllers = network?.controllers_by_train_id || {};
  return ((network?.trains as any[]) || []).map((t) => {
    const c = controllers[t.id] || {};
    return {
      id: String(t.id),
      name: String(t.car_number || t.id),
      carNumber: String(t.car_number ?? ''),
      lengthCm: typeof t.length_cm === 'number' ? t.length_cm : null,
      speedProfileId: t.speed_profile_id == null ? null : Number(t.speed_profile_id),
      placed: c.span != null,
      status: String(c.train_status || 'off').toUpperCase(),
      stopped: c.is_stopped !== false,
    };
  });
}

export function profilesFromNetwork(network: any): ProfileSummary[] {
  const all: Record<string, any> = network?.speed_profiles || {};
  return Object.values(all).map((p: any) => ({
    profile_id: Number(p.profile_id),
    model_id: p.model_id == null ? null : Number(p.model_id),
    set_name: String(p.set_name ?? ''),
    parent_profile_id: p.parent_profile_id == null ? null : Number(p.parent_profile_id),
    calibrated_at: p.calibrated_at ?? null,
  }));
}

export function modelsFromNetwork(network: any): ModelSummary[] {
  return ((network?.locomotive_models as any[]) || []).map((m) => ({
    model_id: Number(m.model_id),
    name: String(m.name ?? ''),
  }));
}

/** The trains on the layout, in name order. */
export function placedTrains(trains: TrainSummary[]): TrainSummary[] {
  return trains.filter((t) => t.placed).sort((a, b) => byText(a.name, b.name));
}

/** The models in name order. */
export function sortedModels(models: ModelSummary[]): ModelSummary[] {
  return [...models].sort((a, b) => byText(a.name, b.name));
}

/** A model's sets in set-name order. */
export function setsOf(modelId: number, profiles: ProfileSummary[]): ProfileSummary[] {
  return profiles.filter((p) => p.model_id === modelId).sort((a, b) => byText(a.set_name, b.set_name));
}

export function profileById(profileId: number | null, profiles: ProfileSummary[]): ProfileSummary | null {
  if (profileId == null) return null;
  return profiles.find((p) => p.profile_id === profileId) ?? null;
}

/** Where a train's profile came from, by name: `model · set`, `Default`,
 *  `Set deleted` or `Scene profile`. */
export function trainSource(train: TrainSummary, profiles: ProfileSummary[], models: ModelSummary[]): string {
  return profileSourceLabel(profileById(train.speedProfileId, profiles), profiles, models);
}

/** When a profile's curve was measured, or null. */
export function measuredAt(profileId: number | null, profiles: ProfileSummary[]): string | null {
  return profileById(profileId, profiles)?.calibrated_at ?? null;
}

/** The trains on the layout whose instance profile was copied from this
 *  profile. */
export function trainsFrom(profileId: number, trains: TrainSummary[], profiles: ProfileSummary[]): TrainSummary[] {
  return placedTrains(trains).filter((t) => profileById(t.speedProfileId, profiles)?.parent_profile_id === profileId);
}

/** The model a set belongs to. */
export function modelOf(set: ProfileSummary | null, models: ModelSummary[]): ModelSummary | null {
  if (!set || set.model_id == null) return null;
  return models.find((m) => m.model_id === set.model_id) ?? null;
}

/**
 * Which profile the Speed Profile panel shows for a pick, and which train its
 * locomotive row names. A train shows its own instance profile; a set shows
 * itself. A model and the Default profile show no panel.
 */
export function panelTarget(
  selection: Selection,
  trains: TrainSummary[]
): { profileId: number | null; trainId: string | null } {
  if (!selection) return { profileId: null, trainId: null };
  if (selection.kind === 'train') {
    const train = trains.find((t) => t.id === selection.trainId);
    return { profileId: train?.speedProfileId ?? null, trainId: train?.id ?? null };
  }
  if (selection.kind === 'set') return { profileId: selection.profileId, trainId: null };
  return { profileId: null, trainId: null };
}

/** The pick that opens a train. */
export function selectionForTrain(trainId: string): Selection {
  return { kind: 'train', trainId };
}

/**
 * The pick for a profile the Speed Profile panel opened on its own. A train's
 * instance profile picks that train; a set picks itself; the Default profile
 * picks the Default row.
 */
export function selectionForProfile(profileId: number, trains: TrainSummary[], profiles: ProfileSummary[]): Selection {
  if (profileId === DEFAULT_PROFILE_ID) return { kind: 'default' };
  const owner = trains.find((t) => t.speedProfileId === profileId);
  if (owner) return { kind: 'train', trainId: owner.id };
  const profile = profileById(profileId, profiles);
  if (profile?.model_id != null) return { kind: 'set', profileId, fromPanel: true };
  return null;
}

// === Calibration steps ===

/** The seven steps of design_new.md "Operator workflows" → "Calibrating a
 *  locomotive", in order. */
export const CALIBRATION_STEPS: ReadonlyArray<{ title: string; hint: string }> = [
  { title: 'Name the locomotive', hint: 'Pick the train in the Locomotive row.' },
  { title: 'Name the measured block and start the speed pass', hint: 'Press Calibrate speed, pick a block on the map or in the list, then Start.' },
  { title: 'Find the lighting voltage', hint: 'Raise or lower the voltage until the lamps light and the train stands still, then Keep, which saves it. A locomotive whose lamps light only once it moves gets No lamps.' },
  { title: 'Find the starting voltage', hint: 'Raise the voltage until the train just moves, then It moves. That saves the starting voltage, above the lighting voltage you kept, and starts the sweep.' },
  { title: 'Let the sweep run', hint: 'The train runs back and forth across the measured block at each voltage step above the starting voltage. Stop ends it and writes nothing.' },
  { title: 'Commit the curve', hint: 'Save to this train, or Promote to keep it as a set too. Nothing is written until you pick one; Discard throws it away.' },
  { title: 'Test and commit the ATO minimum voltage', hint: 'Press Test, and raise or lower the voltage while the train runs back and forth until the speed holds steady, then Commit. You may also type it into the profile.' },
];

export type StepState = 'done' | 'current' | 'todo';

/**
 * Where the operator is in the seven steps. The step the open run belongs to
 * is the current one: the speed pass's setup, its two trials, its sweep, its
 * held result, or the ATO minimum test. With no run open, the next step is
 * the speed pass until one was committed for the named train, then the ATO
 * minimum test; once that is committed every step is done.
 */
export function calibrationSteps(summary: CalibrationGroupSummary | null): StepState[] {
  const states: StepState[] = CALIBRATION_STEPS.map(() => 'todo');
  states[0] = 'current';
  if (!summary?.trainId) return states;
  states[0] = 'done';
  const stageStep: Record<string, number> = { lighting_trial: 2, starting_trial: 3, sweep: 4 };
  let open: number | undefined;
  if (summary.view === 'speed-setup') open = 1;
  else if (summary.view === 'speed-running') open = stageStep[summary.passStage || ''] ?? 2;
  else if (summary.view === 'speed-held') open = 5;
  else if (summary.view === 'ato-setup' || summary.view === 'ato-running') open = 6;
  if (open !== undefined) {
    for (let i = 1; i < open; i++) states[i] = 'done';
    states[open] = 'current';
    return states;
  }
  if (summary.atoCommitted) return states.map(() => 'done');
  if (summary.speedCommitted) {
    for (let i = 1; i < 6; i++) states[i] = 'done';
    states[6] = 'current';
    return states;
  }
  states[1] = 'current';
  return states;
}

/** Whether a calibration run is open, or being set up on the map. The
 *  layout canvas shows only then (plan §6). */
export function calibrationRunShown(summary: CalibrationGroupSummary | null): boolean {
  return !!summary?.trainId && summary.view !== 'none';
}

/**
 * The train commands design_new.md "Calibrating a locomotive", While a run is
 * open, refuses while the train reads `CALIBRATING`. Emergency brake and
 * power off are not in this list: both stay available, and each ends the run.
 */
export const REFUSED_WHILE_CALIBRATING: ReadonlyArray<string> = [
  'Assign a route',
  'Manual / ATO',
  'Start route',
  'Go',
  'Manual submode',
  'Handle and power',
];

/** The reason shown on each refused command. */
export function refusedReason(trainName: string): string {
  return `Refused while ${trainName} reads CALIBRATING. End the pass first with Stop.`;
}

// === Formatting ===

export function formatNumber(n: number | null, unit: string): string {
  if (n == null) return '—';
  const text = Number.isInteger(n) ? String(n) : n.toFixed(1);
  return `${text} ${unit}`;
}

/** A `calibrated_at` stamp, short: date and minutes in UTC. */
export function formatStamp(iso: string | null): string {
  if (!iso) return '';
  return iso.replace('T', ' ').replace(/:\d\d(\.\d+)?(\+00:00|Z)$/, ' UTC');
}
