/**
 * What the header shows and offers for the session state.
 *
 * The server derives the session state — Setup, Live, or Halted with a reason —
 * and serves it as `session_state` on the network snapshot (design_v2.md
 * "Session state"). This file only turns that value into a label
 * and decides which session controls the header offers; it never derives the
 * state itself.
 */

export interface SessionStateView {
  value: 'setup' | 'live' | 'halted' | string;
  halt_reason: string | null;
}

/** The operating environments' names, as the header and Hardware Setup show
 *  them (design_v2.md "Operating environment and activation"). */
export const ENVIRONMENT_LABELS: Record<string, string> = {
  virtual_hardware: 'Virtual Hardware',
  real_hardware: 'Real Hardware',
};

/** The name of an operating environment, or '' before the first snapshot. */
export function environmentLabel(environment: string | null | undefined): string {
  if (!environment) return '';
  return ENVIRONMENT_LABELS[environment] ?? environment;
}

const HALT_LABELS: Record<string, string> = {
  hardware_failure: 'hardware failure',
  not_activated: 'not activated',
};

/** The header label: "Live", "Setup", or "Halted — <reason>". */
export function sessionLabel(state: SessionStateView | null | undefined): string {
  if (!state) return '—';
  switch (state.value) {
    case 'live':
      return 'Live';
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
  if (state.value === 'setup') return 'Building and testing the rig. Press Activate to go live.';
  switch (state.halt_reason) {
    case 'hardware_failure':
      return 'Repair the device, then press Activate.';
    case 'not_activated':
      return 'Press Activate to go live.';
    default:
      return '';
  }
}

export interface SessionControls {
  /** Activate appears in the header when activating is the way to Live: from
   *  Setup, and from a halt for `not_activated` or `hardware_failure`. */
  activateVisible: boolean;
}

/** Which session controls the header offers for this state. */
export function sessionControls(state: SessionStateView | null | undefined): SessionControls {
  const value = state?.value;
  return {
    activateVisible:
      value === 'setup' ||
      (value === 'halted' &&
        (state?.halt_reason === 'not_activated' || state?.halt_reason === 'hardware_failure')),
  };
}

/**
 * Whether the Setup tools that drive outputs may run: the commissioning
 * session, the track direction sweep, the occupancy bench, and a port-test
 * output pulse are accepted only in Setup (design_v2.md "Setting up the
 * hardware and going live", step 4).
 */
export function setupToolsAllowed(state: SessionStateView | null | undefined): boolean {
  return state?.value === 'setup';
}

/** Why a Setup tool is unavailable outside Setup. */
export const SETUP_TOOL_HINT = 'Runs only in Setup. Enter Setup from Hardware Setup first.';

/**
 * Why Activate cannot be pressed right now, or null when it can. Activate is
 * refused while the session is Live (design_v2.md "Session state", the
 * transition table).
 */
export function activateUnavailableReason(
  state: SessionStateView | null | undefined
): string | null {
  if (state?.value === 'live') return 'The session is Live. Enter Setup before activating.';
  return null;
}
