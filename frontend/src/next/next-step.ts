/**
 * The one button the top bar's status control offers: the step that gets the
 * session to Live, or keeps it going, from where it is now
 * (docs/ui-layout-rewrite-plan.md §3; design_v2.md "Session state").
 */
import { sessionControls, type SessionStateView } from '../lib/session-controls';

export type NextStep = 'activate';

/** The next step for this session state, or null when there is none to offer
 *  (Live, or before the first snapshot). */
export function nextStep(state: SessionStateView | null | undefined): NextStep | null {
  if (!state) return null;
  const controls = sessionControls(state);
  if (controls.activateVisible) return 'activate';
  return null;
}

/** How each step's button reads. */
export const NEXT_STEP_BUTTON: Record<NextStep, { label: string; icon: string; title: string }> = {
  activate: {
    label: 'Activate',
    icon: 'ti ti-power',
    title: 'Activate the selected environment',
  },
};
