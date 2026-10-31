import { describe, expect, it } from 'vitest';

import {
  THROW_GAP_MS,
  listText,
  runRigTest,
  type BlockCheckAnswer,
  type RigTestCalls,
  type SavedSetup,
} from './rig-test';

/** Two wired blocks on one hc165, two switches on one hc595, and two I²C
 *  chips. */
function setup(): SavedSetup {
  return {
    chips: [
      { chip_id: 'pwm1', kind: 'pca9685', bus: 'i2c', i2c_address: 0x40 },
      { chip_id: 'ina1', kind: 'ina3221', bus: 'i2c', i2c_address: 0x41 },
      { chip_id: 'in1', kind: 'hc165', bus: 'spi' },
      { chip_id: 'out1', kind: 'hc595', bus: 'spi' },
    ],
    block_track: { B1: {}, B2: {} },
    block_occupancy: { B1: { chip_id: 'in1', port: 2 }, B2: { chip_id: 'in1', port: 7 } },
    switch_output: { S1: { chip_id: 'out1' }, S2: { chip_id: 'out1' } },
  };
}

const WIRED: BlockCheckAnswer = { ok: true, verdict: 'wired', digital: 'followed_drive', current: 'none', others: [] };

/** Fake calls that answer like a healthy rig, recording each call. Pass
 *  overrides to make one step misbehave. */
function fakeCalls(overrides: Partial<RigTestCalls> = {}, saved: SavedSetup = setup()) {
  const log: string[] = [];
  const calls: RigTestCalls = {
    readSetup: async () => ({ setup: saved, switchPositions: { S1: 0, S2: 1 } }),
    selfTest: async () => {
      log.push('scan');
      return { reachable: true, answered: ['0x40', '0x41'] };
    },
    openBench: async () => {
      log.push('open');
      return { ok: true };
    },
    checkBlock: async (blockId) => {
      log.push(`check ${blockId}`);
      return WIRED;
    },
    closeBench: async () => {
      log.push('close');
    },
    throwSwitch: async (switchId, position) => {
      log.push(`throw ${switchId} ${position}`);
      return { ok: true };
    },
    releaseSwitches: async () => {
      log.push('release');
    },
    pause: async (ms) => {
      log.push(`pause ${ms}`);
    },
    progress: () => {},
    ...overrides,
  };
  return { calls, log };
}

describe('runRigTest', () => {
  it('runs self-test, then the block checks inside one bench, then each switch out and back', async () => {
    const { calls, log } = fakeCalls();
    await runRigTest(calls);
    expect(log).toEqual([
      'scan',
      'open',
      'check B1',
      'check B2',
      'close',
      // S1 starts on main, so it goes to diverge and back; S2 the other way.
      'throw S1 1',
      `pause ${THROW_GAP_MS}`,
      'throw S1 0',
      'throw S2 0',
      `pause ${THROW_GAP_MS}`,
      'throw S2 1',
      'release',
    ]);
  });

  it('proves the I²C chips by the scan and the hc165 by its bits, and leaves the hc595 to the operator', async () => {
    const { calls } = fakeCalls();
    const report = await runRigTest(calls);
    expect(report.failed).toEqual([]);
    expect(report.proved).toContain('pwm1 answered at 0x40.');
    expect(report.proved).toContain('B1 and B2: each bit followed the drive.');
    expect(report.proved).toContain('in1: every bit followed the drive (2 blocks).');
    expect(report.chipNotes).toEqual([
      { chip_id: 'in1', result: 'proven' },
      { chip_id: 'out1', result: 'ran' },
    ]);
    expect(report.needsYou.some((t) => t.startsWith('out1: S1 and S2 were thrown'))).toBe(true);
    // Nothing stood on either block, so neither current channel was checked.
    expect(report.needsYou.some((t) => t.startsWith('B1 and B2: nothing stood on them'))).toBe(true);
  });

  it('keeps going after a failed step, and a crossed block takes the hc165 proof back', async () => {
    const { calls, log } = fakeCalls({
      selfTest: async () => ({ reachable: true, answered: ['0x40'] }),
      checkBlock: async (blockId) =>
        blockId === 'B1'
          ? { ok: true, verdict: 'crossed', digital: 'followed_drive', current: 'none', others: [{ block_id: 'B2', changed: ['digital'] }] }
          : WIRED,
    });
    const report = await runRigTest(calls);
    expect(report.failed).toEqual([
      'ina1 did not answer at 0x41.',
      "B1: driving it also changed B2's bit, so a wire is on the wrong block's rails.",
    ]);
    expect(report.chipNotes[0]).toEqual({ chip_id: 'in1', result: 'failed', reason: report.failed[1] });
    expect(log).toContain('throw S2 1');
  });

  it('lists a block whose current channel read a load on its own line', async () => {
    const { calls } = fakeCalls({
      checkBlock: async (blockId) => (blockId === 'B2' ? { ...WIRED, current: 'load' } : WIRED),
    });
    const report = await runRigTest(calls);
    expect(report.proved).toContain('B2: its bit followed the drive, and its current channel read a load.');
    expect(report.proved).toContain('B1: its bit followed the drive.');
    expect(report.needsYou.some((t) => t.startsWith('B1: nothing stood on it, so its current channel was'))).toBe(true);
  });

  it('names the bit to check when it never answered', async () => {
    const { calls } = fakeCalls({
      checkBlock: async (blockId) => (blockId === 'B2' ? { ok: true, verdict: 'no_response', digital: 'no_response', current: 'none', others: [] } : WIRED),
    });
    const report = await runRigTest(calls);
    expect(report.failed).toEqual([
      "B2: its bit did not change while the block was driven. Check the wire from the block's detector to in1 bit 7.",
    ]);
    expect(report.chipNotes[0].result).toBe('failed');
  });

  it('reports a bench that did not open and still throws the switches', async () => {
    const { calls, log } = fakeCalls({ openBench: async () => ({ ok: false, error: 'controller busy' }) });
    const report = await runRigTest(calls);
    expect(report.failed).toEqual(['The occupancy bench did not open: controller busy, so no block was checked.']);
    expect(log).not.toContain('check B1');
    expect(log).not.toContain('close');
    // Not checked is not failed: the hc165 gets no note at all.
    expect(report.chipNotes.map((n) => n.chip_id)).toEqual(['out1']);
    expect(log).toContain('throw S1 1');
  });

  it('turns a request that threw into a line in the summary', async () => {
    const { calls } = fakeCalls({
      selfTest: async () => {
        throw new Error('network down');
      },
      checkBlock: async () => {
        throw new Error('409 bench not open');
      },
    });
    const report = await runRigTest(calls);
    expect(report.failed[0]).toBe('The bus scan failed: network down.');
    expect(report.failed[1]).toBe('B1: the check failed: 409 bench not open.');
  });

  it('stops a switch at its first failed throw and takes the hc595 proof back', async () => {
    const { calls, log } = fakeCalls({
      throwSwitch: async (switchId, position) => {
        log.push(`throw ${switchId} ${position}`);
        return switchId === 'S1' ? { ok: false, error: 'coil open' } : { ok: true };
      },
    });
    const report = await runRigTest(calls);
    expect(log.filter((l) => l.startsWith('throw S1'))).toEqual(['throw S1 1']);
    expect(log).toContain('throw S2 1');
    expect(report.failed).toEqual(['S1: the throw to diverge failed: coil open.']);
    expect(report.chipNotes).toContainEqual({ chip_id: 'out1', result: 'failed', reason: report.failed[0] });
  });

  it('cannot prove an hc165 with a bit on a block that has no rails', async () => {
    const saved = setup();
    delete saved.block_track.B2;
    const { calls } = fakeCalls({}, saved);
    const report = await runRigTest(calls);
    expect(report.needsYou).toContain('in1: B2 has no rail binding, so its bit could not be checked.');
    expect(report.chipNotes.find((n) => n.chip_id === 'in1')).toBeUndefined();
  });

  it('skips a bound switch the layout does not have', async () => {
    const { calls, log } = fakeCalls({
      readSetup: async () => ({ setup: setup(), switchPositions: { S2: 0 } }),
    });
    const report = await runRigTest(calls);
    expect(log.some((l) => l.startsWith('throw S1'))).toBe(false);
    expect(report.needsYou).toContain('S1: it is bound but not in the loaded layout, so it was not thrown.');
  });

  it('stops with one line when the saved setup cannot be read', async () => {
    const { calls, log } = fakeCalls({
      readSetup: async () => {
        throw new Error('server gone');
      },
    });
    const report = await runRigTest(calls);
    expect(report.failed).toEqual(['Could not read the saved setup: server gone.']);
    expect(log).toEqual([]);
  });
});

describe('listText', () => {
  it('joins with commas and a final "and"', () => {
    expect(listText([])).toBe('');
    expect(listText(['A'])).toBe('A');
    expect(listText(['A', 'B'])).toBe('A and B');
    expect(listText(['A', 'B', 'C'])).toBe('A, B and C');
  });
});
