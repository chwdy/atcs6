import { describe, expect, it } from 'vitest';

import { devRows } from './hardware-panel';

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
