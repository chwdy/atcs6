import { describe, expect, it } from 'vitest';

import { sessionControls, sessionHint, sessionLabel } from './session-controls';

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
