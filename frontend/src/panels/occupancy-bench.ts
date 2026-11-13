/**
 * OccupancyBench — a steady environment for chasing occupancy faults.
 *
 * The panel is two halves of one question. On top, a schematic of the layout in
 * which every block is a button: pressing it flips that block's drive direction,
 * and its label reads the two detectors and the verdict. Below, Play / Stop and
 * the level they hold every block at.
 *
 * Why the readout is `idle-current overall` rather than one verdict: occupancy
 * is a hybrid of a '165 digital detector and an INA current channel, and the
 * runtime believes exactly one of them per block depending on how hard that
 * block is being driven — so "occupied is wrong here" splits into two very
 * different faults (the trusted detector is lying vs. the wrong detector is
 * being trusted) that a single verdict cannot tell apart. The bench reads both
 * every poll and shows both.
 *
 * Why the drive level is an input and not a constant: which detector answers is
 * a function of that level, so it is the variable of the experiment. It opens at
 * 40% (4.8 V, the `driven` zone) because that is what a wiring check wants, but
 * the band worth the most is the one BELOW the current-sense floor — the
 * `lighting` zone, where a locomotive standing at its lighting voltage or driven
 * slowly actually sits and both detectors are at their weakest. That is the band
 * where `threshold_ma` and `SpeedProfile.lighting_voltage` get set, and it cannot be reached from
 * a fixed 40%. The footer names the zone each level lands in, so choosing one is
 * deliberate rather than discovered afterwards.
 *
 * Why the direction button: the detectors are not symmetric in the rails. One
 * rail of the pair carries the idle detection, so a block wired backwards reads
 * plausibly in one direction and not at all in the other, and flipping it is the
 * fastest way to see that. The flip is bench-local — it negates the commanded
 * sign on top of the rail binding's own `reversed` flag and never writes the
 * configuration, so nothing here can leave the layout mis-wired behind you.
 *
 * The bench is a Setup tool (design_v2.md "Setup tools", Occupancy bench):
 * it opens only in Setup, where it owns the outputs while the detectors keep
 * being read. So the layout goes on showing what the app believes, and
 * comparing the two is the diagnosis. Closing it never activates anything.
 *
 * Hardware Setup step 4 builds it (mountOccupancyBench), inside the step.
 */

import { api } from '../lib/api';
import { setupToolsAllowed, SETUP_TOOL_HINT } from '../lib/session-controls';

const REFRESH_HZ = 4;

// ---------------------------------------------------------------------------
// Layout geometry. Everything below is pure so it can be unit-tested without a
// DOM or a server.

/** One tile as `/api/network` reports it. */
export interface BenchTile {
    x: number;
    y: number;
    track_bits: number;
    segment_id?: string | null;
    switch_id?: string | null;
}

export interface BenchPoint {
    x: number;
    y: number;
}

/** One drawn rail: the straight run between two edge midpoints of one tile. */
export interface BenchStroke {
    /** The block that owns the tile, or null for a switch / unassigned tile. */
    blockId: string | null;
    from: BenchPoint;
    to: BenchPoint;
}

export interface BenchSchematic {
    strokes: BenchStroke[];
    /** Per block, the mean of its own strokes' endpoints — where its label goes. */
    centroids: Record<string, BenchPoint>;
    viewBox: { x: number; y: number; width: number; height: number };
}

/** `track_bits` bit value -> the two tile edges that bit connects. Mirrors
 *  `BIT_TO_EDGES` in track.py, which is the authority. */
const BIT_EDGES: ReadonlyArray<readonly [number, string, string]> = [
    [1, 'N', 'S'],
    [2, 'E', 'W'],
    [4, 'N', 'E'],
    [8, 'N', 'W'],
    [16, 'S', 'E'],
    [32, 'S', 'W'],
];

/** The tile size the projection below works in. Arbitrary — the viewBox is
 *  fitted to the result, so this only sets the units the stroke width is in. */
const TILE = 40;

/**
 * Where a tile's centre lands on screen.
 *
 * This is the canvas renderer's own projection (lib/camera.ts `gridToScreenX/Y`
 * at scale 1, no pan): the grid is drawn as diamonds, so +x runs down-right and
 * +y runs up-right. Reproducing it rather than laying the grid out square is
 * what makes the schematic recognisable as the layout the operator is looking
 * at — the same loop drawn on a square grid comes out as an unrecognisable
 * diamond.
 */
export function tileCentre(x: number, y: number): BenchPoint {
    return { x: ((x + y + 1) * TILE) / 2, y: ((x - y) * TILE) / 2 };
}

/** A tile edge's midpoint, projected. NORTH is +y and EAST is +x in grid terms
 *  (enums.py `Direction`), so each edge sits a quarter-tile from the centre
 *  along the projected axis of the neighbour it faces. */
export function edgeMidpoint(x: number, y: number, edge: string): BenchPoint {
    const c = tileCentre(x, y);
    const q = TILE / 4;
    switch (edge) {
        case 'N':
            return { x: c.x + q, y: c.y - q };
        case 'E':
            return { x: c.x + q, y: c.y + q };
        case 'S':
            return { x: c.x - q, y: c.y + q };
        default: // 'W'
            return { x: c.x - q, y: c.y - q };
    }
}

/**
 * Project the layout into drawable strokes, one per set track bit.
 *
 * `segmentToBlock` maps a tile's segment to its owning block; a tile carrying a
 * `switch_id` is drawn unowned however its segment resolves, because a switch is
 * not something this bench can drive or detect and must not look like it is.
 */
export function buildSchematic(
    tiles: ReadonlyArray<BenchTile>,
    segmentToBlock: Readonly<Record<string, string>>
): BenchSchematic {
    const strokes: BenchStroke[] = [];
    const sums: Record<string, { x: number; y: number; n: number }> = {};
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;

    for (const tile of tiles) {
        const blockId = tile.switch_id
            ? null
            : segmentToBlock[tile.segment_id ?? ''] ?? null;
        for (const [mask, a, b] of BIT_EDGES) {
            if (!(tile.track_bits & mask)) continue;
            const from = edgeMidpoint(tile.x, tile.y, a);
            const to = edgeMidpoint(tile.x, tile.y, b);
            strokes.push({ blockId, from, to });
            for (const p of [from, to]) {
                if (p.x < minX) minX = p.x;
                if (p.x > maxX) maxX = p.x;
                if (p.y < minY) minY = p.y;
                if (p.y > maxY) maxY = p.y;
            }
            if (blockId) {
                const acc = sums[blockId] ?? (sums[blockId] = { x: 0, y: 0, n: 0 });
                acc.x += (from.x + to.x) / 2;
                acc.y += (from.y + to.y) / 2;
                acc.n += 1;
            }
        }
    }

    if (!strokes.length) {
        return { strokes, centroids: {}, viewBox: { x: 0, y: 0, width: 1, height: 1 } };
    }

    const centroids: Record<string, BenchPoint> = {};
    for (const [blockId, acc] of Object.entries(sums)) {
        centroids[blockId] = { x: acc.x / acc.n, y: acc.y / acc.n };
    }

    // Padding covers the stroke's own half-width plus the round cap, so a rail
    // running along the edge of the bounding box is not clipped in half.
    const pad = TILE / 2;
    return {
        strokes,
        centroids,
        viewBox: {
            x: minX - pad,
            y: minY - pad,
            width: maxX - minX + pad * 2,
            height: maxY - minY + pad * 2,
        },
    };
}

/** segment id -> block id, from the `/api/network` blocks map. */
export function segmentBlockIndex(
    blocks: Readonly<Record<string, { segment_ids?: string[] }>>
): Record<string, string> {
    const index: Record<string, string> = {};
    for (const [blockId, block] of Object.entries(blocks || {})) {
        for (const segmentId of block?.segment_ids ?? []) index[segmentId] = blockId;
    }
    return index;
}

// ---------------------------------------------------------------------------
// Readout derivation.

/** One block's row as the bench reports it. */
export interface BenchBlockState {
    /** The '165 digital detector, or null when the block has no digital binding. */
    digital?: boolean | null;
    /** The INA current channel, or null when the block has no current binding. */
    current?: boolean | null;
    /** The verdict the runtime's zone rule takes from the two. */
    overall?: boolean;
    source?: string;
    zone?: string;
    commanded_volts?: number | null;
    current_ma?: number | null;
    /** The threshold the reading was actually judged against — the bench
     *  override when one is set, otherwise the block's configured value. */
    threshold_ma?: number | null;
    /** What the block is configured with, whatever the override says. */
    configured_threshold_ma?: number | null;
    baseline_ma?: number | null;
    raw_bit?: boolean;
    flipped?: boolean;
    track_bound?: boolean;
    /** Largest-magnitude reading this channel has produced since the last reset,
     *  sign preserved. Null when the channel has produced no number at all. */
    peak_ma?: number | null;
    /** How many reads are behind that peak — a peak from three polls is not one. */
    peak_samples?: number;
}

/** The worst channel on the layout, as the bench summarises it. */
export interface BenchPeak {
    block_id?: string | null;
    peak_ma?: number | null;
    samples?: number;
}

export interface BenchRow {
    blockId: string;
    /** The `idle-current overall` readout, e.g. "false-true true". */
    text: string;
    occupied: boolean;
    /** No rail binding — the button cannot drive this block. */
    unpowered: boolean;
    /** Which detector the runtime would believe at this drive level. */
    source: string;
    flipped: boolean;
    /** Signed commanded volts, or null when nothing is bound. */
    volts: number | null;
    /** What the current channel measured, e.g. "14.2 mA", or "— mA" when there
     *  is no number yet. Never empty — the chip is always on screen. */
    currentText: string;
    /** The running peak, e.g. "pk 7.2", or "" when the channel has read nothing
     *  yet. Empty rather than "pk —" because this chip is additional to the live
     *  reading beside it: a placeholder for a number that does not exist yet
     *  would just be a second way of saying what "— mA" already says. */
    peakText: string;
    /** How far the reading sits from the threshold deciding it, for the tooltip. */
    marginText: string;
    /** The long-form explanation, for the row's tooltip. */
    detail: string;
}

/**
 * What the threshold box means, given its raw text.
 *
 * Empty is `null` — "put every block back on its own configured threshold" —
 * which is a real setting and not a missing one, so it has to survive the trip
 * to the server rather than being skipped as falsy. Anything unparseable is also
 * null: a half-typed "1.", blanked, is better read as "no override" than as some
 * number the operator did not mean. Negative is clamped, because |Δ| can never
 * be under it and the block would latch occupied forever.
 */
export function parseThreshold(raw: string): number | null {
    const text = String(raw ?? '').trim();
    if (!text) return null;
    const parsed = Number(text);
    if (!Number.isFinite(parsed)) return null;
    return Math.max(0, parsed);
}

/**
 * What the drive box means, given its raw text.
 *
 * Null for anything unreadable, which the caller turns into "put the box back to
 * what the bench holds" — unlike the threshold field, blank is NOT a setting
 * here. There is no such thing as "no drive level"; 0 % is how you ask for no
 * volts, and it is typed.
 *
 * Clamped to the same 0..100 the session clamps to, so the box and the rails
 * agree about what a typed 250 did rather than leaving the panel claiming a
 * level the server silently reduced.
 */
export function parsePercent(raw: string): number | null {
    const text = String(raw ?? '').trim();
    if (!text) return null;
    const parsed = Number(text);
    if (!Number.isFinite(parsed)) return null;
    return Math.min(100, Math.max(0, parsed));
}

/**
 * Which detector regime a commanded level lands in, named as `OccupancyDriver`
 * names them. This is the whole reason the drive control is worth having: the
 * three zones are the difference between testing the '165, testing the INA, and
 * testing the band where a lit standing or slow locomotive sits and neither
 * detector is reliable.
 *
 * Null when the bounds are unknown (no bench open), so the caller can leave the
 * label off rather than assert a zone from defaults that may not be this rig's.
 */
export function driveZone(
    volts: number,
    idleDeadbandV: number | null,
    minDriveV: number | null,
): 'idle' | 'lighting' | 'driven' | null {
    if (idleDeadbandV === null || minDriveV === null) return null;
    const magnitude = Math.abs(volts);
    if (magnitude <= idleDeadbandV) return 'idle';
    if (magnitude >= minDriveV) return 'driven';
    return 'lighting';
}

/** What each zone means for the reading, in the operator's terms. */
export const ZONE_HELP: Record<'idle' | 'lighting' | 'driven', string> = {
    idle: "rails quiet — the '165 decides and the current channel reads its noise floor",
    lighting:
        "the band a lit standing or slow loco sits in — the '165 flickers under PWM and the " +
        'current channel is at its weakest, so no single read clears a block here',
    driven: "the current channel decides and the '165 is swamped",
};

/** A finite number, or null for anything the backend left out or nulled. `0` is
 *  a real reading and must survive — a channel measuring nothing is exactly what
 *  a miswired block looks like. */
export function numberOrNull(value: number | null | undefined): number | null {
    if (value === null || value === undefined) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
}

/** "true" / "false", or "—" for a detector this block has no binding for. An
 *  unwired detector must not read as `false`: "nothing is wired here" and "the
 *  detector says clear" are the two answers this panel exists to separate. */
export function detectorText(value: boolean | null | undefined): string {
    if (value === null || value === undefined) return '—';
    return value ? 'true' : 'false';
}

/**
 * One block's row.
 *
 * `flipped` is passed in rather than read off `state`, and that is the whole
 * point of the parameter: the panel's own flip set is what the operator just
 * pressed, and the server's echo is one round trip behind it. Reading the echo
 * here made the rails go dashed on click while the button's own arrow stayed
 * put — and with the bench closed, where no request is ever sent, the arrow
 * could never move at all. The panel's set renders; the server's echo only
 * reconciles it (`_applyStatus`).
 */
export function buildRow(
    blockId: string,
    state: BenchBlockState | undefined,
    flipped?: boolean
): BenchRow {
    const entry = state ?? {};
    const occupied = !!entry.overall;
    const text = `${detectorText(entry.digital)}-${detectorText(entry.current)} ${
        occupied ? 'true' : 'false'
    }`;
    const volts =
        entry.commanded_volts === null || entry.commanded_volts === undefined
            ? null
            : Number(entry.commanded_volts);

    const ma = numberOrNull(entry.current_ma);
    const threshold = numberOrNull(entry.threshold_ma);
    const baseline = numberOrNull(entry.baseline_ma);
    // ALWAYS a value, never blank. This read used to collapse to "" whenever
    // there was no number — no binding, a failed read, or simply a bench that
    // does not hold the controller yet — and the panel then dropped the chip
    // altogether, so the current reading was missing from every block at exactly
    // the moment someone would go looking for it. A field that disappears when
    // it has nothing to say is indistinguishable from a field that was never
    // built. "— mA" says "no number here", which is the honest answer.
    const currentText = ma === null ? '— mA' : `${ma.toFixed(1)} mA`;
    const peak = numberOrNull(entry.peak_ma);
    const peakSamples = Number(entry.peak_samples) || 0;
    const peakText = peak === null ? '' : `pk ${peak.toFixed(1)}`;
    // What actually decides the verdict is |reading − baseline| against the
    // threshold, so the margin is the number worth reading when a block is
    // sitting near its own crossover — which is the whole reason to sweep it.
    const marginText =
        ma === null || threshold === null
            ? ''
            : `Δ ${Math.abs(ma - (baseline ?? 0)).toFixed(1)} of ${threshold.toFixed(1)} mA`;

    const detail: string[] = [];
    detail.push(`idle detector: ${detectorText(entry.digital)}`);
    detail.push(
        ma === null
            ? `current detector: ${detectorText(entry.current)}`
            : `current detector: ${detectorText(entry.current)} (${currentText})`
    );
    if (marginText) {
        const configured = numberOrNull(entry.configured_threshold_ma);
        const overridden =
            configured !== null && threshold !== null && configured !== threshold;
        detail.push(
            overridden
                ? `${marginText} — bench override, configured ${configured.toFixed(1)}`
                : marginText
        );
    }
    if (peak !== null) {
        detail.push(`peak ${peak.toFixed(1)} mA over ${peakSamples} read${peakSamples === 1 ? '' : 's'}`);
    }
    if (entry.source) detail.push(`decided by: ${entry.source}`);
    if (entry.zone) detail.push(`drive zone: ${entry.zone}`);
    if (volts !== null) detail.push(`commanded: ${volts.toFixed(1)} V`);
    if (entry.track_bound === false) detail.push('no rail binding — cannot be driven');

    return {
        blockId,
        text,
        occupied,
        unpowered: entry.track_bound === false,
        source: entry.source ?? '',
        flipped: flipped === undefined ? !!entry.flipped : flipped,
        volts,
        currentText,
        peakText,
        marginText,
        detail: detail.join(' · '),
    };
}

/**
 * The transport's peak line: the worst channel on the layout, or why there
 * isn't one yet.
 *
 * Named as its own pure function because it is the sentence the whole feature
 * exists to produce — the number an operator reads off to pick a channel's
 * `baseline_ma` and `threshold_ma` — and it has to stay legible in the three
 * states it can be in: nothing measured yet, measured but silent, measured with
 * a winner.
 */
export function peakSummaryText(peak: BenchPeak | null | undefined): string {
    const ma = numberOrNull(peak?.peak_ma);
    const samples = Number(peak?.samples) || 0;
    if (!samples) return 'No current readings yet';
    if (ma === null || !peak?.block_id) return `No current channel read a number over ${samples} reads`;
    return `Peak ${ma.toFixed(1)} mA on ${shortBlockName(String(peak.block_id))} over ${samples} reads`;
}

function escapeHtml(value: string): string {
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// ---------------------------------------------------------------------------

class OccupancyBench {
    // Assigned by _buildContentDom, which mountInto calls first — declared
    // definite per the house idiom.
    private _stageEl!: HTMLElement;
    private _statusEl!: HTMLElement;
    private _playBtnEl!: HTMLButtonElement;
    private _thresholdEl!: HTMLInputElement;
    private _driveEl!: HTMLInputElement;
    private _peakEl!: HTMLElement;
    private _peakListEl!: HTMLElement;
    private _resetPeaksBtnEl!: HTMLButtonElement;
    private _footerEl!: HTMLElement;

    private _pollHandle: ReturnType<typeof setInterval> | null = null;
    private _inflight = false;

    /** The open `mountInto` started, which `unmount` waits for. */
    private _entering: Promise<void> | null = null;
    private _schematic: BenchSchematic | null = null;
    private _blockIds: string[] = [];
    private _states: Record<string, BenchBlockState> = {};
    private _flipped = new Set<string>();
    private _running = false;
    private _open = false;
    private _percent = 40;
    /** Full-scale track voltage, as the session reports it. */
    private _maxVoltage = 12;
    /** The two zone boundaries, from the rig's config — null until a bench is
     *  open, which is when they become knowable. See `driveZone`. */
    private _idleDeadbandV: number | null = null;
    private _minDriveV: number | null = null;
    /** One threshold for every current channel, or null to leave each block on
     *  its own configured value. */
    private _thresholdMa: number | null = null;
    /** The worst current channel since the last reset, as the server reports it. */
    private _peak: BenchPeak | null = null;
    private _busy = false;
    private _error: string | null = null;

    /** Build the bench inside `host` and open it. Hardware Setup step 4
     *  hosts the bench this way. */
    mountInto(host: HTMLElement): Promise<void> {
        this._buildContentDom(host);
        this._render();
        this._entering = this._enter();
        return this._entering;
    }

    /** Close a bench built by `mountInto`, giving the controller back. An
     *  open still in flight finishes first, so it cannot take the controller
     *  after the close. */
    async unmount(): Promise<void> {
        await this._entering;
        this._stopPolling();
        await this._leave();
    }

    /** Whether the bench holds the controller. */
    isOpen(): boolean {
        return this._open;
    }

    private _buildContentDom(contentEl: HTMLElement): void {
        contentEl.innerHTML = `
            <div class="bench-status" data-role="status"></div>
            <div class="bench-stage" data-role="stage"></div>
            <div class="bench-transport">
                <button class="bench-play" type="button" data-role="play"></button>
                <label class="bench-threshold">
                    <span class="bench-threshold-label">Drive</span>
                    <span class="bench-threshold-input">
                        <input type="number" data-role="drive" min="0" max="100" step="1"
                               inputmode="decimal" />
                        <span class="bench-threshold-unit">%</span>
                    </span>
                </label>
                <label class="bench-threshold">
                    <span class="bench-threshold-label">Current threshold</span>
                    <span class="bench-threshold-input">
                        <input type="number" data-role="threshold" min="0" step="0.1"
                               placeholder="per block" inputmode="decimal" />
                        <span class="bench-threshold-unit">mA</span>
                    </span>
                </label>
                <span class="bench-footer" data-role="footer"></span>
            </div>
            <div class="bench-peaks">
                <span class="bench-peaks-summary" data-role="peak"></span>
                <span class="bench-peaks-list" data-role="peak-list"></span>
                <button class="bench-peak-reset" type="button" data-role="reset-peaks"
                        title="Start every channel's peak over — press after changing the layout (lifting a loco off, rewiring a block). Changing the drive resets it on its own.">
                    Reset peaks
                </button>
            </div>
        `;
        this._stageEl = contentEl.querySelector('[data-role="stage"]') as HTMLElement;
        this._statusEl = contentEl.querySelector('[data-role="status"]') as HTMLElement;
        this._playBtnEl = contentEl.querySelector('[data-role="play"]') as HTMLButtonElement;
        this._thresholdEl = contentEl.querySelector('[data-role="threshold"]') as HTMLInputElement;
        this._driveEl = contentEl.querySelector('[data-role="drive"]') as HTMLInputElement;
        this._peakEl = contentEl.querySelector('[data-role="peak"]') as HTMLElement;
        this._peakListEl = contentEl.querySelector('[data-role="peak-list"]') as HTMLElement;
        this._resetPeaksBtnEl = contentEl.querySelector(
            '[data-role="reset-peaks"]'
        ) as HTMLButtonElement;
        this._footerEl = contentEl.querySelector('[data-role="footer"]') as HTMLElement;

        this._playBtnEl.addEventListener('click', () => void this._togglePlay());
        this._resetPeaksBtnEl.addEventListener('click', () => void this._resetPeaks());
        // Commit on blur and on Enter, the same contract as the block editor's
        // numeric fields — a threshold sweep is typed, not stepped, and firing
        // per keystroke would send a request for every digit of "12.5".
        this._thresholdEl.addEventListener('change', () => void this._commitThreshold());
        this._thresholdEl.addEventListener('keydown', (event) => {
            if ((event as KeyboardEvent).key === 'Enter') {
                event.preventDefault();
                void this._commitThreshold();
            }
        });
        // Same commit contract as the threshold box, and for a stronger reason:
        // this one moves real rails, so firing per keystroke would sweep the
        // track through 1 %, 12 %, 120 %-clamped on the way to "12".
        this._driveEl.addEventListener('change', () => void this._commitDrive());
        this._driveEl.addEventListener('keydown', (event) => {
            if ((event as KeyboardEvent).key === 'Enter') {
                event.preventDefault();
                void this._commitDrive();
            }
        });
        // Delegated: the schematic is re-rendered on every poll, so per-button
        // listeners would be re-bound four times a second.
        this._stageEl.addEventListener('click', (event) => {
            const target = (event.target as HTMLElement | null)?.closest('[data-block]');
            const blockId = target?.getAttribute('data-block');
            if (blockId) void this._toggleFlip(blockId);
        });
    }

    // ----- bench lifecycle -------------------------------------------------

    /** Load the layout, open the bench, and start polling. The bench is a
     *  Setup tool, so outside Setup it says so instead of asking the server. */
    private async _enter(): Promise<void> {
        this._busy = true;
        this._error = null;
        this._render();
        try {
            if (!this._schematic) await this._loadSchematic();
            if (!setupToolsAllowed(window.app?.sessionState)) {
                throw new Error(`The Occupancy Bench ${SETUP_TOOL_HINT.charAt(0).toLowerCase()}${SETUP_TOOL_HINT.slice(1)}`);
            }
            const result = await api.openOccupancyBench();
            if (result?.ok === false) throw new Error(result.error || 'could not open the bench');
            this._applyStatus(result);
            this._open = true;
        } catch (err) {
            this._open = false;
            this._error = (err as Error)?.message || String(err);
        } finally {
            this._busy = false;
        }
        this._render();
        if (this._open) {
            await this._pollOnce();
            this._startPolling();
        }
    }

    /** Close the bench. Closing completes the safe state and leaves the
     *  session in Setup; it never activates an environment (design_v2.md
     *  "Setup tools", Leaving Setup). Activate is the way back to Live.
     *
     *  Best-effort on the way out: the panel is already closing, and the session
     *  expires and de-energizes on its own timer regardless. */
    private async _leave(): Promise<void> {
        if (!this._open) return;
        this._open = false;
        this._running = false;
        try {
            await api.closeOccupancyBench();
        } catch {
            /* the idle timeout is the backstop */
        }
    }

    private async _loadSchematic(): Promise<void> {
        const network = await api.getNetwork();
        const tiles: BenchTile[] = Array.isArray(network?.tracks) ? network.tracks : [];
        this._schematic = buildSchematic(tiles, segmentBlockIndex(network?.blocks || {}));
    }

    // ----- commands --------------------------------------------------------

    private async _togglePlay(): Promise<void> {
        if (!this._open || this._busy) return;
        await this._commandDrive(!this._running);
    }

    private async _toggleFlip(blockId: string): Promise<void> {
        // A closed bench commands nothing, and `open()` does not carry a flip
        // set — so a flip pressed while closed would be silently discarded by
        // the next status the server sends back. Refusing the press is honest;
        // the buttons render disabled for the same reason.
        if (this._busy || !this._open) return;
        if (this._flipped.has(blockId)) this._flipped.delete(blockId);
        else this._flipped.add(blockId);
        // Repaint the pressed state before the round trip: a direction flip is a
        // button press, and a button that only moves once the rails have answered
        // reads as a dropped click.
        this._render();
        await this._commandDrive(this._running);
    }

    /** Send the drive level, keeping the running state exactly as it is.
     *
     *  Settable stopped as well as running: choosing the level and then pressing
     *  Play is the ordinary way to approach a rig, and a control that only
     *  worked while energized would force every change to go through the track.
     *  When it IS running the rails follow immediately, which is the point — a
     *  sweep down through the lighting band is meant to be watched, not
     *  reconstructed from a sequence of stop/set/start.
     */
    private async _commitDrive(): Promise<void> {
        const next = parsePercent(this._driveEl.value);
        if (next === null || !this._open) {
            // Unreadable, or nothing to apply it to. Put the box back to what
            // the bench actually holds rather than leaving a number on screen
            // that commands nothing.
            this._renderTransport();
            return;
        }
        if (next === this._percent) return;
        this._percent = next;
        await this._commandDrive(this._running);
    }

    /** Send whatever is in the threshold box: a number, or null when it is
     *  empty (each block back on its own configured threshold). */
    private async _commitThreshold(): Promise<void> {
        const next = parseThreshold(this._thresholdEl.value);
        if (!this._open) {
            // Nothing to apply it to. Put the box back to what the bench holds
            // rather than leaving a number on screen that decides nothing.
            this._renderTransport();
            return;
        }
        if (next === this._thresholdMa) return;
        this._busy = true;
        this._render();
        try {
            const result = await api.setOccupancyBenchThreshold(next);
            if (result?.ok === false) throw new Error(result.error || 'the threshold was refused');
            this._applyStatus(result);
            this._error = null;
        } catch (err) {
            this._error = (err as Error)?.message || String(err);
        } finally {
            this._busy = false;
        }
        this._render();
    }

    /** Clear every channel's running peak and show the empty state at once.
     *
     *  The local clear is not just optimism about the round trip: the peaks live
     *  in `_states`, which only a poll replaces, so a reset that waited for the
     *  server would leave the old numbers on screen for up to a poll period —
     *  long enough to read them as the post-reset measurement. */
    private async _resetPeaks(): Promise<void> {
        if (!this._open || this._busy) return;
        for (const state of Object.values(this._states)) {
            state.peak_ma = null;
            state.peak_samples = 0;
        }
        this._peak = null;
        this._busy = true;
        this._render();
        try {
            const result = await api.resetOccupancyBenchPeaks();
            if (result?.ok === false) throw new Error(result.error || 'the reset was refused');
            this._applyStatus(result);
            this._error = null;
        } catch (err) {
            this._error = (err as Error)?.message || String(err);
        } finally {
            this._busy = false;
        }
        this._render();
        await this._pollOnce();
    }

    private async _commandDrive(running: boolean): Promise<void> {
        // The session drops its peaks whenever the drive actually changes, and
        // every caller here changes something. Drop the displayed ones with it,
        // so the panel never shows a peak measured at the previous drive level
        // beside the new one's live reading.
        for (const state of Object.values(this._states)) {
            state.peak_ma = null;
            state.peak_samples = 0;
        }
        this._peak = null;
        this._busy = true;
        this._render();
        try {
            const result = await api.setOccupancyBenchDrive(
                running,
                this._percent,
                [...this._flipped]
            );
            if (result?.ok === false) throw new Error(result.error || 'the drive was refused');
            this._applyStatus(result);
            this._error = null;
        } catch (err) {
            this._error = (err as Error)?.message || String(err);
        } finally {
            this._busy = false;
        }
        this._render();
    }

    /** Adopt the session's own view of the drive state. The server is the
     *  authority on what the rails are actually doing — a refused or clamped
     *  command must not leave the panel claiming otherwise.
     *
     *  `adoptIntent` is false for a poll that lands while a command is in
     *  flight. Polls run at 4 Hz against a command that takes a round trip, so
     *  one routinely arrives carrying the state from BEFORE the click — adopting
     *  it would snap the arrow and the dashes back for a frame and then forward
     *  again when the command's own reply lands. The block list is adopted
     *  either way: it is not something a click changes. */
    private _applyStatus(payload: any, adoptIntent = true): void {
        if (!payload || typeof payload !== 'object') return;
        if (Array.isArray(payload.block_ids)) this._blockIds = payload.block_ids.map(String);
        // Adopted on every status, `adoptIntent` or not, for the same reason the
        // block list is: these are the rig's configuration, not a thing a click
        // in this panel changes.
        if ('max_voltage' in payload) {
            this._maxVoltage = numberOrNull(payload.max_voltage) ?? this._maxVoltage;
        }
        if ('idle_deadband_v' in payload) {
            this._idleDeadbandV = numberOrNull(payload.idle_deadband_v);
        }
        if ('current_sense_min_drive_v' in payload) {
            this._minDriveV = numberOrNull(payload.current_sense_min_drive_v);
        }
        if (!adoptIntent) return;
        if (typeof payload.running === 'boolean') this._running = payload.running;
        if (Number.isFinite(Number(payload.percent))) this._percent = Number(payload.percent);
        if (Array.isArray(payload.flipped)) this._flipped = new Set(payload.flipped.map(String));
        if ('threshold_ma' in payload) this._thresholdMa = numberOrNull(payload.threshold_ma);
    }

    // ----- polling ---------------------------------------------------------

    private _startPolling(): void {
        this._stopPolling();
        this._pollHandle = setInterval(() => void this._pollOnce(), Math.round(1000 / REFRESH_HZ));
    }

    private _stopPolling(): void {
        if (this._pollHandle !== null) {
            clearInterval(this._pollHandle);
            this._pollHandle = null;
        }
    }

    private async _pollOnce(): Promise<void> {
        // A read that outruns its period would otherwise stack up behind itself;
        // a read-back panel only ever wants the latest answer.
        if (this._inflight || !this._open) return;
        this._inflight = true;
        try {
            const payload = await api.getOccupancyBenchState();
            if (payload?.open === false) {
                // The session expired under us (an idle timeout, or another
                // screen took the controller). Say so rather than freezing on
                // the last good read.
                this._open = false;
                this._running = false;
                this._stopPolling();
                this._error = 'the bench released the controller — reopen it';
            } else if (payload?.ok === false) {
                this._error = payload.error || 'the bench read failed';
            } else {
                this._states = payload?.blocks || {};
                this._peak = payload?.peak ?? null;
                this._applyStatus(payload, !this._busy);
                this._error = null;
            }
            this._render();
        } catch (err) {
            this._error = (err as Error)?.message || String(err);
            this._render();
        } finally {
            this._inflight = false;
        }
    }

    // ----- render ----------------------------------------------------------

    private _render(): void {
        if (!this._stageEl) return;
        this._renderStatus();
        this._renderStage();
        this._renderTransport();
    }

    private _renderStatus(): void {
        if (this._error) {
            this._statusEl.hidden = false;
            this._statusEl.className = 'bench-status bench-status-danger';
            this._statusEl.textContent = this._error;
            return;
        }
        if (this._busy) {
            this._statusEl.hidden = false;
            this._statusEl.className = 'bench-status bench-status-info';
            this._statusEl.textContent = 'Taking the controller…';
            return;
        }
        if (!this._open) {
            this._statusEl.hidden = false;
            this._statusEl.className = 'bench-status bench-status-info';
            this._statusEl.textContent = 'The bench is closed. Fold this section and open it again to take the rig.';
            return;
        }
        this._statusEl.hidden = false;
        this._statusEl.className = 'bench-status bench-status-info';
        this._statusEl.textContent = 'The bench holds every bound block at one voltage. Folding this section ends the bench.';
    }

    private _renderStage(): void {
        if (!this._schematic || !this._schematic.strokes.length) {
            this._stageEl.innerHTML =
                '<div class="empty-state">No track in this layout to bench.</div>';
            return;
        }
        const { viewBox } = this._schematic;
        const strokes = this._schematic.strokes
            .map((stroke) => this._strokeSvg(stroke))
            .join('');
        // Labels ride in HTML over the SVG rather than as <text>: they carry a
        // background and a state colour, and an HTML box gets those from the
        // stylesheet's own tokens instead of hand-placed SVG rectangles.
        const labels = Object.entries(this._schematic.centroids)
            .map(([blockId, point]) => this._labelHtml(blockId, point, viewBox))
            .join('');
        this._stageEl.innerHTML = `
            <div class="bench-canvas" style="aspect-ratio:${viewBox.width} / ${viewBox.height}">
                <svg class="bench-svg" viewBox="${viewBox.x} ${viewBox.y} ${viewBox.width} ${
                    viewBox.height
                }" preserveAspectRatio="xMidYMid meet" aria-hidden="true">${strokes}</svg>
                ${labels}
            </div>
        `;
    }

    private _strokeSvg(stroke: BenchStroke): string {
        const classes = ['bench-rail'];
        if (!stroke.blockId) {
            classes.push('bench-rail-unowned');
        } else {
            const flipped = this._flipped.has(stroke.blockId);
            const row = buildRow(stroke.blockId, this._states[stroke.blockId], flipped);
            if (row.occupied) classes.push('bench-rail-occupied');
            else if (this._running) classes.push('bench-rail-live');
            if (flipped) classes.push('bench-rail-flipped');
        }
        return `<line class="${classes.join(' ')}" x1="${stroke.from.x.toFixed(2)}" y1="${stroke.from.y.toFixed(
            2
        )}" x2="${stroke.to.x.toFixed(2)}" y2="${stroke.to.y.toFixed(2)}" />`;
    }

    private _labelHtml(
        blockId: string,
        point: BenchPoint,
        viewBox: { x: number; y: number; width: number; height: number }
    ): string {
        // The panel's own flip set, the same source the rails read — see
        // `buildRow`. The two used to disagree, and the click looked half-dead.
        const row = buildRow(blockId, this._states[blockId], this._flipped.has(blockId));
        // Percent of the viewBox, so the label tracks its block through any
        // resize — the SVG scales, the overlay must scale with it.
        const left = ((point.x - viewBox.x) / viewBox.width) * 100;
        const top = ((point.y - viewBox.y) / viewBox.height) * 100;
        const classes = ['bench-block', anchorClass(left)];
        if (row.occupied) classes.push('bench-block-occupied');
        if (row.flipped) classes.push('bench-block-flipped');
        if (row.unpowered) classes.push('bench-block-unpowered');
        const volts =
            row.volts === null || row.volts === 0 ? '' : `${row.volts > 0 ? '+' : ''}${row.volts.toFixed(1)}V`;
        return `
            <div class="${classes.join(' ')}" style="left:${left.toFixed(3)}%;top:${top.toFixed(3)}%">
                <button class="bench-block-btn" type="button" data-block="${escapeHtml(blockId)}"
                        title="${escapeHtml(directionTitle(row, this._open))}"
                        aria-label="${escapeHtml(directionLabel(row))}"
                        ${this._open ? '' : 'disabled'}>
                    <i class="ti ${
                        row.flipped ? 'ti-arrow-narrow-left' : 'ti-arrow-narrow-right'
                    } bench-block-arrow" aria-hidden="true"></i>
                    <span class="bench-block-name">${escapeHtml(shortBlockName(blockId))}</span>
                    ${volts ? `<span class="bench-block-volts">${escapeHtml(volts)}</span>` : ''}
                    <i class="ti ti-switch-horizontal bench-block-flip" aria-hidden="true"></i>
                </button>
                <span class="bench-block-readout" title="${escapeHtml(row.detail)}">${escapeHtml(
                    row.text
                )}</span>
                <span class="bench-block-ma" title="${escapeHtml(
                    row.marginText || row.detail
                )}">${escapeHtml(row.currentText)}</span>
            </div>
        `;
    }

    private _renderTransport(): void {
        const label = this._running ? '■ Stop' : '▶ Play';
        this._playBtnEl.textContent = label;
        this._playBtnEl.disabled = !this._open || this._busy;
        this._playBtnEl.classList.toggle('bench-play-running', this._running);
        this._playBtnEl.setAttribute('aria-pressed', this._running ? 'true' : 'false');

        this._thresholdEl.disabled = !this._open || this._busy;
        // Never overwrite what the operator is part-way through typing — a poll
        // lands every 250 ms and would eat the keystroke before the commit.
        if (document.activeElement !== this._thresholdEl) {
            this._thresholdEl.value = this._thresholdMa === null ? '' : String(this._thresholdMa);
        }
        this._thresholdEl.title =
            this._thresholdMa === null
                ? 'Every block is judged against its own configured threshold. ' +
                  'Enter a value to override all of them for this bench session.'
                : `Every current channel is judged against ${this._thresholdMa} mA. ` +
                  'Clear the box to put each block back on its configured threshold.';

        this._driveEl.disabled = !this._open || this._busy;
        if (document.activeElement !== this._driveEl) {
            this._driveEl.value = String(this._percent);
        }

        this._renderPeaks();

        const volts = (this._percent / 100) * this._maxVoltage;
        const zone = driveZone(volts, this._idleDeadbandV, this._minDriveV);
        this._driveEl.title = zone
            ? `${volts.toFixed(1)} V of ${this._maxVoltage} V — the ${zone} zone: ${ZONE_HELP[zone]}.`
            : 'Percent of full-scale track voltage.';

        // The zone rides in the footer beside the level, because the level alone
        // does not say what is being measured — 12 % and 40 % put the question to
        // two different detectors, and an operator reading a channel's peak has
        // to know which one answered.
        // Inside the parenthesis with the volts, not appended after it: the zone
        // is a property of that level, and hanging it off the end split
        // "40% (4.8 V)" from the "when running" that qualifies it.
        const level = zone
            ? `${this._percent}% (${volts.toFixed(1)} V, ${zone} zone)`
            : `${this._percent}% (${volts.toFixed(1)} V)`;
        const occupied = this._blockIds.filter((id) => this._states[id]?.overall).length;
        this._footerEl.textContent = this._running
            ? `${level} on ${this._blockIds.length} blocks · ${occupied} reading occupied`
            : `Stopped · ${level} when running · ${occupied} reading occupied`;
    }

    /**
     * The peaks strip: the layout-wide worst channel, then every block's own.
     *
     * Its own row rather than a fourth chip on each block label, which is where
     * this started and where it does not fit — a fourth chip pushed two adjacent
     * labels into each other and clipped one block's peak behind another's. The
     * schematic is for spotting WHICH block is misbehaving; this row is for
     * reading numbers off to write into a channel's baseline and threshold, and
     * a flat list beats hunting five labels scattered across a diagram for that.
     */
    private _renderPeaks(): void {
        this._peakEl.textContent = peakSummaryText(this._peak);
        this._peakEl.title =
            'The largest-magnitude current any channel has read since the last ' +
            'reset. With the track empty this is the number a threshold has to ' +
            'clear; with a locomotive on a block it is what that block actually draws.';
        this._resetPeaksBtnEl.disabled = !this._open || this._busy;

        const worst = this._peak?.block_id ?? null;
        this._peakListEl.innerHTML = this._blockIds
            .map((blockId) => {
                const row = buildRow(blockId, this._states[blockId], this._flipped.has(blockId));
                const classes = ['bench-peaks-item'];
                if (blockId === worst) classes.push('bench-peaks-item-worst');
                // An unmeasured channel still gets a slot: a block missing from
                // the list reads as a block with a quiet channel, and those are
                // the opposite finding.
                const value = row.peakText ? row.peakText.replace(/^pk /, '') : '—';
                return `<span class="${classes.join(' ')}" title="${escapeHtml(row.detail)}">
                    <span class="bench-peaks-item-name">${escapeHtml(shortBlockName(blockId))}</span>
                    <span class="bench-peaks-item-ma">${escapeHtml(value)}</span>
                </span>`;
            })
            .join('');
    }

}

/** `BLK_4F7A2F` -> `4F7A2F`. The prefix is on every row and earns no space in a
 *  label that has to sit inside the layout. */
export function shortBlockName(blockId: string): string {
    return blockId.replace(/^BLK_/, '');
}

/**
 * The block button's tooltip, which leads with what pressing it DOES.
 *
 * The chip used to carry a ▶ / ◀ glyph for direction, which is the play-button
 * glyph: it read as "run this block", made the one Play/Stop at the bottom look
 * like a duplicate, and left the direction control invisible — nobody presses a
 * button they have read as a status icon. The glyph is now a plain direction
 * arrow beside a swap icon, and the tooltip names the action first and the
 * detector readings second.
 */
export function directionTitle(row: BenchRow, benchOpen = true): string {
    const now = row.flipped ? 'reversed' : 'forward';
    const next = row.flipped ? 'forward' : 'reversed';
    const at = row.volts === null || row.volts === 0 ? '' : ` at ${row.volts.toFixed(1)} V`;
    let action: string;
    if (!benchOpen) {
        action = 'The bench does not hold the controller — nothing can be driven yet';
    } else if (row.unpowered) {
        action = 'No rail binding — this block cannot be driven';
    } else {
        action = `Driving ${now}${at} · click to flip to ${next}`;
    }
    return `${action}\n${row.detail}`;
}

/** The same thing for a screen reader, which gets no tooltip and no arrow. */
export function directionLabel(row: BenchRow): string {
    const now = row.flipped ? 'reversed' : 'forward';
    return `Block ${shortBlockName(row.blockId)}, driving ${now} — flip voltage direction`;
}

/**
 * Which way a block's label grows from its centroid, given the centroid's
 * horizontal position as a percentage of the frame.
 *
 * A label is a button plus a readout — wide enough that centring it on a block
 * near the edge of the layout runs it off the panel, and the readout is the half
 * that goes. So the outer thirds anchor inward: a block on the right edge hangs
 * its label to the left of its centroid and puts the readout on the inside,
 * keeping the button against the rail it names. Only the middle third, which has
 * room either way, stays centred.
 */
export function anchorClass(leftPercent: number): string {
    if (leftPercent > 66) return 'bench-block-right';
    if (leftPercent < 34) return 'bench-block-left';
    return 'bench-block-centre';
}

/** Build an Occupancy Bench inside `host` and open it; `close()` closes it. */
export function mountOccupancyBench(host: HTMLElement): { close(): Promise<void>; isOpen(): boolean } {
    const bench = new OccupancyBench();
    void bench.mountInto(host);
    return { close: () => bench.unmount(), isOpen: () => bench.isOpen() };
}
