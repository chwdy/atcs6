/**
 * What the header shows and offers for the session state.
 *
 * The server derives the session state — Setup, Live, Paused, or Halted with a
 * reason — and serves it as `session_state` on the network snapshot
 * (design_new.md "Session state"). This file only turns that value into a label
 * and decides which session controls the header offers; it never derives the
 * state itself.
 */

export interface SessionStateView {
  value: 'setup' | 'live' | 'paused' | 'halted' | string;
  halt_reason: string | null;
}

const HALT_LABELS: Record<string, string> = {
  layout_emergency_cut_out: 'Emergency stop',
  hardware_failure: 'hardware failure',
  runtime_failure: 'track conflict',
  not_activated: 'not activated',
};

/** The header label: "Live", "Paused", "Setup", or "Halted — <reason>". */
export function sessionLabel(state: SessionStateView | null | undefined): string {
  if (!state) return '—';
  switch (state.value) {
    case 'live':
      return 'Live';
    case 'paused':
      return 'Paused';
    case 'setup':
      return 'Setup';
    case 'halted': {
      const reason = state.halt_reason ? HALT_LABELS[state.halt_reason] ?? state.halt_reason : '';
      return reason ? `Halted — ${reason}` : 'Halted';
    }
    default:
      return state.value;
  }
}

/** What to do next in each state, shown as the label's tooltip. */
export function sessionHint(state: SessionStateView | null | undefined): string {
  if (!state) return '';
  if (state.value === 'live') return 'Trains may be driven.';
  if (state.value === 'paused') return 'Time is frozen. Press Play to resume.';
  if (state.value === 'setup') return 'Building and testing the rig. Activate to go live.';
  switch (state.halt_reason) {
    case 'layout_emergency_cut_out':
      return 'Every train is cut out. Press Reset to release them.';
    case 'hardware_failure':
      return 'Repair the device, then activate the environment again.';
    case 'runtime_failure':
      return 'Sort the conflict out on the layout, then clear it.';
    case 'not_activated':
      return 'Activate the environment in Hardware setup to go live.';
    default:
      return '';
  }
}

export interface SessionControls {
  /** Play/Pause and the speed presets exist only in Simulation. */
  playbackVisible: boolean;
  /** Which way the Play/Pause button points, and whether it can be pressed. */
  playPause: 'play' | 'pause';
  playPauseEnabled: boolean;
  /** Reset appears only after Emergency stop. */
  resetVisible: boolean;
}

/** Which session controls the header offers for this state and environment. */
export function sessionControls(
  state: SessionStateView | null | undefined,
  activeEnvironment: string | null | undefined
): SessionControls {
  const value = state?.value;
  const inSimulation = (activeEnvironment || 'simulation') === 'simulation';
  return {
    playbackVisible: inSimulation,
    playPause: value === 'live' ? 'pause' : 'play',
    playPauseEnabled: inSimulation && (value === 'live' || value === 'paused'),
    resetVisible: value === 'halted' && state?.halt_reason === 'layout_emergency_cut_out',
  };
}
