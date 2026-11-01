/**
 * The snapshot shapes and helper functions behind the Logs screen's Detectors
 * tab (frontend/src/next/logs/detectors-tab.tsx): live read-back of what the
 * occupancy detectors actually see, one row per detected block.
 *
 * Polls GET /api/hardware/occupancy/raw, which reports recorded state and
 * issues no bus traffic, so polling it fast cannot perturb the tick or the
 * detectors it reports on.
 *
 * What this panel is for: the layout paints the CONFIRMED verdict, which is what
 * the runtime acts on — and when that disagrees with the hardware, the layout is
 * the last place the disagreement shows up. Three things live here that appear
 * nowhere else:
 *
 *  - **Raw vs confirmed.** The pre-debounce read beside the verdict, so a block
 *    mid-transition reads as mid-transition rather than as stuck.
 *  - **Which source answered.** Occupancy is a hybrid — a '165 digital bit or an
 *    INA current channel, chosen per block by how hard the rails are being
 *    driven. "The detector is wrong" is usually "the other detector answered".
 *  - **The commanded drive.** Occupancy and drive are a closed loop (a block is
 *    read through the same rails software is energizing), so the drive zone is
 *    half the explanation for any reading.
 */


// ---------------------------------------------------------------------------
// Snapshot shapes returned by GET /api/hardware/occupancy/raw. Loosely typed and
// read defensively: sim mode omits every hardware-only member, and a build with
// no wired detectors omits the blocks themselves.

export interface RawOccupancyBlock {
    raw?: boolean;
    confirmed?: boolean;
    on_count?: number;
    off_count?: number;
    /** Real driver only, from OccupancyDriver.last_read_detail(). */
    zone?: string;
    driven?: boolean;
    commanded_volts?: number | null;
    source?: string;
    raw_bit?: boolean;
    current_ma?: number | null;
    threshold_ma?: number;
    baseline_ma?: number;
    held_from_previous?: boolean;
}

export interface RawOccupancyLink {
    open?: boolean;
    link_down?: boolean;
    reconnects?: number;
    last_error?: string | null;
}

export interface RawOccupancySnapshot {
    read_count?: number;
    on_threshold?: number;
    off_threshold?: number;
    blocks?: Record<string, RawOccupancyBlock>;
    /** null in sim mode — there is no USB link to lose. */
    link?: RawOccupancyLink | null;
    monotonic?: number;
}

/** One block's row, fully resolved for rendering. */
export interface OccupancyRow {
    blockId: string;
    raw: boolean;
    confirmed: boolean;
    /** Raw and confirmed disagree — the block is mid-debounce. */
    settling: boolean;
    source: string;
    sourceTone: ChipTone;
    /** What the answering detector measured: "14.2 mA", "bit 1", or "" when the
     *  backend reported no per-source detail (sim mode). */
    reading: string;
    /** The bar the reading is judged against, e.g. "Δ ≥ 5 mA from 0.4". */
    readingDetail: string;
    drive: string;
    zone: string;
    debounce: DebouncePhase | null;
    note: string;
}

export type ChipTone = 'success' | 'info' | 'warn' | 'danger' | 'muted' | 'accent';

export interface DebouncePhase {
    /** 'confirming' — raw says present, the verdict has not caught up yet.
     *  'clearing'   — raw says clear, the block is riding out its off window. */
    kind: 'confirming' | 'clearing';
    label: string;
    count: number;
    total: number;
    /** 0..1, for the progress bar. */
    fraction: number;
}

export type OccupancyFilter = 'all' | 'occupied' | 'settling';

// ---------------------------------------------------------------------------
// Pure derivation. Everything the panel renders is computed here so it can be
// tested without a DOM.

/** How far a block is through its debounce window, or null when raw and the
 *  verdict already agree.
 *
 *  The counters are consecutive-run counters and mutually exclusive — a present
 *  read zeroes the off count and vice versa (`BlockOccupancyDetector.step`) — so
 *  which one is live follows from which way the two disagree.
 */
export function describeDebounce(
    entry: RawOccupancyBlock,
    snapshot: RawOccupancySnapshot
): DebouncePhase | null {
    const raw = !!entry.raw;
    const confirmed = !!entry.confirmed;
    if (raw === confirmed) return null;
    const total = Math.max(
        1,
        Number(raw ? snapshot.on_threshold : snapshot.off_threshold) || 1
    );
    const count = Math.max(0, Number(raw ? entry.on_count : entry.off_count) || 0);
    return {
        kind: raw ? 'confirming' : 'clearing',
        label: raw ? 'confirming' : 'clearing',
        count,
        total,
        fraction: Math.min(1, count / total),
    };
}

/** What the answering detector measured, and the bar it is judged against. */
export function formatReading(entry: RawOccupancyBlock): { reading: string; detail: string } {
    if (entry.source === 'current' || typeof entry.current_ma === 'number') {
        if (typeof entry.current_ma !== 'number') {
            // The INA read threw; `_read_current` records the null and answers clear.
            return { reading: 'read failed', detail: '' };
        }
        const detail =
            typeof entry.threshold_ma === 'number'
                ? `Δ ≥ ${formatNumber(entry.threshold_ma)} mA from ${formatNumber(
                      entry.baseline_ma ?? 0
                  )}`
                : '';
        return { reading: `${formatNumber(entry.current_ma)} mA`, detail };
    }
    if (typeof entry.raw_bit === 'boolean') {
        return { reading: `bit ${entry.raw_bit ? 1 : 0}`, detail: '' };
    }
    return { reading: '', detail: '' };
}

/** The chip tone for the source that answered. A block held from its previous
 *  read is worth calling out: it is occupied on the strength of that read rather
 *  than this one, because in the lighting zone a clear takes a run of reads. */
export function sourceTone(source: string | undefined, heldFromPrevious = false): ChipTone {
    if (heldFromPrevious) return 'warn';
    return 'muted';
}

function formatNumber(value: number): string {
    if (!Number.isFinite(value)) return '—';
    const abs = Math.abs(value);
    const decimals = abs >= 100 ? 0 : abs >= 10 ? 1 : 2;
    // Trim trailing zeros (and a bare trailing point) so a whole threshold reads
    // "5 mA" rather than "5.00 mA". A column of padded zeros is noise around the
    // digits that are actually moving, which is the whole reason to watch this.
    return value
        .toFixed(decimals)
        .replace(/(\.\d*?)0+$/, '$1')
        .replace(/\.$/, '');
}

/** Turn a snapshot into sorted rows, filtered for what the operator asked to see. */
export function buildRows(
    snapshot: RawOccupancySnapshot | null,
    filter: OccupancyFilter = 'all'
): OccupancyRow[] {
    const blocks = snapshot?.blocks;
    if (!snapshot || !blocks) return [];
    const rows: OccupancyRow[] = [];
    for (const blockId of Object.keys(blocks).sort()) {
        const entry = blocks[blockId] || {};
        const raw = !!entry.raw;
        const confirmed = !!entry.confirmed;
        const { reading, detail } = formatReading(entry);
        const notes: string[] = [];
        if (entry.held_from_previous) {
            notes.push('held from the previous read — a clear here takes a run of reads');
        }
        if (entry.source === 'none') {
            notes.push('no detector answered for this block');
        }
        rows.push({
            blockId,
            raw,
            confirmed,
            settling: raw !== confirmed,
            source: entry.source || '',
            sourceTone: sourceTone(entry.source, !!entry.held_from_previous),
            reading,
            readingDetail: detail,
            drive:
                typeof entry.commanded_volts === 'number'
                    ? `${formatNumber(entry.commanded_volts)} V`
                    : '',
            zone: entry.zone || '',
            debounce: describeDebounce(entry, snapshot),
            note: notes.join(' · '),
        });
    }
    if (filter === 'occupied') return rows.filter((r) => r.confirmed || r.raw);
    if (filter === 'settling') return rows.filter((r) => r.settling);
    return rows;
}

/** The USB link strip, or null when there is no link to report (sim mode).
 *
 *  Deliberately silent rather than "n/a" in sim mode: a permanent row saying
 *  nothing trains the eye to skip the row that will one day say something. */
export function describeLink(
    link: RawOccupancyLink | null | undefined
): { tone: 'ok' | 'danger' | 'warn'; text: string } | null {
    if (!link) return null;
    const reconnects = Number(link.reconnects) || 0;
    const reconnectNote =
        reconnects > 0 ? ` · ${reconnects} reconnect${reconnects === 1 ? '' : 's'} this session` : '';
    if (link.link_down || !link.open) {
        const why = link.last_error ? ` — ${link.last_error}` : '';
        return { tone: 'danger', text: `USB link down${why}${reconnectNote}` };
    }
    if (reconnects > 0) {
        // Carrying traffic, but it has dropped before. Worth surfacing: a bridge
        // that dropped once is telling you something about the supply.
        return { tone: 'warn', text: `USB link up${reconnectNote}` };
    }
    return { tone: 'ok', text: 'USB link up' };
}

/** Blocks whose confirmed verdict differs between two snapshots — the flip is
 *  the event this panel exists to catch, so it is what gets flashed. */
export function changedBlocks(
    previous: RawOccupancySnapshot | null,
    next: RawOccupancySnapshot | null
): Set<string> {
    const changed = new Set<string>();
    const before = previous?.blocks;
    const after = next?.blocks;
    if (!before || !after) return changed;
    for (const blockId of Object.keys(after)) {
        if (!(blockId in before)) continue;
        if (!!before[blockId]?.confirmed !== !!after[blockId]?.confirmed) {
            changed.add(blockId);
        }
    }
    return changed;
}

// ---------------------------------------------------------------------------

export const FLASH_MS = 900;

export const REFRESH_OPTIONS = [
    { label: '10 Hz', hz: 10 },
    { label: '4 Hz', hz: 4 },
    { label: '1 Hz', hz: 1 },
    { label: 'Every 5s', hz: 0.2 },
    { label: 'On-demand', hz: 0 },
];

export const FILTER_OPTIONS: Array<{ label: string; value: OccupancyFilter }> = [
    { label: 'All blocks', value: 'all' },
    { label: 'Occupied only', value: 'occupied' },
    { label: 'Settling only', value: 'settling' },
];
