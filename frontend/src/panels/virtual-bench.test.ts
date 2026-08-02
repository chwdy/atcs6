import { describe, it, expect } from 'vitest';
import { benchRows, describeDrive, type BenchSnapshot } from './virtual-bench';

const snapshot = (blocks: Record<string, any>): BenchSnapshot => ({
    virtual_hardware_active: true,
    blocks,
});

describe('benchRows', () => {
    it('lists a block bound only to track power', () => {
        // The gap this panel exists to close: an output-only block has a
        // voltage worth watching and no detector at all, and a row set built
        // from the occupancy bindings drops it entirely.
        const rows = benchRows(
            snapshot({
                B_OUT: {
                    has_track: true, has_digital: false, has_current: false,
                    volts: 6, duty_forward: 0.5, duty_reverse: 0,
                    occupancy_bit: null, current_ma: null, occupied: null,
                },
            })
        );
        expect(rows).toHaveLength(1);
        expect(rows[0].hasTrack).toBe(true);
        expect(rows[0].hasDigital).toBe(false);
        expect(rows[0].present).toBe(false);
        expect(rows[0].driven).toBe(true);
    });

    it('counts the current channel as present only above its own threshold', () => {
        // A block reading its baseline is what an EMPTY block looks like, so a
        // panel that called any current "present" would show a train on every
        // wired block the moment it opened.
        const rows = benchRows(
            snapshot({
                BASELINE: {
                    has_current: true, current_ma: 2, baseline_ma: 2, threshold_ma: 10,
                    detector_present: null, occupied: false,
                },
                LOADED: {
                    has_current: true, current_ma: 18, baseline_ma: 2, threshold_ma: 10,
                    detector_present: null, occupied: true,
                },
            })
        );
        expect(rows.find((r) => r.blockId === 'BASELINE')?.present).toBe(false);
        expect(rows.find((r) => r.blockId === 'LOADED')?.present).toBe(true);
    });

    it('treats a present digital detector as present regardless of the current channel', () => {
        const rows = benchRows(
            snapshot({
                B1: {
                    has_digital: true, has_current: true,
                    detector_present: true, occupancy_bit: true,
                    current_ma: 0, baseline_ma: 0, threshold_ma: 10,
                    occupied: true,
                },
            })
        );
        // Deliberately disagreeing sources: the detector says train, the
        // current says empty. "Present" reports what any detector was told, so
        // the raw controls' whole point stays visible in the row.
        expect(rows[0].present).toBe(true);
    });

    it('reads presence off the resolved detector, not the raw bit', () => {
        // An active-low chain pulls its line LOW when current flows, so the raw
        // bit is 0 exactly when a train IS there. Keying the Place toggle to
        // the raw bit would offer "Place" on every occupied block and "lift" on
        // every empty one — backwards on the rigs that actually ship.
        const rows = benchRows(
            snapshot({
                B1: {
                    has_digital: true, active_low: true,
                    occupancy_bit: false, detector_present: true, occupied: true,
                },
            })
        );
        expect(rows[0].occupancyBit).toBe(false);
        expect(rows[0].present).toBe(true);
        expect(rows[0].activeLow).toBe(true);
    });

    it('sorts by block id and keeps every wired block under the all filter', () => {
        const rows = benchRows(snapshot({ B2: { has_track: true }, B1: { has_track: true } }));
        expect(rows.map((r) => r.blockId)).toEqual(['B1', 'B2']);
    });

    it('filters to blocks that are doing something', () => {
        const state = snapshot({
            IDLE: { has_track: true, volts: 0, occupied: false },
            DRIVEN: { has_track: true, volts: 9, occupied: false },
            OCCUPIED: { has_digital: true, detector_present: true, occupied: true },
        });
        expect(benchRows(state, 'active').map((r) => r.blockId)).toEqual(['DRIVEN', 'OCCUPIED']);
        expect(benchRows(state, 'occupied').map((r) => r.blockId)).toEqual(['OCCUPIED']);
    });

    it('reports no rows for a snapshot the endpoint never answered', () => {
        expect(benchRows(null)).toEqual([]);
    });
});

describe('describeDrive', () => {
    const row = (over: Partial<ReturnType<typeof benchRows>[0]>) =>
        benchRows(snapshot({ B1: { has_track: true, volts: 0, ...over } }))[0];

    it('shows a direction glyph and the signed voltage when the rails are live', () => {
        expect(describeDrive(row({ volts: 7.5 }))).toEqual({
            glyph: '▶', text: '+7.5 V', tone: 'fwd',
        });
        expect(describeDrive(row({ volts: -7.5 }))).toEqual({
            glyph: '◀', text: '-7.5 V', tone: 'rev',
        });
    });

    it('reads a block resting at zero as idle rather than forward', () => {
        // Sign alone would call 0 V "forward", which paints every parked block
        // as energized — the one state the output column must not get wrong.
        const drive = describeDrive(row({ volts: 0 }));
        expect(drive.tone).toBe('idle');
        expect(drive.text).toBe('0.0 V');
    });

    it('says nothing for a block with no rails bound', () => {
        const unbound = benchRows(snapshot({ B1: { has_digital: true, detector_present: false } }))[0];
        expect(describeDrive(unbound)).toEqual({ glyph: '', text: '—', tone: 'idle' });
    });
});
