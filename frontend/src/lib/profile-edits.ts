/**
 * Which fields a Speed Profile panel save sends.
 *
 * design_new.md "SpeedProfile", Editing a profile by hand: a save writes only
 * the fields the operator changed, so a locomotive's own profile (an instance
 * profile) goes on reading every field it has not set itself from its model
 * profile. Three fields are never edited by hand, and on an instance profile
 * only its name, its lighting voltage and its voltage/speed curve may be.
 */

/** Never edited by hand: only a speed calibration commit writes
 *  `calibrated_at`, and whether a record is a model or an instance profile is
 *  not an edit. */
const NOT_HAND_EDITED = new Set(['profile_id', 'derived_from', 'calibrated_at']);

/** What a hand edit may change on an instance profile; every other field
 *  belongs to the model profile it derives from. */
const INSTANCE_HAND_EDITED = new Set(['name', 'lighting_voltage', 'voltage_speed_curve']);

/** Whether the panel may edit `field` on this kind of profile. */
export function handEditable(field: string, isInstance: boolean): boolean {
  if (NOT_HAND_EDITED.has(field)) return false;
  return !isInstance || INSTANCE_HAND_EDITED.has(field);
}

/** The fields of `current` that differ from `saved` and may be hand-edited. */
export function changedProfileFields(
  saved: Record<string, unknown> | null | undefined,
  current: Record<string, unknown>,
  isInstance: boolean
): Record<string, unknown> {
  const changed: Record<string, unknown> = {};
  for (const [field, value] of Object.entries(current)) {
    if (!handEditable(field, isInstance)) continue;
    if (JSON.stringify(value) !== JSON.stringify(saved?.[field])) changed[field] = value;
  }
  return changed;
}
