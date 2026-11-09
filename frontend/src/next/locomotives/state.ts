/**
 * What the Locomotives screen reads, as Preact signals (see next/state.ts for
 * why signals). `updateFromNetwork` copies it out of each network snapshot,
 * so the screen runs no timer of its own for it.
 */
import { signal } from '@preact/signals';

import type { CalibrationGroupSummary } from '../../editor/editor-speed-profiles';
import {
  modelsFromNetwork,
  profilesFromNetwork,
  trainsFromNetwork,
  type ModelSummary,
  type ProfileSummary,
  type Selection,
  type TrainSummary,
} from './model';

/** Every train the layout holds, placed or not. */
export const trains = signal<TrainSummary[]>([]);

/** Every speed profile: the Default profile, the sets, and each train's own. */
export const profiles = signal<ProfileSummary[]>([]);

/** Every locomotive model. */
export const models = signal<ModelSummary[]>([]);

/** What the left list has picked. */
export const selection = signal<Selection>(null);

/** What the Speed Profile panel's calibration group shows, or null while no
 *  profile is open. Written by the panel through its host. */
export const calibration = signal<CalibrationGroupSummary | null>(null);

/** Write `next` only when it differs, so a poll that changes nothing re-draws nothing. */
function setIfChanged<T>(target: { peek(): T; value: T }, next: T): void {
  if (JSON.stringify(target.peek()) !== JSON.stringify(next)) target.value = next;
}

export function updateFromNetwork(network: any): void {
  setIfChanged(trains, trainsFromNetwork(network));
  setIfChanged(profiles, profilesFromNetwork(network));
  setIfChanged(models, modelsFromNetwork(network));
}

export function setCalibration(summary: CalibrationGroupSummary | null): void {
  setIfChanged(calibration, summary);
}
