/**
 * Which step the Hardware Setup screen shows, and how other parts of the
 * ?ui=next layout open it (docs/ui-layout-rewrite-plan.md §7).
 */
import { signal } from '@preact/signals';

import { screen } from '../state';
import type { StepId } from './steps';

/** The step the screen shows. Null shows the first step that is not done,
 *  and the screen keeps that step once the setup has loaded. */
export const hardwareStep = signal<StepId | null>(null);

/**
 * Show the Hardware Setup screen. With a `step`, show that step (the top
 * bar's session-state chip opens step 1). Without one, coming from another
 * screen shows the first step that is not done, and a click on the tab that
 * is already showing keeps the step on screen.
 */
export function showHardwareSetup(step: StepId | null = null): void {
  if (step !== null || screen.peek() !== 'hardware') hardwareStep.value = step;
  screen.value = 'hardware';
}
