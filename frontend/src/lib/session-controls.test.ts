import { describe, expect, it } from 'vitest';

import {
  activateUnavailableReason,
  environmentLabel,
  sessionControls,
  sessionHint,
  sessionLabel,
  setupToolsAllowed,
} from './session-controls';

describe('sessionLabel', () => {
  it('names each state, with the halt reason', () => {
    expect(sessionLabel({ value: 'live', halt_reason: null })).toBe('Live');
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
    expect(sessionHint({ value: 'halted', halt_reason: 'hardware_failure' })).toMatch(/Activate/);
    expect(sessionHint({ value: 'halted', halt_reason: 'not_activated' })).toMatch(/Activate/);
  });
});

describe('environmentLabel', () => {
  it('names each operating environment', () => {
    expect(environmentLabel('virtual_hardware')).toBe('Virtual Hardware');
    expect(environmentLabel('real_hardware')).toBe('Real Hardware');
  });

  it('is empty before the first snapshot', () => {
    expect(environmentLabel(null)).toBe('');
  });
});

describe('sessionControls', () => {
  it('offers nothing while Live', () => {
    const c = sessionControls({ value: 'live', halt_reason: null });
    expect(c).toEqual({ resetVisible: false, activateVisible: false });
  });

  it('shows Reset after Emergency stop', () => {
    const c = sessionControls({ value: 'halted', halt_reason: 'layout_emergency_cut_out' });
    expect(c.resetVisible).toBe(true);
    expect(c.activateVisible).toBe(false);
  });

  it('offers Activate in the header only where activating is the way to Live', () => {
    const shows = (value: string, halt_reason: string | null) =>
      sessionControls({ value, halt_reason }).activateVisible;
    expect(shows('setup', null)).toBe(true);
    expect(shows('halted', 'not_activated')).toBe(true);
    expect(shows('halted', 'hardware_failure')).toBe(true);
    expect(shows('live', null)).toBe(false);
    expect(shows('halted', 'runtime_failure')).toBe(false);
    expect(shows('halted', 'layout_emergency_cut_out')).toBe(false);
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

  it('offers Activate from Setup and the other halts', () => {
    expect(activateUnavailableReason({ value: 'setup', halt_reason: null })).toBeNull();
    for (const reason of ['not_activated', 'hardware_failure', 'layout_emergency_cut_out']) {
      expect(activateUnavailableReason({ value: 'halted', halt_reason: reason })).toBeNull();
    }
  });
});
