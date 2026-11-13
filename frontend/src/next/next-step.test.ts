import { describe, expect, it } from 'vitest';

import { nextStep } from './next-step';

const state = (value: string, halt_reason: string | null = null) => ({ value, halt_reason });

describe('nextStep', () => {
  it('offers Activate wherever activating is the way to Live', () => {
    expect(nextStep(state('setup'))).toBe('activate');
    expect(nextStep(state('halted', 'not_activated'))).toBe('activate');
    expect(nextStep(state('halted', 'hardware_failure'))).toBe('activate');
  });

  it('offers nothing while Live', () => {
    expect(nextStep(state('live'))).toBeNull();
  });

  it('offers nothing before the first snapshot', () => {
    expect(nextStep(null)).toBeNull();
  });
});
