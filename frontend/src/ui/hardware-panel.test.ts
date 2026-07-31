import { describe, expect, it } from 'vitest';

import {
  addressGroups,
  coverageRows,
  devRows,
  parseAddr,
  parseDeadband,
  staleBindings,
} from './hardware-panel';
import type { Coverage } from './hardware-panel';

// devRows() is the pure row model behind the develop-mode "Fake inputs" section:
// it joins the config's occupancy / current binding maps to the latest device
// read-back. No DOM is needed (this suite has none).

describe('devRows', () => {
  it('is empty when nothing is bound', () => {
    expect(devRows({ block_occupancy: {}, block_occupancy_current: {} }, null)).toEqual([]);
  });

  it('lists the union of digital and current bindings, sorted', () => {
    const rows = devRows(
      {
        block_occupancy: { B2: { chip_id: 'in1', port: 1 }, B1: { chip_id: 'in1', port: 0 } },
        block_occupancy_current: { B3: { chip_id: 'cur1', port: 0 } },
      },
      null
    );
    expect(rows.map((r) => r.blockId)).toEqual(['B1', 'B2', 'B3']);
    expect(rows[0]).toMatchObject({ blockId: 'B1', hasDigital: true, hasCurrent: false });
    expect(rows[2]).toMatchObject({ blockId: 'B3', hasDigital: false, hasCurrent: true });
  });

  it('flags a block that has both a digital and a current binding', () => {
    const rows = devRows(
      {
        block_occupancy: { B1: { chip_id: 'in1', port: 0 } },
        block_occupancy_current: { B1: { chip_id: 'cur1', port: 0 } },
      },
      null
    );
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ hasDigital: true, hasCurrent: true });
  });

  it('merges the device read-back into each row', () => {
    const rows = devRows(
      { block_occupancy: { B1: { chip_id: 'in1', port: 0 } }, block_occupancy_current: {} },
      { blocks: { B1: { occupancy_bit: true, current_ma: 22.5, volts: 8.0, occupied: true } } }
    );
    expect(rows[0]).toMatchObject({
      occupancyBit: true,
      currentMa: 22.5,
      volts: 8.0,
      occupied: true,
    });
  });

  it('leaves read-back fields null when a block is absent from the read-back', () => {
    const rows = devRows(
      { block_occupancy: { B9: { chip_id: 'in1', port: 3 } }, block_occupancy_current: {} },
      { blocks: {} }
    );
    expect(rows[0]).toMatchObject({
      occupancyBit: null,
      currentMa: null,
      volts: null,
      occupied: null,
    });
  });

  it('tolerates undefined binding maps', () => {
    expect(devRows({} as any, null)).toEqual([]);
  });
});

// addressGroups() is the option model behind every I²C address dropdown. The
// rule it encodes: once the bus has been scanned, what answered IS the list —
// the chip kind's strap range never caps it.

// An INA3221's nominal A0 straps: 4 addresses topping out at 0x43.
const INA3221_STRAPS = [0x40, 0x41, 0x42, 0x43];

describe('addressGroups', () => {
  it('offers only the answering addresses once a scan has run', () => {
    const groups = addressGroups(INA3221_STRAPS, [0x41, 0x45], null);
    expect(groups).toEqual([{ label: 'Answering on the bus', addrs: [0x41, 0x45] }]);
  });

  it('offers an answering address above the chip kind strap range', () => {
    const groups = addressGroups(INA3221_STRAPS, [0x45, 0x4f, 0x70], null);
    expect(groups.flatMap((g) => g.addrs)).toContain(0x45);
    expect(groups.flatMap((g) => g.addrs)).toContain(0x70);
  });

  it('drops strap addresses nothing answered on', () => {
    const groups = addressGroups(INA3221_STRAPS, [0x45], null);
    expect(groups.flatMap((g) => g.addrs)).not.toContain(0x40);
  });

  it('keeps the configured address when the bus does not answer there', () => {
    const groups = addressGroups(INA3221_STRAPS, [0x45], 0x41);
    expect(groups).toEqual([
      { label: 'Answering on the bus', addrs: [0x45] },
      { label: 'Configured, not answering', addrs: [0x41] },
    ]);
  });

  it('does not repeat the configured address when it answered', () => {
    const groups = addressGroups(INA3221_STRAPS, [0x41, 0x45], 0x41);
    expect(groups).toHaveLength(1);
    expect(groups[0].addrs).toEqual([0x41, 0x45]);
  });

  it('sorts and de-duplicates what the scan returned', () => {
    const groups = addressGroups(INA3221_STRAPS, [0x45, 0x41, 0x45], null);
    expect(groups[0].addrs).toEqual([0x41, 0x45]);
  });

  it('leads with the strap range but does not stop there when unscanned', () => {
    const groups = addressGroups(INA3221_STRAPS, [], null);
    expect(groups[0]).toEqual({ label: 'Typical straps for this chip', addrs: INA3221_STRAPS });
    expect(groups[1].addrs).toContain(0x45);
    expect(groups[1].addrs).not.toContain(0x40);
  });

  it('spans the legal 7-bit peripheral range and no further when unscanned', () => {
    const all = addressGroups(INA3221_STRAPS, [], null).flatMap((g) => g.addrs).sort((a, b) => a - b);
    expect(all[0]).toBe(0x03);
    expect(all[all.length - 1]).toBe(0x77);
    expect(all).toHaveLength(0x77 - 0x03 + 1);
  });

  it('offers the whole range for a kind with no strap list', () => {
    const groups = addressGroups([], [], null);
    expect(groups).toHaveLength(1);
    expect(groups[0].label).toBe('I²C address');
  });
});

describe('parseAddr', () => {
  it('reads hex and decimal, and rejects blanks', () => {
    expect(parseAddr('0x45')).toBe(0x45);
    expect(parseAddr(' 0X4F ')).toBe(0x4f);
    expect(parseAddr('65')).toBe(65);
    expect(parseAddr('')).toBeNaN();
    expect(parseAddr('   ')).toBeNaN();
  });
});

describe('parseDeadband', () => {
  it('reads a number and keeps it', () => {
    expect(parseDeadband('1.5')).toBe(1.5);
    expect(parseDeadband(' 0.4 ')).toBe(0.4);
    expect(parseDeadband('0')).toBe(0);
  });

  it('falls back to the default rather than 0 on a blank or junk field', () => {
    // 0 would silently mean "any commanded voltage counts as driven", which is
    // a behavior change nobody asked for by clearing a box.
    expect(parseDeadband('')).toBe(0.25);
    expect(parseDeadband('   ')).toBe(0.25);
    expect(parseDeadband('loud')).toBe(0.25);
  });

  it('clamps into the range the server accepts', () => {
    expect(parseDeadband('-3')).toBe(0);
    expect(parseDeadband('99')).toBeLessThan(5);
  });
});


// coverageRows() is the pure row model behind stage 3: how much of the loaded
// layout is bound to a port at all. Distinct from chip readiness — a board can
// be perfectly proven with half the blocks bound to nothing, and the config
// validator passes that cleanly because it only checks bindings that exist.

const COVERAGE: Coverage = {
  enabled: true,
  blocks: {
    total: 3,
    occupancy: { bound: ['B1'], unbound: ['B2', 'B3'] },
    track: { bound: ['B1', 'B2', 'B3'], unbound: [] },
  },
  switches: { total: 2, bound: ['SW_A'], unbound: ['SW_B'] },
  stale: { block_occupancy: ['B_GONE'], switch_output: [] },
  complete: false,
};

describe('coverageRows', () => {
  it('is empty with no report', () => {
    expect(coverageRows(null)).toEqual([]);
  });

  it('counts each binding kind against the layout total', () => {
    const rows = coverageRows(COVERAGE);
    expect(rows.map((r) => [r.key, r.bound, r.total])).toEqual([
      ['occupancy', 1, 3],
      ['track', 3, 3],
      ['switches', 1, 2],
    ]);
  });

  it('names the unwired entities so they can be found and wired', () => {
    const rows = coverageRows(COVERAGE);
    expect(rows[0].missing).toEqual(['B2', 'B3']);
    expect(rows[1].missing).toEqual([]);
    expect(rows[2].missing).toEqual(['SW_B']);
  });

  it('says what breaks for each kind, since none of it announces itself', () => {
    for (const row of coverageRows(COVERAGE)) {
      expect(row.consequence.length).toBeGreaterThan(0);
    }
  });
});

describe('staleBindings', () => {
  it('is empty with no report', () => {
    expect(staleBindings(null)).toEqual([]);
  });

  it('flattens every map, de-duplicated — one entity can be stale in two', () => {
    expect(staleBindings(COVERAGE)).toEqual(['B_GONE']);
    expect(
      staleBindings({
        ...COVERAGE,
        stale: { block_occupancy: ['B_GONE'], block_track: ['B_GONE', 'B_OLD'] },
      })
    ).toEqual(['B_GONE', 'B_OLD']);
  });
});
