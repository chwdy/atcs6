/**
 * The one button the top bar's status control offers: the step that gets the
 * session to Live, or keeps it going, from where it is now
 * (docs/ui-layout-rewrite-plan.md §3; design_new.md "Session state").
 */
import { sessionControls, type SessionStateView } from '../lib/session-controls';

export type NextStep = 'activate' | 'play' | 'pause' | 'reset' | 'clear-conflict';

/** The next step for this session state and active environment, or null when
 *  there is none to offer (Live on hardware, or before the first snapshot). */
export function nextStep(
  state: SessionStateView | null | undefined,
  environment: string | null | undefined
): NextStep | null {
  if (!state) return null;
  const controls = sessionControls(state, environment);
  if (controls.activateVisible) return 'activate';
  if (controls.resetVisible) return 'reset';
  if (state.value === 'halted' && state.halt_reason === 'runtime_failure') return 'clear-conflict';
  if (controls.playPauseEnabled) return controls.playPause;
  return null;
}

/** How each step's button reads. */
export const NEXT_STEP_BUTTON: Record<NextStep, { label: string; icon: string; title: string }> = {
  activate: {
    label: 'Activate',
    icon: 'ti ti-power',
    title: 'Activate the selected environment',
  },
  play: { label: 'Play', icon: 'ti ti-player-play', title: 'Resume the simulation' },
  pause: { label: 'Pause', icon: 'ti ti-player-pause', title: 'Freeze the simulation' },
  reset: {
    label: 'Reset',
    icon: 'ti ti-rotate',
    title: 'Release every train after Emergency stop',
  },
  'clear-conflict': {
    label: 'Clear conflict',
    icon: 'ti ti-eraser',
    title: 'Clear the fatal track conflict once it is sorted out on the layout',
  },
};
