import { describe, it, expect } from 'vitest';
import {
    buildRows,
    changedBlocks,
    describeDebounce,
    describeLink,
    formatReading,
} from './occupancy-monitor';

describe('describeDebounce', () => {
    it('says nothing when the raw read and the verdict already agree', () => {
        const settled = { raw: true, confirmed: true, on_count: 40, off_count: 0 };
        expect(describeDebounce(settled, { on_threshold: 1, off_threshold: 10 })).toBeNull();
    });

    it('counts a block toward clearing while it rides out its off window', () => {
        // The interesting direction in practice: on_threshold is 1 (occupy
        // immediately) while off_threshold rides out dirty-rail dropouts, so
        // "clearing" is the phase an operator actually watches.
        const phase = describeDebounce(
            { raw: false, confirmed: true, on_count: 0, off_count: 4 },
            { on_threshold: 1, off_threshold: 10 }
        );
        expect(phase).toEqual({
            kind: 'clearing',
            label: 'clearing',
            count: 4,
            total: 10,
            fraction: 0.4,
        });
    });

    it('counts a block toward confirming when raw leads the verdict', () => {
        const phase = describeDebounce(
            { raw: true, confirmed: false, on_count: 1, off_count: 0 },
            { on_threshold: 3, off_threshold: 10 }
        );
        expect(phase?.kind).toBe('confirming');
        expect(phase?.count).toBe(1);
        expect(phase?.total).toBe(3);
    });

    it('never divides by a missing threshold', () => {
        const phase = describeDebounce({ raw: false, confirmed: true, off_count: 2 }, {});
        expect(phase?.total).toBe(1);
        expect(phase?.fraction).toBe(1);
    });
});

describe('formatReading', () => {
    it('reports a current channel with the bar it is judged against', () => {
        expect(formatReading({ source: 'current', current_ma: 14.23, threshold_ma: 5, baseline_ma: 0.4 }))
            .toEqual({ reading: '14.2 mA', detail: 'Δ ≥ 5 mA from 0.4' });
    });

    it('reports a digital chain as the bit it actually clocked in', () => {
        // Pre-polarity-normalization: `raw_bit` is the wire level, which is what
        // someone tracing an active-low LM339 needs to see.
        expect(formatReading({ source: 'digital', raw_bit: false }).reading).toBe('bit 0');
    });

    it('distinguishes an INA that threw from one that read zero', () => {
        // `_read_current` records a null and answers clear on an exception; a
        // silent "0.00 mA" would read as a working sensor seeing nothing.
        expect(formatReading({ source: 'current', current_ma: null }).reading).toBe('read failed');
        expect(formatReading({ source: 'current', current_ma: 0 }).reading).toBe('0 mA');
    });

    it('says nothing when the backend reported no per-source detail', () => {
        // Sim mode: the detector still debounces, but no hardware answered.
        expect(formatReading({ raw: true })).toEqual({ reading: '', detail: '' });
    });
});

describe('buildRows', () => {
    const snapshot = {
        on_threshold: 1,
        off_threshold: 10,
        blocks: {
            BLK_B: { raw: false, confirmed: true, off_count: 3, source: 'digital', raw_bit: false },
            BLK_A: {
                raw: true,
                confirmed: true,
                source: 'current',
                current_ma: 12,
                commanded_volts: 11.5,
                zone: 'driven',
            },
        },
    };

    it('sorts by block id so a row does not move under the pointer', () => {
        expect(buildRows(snapshot).map((r) => r.blockId)).toEqual(['BLK_A', 'BLK_B']);
    });

    it('marks a row settling exactly when raw and the verdict disagree', () => {
        const rows = buildRows(snapshot);
        expect(rows.find((r) => r.blockId === 'BLK_A')?.settling).toBe(false);
        expect(rows.find((r) => r.blockId === 'BLK_B')?.settling).toBe(true);
    });

    it('carries the commanded drive and its zone', () => {
        const row = buildRows(snapshot).find((r) => r.blockId === 'BLK_A');
        expect(row?.drive).toBe('11.5 V');
        expect(row?.zone).toBe('driven');
    });

    it('counts a block occupied for the filter when EITHER read says so', () => {
        // A block still clearing is one the operator is hunting; dropping it from
        // "occupied" the instant the raw read goes clear would hide the case.
        expect(buildRows(snapshot, 'occupied').map((r) => r.blockId)).toEqual(['BLK_A', 'BLK_B']);
    });

    it('narrows to the blocks mid-transition', () => {
        expect(buildRows(snapshot, 'settling').map((r) => r.blockId)).toEqual(['BLK_B']);
    });

    it('explains a lighting-zone hold rather than showing a bare occupied dot', () => {
        const rows = buildRows({
            blocks: { BLK_L: { raw: true, confirmed: true, source: 'held', held_from_previous: true } },
        });
        expect(rows[0].source).toBe('held');
        expect(rows[0].sourceTone).toBe('warn');
        expect(rows[0].note).toContain('held from the previous read');
    });

    it('calls out a block no detector answered for', () => {
        const rows = buildRows({ blocks: { BLK_X: { source: 'none' } } });
        expect(rows[0].note).toContain('no detector answered');
    });

    it('survives a snapshot with nothing in it', () => {
        expect(buildRows(null)).toEqual([]);
        expect(buildRows({})).toEqual([]);
        expect(buildRows({ blocks: {} })).toEqual([]);
    });
});

describe('describeLink', () => {
    it('shows nothing in sim mode, where there is no link to lose', () => {
        expect(describeLink(null)).toBeNull();
        expect(describeLink(undefined)).toBeNull();
    });

    it('reports a healthy link quietly', () => {
        expect(describeLink({ open: true, link_down: false, reconnects: 0 })).toEqual({
            tone: 'ok',
            text: 'USB link up',
        });
    });

    it('names the error when the bridge is off the bus', () => {
        const link = describeLink({
            open: false,
            link_down: true,
            reconnects: 2,
            last_error: 'read error',
        });
        expect(link?.tone).toBe('danger');
        expect(link?.text).toBe('USB link down — read error · 2 reconnects this session');
    });

    it('keeps a recovered link visible, because one drop predicts the next', () => {
        const link = describeLink({ open: true, link_down: false, reconnects: 1 });
        expect(link?.tone).toBe('warn');
        expect(link?.text).toBe('USB link up · 1 reconnect this session');
    });
});

describe('changedBlocks', () => {
    it('reports a verdict that flipped', () => {
        const before = { blocks: { A: { confirmed: false }, B: { confirmed: true } } };
        const after = { blocks: { A: { confirmed: true }, B: { confirmed: true } } };
        expect([...changedBlocks(before, after)]).toEqual(['A']);
    });

    it('ignores the raw read, which flickers without the verdict moving', () => {
        const before = { blocks: { A: { raw: false, confirmed: true } } };
        const after = { blocks: { A: { raw: true, confirmed: true } } };
        expect(changedBlocks(before, after).size).toBe(0);
    });

    it('does not flash a block on its first appearance', () => {
        expect(changedBlocks(null, { blocks: { A: { confirmed: true } } }).size).toBe(0);
        expect(changedBlocks({ blocks: {} }, { blocks: { A: { confirmed: true } } }).size).toBe(0);
    });
});
