/**
 * OccupancyMonitor — live read-back of what the occupancy detectors actually
 * see, one row per detected block.
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
 *
 * Window chrome (drag, resize, z-order, persistence) is owned by FloatingWindow
 * (lib/window-manager.ts); this file builds and updates only its own content.
 */

import { api } from '../lib/api';
import { FloatingWindow, windowManager, type WindowGeometry } from '../lib/window-manager';
import {
    browserWindowPersistenceEnv,
    loadWindowState,
    saveWindowState,
    ensureRegistered,
} from '../lib/window-persistence';

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

const WINDOW_ID = 'occupancy-monitor';
const DEFAULT_WIDTH = 540;
const DEFAULT_HEIGHT = 420;
const MIN_WIDTH = 380;
const MAX_WIDTH = 960;
const FLASH_MS = 900;

const REFRESH_OPTIONS = [
    { label: '10 Hz', hz: 10 },
    { label: '4 Hz', hz: 4 },
    { label: '1 Hz', hz: 1 },
    { label: 'Every 5s', hz: 0.2 },
    { label: 'On-demand', hz: 0 },
];

const FILTER_OPTIONS: Array<{ label: string; value: OccupancyFilter }> = [
    { label: 'All blocks', value: 'all' },
    { label: 'Occupied only', value: 'occupied' },
    { label: 'Settling only', value: 'settling' },
];

function escapeHtml(value: unknown): string {
    if (value === null || value === undefined) return '';
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

class OccupancyMonitor {
    // Assigned by _buildContentDom, which FloatingWindow calls synchronously from
    // _buildFloatingWindow during init() — declared definite per the house idiom.
    private _window!: FloatingWindow;
    private _bodyEl!: HTMLElement;
    private _linkEl!: HTMLElement;
    private _footerEl!: HTMLElement;
    private _rateSelectEl!: HTMLSelectElement;
    private _filterSelectEl!: HTMLSelectElement;
    private _refreshBtnEl!: HTMLElement;

    private _persistenceEnv = browserWindowPersistenceEnv();
    private _pollHandle: ReturnType<typeof setInterval> | null = null;
    private _flashHandle: ReturnType<typeof setTimeout> | null = null;
    private _inflight = false;

    private _snapshot: RawOccupancySnapshot | null = null;
    private _flashing = new Set<string>();
    private _visible = false;
    private _refreshHz = 4;
    private _filter: OccupancyFilter = 'all';
    private _error: string | null = null;

    init(): void {
        const saved = loadWindowState(this._persistenceEnv, WINDOW_ID);
        this._visible = saved ? !saved.hidden : false;
        const settings = saved?.occupancyMonitor;
        if (settings) {
            if (Number.isFinite(Number(settings.refreshHz))) {
                this._refreshHz = Number(settings.refreshHz);
            }
            if (FILTER_OPTIONS.some((o) => o.value === settings.filter)) {
                this._filter = settings.filter as OccupancyFilter;
            }
        }

        this._buildFloatingWindow(saved?.geometry);
        ensureRegistered(this._persistenceEnv, WINDOW_ID, 'occupancy-monitor');
        this._wireToggleButton();
        this._wireControls();

        this._rateSelectEl.value = String(this._refreshHz);
        this._filterSelectEl.value = this._filter;
        this._render();
        if (this._visible) {
            this._pollOnce();
            this._startPolling();
        }
    }

    // ----- window chrome ---------------------------------------------------

    private _buildFloatingWindow(saved?: WindowGeometry): void {
        const geometry: Partial<WindowGeometry> = {};
        if (saved && typeof saved.left === 'number' && typeof saved.top === 'number') {
            geometry.left = saved.left;
            geometry.top = saved.top;
        } else {
            geometry.left = Math.max(20, window.innerWidth - DEFAULT_WIDTH - 20);
            geometry.top = 120;
        }
        geometry.width = saved?.width && saved.width > 0 ? saved.width : DEFAULT_WIDTH;
        geometry.height = saved?.height && saved.height > 0 ? saved.height : DEFAULT_HEIGHT;

        this._window = new FloatingWindow({
            id: WINDOW_ID,
            kind: 'occupancy-monitor',
            manager: windowManager,
            title: 'Raw Occupancy',
            minW: MIN_WIDTH,
            maxW: MAX_WIDTH,
            initialGeometry: geometry,
            initialHidden: !this._visible,
            content: { mount: (el) => this._buildContentDom(el) },
            // A singleton toggled from the toolbar, so its × hides rather than
            // destroys — the same contract as diagnostics and the train monitor.
            onCloseRequested: () => this.hide(),
            onGeometryChange: () => this._persist(),
        });
    }

    private _buildContentDom(contentEl: HTMLElement): void {
        contentEl.innerHTML = `
            <div class="occ-toolbar">
                <label class="occ-field">
                    Refresh
                    <select data-role="rate"></select>
                </label>
                <label class="occ-field">
                    Show
                    <select data-role="filter"></select>
                </label>
                <button class="occ-icon-btn" type="button" data-role="refresh" title="Read now">↻</button>
            </div>
            <div class="occ-link" data-role="link" hidden></div>
            <div class="occ-body" data-role="body"></div>
            <div class="occ-footer" data-role="footer"></div>
        `;
        this._bodyEl = contentEl.querySelector('[data-role="body"]') as HTMLElement;
        this._linkEl = contentEl.querySelector('[data-role="link"]') as HTMLElement;
        this._footerEl = contentEl.querySelector('[data-role="footer"]') as HTMLElement;
        this._rateSelectEl = contentEl.querySelector('[data-role="rate"]') as HTMLSelectElement;
        this._filterSelectEl = contentEl.querySelector('[data-role="filter"]') as HTMLSelectElement;
        this._refreshBtnEl = contentEl.querySelector('[data-role="refresh"]') as HTMLElement;

        this._rateSelectEl.innerHTML = REFRESH_OPTIONS.map(
            (o) => `<option value="${o.hz}">${escapeHtml(o.label)}</option>`
        ).join('');
        this._filterSelectEl.innerHTML = FILTER_OPTIONS.map(
            (o) => `<option value="${o.value}">${escapeHtml(o.label)}</option>`
        ).join('');
    }

    private _wireToggleButton(): void {
        const btn = document.getElementById('btn-occupancy-toggle');
        if (btn) btn.addEventListener('click', () => this.toggle());
    }

    private _wireControls(): void {
        this._rateSelectEl.addEventListener('change', () => {
            const hz = Number(this._rateSelectEl.value);
            this._refreshHz = Number.isFinite(hz) ? hz : 4;
            this._persist();
            this._stopPolling();
            if (this._visible) {
                this._pollOnce();
                this._startPolling();
            }
        });
        this._filterSelectEl.addEventListener('change', () => {
            this._filter = this._filterSelectEl.value as OccupancyFilter;
            this._persist();
            this._render();
        });
        this._refreshBtnEl.addEventListener('click', () => this._pollOnce());
    }

    // ----- visibility ------------------------------------------------------

    toggle(): void {
        if (this._visible) this.hide();
        else this.show();
    }

    show(): void {
        this._visible = true;
        this._window.setHidden(false);
        this._window.focus();
        this._persist();
        this._pollOnce();
        this._startPolling();
    }

    hide(): void {
        this._visible = false;
        this._window.setHidden(true);
        this._persist();
        this._stopPolling();
    }

    // ----- polling ---------------------------------------------------------

    private _startPolling(): void {
        this._stopPolling();
        if (!this._refreshHz || this._refreshHz <= 0) return;
        const period = Math.max(50, Math.round(1000 / this._refreshHz));
        this._pollHandle = setInterval(() => this._pollOnce(), period);
    }

    /** Stop the timer. Deliberately does NOT cancel a request already in flight:
     *  `_inflight` is what keeps requests from stacking, so an abort here would
     *  buy nothing and cost a logged rejection — `show()` starts the timer right
     *  after firing its first read, and aborting that read logged an API error on
     *  every single open of the panel. */
    private _stopPolling(): void {
        if (this._pollHandle !== null) {
            clearInterval(this._pollHandle);
            this._pollHandle = null;
        }
    }

    private async _pollOnce(): Promise<void> {
        // At 10 Hz a response slower than the period would otherwise stack up
        // behind itself. Skipping is right for a read-back panel: it only ever
        // wants the latest answer, never a queue of stale ones.
        if (this._inflight) return;
        this._inflight = true;
        try {
            const snapshot = (await api.getRawOccupancy()) as RawOccupancySnapshot;
            const changed = changedBlocks(this._snapshot, snapshot);
            this._snapshot = snapshot;
            this._error = null;
            if (changed.size) this._flash(changed);
            this._render();
        } catch (err) {
            this._error = (err as Error)?.message || String(err);
            this._render();
        } finally {
            this._inflight = false;
        }
    }

    private _flash(blockIds: Set<string>): void {
        for (const id of blockIds) this._flashing.add(id);
        if (this._flashHandle !== null) clearTimeout(this._flashHandle);
        this._flashHandle = setTimeout(() => {
            this._flashing.clear();
            this._flashHandle = null;
            this._render();
        }, FLASH_MS);
    }

    // ----- render ----------------------------------------------------------

    private _render(): void {
        if (!this._bodyEl) return;
        this._renderLink();
        this._renderBody();
        this._renderFooter();
    }

    private _renderLink(): void {
        const link = describeLink(this._snapshot?.link);
        if (!link) {
            this._linkEl.hidden = true;
            this._linkEl.textContent = '';
            return;
        }
        this._linkEl.hidden = false;
        this._linkEl.className = `occ-link occ-link-${link.tone}`;
        this._linkEl.textContent = link.text;
    }

    private _renderBody(): void {
        if (this._error) {
            this._bodyEl.innerHTML = `<div class="empty-state empty-state-error">${escapeHtml(
                this._error
            )}</div>`;
            return;
        }
        if (!this._snapshot) {
            this._bodyEl.innerHTML = '<div class="empty-state">Reading…</div>';
            return;
        }
        const rows = buildRows(this._snapshot, this._filter);
        if (!rows.length) {
            const total = Object.keys(this._snapshot.blocks || {}).length;
            const message = total
                ? 'No block matches this filter.'
                : 'No block is being detected. Bind occupancy inputs in Hardware Setup.';
            this._bodyEl.innerHTML = `<div class="empty-state">${escapeHtml(message)}</div>`;
            return;
        }
        this._bodyEl.innerHTML = `
            <div class="occ-head">
                <span>Block</span>
                <span>Source</span>
                <span>Reading</span>
                <span>Drive</span>
                <span class="occ-col-state" title="Raw read · Confirmed verdict">Raw · Conf</span>
            </div>
            ${rows.map((row) => this._rowHtml(row)).join('')}
        `;
    }

    private _rowHtml(row: OccupancyRow): string {
        const classes = ['occ-row'];
        if (row.settling) classes.push('occ-row-settling');
        if (this._flashing.has(row.blockId)) classes.push('occ-row-flash');
        // Only genuinely eventful notes earn a sub-line. The reading's threshold
        // and baseline are static config, not per-tick data — on a layout with
        // twenty current-sense blocks, a sub-line for each doubles the panel's
        // height to repeat something that never moves. It rides in the reading
        // cell's tooltip instead, where it is one hover away when the margin
        // actually matters.
        const sub = row.note ? [escapeHtml(row.note)] : [];
        const debounce = row.debounce
            ? `<div class="occ-debounce">
                   <span class="occ-debounce-label">${escapeHtml(row.debounce.label)} ${
                       row.debounce.count
                   }/${row.debounce.total}</span>
                   <span class="occ-debounce-track"><span class="occ-debounce-fill" style="width:${Math.round(
                       row.debounce.fraction * 100
                   )}%"></span></span>
               </div>`
            : '';
        return `
            <div class="${classes.join(' ')}">
                <span class="occ-block">${escapeHtml(row.blockId)}</span>
                <span class="occ-source">${
                    row.source
                        ? `<span class="chip chip-${row.sourceTone}">${escapeHtml(row.source)}</span>`
                        : ''
                }</span>
                <span class="occ-reading"${
                    row.readingDetail ? ` title="${escapeHtml(row.readingDetail)}"` : ''
                }>${escapeHtml(row.reading) || '<span class="occ-dash">—</span>'}</span>
                <span class="occ-drive">${escapeHtml(row.drive)}${
                    row.zone ? `<span class="occ-zone">${escapeHtml(row.zone)}</span>` : ''
                }</span>
                <span class="occ-state">
                    <i class="occ-dot ${row.raw ? 'occ-dot-on' : ''}" title="Raw read: ${
                        row.raw ? 'present' : 'clear'
                    }"></i>
                    <i class="occ-dot occ-dot-verdict ${
                        row.confirmed ? 'occ-dot-on' : ''
                    }" title="Confirmed verdict: ${row.confirmed ? 'occupied' : 'clear'}"></i>
                </span>
                ${sub.length ? `<div class="occ-sub">${sub.join(' · ')}</div>` : ''}
                ${debounce}
            </div>
        `;
    }

    private _renderFooter(): void {
        if (!this._snapshot) {
            this._footerEl.textContent = '';
            return;
        }
        const total = Object.keys(this._snapshot.blocks || {}).length;
        const parts = [
            `read #${this._snapshot.read_count ?? 0}`,
            `on ≥ ${this._snapshot.on_threshold ?? '?'}`,
            `off ≥ ${this._snapshot.off_threshold ?? '?'}`,
            `${total} block${total === 1 ? '' : 's'}`,
        ];
        this._footerEl.textContent = parts.join(' · ');
    }

    // ----- persistence -----------------------------------------------------

    private _persist(): void {
        saveWindowState(this._persistenceEnv, WINDOW_ID, {
            geometry: this._window.getGeometry(),
            floating: true,
            hidden: !this._visible,
            occupancyMonitor: { refreshHz: this._refreshHz, filter: this._filter },
        });
    }
}

function bootstrap(): void {
    if (window.occupancyMonitor) return; // idempotent
    window.occupancyMonitor = new OccupancyMonitor();
    window.occupancyMonitor.init();
}

// Guarded on `document` so the pure derivation above can be imported by the unit
// tests, which run in Node with no DOM (vite.config.ts sets no jsdom environment).
if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bootstrap);
    } else {
        bootstrap();
    }
}

export { OccupancyMonitor };
