/**
 * Which fields a Speed Profile panel save sends.
 *
 * design_new.md "SpeedProfile", Editing a profile by hand: a save writes only
 * the fields the operator changed, to the one profile on screen. Every record
 * is complete, so a set and a train's instance profile take the same fields.
 * `calibrated_at` and the ids are never edited by hand, and a set's name is
 * edited on the Locomotives screen's card, not here.
 */

/** Never sent by a panel save. */
const NOT_HAND_EDITED = new Set([
  'profile_id',
  'model_id',
  'set_name',
  'parent_profile_id',
  'calibrated_at',
  'kind',
  'parent',
  'model_name',
]);

/** Whether the panel may send `field`. */
export function handEditable(field: string): boolean {
  return !NOT_HAND_EDITED.has(field);
}

/** The fields of `current` that differ from `saved` and may be hand-edited. */
export function changedProfileFields(
  saved: Record<string, unknown> | null | undefined,
  current: Record<string, unknown>
): Record<string, unknown> {
  const changed: Record<string, unknown> = {};
  for (const [field, value] of Object.entries(current)) {
    if (!handEditable(field)) continue;
    if (JSON.stringify(value) !== JSON.stringify(saved?.[field])) changed[field] = value;
  }
  return changed;
}
