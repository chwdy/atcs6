/**
 * Which speed profiles a train's Speed profile dropdown offers, and how a
 * train's profile is named.
 *
 * design_new.md "SpeedProfile", Models, sets and instance profiles: a train
 * is given the Default profile or a set, and the server copies it into the
 * train's own instance profile. So a dropdown offers the Default profile and
 * every set, grouped by model, and never another train's instance profile.
 * A screen names a train's profile by where it came from: the model's name
 * and the set's name (A train shows where its profile came from).
 */

export interface ProfileChoice {
  /** The profile number, as a string for an `<option value>`. */
  id: string;
  label: string;
}

const DEFAULT_ID = 0;

function byModelThenSet(models: Map<number, string>) {
  return (a: any, b: any) =>
    (models.get(a.model_id) || '').localeCompare(models.get(b.model_id) || '', undefined, { numeric: true }) ||
    String(a.set_name || '').localeCompare(String(b.set_name || ''), undefined, { numeric: true });
}

function modelNames(models: any[]): Map<number, string> {
  return new Map((models || []).map((m: any) => [Number(m.model_id), String(m.name || '')]));
}

/** A set as a screen names it: `model · set`. */
export function setLabel(set: any, models: any[]): string {
  const name = modelNames(models).get(Number(set?.model_id)) || 'No model';
  return `${name} · ${set?.set_name || '—'}`;
}

/**
 * The Default profile, then every set grouped by model and set name. With
 * `includeDefault` false the Default profile is left out, for a form whose own
 * first option already stands for it.
 */
export function profileChoices(profiles: any[], models: any[], includeDefault = true): ProfileChoice[] {
  const names = modelNames(models);
  const sets = (profiles || []).filter((p: any) => p?.model_id != null).sort(byModelThenSet(names));
  const choices: ProfileChoice[] = includeDefault ? [{ id: String(DEFAULT_ID), label: 'Default' }] : [];
  for (const set of sets) choices.push({ id: String(set.profile_id), label: setLabel(set, models) });
  return choices;
}

/**
 * Where a train's instance profile came from (design_new.md "SpeedProfile",
 * A train shows where its profile came from): `model · set`, `Default`,
 * `Set deleted` when the parent names no set, or `Scene profile` when it has
 * no parent.
 */
export function profileSourceLabel(instance: any, profiles: any[], models: any[]): string {
  const parentId = instance?.parent_profile_id;
  if (parentId == null) return 'Scene profile';
  if (Number(parentId) === DEFAULT_ID) return 'Default';
  const parent = (profiles || []).find((p: any) => Number(p?.profile_id) === Number(parentId));
  if (!parent || parent.model_id == null) return 'Set deleted';
  return setLabel(parent, models);
}
