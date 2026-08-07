import { describe, it, expect } from 'vitest';
import {
    anchorClass,
    buildRow,
    buildSchematic,
    directionTitle,
    numberOrNull,
    parseThreshold,
    peakSummaryText,
    detectorText,
    edgeMidpoint,
    segmentBlockIndex,
    shortBlockName,
    tileCentre,
    type BenchTile,
} from './occupancy-bench';

describe('tileCentre', () => {
    it('reproduces the canvas renderer’s diamond projection', () => {
        // lib/camera.ts gridToScreenX/Y at scale 1, gridSize 40, no pan:
        //   x = (gx + gy + 1) * tile / 2, y = (gx - gy) * tile / 2.
        expect(tileCentre(0, 0)).toEqual({ x: 20, y: 0 });
        expect(tileCentre(1, 0)).toEqual({ x: 40, y: 20 });
        expect(tileCentre(0, 1)).toEqual({ x: 40, y: -20 });
    });

    it('runs +x down-right and +y up-right, which is what makes a loop read as a loop', () => {
        const origin = tileCentre(5, 5);
        const east = tileCentre(6, 5);
        const north = tileCentre(5, 6);
        expect(east.x - origin.x).toBeGreaterThan(0);
        expect(east.y - origin.y).toBeGreaterThan(0);
        expect(north.x - origin.x).toBeGreaterThan(0);
        expect(north.y - origin.y).toBeLessThan(0);
    });
});

describe('edgeMidpoint', () => {
    it('places each edge halfway to the neighbour it faces', () => {
        // The N edge of (0,0) is shared with (0,1): its midpoint must sit
        // exactly halfway between the two tile centres.
        const here = tileCentre(0, 0);
        const north = tileCentre(0, 1);
        expect(edgeMidpoint(0, 0, 'N')).toEqual({
            x: (here.x + north.x) / 2,
            y: (here.y + north.y) / 2,
        });
    });

    it('is the same point read from either of the two tiles that share it', () => {
        // The E edge of (3,4) and the W edge of (4,4) are one physical joint;
        // if they disagreed, every rail would be drawn with a gap at the seam.
        expect(edgeMidpoint(3, 4, 'E')).toEqual(edgeMidpoint(4, 4, 'W'));
        expect(edgeMidpoint(3, 4, 'N')).toEqual(edgeMidpoint(3, 5, 'S'));
    });
});

describe('segmentBlockIndex', () => {
    it('inverts the blocks map to segment -> block', () => {
        expect(
            segmentBlockIndex({
                BLK_A: { segment_ids: ['SEG_1', 'SEG_2'] },
                BLK_B: { segment_ids: ['SEG_3'] },
            })
        ).toEqual({ SEG_1: 'BLK_A', SEG_2: 'BLK_A', SEG_3: 'BLK_B' });
    });

    it('tolerates a block with no segments', () => {
        expect(segmentBlockIndex({ BLK_A: {} })).toEqual({});
    });
});

describe('buildSchematic', () => {
    const index = { SEG_1: 'BLK_A' };

    it('draws one stroke per set track bit', () => {
        // 3 = TRACK_NS | TRACK_EW: a diamond crossing, two rails on one tile.
        const tiles: BenchTile[] = [{ x: 0, y: 0, track_bits: 3, segment_id: 'SEG_1' }];
        expect(buildSchematic(tiles, index).strokes).toHaveLength(2);
    });

    it('attributes each stroke to the block owning its segment', () => {
        const tiles: BenchTile[] = [{ x: 0, y: 0, track_bits: 1, segment_id: 'SEG_1' }];
        expect(buildSchematic(tiles, index).strokes[0].blockId).toBe('BLK_A');
    });

    it('draws a switch tile unowned however its segment resolves', () => {
        // A switch is not something the bench can drive or detect, so it must
        // not be painted as a block whose readout has gone missing.
        const tiles: BenchTile[] = [
            { x: 0, y: 0, track_bits: 1, segment_id: 'SEG_1', switch_id: 'SW_1' },
        ];
        const schematic = buildSchematic(tiles, index);
        expect(schematic.strokes[0].blockId).toBeNull();
        expect(schematic.centroids).toEqual({});
    });

    it('puts each block’s label at the mean of its own strokes', () => {
        const tiles: BenchTile[] = [
            { x: 0, y: 0, track_bits: 1, segment_id: 'SEG_1' },
            { x: 4, y: 4, track_bits: 1, segment_id: 'SEG_1' },
        ];
        const { centroids, strokes } = buildSchematic(tiles, index);
        const mean = (pick: (p: { x: number; y: number }) => number) =>
            strokes.reduce((sum, s) => sum + (pick(s.from) + pick(s.to)) / 2, 0) / strokes.length;
        expect(centroids.BLK_A.x).toBeCloseTo(mean((p) => p.x));
        expect(centroids.BLK_A.y).toBeCloseTo(mean((p) => p.y));
    });

    it('pads the viewBox so a rail on the boundary is not clipped in half', () => {
        const tiles: BenchTile[] = [{ x: 0, y: 0, track_bits: 2, segment_id: 'SEG_1' }];
        const { strokes, viewBox } = buildSchematic(tiles, index);
        const xs = strokes.flatMap((s) => [s.from.x, s.to.x]);
        expect(viewBox.x).toBeLessThan(Math.min(...xs));
        expect(viewBox.x + viewBox.width).toBeGreaterThan(Math.max(...xs));
    });

    it('returns a usable viewBox for an empty layout rather than Infinity', () => {
        const { strokes, viewBox } = buildSchematic([], index);
        expect(strokes).toEqual([]);
        expect(Number.isFinite(viewBox.width)).toBe(true);
        expect(viewBox.width).toBeGreaterThan(0);
    });
});

describe('detectorText', () => {
    it('reads true / false for a bound detector', () => {
        expect(detectorText(true)).toBe('true');
        expect(detectorText(false)).toBe('false');
    });

    it('does NOT read an unbound detector as false', () => {
        // "nothing is wired here" and "the detector says clear" are the two
        // answers this panel exists to tell apart.
        expect(detectorText(null)).toBe('—');
        expect(detectorText(undefined)).toBe('—');
    });
});

describe('buildRow', () => {
    it('formats the readout as idle-current overall', () => {
        const row = buildRow('BLK_A', { digital: false, current: true, overall: true });
        expect(row.text).toBe('false-true true');
        expect(row.occupied).toBe(true);
    });

    it('reports the verdict the server gave, not a recomputed one', () => {
        // In the lighting zone either source can confirm and neither can clear
        // on one read — the panel must not second-guess that rule.
        const row = buildRow('BLK_A', {
            digital: true,
            current: false,
            overall: false,
            zone: 'lighting',
        });
        expect(row.text).toBe('true-false false');
        expect(row.occupied).toBe(false);
    });

    it('carries an unbound detector through to the readout', () => {
        const row = buildRow('BLK_A', { digital: true, current: null, overall: true });
        expect(row.text).toBe('true-— true');
    });

    it('marks a block with no rail binding as unpowered', () => {
        expect(buildRow('BLK_A', { track_bound: false }).unpowered).toBe(true);
        expect(buildRow('BLK_A', { track_bound: true }).unpowered).toBe(false);
    });

    it('keeps the sign of the commanded voltage, which is the flip’s whole point', () => {
        expect(buildRow('BLK_A', { commanded_volts: -4.8, flipped: true }).volts).toBe(-4.8);
        expect(buildRow('BLK_A', { commanded_volts: 4.8 }).volts).toBe(4.8);
    });

    it('renders a row for a block the bench reported nothing about', () => {
        const row = buildRow('BLK_A', undefined);
        expect(row.text).toBe('—-— false');
        expect(row.occupied).toBe(false);
    });

    it('puts the measured current in the detail, where the margin can be judged', () => {
        const row = buildRow('BLK_A', { current: true, current_ma: 14.24, source: 'current' });
        expect(row.detail).toContain('14.2 mA');
        expect(row.detail).toContain('decided by: current');
    });
});

describe('current reading', () => {
    it('shows what the channel measured, in mA', () => {
        expect(buildRow('BLK_A', { current_ma: 14.24 }).currentText).toBe('14.2 mA');
    });

    it('shows a zero reading rather than hiding it — that is a real fault signature', () => {
        expect(buildRow('BLK_A', { current_ma: 0 }).currentText).toBe('0.0 mA');
    });

    it('never goes blank — a field that vanishes cannot be found', () => {
        // It used to collapse to "" with no reading, and the panel then dropped
        // the chip: on a bench that has not taken the controller yet that is
        // EVERY block, so the current reading was missing from the whole panel
        // at exactly the moment someone goes looking for it.
        expect(buildRow('BLK_A', { current: null }).currentText).toBe('— mA');
        expect(buildRow('BLK_A', undefined).currentText).toBe('— mA');
        expect(buildRow('BLK_A', { current_ma: null }).currentText).toBe('— mA');
    });

    it('leaves the mA out of the tooltip when there is no number to report', () => {
        expect(buildRow('BLK_A', { current: null }).detail).toContain('current detector: —');
        expect(buildRow('BLK_A', { current: null }).detail).not.toContain('mA');
    });

    it('reports the margin against the threshold, measured from the baseline', () => {
        // |reading − baseline| vs threshold is what actually decides the verdict.
        const row = buildRow('BLK_A', { current_ma: 12.0, baseline_ma: 3.2, threshold_ma: 5.0 });
        expect(row.marginText).toBe('Δ 8.8 of 5.0 mA');
    });

    it('names the configured value when a bench override is judging instead', () => {
        const row = buildRow('BLK_A', {
            current_ma: 4.0,
            baseline_ma: 0,
            threshold_ma: 1.5,
            configured_threshold_ma: 3.0,
        });
        expect(row.detail).toContain('bench override, configured 3.0');
    });

    it('does not claim an override when the effective threshold is the configured one', () => {
        const row = buildRow('BLK_A', {
            current_ma: 4.0,
            baseline_ma: 0,
            threshold_ma: 3.0,
            configured_threshold_ma: 3.0,
        });
        expect(row.detail).not.toContain('override');
        expect(row.detail).toContain('Δ 4.0 of 3.0 mA');
    });
});

describe('peak reading', () => {
    it('shows the running peak beside the live reading, not instead of it', () => {
        const row = buildRow('BLK_A', { current_ma: 2.0, peak_ma: 7.24, peak_samples: 40 });
        expect(row.currentText).toBe('2.0 mA');
        expect(row.peakText).toBe('pk 7.2');
    });

    it('keeps the sign, so the number matches a reading that was on screen', () => {
        // A reversed channel reads negative; showing |9.0| would be a value the
        // operator never saw the live chip display.
        expect(buildRow('BLK_A', { peak_ma: -9.04 }).peakText).toBe('pk -9.0');
    });

    it('stays empty when the channel has read nothing — "— mA" already says that', () => {
        expect(buildRow('BLK_A', {}).peakText).toBe('');
        expect(buildRow('BLK_A', { peak_ma: null }).peakText).toBe('');
        expect(buildRow('BLK_A', undefined).peakText).toBe('');
    });

    it('puts the sample count in the tooltip — a peak from three polls is not one', () => {
        expect(buildRow('BLK_A', { peak_ma: 6.0, peak_samples: 128 }).detail).toContain(
            'peak 6.0 mA over 128 reads'
        );
        expect(buildRow('BLK_A', { peak_ma: 6.0, peak_samples: 1 }).detail).toContain(
            'over 1 read'
        );
    });
});

describe('peakSummaryText', () => {
    it('names the worst channel and how long it was watched', () => {
        expect(
            peakSummaryText({ block_id: 'BLK_4F5CE3', peak_ma: 6.04, samples: 512 })
        ).toBe('Peak 6.0 mA on 4F5CE3 over 512 reads');
    });

    it('says nothing has been measured rather than reporting a peak of zero', () => {
        expect(peakSummaryText({ block_id: null, peak_ma: null, samples: 0 })).toBe(
            'No current readings yet'
        );
        expect(peakSummaryText(null)).toBe('No current readings yet');
        expect(peakSummaryText(undefined)).toBe('No current readings yet');
    });

    it('distinguishes "watched but silent" from "not watched"', () => {
        expect(peakSummaryText({ block_id: null, peak_ma: null, samples: 8 })).toBe(
            'No current channel read a number over 8 reads'
        );
    });

    it('reports a genuine zero peak as a measurement, not as missing', () => {
        // A channel that really reads 0.0 is a finding — an unwired shunt looks
        // exactly like this, and it must not be swallowed as "no data".
        expect(peakSummaryText({ block_id: 'BLK_A', peak_ma: 0, samples: 20 })).toBe(
            'Peak 0.0 mA on A over 20 reads'
        );
    });
});

describe('parseThreshold', () => {
    it('reads an empty box as "each block on its own configured threshold"', () => {
        // A real setting, not a missing one — it must survive the trip as null
        // rather than being skipped as falsy.
        expect(parseThreshold('')).toBeNull();
        expect(parseThreshold('   ')).toBeNull();
    });

    it('reads a number, including zero', () => {
        expect(parseThreshold('5')).toBe(5);
        expect(parseThreshold('12.5')).toBe(12.5);
        expect(parseThreshold('0')).toBe(0);
    });

    it('clamps a negative, which no |Δ| could ever fall under', () => {
        expect(parseThreshold('-4')).toBe(0);
    });

    it('treats unparseable text as no override rather than as a number', () => {
        expect(parseThreshold('abc')).toBeNull();
    });
});

describe('numberOrNull', () => {
    it('keeps zero, which is a real reading', () => {
        expect(numberOrNull(0)).toBe(0);
    });

    it('drops what the backend left out or nulled', () => {
        expect(numberOrNull(null)).toBeNull();
        expect(numberOrNull(undefined)).toBeNull();
        expect(numberOrNull(NaN)).toBeNull();
    });
});

describe('buildRow flip source', () => {
    it('renders the flip the operator just pressed, not the server’s stale echo', () => {
        // The click updates the panel's own set and repaints immediately; the
        // server's `flipped` is still one round trip behind. Reading the echo
        // here left the rails dashed while the button's arrow stayed put.
        const row = buildRow('BLK_A', { flipped: false, commanded_volts: 4.8 }, true);
        expect(row.flipped).toBe(true);
    });

    it('falls back to the server’s value when no local intent is supplied', () => {
        expect(buildRow('BLK_A', { flipped: true }).flipped).toBe(true);
        expect(buildRow('BLK_A', { flipped: false }).flipped).toBe(false);
    });
});

describe('directionTitle', () => {
    it('leads with the action, so the chip does not read as a status icon', () => {
        const title = directionTitle(buildRow('BLK_A', { commanded_volts: 4.8 }, false));
        expect(title).toContain('Driving forward');
        expect(title).toContain('click to flip to reversed');
    });

    it('names the opposite direction once a block is already flipped', () => {
        const title = directionTitle(buildRow('BLK_A', { commanded_volts: -4.8 }, true));
        expect(title).toContain('Driving reversed');
        expect(title).toContain('click to flip to forward');
    });

    it('says why nothing can be driven when the bench holds no controller', () => {
        const title = directionTitle(buildRow('BLK_A', undefined, false), false);
        expect(title).toContain('does not hold the controller');
    });

    it('says why nothing can be driven on a block with no rails', () => {
        const title = directionTitle(buildRow('BLK_A', { track_bound: false }, false));
        expect(title).toContain('No rail binding');
    });

    it('keeps the detector detail after the action', () => {
        const title = directionTitle(buildRow('BLK_A', { digital: true, current: false }, false));
        expect(title).toContain('idle detector: true');
    });
});

describe('anchorClass', () => {
    it('hangs an edge block’s label inward so the readout is not clipped off', () => {
        expect(anchorClass(89)).toBe('bench-block-right');
        expect(anchorClass(10)).toBe('bench-block-left');
    });

    it('leaves a block with room on both sides centred on its own track', () => {
        expect(anchorClass(50)).toBe('bench-block-centre');
    });
});

describe('shortBlockName', () => {
    it('drops the prefix every row shares', () => {
        expect(shortBlockName('BLK_4F7A2F')).toBe('4F7A2F');
        expect(shortBlockName('PLT_YA_1_BLOCK')).toBe('PLT_YA_1_BLOCK');
    });
});
