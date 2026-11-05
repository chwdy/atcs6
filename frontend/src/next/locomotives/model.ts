/**
 * The decisions behind the Locomotives screen (docs/ui-layout-rewrite-plan.md
 * §6), kept free of the DOM so they can be tested on their own.
 *
 * The screen lists the locomotives the operator owns (design_new.md "Train
 * Roster"), the placed trains that came from no roster entry, and the model
 * profiles. Picking one shows its Speed Profile panel, whose calibration
 * group walks the five steps of design_new.md "Calibrating a locomotive".
 */
import type { CalibrationGroupSummary } from '../../editor/editor-speed-profiles';

/** One `RosterEntry` as the server sends it (train_roster.py). */
export interface RosterEntry {
  entry_id: string;
  name: string;
  model: string;
  purchase_date: string | null;
  purchase_price: number | null;
  consist_length_cm: number | null;
  max_speed: number | null;
  min_radius_cm: number | null;
  max_slope: number;
  speed_profile_id: string | null;
}

/** What the screen reads about one train from the network snapshot. */
export interface TrainSummary {
  id: string;
  /** The car number, or the id when it has none. */
  name: string;
  model: string;
  lengthCm: number | null;
  speedProfileId: string | null;
  rosterEntryId: string | null;
  /** The train stands on the layout (its controller has a span). */
  placed: boolean;
  /** `train_status`, upper case as the spec writes it (`OFF`, `MANUAL`, …). */
  status: string;
  stopped: boolean;
}

/** What the screen reads about one speed profile. */
export interface ProfileSummary {
  profile_id: string;
  name: string;
  model: string;
  derived_from: string | null;
  calibrated_at: string | null;
}

/** What the left list has picked. `new` is the Add locomotive form. A pick
 *  the Speed Profile panel made itself (it jumped to another profile) carries
 *  `fromPanel`, so the screen does not ask the panel to show it again. */
export type Selection =
  /** `trainId`: which placed train to name when more than one came from the entry. */
  | { kind: 'roster'; entryId: string; trainId?: string }
  | { kind: 'train'; trainId: string }
  | { kind: 'profile'; profileId: string; fromPanel?: boolean }
  | { kind: 'new' }
  | null;

/** The built-in profile every layout has. The network snapshot leaves it
 *  out (it is code, not layout data), so the list adds it. */
export const DEFAULT_PROFILE_ID = 'default';

const byName = (a: { name: string }, b: { name: string }) =>
  a.name.localeCompare(b.name, undefined, { numeric: true });

export function rosterFromNetwork(network: any): RosterEntry[] {
  const list: any[] = Array.isArray(network?.train_roster) ? network.train_roster : [];
  return list.map((e) => ({
    entry_id: String(e.entry_id),
    name: String(e.name ?? ''),
    model: String(e.model ?? ''),
    purchase_date: e.purchase_date ?? null,
    purchase_price: typeof e.purchase_price === 'number' ? e.purchase_price : null,
    consist_length_cm: typeof e.consist_length_cm === 'number' ? e.consist_length_cm : null,
    max_speed: typeof e.max_speed === 'number' ? e.max_speed : null,
    min_radius_cm: typeof e.min_radius_cm === 'number' ? e.min_radius_cm : null,
    max_slope: Number(e.max_slope) || 0,
    speed_profile_id: e.speed_profile_id ?? null,
  }));
}

export function trainsFromNetwork(network: any): TrainSummary[] {
  const controllers = network?.controllers_by_train_id || {};
  return ((network?.trains as any[]) || []).map((t) => {
    const c = controllers[t.id] || {};
    return {
      id: String(t.id),
      name: String(t.car_number || t.id),
      model: String(t.model ?? ''),
      lengthCm: typeof t.length_cm === 'number' ? t.length_cm : null,
      speedProfileId: t.speed_profile_id ?? null,
      rosterEntryId: t.roster_entry_id ?? null,
      placed: c.span != null,
      status: String(c.train_status || 'off').toUpperCase(),
      stopped: c.is_stopped !== false,
    };
  });
}

export function profilesFromNetwork(network: any): ProfileSummary[] {
  const all: Record<string, any> = network?.speed_profiles || {};
  return Object.values(all).map((p: any) => ({
    profile_id: String(p.profile_id),
    name: String(p.name ?? ''),
    model: String(p.model ?? ''),
    derived_from: p.derived_from ?? null,
    calibrated_at: p.calibrated_at ?? null,
  }));
}

/** The name a roster entry shows: its name, else its model, else its id. */
export function entryName(e: RosterEntry): string {
  return e.name || e.model || e.entry_id;
}

/** The roster entries in name order. */
export function sortedRoster(roster: RosterEntry[]): RosterEntry[] {
  return [...roster].sort((a, b) => byName({ name: entryName(a) }, { name: entryName(b) }));
}

/** The placed train put on the layout from this roster entry, if any. A
 *  roster entry is a template, so more than one may exist; the first by name
 *  is the one the screen names. */
export function placedTrainFor(entry: RosterEntry, trains: TrainSummary[]): TrainSummary | null {
  const placed = trains.filter((t) => t.placed && t.rosterEntryId === entry.entry_id).sort(byName);
  return placed[0] ?? null;
}

/** Placed trains that came from no roster entry still listed: they can be
 *  calibrated too, so the list shows them in their own group. */
export function trainsNotInRoster(trains: TrainSummary[], roster: RosterEntry[]): TrainSummary[] {
  const ids = new Set(roster.map((e) => e.entry_id));
  return trains.filter((t) => t.placed && !(t.rosterEntryId && ids.has(t.rosterEntryId))).sort(byName);
}

/** The model profiles (those deriving from no other), `default` first. */
export function modelProfiles(profiles: ProfileSummary[]): ProfileSummary[] {
  const models = profiles
    .filter((p) => !p.derived_from && p.profile_id !== DEFAULT_PROFILE_ID)
    .sort((a, b) => a.profile_id.localeCompare(b.profile_id, undefined, { numeric: true }));
  const def = profiles.find((p) => p.profile_id === DEFAULT_PROFILE_ID) ?? {
    profile_id: DEFAULT_PROFILE_ID,
    name: 'Default',
    model: '',
    derived_from: null,
    calibrated_at: null,
  };
  return [def, ...models];
}

/**
 * Which profile the Speed Profile panel shows for a pick, and which train its
 * locomotive row names. A roster entry shows the instance profile it names,
 * or, when it names none yet, the one its placed train runs on.
 */
export function panelTarget(
  selection: Selection,
  roster: RosterEntry[],
  trains: TrainSummary[]
): { profileId: string | null; trainId: string | null } {
  if (!selection) return { profileId: null, trainId: null };
  if (selection.kind === 'roster') {
    const entry = roster.find((e) => e.entry_id === selection.entryId);
    if (!entry) return { profileId: null, trainId: null };
    const asked = trains.find((t) => t.placed && t.id === selection.trainId && t.rosterEntryId === entry.entry_id);
    const train = asked ?? placedTrainFor(entry, trains);
    return { profileId: entry.speed_profile_id || train?.speedProfileId || null, trainId: train?.id ?? null };
  }
  if (selection.kind === 'train') {
    const train = trains.find((t) => t.id === selection.trainId);
    return { profileId: train?.speedProfileId ?? null, trainId: train?.id ?? null };
  }
  if (selection.kind === 'profile') return { profileId: selection.profileId, trainId: null };
  return { profileId: null, trainId: null };
}

/** The pick that opens a train's locomotive: its roster entry when it came
 *  from one that is still listed, otherwise the train itself. */
export function selectionForTrain(trainId: string, roster: RosterEntry[], trains: TrainSummary[]): Selection {
  const train = trains.find((t) => t.id === trainId);
  const entryId = train?.rosterEntryId;
  if (entryId && roster.some((e) => e.entry_id === entryId)) return { kind: 'roster', entryId, trainId };
  return { kind: 'train', trainId };
}

/**
 * The pick for a profile the Speed Profile panel opened on its own. A
 * locomotive's own (instance) profile picks the roster entry or the placed
 * train that names it; any other profile is picked as itself.
 */
export function selectionForProfile(profileId: string, roster: RosterEntry[], trains: TrainSummary[]): Selection {
  const entry = roster.find((e) => e.speed_profile_id === profileId);
  if (entry) return { kind: 'roster', entryId: entry.entry_id };
  const train = trains.find((t) => t.placed && t.speedProfileId === profileId);
  if (train) return selectionForTrain(train.id, roster, trains);
  return { kind: 'profile', profileId, fromPanel: true };
}

// === Calibration steps ===

/** The five steps of design_new.md "Operator workflows" → "Calibrating a
 *  locomotive", in order. */
export const CALIBRATION_STEPS: ReadonlyArray<{ title: string; hint: string }> = [
  { title: 'Name the locomotive', hint: 'Pick the train in the Locomotive row.' },
  { title: 'Set the lighting voltage, when the lamps light before it moves', hint: 'Type the voltage into the Lighting voltage field; it must stay below the starting voltage. Leave it empty for a locomotive whose lamps light only once it moves. It can be set before or after the speed pass.' },
  { title: 'Name the measured block and start the speed pass', hint: 'Press Calibrate speed, pick a block on the map or in the list, then Start.' },
  { title: 'Let the pass run', hint: 'The train runs back and forth across the measured block at each voltage step. Stop ends it and writes nothing.' },
  { title: 'Commit the result', hint: 'Save to this locomotive, or Promote to model. Nothing is written until you pick one.' },
];

export type StepState = 'done' | 'current' | 'todo';

/**
 * Where the operator is in the five steps. The step the open pass belongs to
 * is the current one. With no pass open, the next step is the lighting
 * voltage until the named train runs on one, then the speed pass; once a pass
 * is committed every step is done. The lighting voltage is a field, not a
 * run, and a locomotive whose lamps light only once it moves leaves it empty,
 * so opening the speed pass moves on past it either way.
 */
export function calibrationSteps(summary: CalibrationGroupSummary | null): StepState[] {
  const states: StepState[] = ['current', 'todo', 'todo', 'todo', 'todo'];
  if (!summary?.trainId) return states;
  states[0] = 'done';
  if (summary.lightingVoltageSet) states[1] = 'done';
  const runStep: Partial<Record<CalibrationGroupSummary['view'], number>> = {
    'speed-setup': 2,
    'speed-running': 3,
    'speed-held': 4,
  };
  const open = runStep[summary.view];
  if (open !== undefined) {
    for (let i = 2; i < open; i++) states[i] = 'done';
    states[open] = 'current';
    return states;
  }
  if (summary.speedCommitted) return states.map(() => 'done');
  states[summary.lightingVoltageSet ? 2 : 1] = 'current';
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

// === Roster entry form ===

/** The Add / Edit locomotive form's fields, as typed. */
export interface RosterForm {
  name: string;
  model: string;
  purchase_date: string;
  purchase_price: string;
  consist_length_cm: string;
  max_speed: string;
  min_radius_cm: string;
  max_slope: string;
}

export function emptyRosterForm(): RosterForm {
  return {
    name: '',
    model: '',
    purchase_date: '',
    purchase_price: '',
    consist_length_cm: '',
    max_speed: '',
    min_radius_cm: '',
    max_slope: '',
  };
}

const typed = (n: number | null | undefined): string => (n == null ? '' : String(n));

export function rosterFormFrom(entry: RosterEntry): RosterForm {
  return {
    name: entry.name,
    model: entry.model,
    purchase_date: entry.purchase_date ?? '',
    purchase_price: typed(entry.purchase_price),
    consist_length_cm: typed(entry.consist_length_cm),
    max_speed: typed(entry.max_speed),
    min_radius_cm: typed(entry.min_radius_cm),
    max_slope: entry.max_slope ? String(entry.max_slope) : '',
  };
}

/** A number field: blank is "not given" (null); anything else must parse. */
function numberField(text: string, label: string, errors: string[]): number | null {
  const t = text.trim();
  if (!t) return null;
  const n = Number(t);
  if (!Number.isFinite(n) || n < 0) {
    errors.push(`${label} must be a number of 0 or more.`);
    return null;
  }
  return n;
}

/**
 * The body `POST /api/roster` takes for the form, or the errors to show.
 * POST replaces the whole entry, so an edit sends every field and keeps the
 * entry's id and its profile link.
 */
export function rosterPayload(
  form: RosterForm,
  existing: RosterEntry | null
): { body: Record<string, unknown> | null; errors: string[] } {
  const errors: string[] = [];
  if (!form.name.trim() && !form.model.trim()) errors.push('Give the locomotive a name or a model.');
  const body: Record<string, unknown> = {
    name: form.name.trim(),
    model: form.model.trim(),
    purchase_date: form.purchase_date.trim() || null,
    purchase_price: numberField(form.purchase_price, 'Purchase price', errors),
    consist_length_cm: numberField(form.consist_length_cm, 'Length', errors),
    max_speed: numberField(form.max_speed, 'Top speed', errors),
    min_radius_cm: numberField(form.min_radius_cm, 'Minimum curve radius', errors),
    max_slope: numberField(form.max_slope, 'Maximum grade', errors) ?? 0,
  };
  if (existing) {
    body.entry_id = existing.entry_id;
    body.speed_profile_id = existing.speed_profile_id;
  }
  return { body: errors.length ? null : body, errors };
}

// === Formatting ===

export function formatPrice(n: number | null): string {
  return n == null ? '—' : n.toFixed(2);
}

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
