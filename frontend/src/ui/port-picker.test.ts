import { describe, expect, it } from 'vitest';

import { chipDisplayName, lineLabel } from './port-picker';

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

describe('chip naming', () => {
  it('carries the I²C address, so two of the same part are distinguishable', () => {
    // A rig with two ina219s reads "ina219" twice over; the address is the only
    // thing on the row that says which one a binding points at.
    const a = { chip_id: 'c0', kind: 'ina219', i2c_address: 0x40 } as any;
    const b = { chip_id: 'c1', kind: 'ina219', i2c_address: 0x41 } as any;
    expect(chipDisplayName(a)).toBe('ina219 0x40');
    expect(chipDisplayName(b)).toBe('ina219 0x41');
  });

  it('addresses a labelled chip too — a label is no guarantee of uniqueness', () => {
    expect(chipDisplayName({ chip_id: 'c', kind: 'ina219', label: 'Yard sense', i2c_address: 0x44 } as any))
      .toBe('Yard sense 0x44');
  });

  it('names an SPI chip by its latch line, the coordinate it has instead', () => {
    expect(chipDisplayName({ chip_id: 'c', kind: 'hc595', bus: 'spi', spi_select: 'cs0' } as any))
      .toBe('hc595 CS0');
  });

  it('leaves a chip with no coordinate on its bare name', () => {
    // The bus is deliberately not a fallback: every chip on it shares it.
    expect(chipDisplayName({ chip_id: 'c', kind: 'hc595', bus: 'gpio' } as any)).toBe('hc595');
  });
});
