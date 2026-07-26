import { describe, expect, it } from 'vitest';

import { lineLabel } from './port-picker';

// `pickPorts` itself builds and drives a live popover, which needs a DOM this
// suite does not have (no jsdom is installed). What is testable in isolation —
// and what actually decides whether a user can identify a wire — is how each
// chip's raw lines are named, so that is what is covered here.

describe('line naming', () => {
  const chip = (port_kind: string) => ({ chip_id: 'c', kind: 'k', port_kind } as any);

  it('names PWM lines by channel, so a track pick reads as a real channel', () => {
    // The whole point of the picker: a block's rails are channels, chosen
    // independently, not a "port" standing for a fixed pair.
    const pwm = chip('bipolar_pair');
    expect(lineLabel(pwm, 0)).toBe('ch0');
    expect(lineLabel(pwm, 15)).toBe('ch15');
  });

  it('names shift-register lines by port bit', () => {
    expect(lineLabel(chip('digital_out'), 5)).toBe('P5');
    expect(lineLabel(chip('digital_in'), 0)).toBe('P0');
  });

  it('names current-sense lines 1-based, matching the chip datasheet', () => {
    // Line 0 is the sensor's CH1 — the off-by-one the binding layer also applies.
    const ina = chip('current_sense');
    expect(lineLabel(ina, 0)).toBe('CH1');
    expect(lineLabel(ina, 2)).toBe('CH3');
  });

  it('falls back to a port label for a chip with no port kind', () => {
    expect(lineLabel({ chip_id: 'c', kind: 'k' } as any, 2)).toBe('P2');
  });
});
