import { describe, expect, it } from 'vitest';

import {
  activateUnavailableReason,
  sessionControls,
  sessionHint,
  sessionLabel,
  setupToolsAllowed,
} from './session-controls';

describe('sessionLabel', () => {
  it('names each state, with the halt reason', () => {
    expect(sessionLabel({ value: 'live', halt_reason: null })).toBe('Live');
    expect(sessionLabel({ value: 'paused', halt_reason: null })).toBe('Paused');
    expect(sessionLabel({ value: 'setup', halt_reason: null })).toBe('Setup');
    expect(sessionLabel({ value: 'halted', halt_reason: 'layout_emergency_cut_out' })).toBe(
      'Halted — Emergency stop'
    );
    expect(sessionLabel({ value: 'halted', halt_reason: 'not_activated' })).toBe(
      'Halted — not activated'
    );
  });

  it('shows a dash before the first snapshot', () => {
    expect(sessionLabel(null)).toBe('—');
  });
});

describe('sessionHint', () => {
  it('names the way back from each halt', () => {
    expect(sessionHint({ value: 'halted', halt_reason: 'layout_emergency_cut_out' })).toMatch(/Reset/);
    expect(sessionHint({ value: 'halted', halt_reason: 'hardware_failure' })).toMatch(/activate/);
  });
});

describe('sessionControls', () => {
  it('offers Pause while Live in Simulation', () => {
    const c = sessionControls({ value: 'live', halt_reason: null }, 'simulation');
    expect(c).toEqual({
      playbackVisible: true,
      playPause: 'pause',
      playPauseEnabled: true,
      resetVisible: false,
    });
  });

  it('offers Play while Paused', () => {
    const c = sessionControls({ value: 'paused', halt_reason: null }, 'simulation');
    expect(c.playPause).toBe('play');
    expect(c.playPauseEnabled).toBe(true);
  });

  it('hides playback in a hardware environment', () => {
    const c = sessionControls({ value: 'live', halt_reason: null }, 'real_hardware');
    expect(c.playbackVisible).toBe(false);
    expect(c.playPauseEnabled).toBe(false);
  });

  it('disables Play/Pause and shows Reset after Emergency stop', () => {
    const c = sessionControls(
      { value: 'halted', halt_reason: 'layout_emergency_cut_out' },
      'simulation'
    );
    expect(c.playPauseEnabled).toBe(false);
    expect(c.resetVisible).toBe(true);
  });
});

describe('setupToolsAllowed', () => {
  it('allows the output-driving tools only in Setup', () => {
    expect(setupToolsAllowed({ value: 'setup', halt_reason: null })).toBe(true);
    expect(setupToolsAllowed({ value: 'live', halt_reason: null })).toBe(false);
    expect(setupToolsAllowed({ value: 'halted', halt_reason: 'not_activated' })).toBe(false);
    expect(setupToolsAllowed(null)).toBe(false);
  });
});

describe('activateUnavailableReason', () => {
  it('refuses Activate while Live or after a fatal track conflict', () => {
    expect(activateUnavailableReason({ value: 'live', halt_reason: null })).not.toBeNull();
    expect(activateUnavailableReason({ value: 'halted', halt_reason: 'runtime_failure' })).not.toBeNull();
  });

  it('offers Activate from Setup, Paused and the other halts', () => {
    expect(activateUnavailableReason({ value: 'setup', halt_reason: null })).toBeNull();
    expect(activateUnavailableReason({ value: 'paused', halt_reason: null })).toBeNull();
    for (const reason of ['not_activated', 'hardware_failure', 'layout_emergency_cut_out']) {
      expect(activateUnavailableReason({ value: 'halted', halt_reason: reason })).toBeNull();
    }
  });
});
