/**
 * Which speed profiles a train's Speed profile dropdown offers.
 *
 * design_new.md "SpeedProfile", Model profiles and instance profiles: a train
 * names an instance profile and never a model profile, and an instance profile
 * describes one physical locomotive. So a dropdown offers only instance
 * profiles that no train names yet, such as a roster locomotive that is not on
 * the layout. That also hides the blank pair every unidentified train carries:
 * its model profile is a model, and its instance is named by that train.
 */

export interface ProfileChoice {
  id: string;
  label: string;
}

function profileId(p: any): string {
  return String(p?.profile_id ?? p?.id ?? '');
}

/**
 * The instance profiles no train names, plus `currentId` (the profile the train
 * on screen names now) so the dropdown still shows what is selected.
 */
export function pickableProfileChoices(
  profiles: any[],
  trains: any[],
  currentId: string | null = null
): ProfileChoice[] {
  const named = new Set((trains || []).map((t) => String(t?.speed_profile_id ?? '')).filter(Boolean));
  const choices: ProfileChoice[] = [];
  for (const p of profiles || []) {
    const id = profileId(p);
    if (!id) continue;
    const isCurrent = id === currentId;
    if (!isCurrent && (!p?.derived_from || named.has(id))) continue;
    choices.push({ id, label: String(p?.name || id) });
  }
  if (currentId && !choices.some((c) => c.id === currentId)) {
    choices.unshift({ id: currentId, label: currentId });
  }
  return choices;
}
