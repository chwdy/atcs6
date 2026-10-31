import { describe, expect, it } from 'vitest';

import { nextStep } from './next-step';

const state = (value: string, halt_reason: string | null = null) => ({ value, halt_reason });

describe('nextStep', () => {
  it('offers Activate wherever activating is the way to Live', () => {
    expect(nextStep(state('setup'), 'real_hardware')).toBe('activate');
    expect(nextStep(state('halted', 'not_activated'), 'real_hardware')).toBe('activate');
    expect(nextStep(state('halted', 'hardware_failure'), 'virtual_hardware')).toBe('activate');
  });

  it('offers Play while Paused and Pause while Live, in Simulation only', () => {
    expect(nextStep(state('paused'), 'simulation')).toBe('play');
    expect(nextStep(state('live'), 'simulation')).toBe('pause');
    expect(nextStep(state('live'), 'real_hardware')).toBeNull();
  });

  it('offers the way out of each halt', () => {
    expect(nextStep(state('halted', 'layout_emergency_cut_out'), 'simulation')).toBe('reset');
    expect(nextStep(state('halted', 'runtime_failure'), 'real_hardware')).toBe('clear-conflict');
  });

  it('offers nothing before the first snapshot', () => {
    expect(nextStep(null, null)).toBeNull();
  });
});
