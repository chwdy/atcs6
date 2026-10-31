/**
 * VirtualBench — the workbench for the Virtual Hardware environment.
 *
 * Virtual Hardware runs the real chip drivers, bindings, HAL, and occupancy
 * pipeline against an in-memory device, so everything the app does to the rig
 * and everything the rig tells the app is observable and controllable. This
 * panel is both halves of that, one row per block:
 *
 *   OUTPUT  — what the app is driving onto the rails right now, read back off
 *             the fake PWM chip rather than from the model, so a block the model
 *             thinks is energized and a board that never got the write read
 *             differently. Plus the sweep test, to drive a block deliberately.
 *   INPUT   — what you are telling the app its detectors see. "Train here"
 *             writes every source the block is bound to at once (see
 *             `set_block_presence`), because which one the runtime believes
 *             depends on how hard the block is being driven; the raw bit / mA
 *             controls are behind the Raw toggle for deliberately making the two
 *             disagree.
 *   VERDICT — what the occupancy pipeline concluded from that input. This is the
 *             column that answers "did the app react", and it is deliberately
 *             the last one: input on the left, the app's answer on the right.
 *
 * Why a floating window and not a section in the Hardware Setup modal (where an
 * earlier version of these controls lives): the point of poking a sensor is
 * watching the layout react to it, and a modal covers the layout.
 *
 * Distinct from the Raw Occupancy monitor, which is read-only, environment-
 * agnostic, and about how a reading was reached (raw vs confirmed, debounce,
 * which source answered). This one is Virtual-Hardware-only and is the write
 * side: it exists to cause readings, not to explain them.
 *
 * Window chrome (drag, resize, z-order, persistence) is owned by FloatingWindow
 * (lib/window-manager.ts); this file builds and updates only its own content.
 */

import { api } from '../lib/api';
import { showToast } from '../lib/toast';
import { setupToolsAllowed, SETUP_TOOL_HINT } from '../lib/session-controls';
import {
  VIRTUAL_FAULT_PAIRS,
  clearFaultBody,
  describeFaultRecord,
  faultPairLabel,
  injectFaultBody,
  type VirtualFaultRecord,
} from '../lib/virtual-hardware-faults';
import { FloatingWindow, windowManager, type WindowGeometry } from '../lib/window-manager';
import {
    browserWindowPersistenceEnv,
    loadWindowState,
    saveWindowState,
} from '../lib/window-persistence';

// ---------------------------------------------------------------------------
// Snapshot shapes returned by GET /api/virtual-hardware. Read defensively: the
// endpoint answers 409 outside Virtual Hardware, and a block carries null for
// every reading it has no binding for.

export interface BenchBlock {
    has_digital?: boolean;
    has_current?: boolean;
    has_track?: boolean;
    /** The raw '165 register bit. On an active-low chain this is the OPPOSITE
     *  of "a train is here" — use `detector_present` for that. */
    occupancy_bit?: boolean | null;
    /** The raw bit resolved through the chain's polarity: what the occupancy
     *  pipeline reads. */
    detector_present?: boolean | null;
    active_low?: boolean | null;
    current_ma?: number | null;
    volts?: number | null;
    duty_forward?: number | null;
    duty_reverse?: number | null;
    track_reversed?: boolean | null;
    threshold_ma?: number | null;
    baseline_ma?: number | null;
    /** The occupancy pipeline's verdict — what the app acts on. */
    occupied?: boolean | null;
}

export interface BenchSwitch {
    main?: boolean;
    diverge?: boolean;
    standby?: boolean | null;
}

export interface BenchSnapshot {
    virtual_hardware_active?: boolean;
    blocks?: Record<string, BenchBlock>;
    switches?: Record<string, BenchSwitch>;
    /** Operator faults currently injected — the source of truth for the panel.
     *  `faults` is the low-level transport view the device also reports. */
    injected_faults?: VirtualFaultRecord[];
    faults?: Record<string, boolean>;
}

export interface BenchRigStatus {
    active?: boolean;
    blocks?: string[];
    index?: number | null;
    block_id?: string | null;
    stalled?: boolean;
}

export type BenchFilter = 'all' | 'active' | 'occupied';

// ---------------------------------------------------------------------------
// Row derivation (pure; unit-tested).

export interface BenchRow {
    blockId: string;
    hasTrack: boolean;
    hasDigital: boolean;
    hasCurrent: boolean;
    /** Signed rail voltage, polarity flag already applied. Null = no track binding. */
    volts: number | null;
    dutyForward: number | null;
    dutyReverse: number | null;
    trackReversed: boolean;
    /** The raw register bit, for the Raw controls. Not "a train is here". */
    occupancyBit: boolean | null;
    /** The digital detector's reading with the chain's polarity applied. */
    detectorPresent: boolean | null;
    activeLow: boolean;
    currentMa: number | null;
    thresholdMa: number | null;
    baselineMa: number | null;
    occupied: boolean | null;
    /** Any detector reads present — what the "Place" toggle reflects. */
    present: boolean;
    /** The rails are being driven hard enough to matter (either polarity). */
    driven: boolean;
}

/** Below this the rails count as idle for the panel's arrow glyph. Cosmetic
 *  only — the runtime's own idle deadband is configured in Hardware Setup and
 *  is what actually decides which detector answers. */
const IDLE_VOLTS = 0.05;

export function benchRows(snapshot: BenchSnapshot | null, filter: BenchFilter = 'all'): BenchRow[] {
    const blocks = snapshot?.blocks || {};
    const rows: BenchRow[] = Object.keys(blocks).sort().map((blockId) => {
        const b = blocks[blockId] || {};
        const volts = typeof b.volts === 'number' ? b.volts : null;
        // "Present" is what the detectors are being told, not the verdict: the
        // current channel counts as present only above its own threshold, since
        // a baseline reading is what an empty block looks like.
        const currentPresent =
            typeof b.current_ma === 'number' && typeof b.threshold_ma === 'number'
                ? b.current_ma > (b.baseline_ma ?? 0) + b.threshold_ma
                : false;
        return {
            blockId,
            hasTrack: !!b.has_track,
            hasDigital: !!b.has_digital,
            hasCurrent: !!b.has_current,
            volts,
            dutyForward: typeof b.duty_forward === 'number' ? b.duty_forward : null,
            dutyReverse: typeof b.duty_reverse === 'number' ? b.duty_reverse : null,
            trackReversed: !!b.track_reversed,
            occupancyBit: b.occupancy_bit ?? null,
            detectorPresent: b.detector_present ?? null,
            activeLow: !!b.active_low,
            currentMa: typeof b.current_ma === 'number' ? b.current_ma : null,
            thresholdMa: typeof b.threshold_ma === 'number' ? b.threshold_ma : null,
            baselineMa: typeof b.baseline_ma === 'number' ? b.baseline_ma : null,
            occupied: b.occupied ?? null,
            // The RESOLVED reading, never the raw bit: on an active-low chain
            // the raw bit is the opposite of presence, so a toggle keyed to it
            // offers "Place" on the blocks that already have something on them.
            present: !!b.detector_present || currentPresent,
            driven: volts !== null && Math.abs(volts) > IDLE_VOLTS,
        };
    });
    if (filter === 'occupied') return rows.filter((r) => r.occupied || r.present);
    if (filter === 'active') return rows.filter((r) => r.driven || r.occupied || r.present);
    return rows;
}

/** The output cell's text: a direction glyph and the signed voltage. Split out
 *  because "which way is this block being driven" is the question the panel
 *  gets opened for, and it is one derivation, not three sprinkled through the
 *  markup. */
export function describeDrive(row: BenchRow): { glyph: string; text: string; tone: 'idle' | 'fwd' | 'rev' } {
    if (!row.hasTrack || row.volts === null) return { glyph: '', text: '—', tone: 'idle' };
    if (!row.driven) return { glyph: '·', text: '0.0 V', tone: 'idle' };
    const forward = row.volts > 0;
    return {
        glyph: forward ? '▶' : '◀',
        text: `${row.volts > 0 ? '+' : ''}${row.volts.toFixed(1)} V`,
        tone: forward ? 'fwd' : 'rev',
    };
}

/** The commanded duty pair, as the output cell prints it. */
function dutyText(row: BenchRow): string {
    if (row.dutyForward === null) return '';
    return `${row.dutyForward.toFixed(2)} / ${(row.dutyReverse ?? 0).toFixed(2)}${
        row.trackReversed ? ' ⇄' : ''
    }`;
}

/** What each detector reads, in the terms the pipeline reads them: the digital
 *  chain as present/clear (its raw bit is in the Raw line, where the polarity is
 *  explained), the current channel as its measurement. */
function readsOf(row: BenchRow): string {
    const reads: string[] = [];
    if (row.hasDigital) reads.push(row.detectorPresent ? 'det ●' : 'det ○');
    if (row.hasCurrent) reads.push(`${(row.currentMa ?? 0).toFixed(0)} mA`);
    return reads.join(' · ');
}

function verdictOf(row: BenchRow): { cls: string; text: string } {
    if (row.occupied === null) return { cls: 'vb-dash', text: '—' };
    return {
        cls: row.occupied ? 'vb-verdict is-occupied' : 'vb-verdict',
        text: row.occupied ? 'occupied' : 'clear',
    };
}

function bitLabel(row: BenchRow): string {
    return `bit ${row.occupancyBit ? '1' : '0'}${row.activeLow ? ' (low=on)' : ''}`;
}

/** The virtual train's one-line status, as the strip prints it. */
function rigStatus(rig: BenchRigStatus | null): string {
    if (!rig?.active) return 'no virtual train';
    return `in ${rig.block_id || '?'}${rig.stalled ? ' · stalled' : ''} (${(rig.index ?? 0) + 1} of ${
        (rig.blocks || []).length
    })`;
}

/** Set an element's text only when it differs. Writing the same string back is
 *  harmless but it is also a mutation, and this runs on every node several times
 *  a second. */
function setText(el: Element | null | undefined, text: string): void {
    if (el && el.textContent !== text) el.textContent = text;
}

function rawNote(row: BenchRow): string {
    return `on ≥ ${((row.baselineMa ?? 0) + (row.thresholdMa ?? 0)).toFixed(0)} mA`;
}

function placeTitle(present: boolean): string {
    return present
        ? 'Lift the virtual train off — clears every detector this block is bound to.'
        : 'Put a virtual train here — writes every detector this block is bound to.';
}

// ---------------------------------------------------------------------------

const WINDOW_ID = 'virtual-bench';
const DEFAULT_WIDTH = 660;
// Tall enough that the virtual-train and fault strips under the block table are
// on screen when the panel first opens — they are the two controls a user comes
// here for and would not know to scroll to.
const DEFAULT_HEIGHT = 620;
const MIN_WIDTH = 460;
const MAX_WIDTH = 1100;

const REFRESH_OPTIONS = [
    { label: '10 Hz', hz: 10 },
    { label: '4 Hz', hz: 4 },
    { label: '1 Hz', hz: 1 },
    { label: 'On-demand', hz: 0 },
];

const FILTER_OPTIONS: Array<{ label: string; value: BenchFilter }> = [
    { label: 'All blocks', value: 'all' },
    { label: 'Driven or occupied', value: 'active' },
    { label: 'Occupied only', value: 'occupied' },
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

class VirtualBench {
    // Assigned by _buildContentDom, which FloatingWindow calls synchronously from
    // _buildFloatingWindow during init() — declared definite per the house idiom.
    private _window!: FloatingWindow;
    private _bodyEl!: HTMLElement;
    private _bannerEl!: HTMLElement;
    private _footerEl!: HTMLElement;
    private _rateSelectEl!: HTMLSelectElement;
    private _filterSelectEl!: HTMLSelectElement;
    private _rawToggleEl!: HTMLInputElement;
    private _placeBtnEl!: HTMLButtonElement;
    private _refreshBtnEl!: HTMLElement;

    private _persistenceEnv = browserWindowPersistenceEnv();
    private _pollHandle: ReturnType<typeof setInterval> | null = null;
    /** The read currently out, so an interval tick can skip it and a post-write
     *  read can queue behind it. Null when nothing is in flight. */
    private _inflight: Promise<void> | null = null;

    private _snapshot: BenchSnapshot | null = null;
    private _rig: BenchRigStatus | null = null;
    private _rigDraft = '';
    // Fault-injection drafts: the target id every inject button names, plus an
    // optional operation (read/write) for a transaction fault. `_faultsSig` is
    // the last-rendered injected list, so the poll only rebuilds it on change.
    private _faultTargetDraft = '';
    private _faultOpDraft = '';
    private _faultsSig = '';
    /** The saved Hardware Setup, needed as the payload for the test endpoints.
     *  Fetched on open rather than polled — it changes only in the setup modal. */
    private _config: any = null;
    private _visible = false;
    /** Built by `mountInto`: no window, and nothing saved. */
    private _embedded = false;
    private _refreshHz = 4;
    private _filter: BenchFilter = 'all';
    private _showRaw = false;
    private _placing = false;
    /** Blocks with a SWEEP or a switch throw in flight, so their buttons can show
     *  it and a double-click cannot start a second sweep on the same block. A
     *  placement is deliberately not in here: it is one fast write, and
     *  disabling the button it was clicked with is how a press gets eaten. */
    private _busy = new Set<string>();
    /** The presence the user last asked for, per block, until the read that
     *  follows the write lands. A toggle clicked again before the previous write
     *  settles has to flip the INTENT — flipping the last drawn state re-sends
     *  the command that is already in flight, which reads as a dropped press. */
    private _presenceIntent = new Map<string, boolean>();
    /** Per-block write chain, so two fast clicks reach the rig in the order they
     *  were made rather than racing. */
    private _presenceChain = new Map<string, Promise<void>>();
    /** What the body's DOM SHAPE was built from. While this is unchanged, a read
     *  updates the existing nodes in place instead of replacing them: at 4 Hz a
     *  wholesale `innerHTML` rewrite destroys whatever button the pointer is on
     *  between its mousedown and its mouseup, and the browser then has no
     *  element left to fire `click` at — the press is simply lost. It also wiped
     *  anything being typed into the raw-current field. */
    private _bodyShape: string | null = null;
    /** Set when the endpoint answers 409 — the session is not Virtual Hardware. */
    private _inactive = false;
    private _error: string | null = null;

    init(): void {
        const saved = loadWindowState(this._persistenceEnv, WINDOW_ID);
        this._visible = saved ? !saved.hidden : false;
        const settings = saved?.virtualBench;
        if (settings) {
            if (Number.isFinite(Number(settings.refreshHz))) {
                this._refreshHz = Number(settings.refreshHz);
            }
            if (FILTER_OPTIONS.some((o) => o.value === settings.filter)) {
                this._filter = settings.filter as BenchFilter;
            }
            this._showRaw = !!settings.showRaw;
        }

        this._buildFloatingWindow(saved?.geometry);
        this._wireToggleButton();
        this._wireControls();

        this._rateSelectEl.value = String(this._refreshHz);
        this._filterSelectEl.value = this._filter;
        this._rawToggleEl.checked = this._showRaw;
        this._render();
        if (this._visible) {
            void this._loadConfig();
            void this._pollOnce();
            this._startPolling();
        }
    }

    /** Build the bench inside `host`, with no window around it, and start
     *  reading the rig. The Hardware Setup screen hosts the bench
     *  this way, in step 4 (docs/ui-layout-rewrite-plan.md §7). That screen
     *  covers the layout, so Place on canvas is left out. */
    mountInto(host: HTMLElement): void {
        this._embedded = true;
        this._buildContentDom(host);
        this._placeBtnEl.hidden = true;
        this._wireControls();
        this._rateSelectEl.value = String(this._refreshHz);
        this._filterSelectEl.value = this._filter;
        this._rawToggleEl.checked = this._showRaw;
        this._visible = true;
        this._render();
        void this._loadConfig();
        void this._pollOnce();
        this._startPolling();
    }

    /** Stop reading the rig, for a bench built by `mountInto`. */
    unmount(): void {
        this._visible = false;
        this._stopPolling();
    }

    // ----- window chrome ---------------------------------------------------

    private _buildFloatingWindow(saved?: WindowGeometry): void {
        const geometry: Partial<WindowGeometry> = {};
        if (saved && typeof saved.left === 'number' && typeof saved.top === 'number') {
            geometry.left = saved.left;
            geometry.top = saved.top;
        } else {
            geometry.left = Math.max(20, window.innerWidth - DEFAULT_WIDTH - 20);
            geometry.top = 150;
        }
        geometry.width = saved?.width && saved.width > 0 ? saved.width : DEFAULT_WIDTH;
        geometry.height = saved?.height && saved.height > 0 ? saved.height : DEFAULT_HEIGHT;

        this._window = new FloatingWindow({
            id: WINDOW_ID,
            kind: 'virtual-bench',
            manager: windowManager,
            title: 'Virtual Bench',
            minW: MIN_WIDTH,
            maxW: MAX_WIDTH,
            initialGeometry: geometry,
            initialHidden: !this._visible,
            content: { mount: (el) => this._buildContentDom(el) },
            // A singleton toggled from the toolbar, so its × hides rather than
            // destroys — the same contract as diagnostics and the occupancy monitor.
            onCloseRequested: () => this.hide(),
            onGeometryChange: () => this._persist(),
        });
    }

    private _buildContentDom(contentEl: HTMLElement): void {
        contentEl.innerHTML = `
            <div class="vb-toolbar">
                <label class="vb-field">
                    Refresh
                    <select data-role="rate"></select>
                </label>
                <label class="vb-field">
                    Show
                    <select data-role="filter"></select>
                </label>
                <label class="vb-check" title="Set the '165 bit and the current channel separately, to make the two detectors disagree on purpose.">
                    <input type="checkbox" data-role="raw" /> Raw
                </label>
                <button class="vb-place-btn" type="button" data-role="place"
                    title="Click a block on the layout to put a virtual train on its detectors, or lift one off.">
                    <i class="ti ti-hand-finger" aria-hidden="true"></i> Place on canvas
                </button>
                <button class="vb-icon-btn" type="button" data-role="refresh" title="Read now">↻</button>
            </div>
            <div class="vb-banner" data-role="banner" hidden></div>
            <div class="vb-body" data-role="body"></div>
            <div class="vb-footer" data-role="footer"></div>
        `;
        this._bodyEl = contentEl.querySelector('[data-role="body"]') as HTMLElement;
        this._bannerEl = contentEl.querySelector('[data-role="banner"]') as HTMLElement;
        this._footerEl = contentEl.querySelector('[data-role="footer"]') as HTMLElement;
        this._rateSelectEl = contentEl.querySelector('[data-role="rate"]') as HTMLSelectElement;
        this._filterSelectEl = contentEl.querySelector('[data-role="filter"]') as HTMLSelectElement;
        this._rawToggleEl = contentEl.querySelector('[data-role="raw"]') as HTMLInputElement;
        this._placeBtnEl = contentEl.querySelector('[data-role="place"]') as HTMLButtonElement;
        this._refreshBtnEl = contentEl.querySelector('[data-role="refresh"]') as HTMLElement;

        this._rateSelectEl.innerHTML = REFRESH_OPTIONS.map(
            (o) => `<option value="${o.hz}">${escapeHtml(o.label)}</option>`
        ).join('');
        this._filterSelectEl.innerHTML = FILTER_OPTIONS.map(
            (o) => `<option value="${o.value}">${escapeHtml(o.label)}</option>`
        ).join('');
    }

    private _wireToggleButton(): void {
        const btn = document.getElementById('btn-virtual-bench-toggle');
        if (btn) btn.addEventListener('click', () => this.toggle());
    }

    private _wireControls(): void {
        this._rateSelectEl.addEventListener('change', () => {
            const hz = Number(this._rateSelectEl.value);
            this._refreshHz = Number.isFinite(hz) ? hz : 4;
            this._persist();
            this._stopPolling();
            if (this._visible) {
                void this._pollOnce();
                this._startPolling();
            }
        });
        this._filterSelectEl.addEventListener('change', () => {
            this._filter = this._filterSelectEl.value as BenchFilter;
            this._persist();
            this._render();
        });
        this._rawToggleEl.addEventListener('change', () => {
            this._showRaw = this._rawToggleEl.checked;
            this._persist();
            this._render();
        });
        this._placeBtnEl.addEventListener('click', () => void this._togglePlacing());
        // "Read now" means now: it jumps the queue rather than riding along with
        // a read that may already be stale.
        this._refreshBtnEl.addEventListener('click', () => void this._pollOnce(true));

        // One delegated listener for every row control: the body is re-rendered
        // wholesale on each poll, so per-button listeners would be re-bound
        // several times a second.
        this._bodyEl.addEventListener('click', (e) => void this._onBodyClick(e));
        this._bodyEl.addEventListener('keydown', (e) => {
            const t = e.target as HTMLElement;
            if ((e as KeyboardEvent).key === 'Enter' && t?.dataset?.role === 'raw-ma') {
                void this._setRawCurrent(t.dataset.id || '', (t as unknown as HTMLInputElement).value);
            }
        });
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
        void this._loadConfig();
        void this._pollOnce();
        this._startPolling();
    }

    hide(): void {
        this._visible = false;
        this._window.setHidden(true);
        this._persist();
        this._stopPolling();
        // Placement is a canvas mode the panel owns; leaving it armed after the
        // panel is gone would make clicks on the layout do something invisible.
        if (this._placing) void this._togglePlacing();
    }

    // ----- polling ---------------------------------------------------------

    private _startPolling(): void {
        this._stopPolling();
        if (!this._refreshHz || this._refreshHz <= 0) return;
        const period = Math.max(50, Math.round(1000 / this._refreshHz));
        this._pollHandle = setInterval(() => void this._pollOnce(), period);
    }

    /** Stop the timer. Deliberately does NOT cancel a request already in flight
     *  — `_inflight` is what keeps requests from stacking, and aborting the read
     *  `show()` fires just before starting the timer logged an API error on
     *  every open. Same reasoning as the occupancy monitor. */
    private _stopPolling(): void {
        if (this._pollHandle !== null) {
            clearInterval(this._pollHandle);
            this._pollHandle = null;
        }
    }

    /** Read the rig now, resolving once the panel is showing that read.
     *
     *  An interval tick passes `force = false` and rides along with whatever is
     *  already out — that is what keeps requests from stacking. A read that
     *  FOLLOWS A WRITE must pass `force = true`, because the in-flight response
     *  was issued *before* the write and would land as the newest answer on
     *  screen: the row goes on offering "Place" for a block that now has a train
     *  on it, and the next click re-sends the same command instead of toggling
     *  — the "I had to click it twice" symptom. A forced read queues behind the
     *  stale one rather than being dropped, so the panel always settles on a
     *  post-write answer. */
    private _pollOnce(force = false): Promise<void> {
        const current = this._inflight;
        if (current && !force) return current;
        const next = (current ?? Promise.resolve()).then(() => this._read());
        this._inflight = next;
        void next.finally(() => {
            if (this._inflight === next) this._inflight = null;
        });
        return next;
    }

    private async _loadConfig(): Promise<void> {
        try {
            const res = await api.getHardwareConfig();
            this._config = res?.config || null;
        } catch {
            this._config = null;   // the test buttons report it when pressed
        }
    }

    /** One read and the render it feeds. Never rejects: every outcome, including
     *  a dead server, is a state this panel draws. `_pollOnce` owns the
     *  scheduling around it. */
    private async _read(): Promise<void> {
        try {
            const [snapshot, rig] = await Promise.all([
                api.getHwDebugState() as Promise<BenchSnapshot>,
                api.getVirtualRigScenario().catch(() => null),
            ]);
            this._snapshot = snapshot;
            this._rig = rig?.scenario || null;
            this._inactive = false;
            this._error = null;
            this._render();
        } catch (err: any) {
            // 409 is the ordinary answer outside Virtual Hardware, not a fault:
            // the panel says which environment it needs instead of an error.
            if (err?.status === 409 || /not running in Virtual Hardware/i.test(err?.message || '')) {
                this._inactive = true;
                this._error = null;
                this._snapshot = null;
            } else {
                this._error = err?.message || String(err);
            }
            this._render();
        }
    }

    // ----- row actions -----------------------------------------------------

    private async _onBodyClick(e: Event): Promise<void> {
        const btn = (e.target as HTMLElement)?.closest?.('[data-role]') as HTMLElement | null;
        if (!btn) return;
        const role = btn.dataset.role;
        const id = btn.dataset.id || '';
        switch (role) {
            case 'place-train': return this._setPresence(id, btn.dataset.present !== '1');
            case 'sweep': return this._runTrackTest(id);
            case 'throw-main': return this._runSwitchTest(id, 0);
            case 'throw-diverge': return this._runSwitchTest(id, 1);
            case 'raw-bit': return this._setRawBit(id, btn.dataset.bit !== '1');
            case 'raw-ma-set': {
                const input = this._bodyEl.querySelector<HTMLInputElement>(
                    `input[data-role="raw-ma"][data-id="${CSS.escape(id)}"]`
                );
                return this._setRawCurrent(id, input?.value ?? '');
            }
            case 'rig-start': return this._rigCommand('start', { blocks: this._readRigDraft() });
            case 'rig-advance': return this._rigCommand('advance');
            case 'rig-stall': return this._rigCommand('stall', { stalled: !this._rig?.stalled });
            case 'rig-remove': return this._rigCommand('remove');
            case 'fault-inject': return this._injectFault(Number(btn.dataset.fp));
            case 'fault-clear-one': return this._clearOneFault(Number(btn.dataset.fi));
            case 'fault-clear': return this._clearFaults();
            default: return;
        }
    }

    /** Put a virtual object on a block's detectors, or lift it off.
     *
     *  Deliberately not gated on `_busy` and deliberately not disabling its own
     *  button: a placement is one fast write, and a toggle that ignores presses
     *  while the last one settles loses every second click of a quick double.
     *  Presses land on the intent, which the row draws immediately; the writes
     *  themselves are chained per block so they reach the rig in order. */
    private async _setPresence(blockId: string, present: boolean): Promise<void> {
        if (!blockId) return;
        this._presenceIntent.set(blockId, present);
        this._render();
        const send = (this._presenceChain.get(blockId) ?? Promise.resolve()).then(async () => {
            try {
                await api.setHwDebugPresence(blockId, present);
                await this._pollOnce(true);
            } catch (err: any) {
                showToast(err?.message || `Could not set ${blockId}.`, 'error');
            }
        });
        this._presenceChain.set(blockId, send);
        await send;
        // Only the LAST press of a burst hands the row back to the rig's own
        // answer — an earlier one settling would redraw the state the user has
        // already clicked past.
        if (this._presenceChain.get(blockId) === send) {
            this._presenceChain.delete(blockId);
            this._presenceIntent.delete(blockId);
            this._render();
        }
    }

    private async _setRawBit(blockId: string, occupied: boolean): Promise<void> {
        try {
            await api.setHwDebugOccupancy(blockId, occupied);
            await this._pollOnce(true);
        } catch (err: any) {
            showToast(err?.message || `Could not set ${blockId}'s detector bit.`, 'error');
        }
    }

    private async _setRawCurrent(blockId: string, raw: string): Promise<void> {
        const ma = Number(raw);
        if (!blockId || !Number.isFinite(ma)) {
            showToast('Enter a current in mA.', 'warning');
            return;
        }
        try {
            await api.setHwDebugCurrent(blockId, ma);
            await this._pollOnce(true);
        } catch (err: any) {
            showToast(err?.message || `Could not set ${blockId}'s current.`, 'error');
        }
    }

    /** Sweep a block's rails forward / off / reverse. The server holds the
     *  simulation lock for its ~3 s, so the row is marked busy for the duration
     *  rather than left looking clickable. */
    private async _runTrackTest(blockId: string): Promise<void> {
        if (!blockId || this._busy.has(blockId)) return;
        if (!this._config) {
            showToast('Hardware Setup has not loaded — reopen the panel.', 'error');
            return;
        }
        this._busy.add(blockId);
        this._render();
        try {
            const res = await api.runHardwareTrackTest(this._config, blockId);
            if (res?.ok === false || res?.status === 'error') {
                showToast(res?.error || `Sweep failed on ${blockId}.`, 'error');
            }
        } catch (err: any) {
            showToast(err?.message || `Sweep failed on ${blockId}.`, 'error');
        } finally {
            this._busy.delete(blockId);
            await this._pollOnce(true);
        }
    }

    private async _runSwitchTest(switchId: string, position: 0 | 1): Promise<void> {
        if (!switchId || this._busy.has(switchId)) return;
        if (!this._config) {
            showToast('Hardware Setup has not loaded — reopen the panel.', 'error');
            return;
        }
        this._busy.add(switchId);
        this._render();
        try {
            const res = await api.switchThrowTest(this._config, switchId, position);
            if (res?.ok === false || res?.status === 'error') {
                showToast(res?.error || `Throw failed on ${switchId}.`, 'error');
            }
        } catch (err: any) {
            showToast(err?.message || `Throw failed on ${switchId}.`, 'error');
        } finally {
            this._busy.delete(switchId);
            await this._pollOnce(true);
        }
    }

    private _readRigDraft(): string[] {
        const input = this._bodyEl.querySelector<HTMLInputElement>('[data-role="rig-blocks"]');
        this._rigDraft = input?.value ?? this._rigDraft;
        return this._rigDraft.split(',').map((s) => s.trim()).filter(Boolean);
    }

    private async _rigCommand(action: string, payload: any = {}): Promise<void> {
        try {
            const res = await api.commandVirtualRigScenario(action, payload);
            this._rig = res?.scenario || null;
            await this._pollOnce(true);
        } catch (err: any) {
            showToast(err?.message || `Virtual train ${action} failed.`, 'error');
        }
    }

    /** Read the fault target id + optional operation off the inputs, tracking
     *  them so they survive a surgical poll (like the rig draft). */
    private _readFaultDraft(): { targetId: string; operation: string } {
        const t = this._bodyEl.querySelector<HTMLInputElement>('[data-role="fault-target"]');
        const o = this._bodyEl.querySelector<HTMLInputElement>('[data-role="fault-op"]');
        this._faultTargetDraft = t?.value ?? this._faultTargetDraft;
        this._faultOpDraft = o?.value ?? this._faultOpDraft;
        return { targetId: this._faultTargetDraft, operation: this._faultOpDraft };
    }

    private async _injectFault(pairIndex: number): Promise<void> {
        const pair = VIRTUAL_FAULT_PAIRS[pairIndex];
        if (!pair) return;
        const { targetId, operation } = this._readFaultDraft();
        const body = injectFaultBody(pair, targetId, operation);
        if (!body) {
            showToast('Enter a target id before injecting a fault.', 'warning');
            return;
        }
        try {
            await api.setVirtualHardwareFault(body);
            await this._pollOnce(true);
        } catch (err: any) {
            showToast(err?.message || `Could not inject ${pair.fault}.`, 'error');
        }
    }

    private async _clearOneFault(index: number): Promise<void> {
        const record = this._snapshot?.injected_faults?.[index];
        if (!record) return;
        try {
            await api.setVirtualHardwareFault(clearFaultBody(record));
            await this._pollOnce(true);
        } catch (err: any) {
            showToast(err?.message || 'Could not clear fault.', 'error');
        }
    }

    private async _clearFaults(): Promise<void> {
        try {
            await api.clearVirtualHardwareFaults();
            await this._pollOnce(true);
        } catch (err: any) {
            showToast(err?.message || 'Could not clear faults.', 'error');
        }
    }

    // ----- canvas placement ------------------------------------------------

    /** Arm / disarm the canvas mode that toggles a block's virtual train on
     *  click. Entering a mode is the editor's job, so this hands it a mode
     *  object exactly like a tool contribution would — the panel just owns the
     *  button that arms it. */
    private async _togglePlacing(): Promise<void> {
        const editor = window.app?.editor;
        if (!editor?.enterMode) {
            showToast('The layout editor is not ready yet.', 'warning');
            return;
        }
        if (this._placing) {
            this._placing = false;
            await editor.exitMode();
            this._syncPlaceButton();
            return;
        }
        this._placing = true;
        this._syncPlaceButton();
        await editor.enterMode({
            name: 'Place virtual train',
            toolName: 'inspect',
            cancelMessage: 'Virtual-train placement exited',
            onClick: async ({ gridPos }: { gridPos: any }) => {
                await this._placeAt(gridPos);
                return true;
            },
            onExit: () => {
                this._placing = false;
                this._syncPlaceButton();
            },
        });
    }

    private async _placeAt(gridPos: any): Promise<void> {
        const editor = window.app?.editor;
        const track = editor?.cachedNetwork?.tracks?.find(
            (t: any) => t.x === gridPos?.x && t.y === gridPos?.y
        );
        const blockId = track?.block_section_id;
        if (!blockId) {
            showToast('No block at this position.', 'warning', 2500);
            return;
        }
        const row = benchRows(this._snapshot).find((r) => r.blockId === blockId);
        if (row && !row.hasDigital && !row.hasCurrent) {
            showToast(`${blockId} has no detector binding to place a train on.`, 'warning', 3500);
            return;
        }
        await this._setPresence(blockId, !(row ? this._shownPresence(row) : false));
    }

    private _syncPlaceButton(): void {
        if (!this._placeBtnEl) return;
        this._placeBtnEl.classList.toggle('is-armed', this._placing);
        this._placeBtnEl.setAttribute('aria-pressed', String(this._placing));
    }

    // ----- render ----------------------------------------------------------

    private _render(): void {
        if (!this._bodyEl) return;
        this._renderBanner();
        this._renderBody();
        this._renderFooter();
        this._syncPlaceButton();
    }

    private _renderBanner(): void {
        let text = '';
        let tone = 'ok';
        if (this._error) {
            text = this._error;
            tone = 'danger';
        } else if (this._inactive) {
            text = 'This session is not running Virtual Hardware. Select it in Hardware Setup and activate.';
            tone = 'warn';
        } else {
            const faults = (this._snapshot?.injected_faults ?? []).map(describeFaultRecord);
            if (faults.length) {
                text = `Injected: ${faults.join(', ')} — the rig is failing on purpose.`;
                tone = 'danger';
            }
        }
        if (!text) {
            this._bannerEl.hidden = true;
            this._bannerEl.textContent = '';
            return;
        }
        this._bannerEl.hidden = false;
        this._bannerEl.className = `vb-banner vb-banner-${tone}`;
        this._bannerEl.textContent = text;
    }

    private _renderBody(): void {
        if (this._inactive || this._error) {
            this._bodyShape = 'empty';
            this._bodyEl.innerHTML = '';
            return;
        }
        if (!this._snapshot) {
            this._bodyShape = 'reading';
            this._bodyEl.innerHTML = '<div class="empty-state">Reading…</div>';
            return;
        }
        const rows = benchRows(this._snapshot, this._filter);
        const shape = this._shapeOf(rows);
        // Same shape as last read: touch up the nodes already on screen and
        // leave every button the pointer might be resting on exactly where it
        // is. See `_bodyShape`.
        if (this._bodyShape === shape) {
            this._updateBody(rows);
            return;
        }
        this._bodyShape = shape;
        const total = Object.keys(this._snapshot.blocks || {}).length;
        const blocksHtml = rows.length
            ? `<div class="vb-head${this._showRaw ? ' vb-raw-on' : ''}">
                    <span>Block</span>
                    <span title="Read back off the fake PWM chip — what the board was actually told, not what the model believes.">Track output</span>
                    <span></span>
                    <span title="What you are telling the app its detectors see.">Detectors</span>
                    <span title="What the occupancy pipeline concluded. This is the column that says whether the app reacted.">App sees</span>
               </div>
               ${rows.map((row) => this._rowHtml(row)).join('')}`
            : `<div class="empty-state">${escapeHtml(
                  total
                      ? 'No block matches this filter.'
                      : 'No block is bound to hardware yet. Bind track power and detectors in each block’s editor.'
              )}</div>`;

        this._bodyEl.innerHTML = `
            <section class="vb-section">${blocksHtml}</section>
            ${this._switchesHtml()}
            ${this._rigHtml()}
            ${this._faultsHtml()}
        `;
    }

    /** The row's markup. Everything a read can change lives in an element with a
     *  class `_updateRow` knows, and nothing a read can change decides whether an
     *  element EXISTS — a cell that is empty this read is drawn hidden rather
     *  than left out, so the shape stays put and the update stays a text edit. */
    private _rowHtml(row: BenchRow): string {
        const busy = this._busy.has(row.blockId);
        const drive = describeDrive(row);
        const duty = row.hasTrack
            ? `<span class="vb-duty"${
                  row.dutyForward === null ? ' hidden' : ''
              } title="Commanded duty on the block's two PWM channels, before the rail-polarity flag${
                  row.trackReversed ? ' (this block is wired reversed)' : ''
              }.">${escapeHtml(dutyText(row))}</span>`
            : '';

        // The sweep drives the block, so it is a Setup tool.
        const inSetup = setupToolsAllowed(window.app?.sessionState);
        const sweep = row.hasTrack
            ? `<button type="button" class="vb-btn" data-role="sweep" data-id="${escapeHtml(row.blockId)}"
                   ${busy || !inSetup ? 'disabled' : ''}
                   title="${escapeHtml(
                       inSetup
                           ? 'Drive this block full forward, off, then full reverse — about three seconds.'
                           : SETUP_TOOL_HINT
                   )}">${busy ? '…' : 'Sweep'}</button>`
            : '<span class="vb-dash">—</span>';

        const canPlace = row.hasDigital || row.hasCurrent;
        const present = this._shownPresence(row);
        const place = canPlace
            ? `<button type="button" class="vb-btn vb-place${present ? ' is-on' : ''}"
                   data-role="place-train" data-id="${escapeHtml(row.blockId)}" data-present="${present ? '1' : '0'}"
                   ${busy ? 'disabled' : ''}
                   title="${placeTitle(present)}"><i class="ti ti-train" aria-hidden="true"></i> <span
                   class="vb-place-label">${present ? 'On block' : 'Place'}</span></button>`
            : '<span class="vb-dash" title="No detector binding — nothing to place a train on.">—</span>';

        const readsText = readsOf(row);
        const verdict = verdictOf(row);
        const raw = this._showRaw ? this._rawCellsHtml(row) : '';

        return `
            <div class="vb-row${this._showRaw ? ' vb-raw-on' : ''}${
                present ? ' vb-row-present' : ''
            }" data-block="${escapeHtml(row.blockId)}">
                <span class="vb-block">${escapeHtml(row.blockId)}</span>
                <span class="vb-drive vb-drive-${drive.tone}">
                    <span class="vb-drive-glyph" aria-hidden="true">${drive.glyph}</span><span
                        class="vb-drive-text">${escapeHtml(drive.text)}</span>
                    ${duty}
                </span>
                <span class="vb-test">${sweep}</span>
                <span class="vb-sensor">${place}<span class="vb-reads"${
                    readsText ? '' : ' hidden'
                }>${escapeHtml(readsText)}</span></span>
                <span class="vb-app"><span class="${verdict.cls}">${escapeHtml(verdict.text)}</span></span>
                ${raw}
            </div>
        `;
    }

    /** The presence the row should DRAW: the one the user last asked for while
     *  that write is still settling, otherwise what the rig reports. */
    private _shownPresence(row: BenchRow): boolean {
        const intent = this._presenceIntent.get(row.blockId);
        return intent === undefined ? row.present : intent;
    }

    // ----- in-place update -------------------------------------------------

    /** Everything the body's markup decides STRUCTURALLY — which rows and cells
     *  exist at all. Readings are deliberately absent: they are what the update
     *  path writes. A change here (a filter, the Raw toggle, a block gaining a
     *  binding) is what earns a rebuild. */
    private _shapeOf(rows: BenchRow[]): string {
        const blocks = rows
            .map((r) => `${r.blockId}:${+r.hasTrack}${+r.hasDigital}${+r.hasCurrent}${+r.trackReversed}`)
            .join(',');
        const switches = Object.entries(this._snapshot?.switches || {})
            .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
            .map(([id, s]) => `${id}:${s?.standby === null || s?.standby === undefined ? 0 : 1}`)
            .join(',');
        const total = Object.keys(this._snapshot?.blocks || {}).length;
        return `${this._filter}|${+this._showRaw}|${total}|${blocks}|${switches}`;
    }

    /** Write this read's numbers into the nodes already on screen. Touches only
     *  text, classes and attributes — never `innerHTML` on anything that holds a
     *  control, because replacing a node the pointer is over is exactly what
     *  swallows the press. */
    private _updateBody(rows: BenchRow[]): void {
        for (const row of rows) {
            const el = this._bodyEl.querySelector<HTMLElement>(
                `.vb-row[data-block="${CSS.escape(row.blockId)}"]`
            );
            if (el) this._updateRow(el, row);
        }
        this._updateSwitches();
        this._updateRig();
        this._updateFaults();
    }

    private _updateRow(el: HTMLElement, row: BenchRow): void {
        const present = this._shownPresence(row);
        el.classList.toggle('vb-row-present', present);

        const drive = describeDrive(row);
        const driveEl = el.querySelector<HTMLElement>('.vb-drive');
        if (driveEl) {
            driveEl.className = `vb-drive vb-drive-${drive.tone}`;
            setText(driveEl.querySelector('.vb-drive-glyph'), drive.glyph);
            setText(driveEl.querySelector('.vb-drive-text'), drive.text);
            const duty = driveEl.querySelector<HTMLElement>('.vb-duty');
            if (duty) {
                duty.hidden = row.dutyForward === null;
                setText(duty, dutyText(row));
            }
        }

        const busy = this._busy.has(row.blockId);
        const sweep = el.querySelector<HTMLButtonElement>('[data-role="sweep"]');
        if (sweep) {
            sweep.disabled = busy;
            setText(sweep, busy ? '…' : 'Sweep');
        }

        const place = el.querySelector<HTMLButtonElement>('[data-role="place-train"]');
        if (place) {
            // Only a SWEEP locks this out — a sweep owns the block's rails for
            // its three seconds. A placement never disables its own button.
            place.disabled = busy;
            place.classList.toggle('is-on', present);
            place.dataset.present = present ? '1' : '0';
            place.title = placeTitle(present);
            setText(place.querySelector('.vb-place-label'), present ? 'On block' : 'Place');
        }

        const reads = el.querySelector<HTMLElement>('.vb-reads');
        if (reads) {
            const text = readsOf(row);
            reads.hidden = !text;
            setText(reads, text);
        }

        const verdict = verdictOf(row);
        const verdictEl = el.querySelector<HTMLElement>('.vb-app > span');
        if (verdictEl) {
            verdictEl.className = verdict.cls;
            setText(verdictEl, verdict.text);
        }

        const bit = el.querySelector<HTMLButtonElement>('[data-role="raw-bit"]');
        if (bit) {
            bit.classList.toggle('is-on', !!row.occupancyBit);
            bit.dataset.bit = row.occupancyBit ? '1' : '0';
            setText(bit, bitLabel(row));
        }
        const ma = el.querySelector<HTMLInputElement>('[data-role="raw-ma"]');
        // Never overwrite a field being typed into: the read that lands between
        // a keystroke and Set would otherwise put the rig's own number back.
        if (ma && document.activeElement !== ma) ma.value = (row.currentMa ?? 0).toFixed(0);
        setText(el.querySelector('.vb-raw-note'), rawNote(row));
    }

    private _updateSwitches(): void {
        const switches = this._snapshot?.switches || {};
        for (const [switchId, s] of Object.entries(switches)) {
            const el = this._bodyEl.querySelector<HTMLElement>(
                `.vb-row-switch[data-switch="${CSS.escape(switchId)}"]`
            );
            if (!el) continue;
            const coil = (name: 'main' | 'diverge' | 'standby', on: boolean | null | undefined) =>
                el.querySelector(`[data-coil="${name}"]`)?.classList.toggle('is-on', !!on);
            coil('main', s?.main);
            coil('diverge', s?.diverge);
            coil('standby', s?.standby);
            const busy = this._busy.has(switchId);
            el.querySelectorAll<HTMLButtonElement>('[data-role^="throw-"]').forEach((btn) => {
                btn.disabled = busy;
            });
        }
    }

    private _updateRig(): void {
        const rig = this._rig;
        const active = !!rig?.active;
        const input = this._bodyEl.querySelector<HTMLInputElement>('[data-role="rig-blocks"]');
        if (input && document.activeElement !== input) {
            input.value = active ? (rig?.blocks || []).join(', ') : this._rigDraft;
        }
        const button = (role: string) =>
            this._bodyEl.querySelector<HTMLButtonElement>(`[data-role="${role}"]`);
        for (const role of ['rig-advance', 'rig-stall', 'rig-remove']) {
            const btn = button(role);
            if (btn) btn.disabled = !active;
        }
        const stall = button('rig-stall');
        if (stall) {
            stall.classList.toggle('is-on', !!rig?.stalled);
            setText(stall, rig?.stalled ? 'Resume' : 'Stall');
        }
        setText(this._bodyEl.querySelector('.vb-strip-status'), rigStatus(rig));
    }

    private _updateFaults(): void {
        // Only the injected list changes on a poll; the inject buttons and the
        // target inputs are static. Rebuild the list only when it actually
        // changed, so a Clear button under the pointer is not replaced mid-press.
        const container = this._bodyEl.querySelector('[data-role="fault-injected"]');
        if (!container) return;
        const html = this._injectedHtml();
        if (this._faultsSig !== html) {
            container.innerHTML = html;
            this._faultsSig = html;
        }
    }

    /** The deliberately-disagree controls: set one source without the other.
     *  Full-width under the row, so the ordinary row stays four readable cells. */
    private _rawCellsHtml(row: BenchRow): string {
        const bit = row.hasDigital
            ? `<button type="button" class="vb-btn vb-btn-xs${row.occupancyBit ? ' is-on' : ''}"
                   data-role="raw-bit" data-id="${escapeHtml(row.blockId)}" data-bit="${row.occupancyBit ? '1' : '0'}"
                   title="The raw register bit. This chain is ${
                       row.activeLow
                           ? 'ACTIVE-LOW, so 0 is the reading the pipeline calls present'
                           : 'active-high, so 1 is present'
                   }."
               >${escapeHtml(bitLabel(row))}</button>`
            : '';
        const current = row.hasCurrent
            ? `<span class="vb-raw-ma">
                   <input type="number" class="vb-input" data-role="raw-ma" data-id="${escapeHtml(row.blockId)}"
                       value="${(row.currentMa ?? 0).toFixed(0)}" step="1" aria-label="${escapeHtml(row.blockId)} current in mA" />
                   <span class="vb-unit">mA</span>
                   <button type="button" class="vb-btn vb-btn-xs" data-role="raw-ma-set" data-id="${escapeHtml(row.blockId)}">Set</button>
                   <span class="vb-raw-note">${escapeHtml(rawNote(row))}</span>
               </span>`
            : '';
        if (!bit && !current) return '';
        return `<div class="vb-raw">${bit}${current}</div>`;
    }

    private _switchesHtml(): string {
        const switches = this._snapshot?.switches || {};
        const ids = Object.keys(switches).sort();
        if (!ids.length) return '';
        const rows = ids.map((switchId) => {
            const s = switches[switchId] || {};
            const busy = this._busy.has(switchId);
            const coil = (on: boolean | null | undefined, label: string) =>
                `<span class="vb-coil${on ? ' is-on' : ''}" data-coil="${label}"><i class="vb-coil-dot"></i>${escapeHtml(
                    label
                )}</span>`;
            const standby = s.standby === null || s.standby === undefined ? '' : coil(s.standby, 'standby');
            return `
                <div class="vb-row vb-row-switch" data-switch="${escapeHtml(switchId)}">
                    <span class="vb-block">${escapeHtml(switchId)}</span>
                    <span class="vb-coils">${coil(s.main, 'main')}${coil(s.diverge, 'diverge')}${standby}</span>
                    <span class="vb-test">
                        <button type="button" class="vb-btn" data-role="throw-main" data-id="${escapeHtml(switchId)}" ${
                            busy ? 'disabled' : ''
                        } title="Command this switch to MAIN — the points and the app stay in agreement.">Main</button>
                        <button type="button" class="vb-btn" data-role="throw-diverge" data-id="${escapeHtml(switchId)}" ${
                            busy ? 'disabled' : ''
                        } title="Command this switch to DIVERGE.">Diverge</button>
                    </span>
                </div>`;
        }).join('');
        return `<section class="vb-section">
            <div class="vb-section-head">Switch coils
                <span class="vb-section-note">the latched bits, read back off the fake shift register</span>
            </div>
            ${rows}
        </section>`;
    }

    /** The multi-block scenario: a train that occupies one block at a time and
     *  steps to the next, clearing the one behind it. Distinct from the per-row
     *  Place button because a boundary crossing — new detector confirming while
     *  the old one clears — is a sequence, not a state. */
    private _rigHtml(): string {
        const rig = this._rig;
        const status = escapeHtml(rigStatus(rig));
        const draft = rig?.active ? (rig.blocks || []).join(', ') : this._rigDraft;
        return `<section class="vb-section">
            <div class="vb-section-head">Virtual train
                <span class="vb-section-note">steps block to block through the detectors, like a train crossing boundaries</span>
            </div>
            <div class="vb-strip">
                <input type="text" class="vb-input vb-input-wide" data-role="rig-blocks"
                    value="${escapeHtml(draft)}" placeholder="B1, B2, B3"
                    aria-label="Block ids the virtual train runs through" />
                <button type="button" class="vb-btn" data-role="rig-start" title="Place the train on the first block.">Place</button>
                <button type="button" class="vb-btn" data-role="rig-advance" ${
                    rig?.active ? '' : 'disabled'
                } title="Cross into the next block: its detector confirms as the one behind clears.">Enter next</button>
                <button type="button" class="vb-btn${rig?.stalled ? ' is-on' : ''}" data-role="rig-stall" ${
                    rig?.active ? '' : 'disabled'
                } title="Freeze the train where it is — 'Enter next' stops having any effect.">${
                    rig?.stalled ? 'Resume' : 'Stall'
                }</button>
                <button type="button" class="vb-btn" data-role="rig-remove" ${
                    rig?.active ? '' : 'disabled'
                } title="Lift the train off every block it is on.">Remove</button>
                <span class="vb-strip-status">${status}</span>
            </div>
        </section>`;
    }

    private _injectedHtml(): string {
        const injected = this._snapshot?.injected_faults ?? [];
        if (!injected.length) return '<span class="vb-section-note">none injected</span>';
        return injected.map((r, i) =>
            `<span class="vb-fault-active">${escapeHtml(describeFaultRecord(r))}
                <button type="button" class="vb-btn" data-role="fault-clear-one" data-fi="${i}">Clear</button></span>`
        ).join('');
    }

    private _faultsHtml(): string {
        // One button per valid (target, fault) pair; each injects against the id
        // in the target field. The injected list below is the live truth.
        const buttons = VIRTUAL_FAULT_PAIRS.map((pair, i) =>
            `<button type="button" class="vb-btn" data-role="fault-inject" data-fp="${i}"
                >${escapeHtml(faultPairLabel(pair))}</button>`
        ).join('');
        return `<section class="vb-section">
            <div class="vb-section-head">Device faults
                <span class="vb-section-note">injected below the HAL — the app meets them exactly as it would a real failure</span>
            </div>
            <div class="vb-strip">
                <input type="text" class="vb-input vb-input-wide" data-role="fault-target"
                    value="${escapeHtml(this._faultTargetDraft)}" placeholder="target id (e.g. IN1, SW_3)" />
                <input type="text" class="vb-input" data-role="fault-op"
                    value="${escapeHtml(this._faultOpDraft)}" placeholder="op: read/write (optional)" />
            </div>
            <div class="vb-strip">${buttons}
                <button type="button" class="vb-btn" data-role="fault-clear">Clear all</button>
            </div>
            <div class="vb-strip" data-role="fault-injected">${this._injectedHtml()}</div>
        </section>`;
    }

    private _renderFooter(): void {
        if (!this._snapshot) {
            this._footerEl.textContent = '';
            return;
        }
        const blocks = Object.keys(this._snapshot.blocks || {});
        const rows = benchRows(this._snapshot);
        const occupied = rows.filter((r) => r.occupied).length;
        const driven = rows.filter((r) => r.driven).length;
        this._footerEl.textContent = [
            `${blocks.length} block${blocks.length === 1 ? '' : 's'} wired`,
            `${driven} driven`,
            `${occupied} occupied`,
        ].join(' · ');
    }

    // ----- persistence -----------------------------------------------------

    private _persist(): void {
        if (this._embedded) return;
        saveWindowState(this._persistenceEnv, WINDOW_ID, {
            geometry: this._window.getGeometry(),
            floating: true,
            hidden: !this._visible,
            virtualBench: {
                refreshHz: this._refreshHz,
                filter: this._filter,
                showRaw: this._showRaw,
            },
        });
    }
}

/** Build a Virtual Bench inside `host`; `close()` stops it. This copy has no
 *  window and saves no window state. */
export function mountVirtualBench(host: HTMLElement): { close(): void } {
    const bench = new VirtualBench();
    bench.mountInto(host);
    return { close: () => bench.unmount() };
}

function bootstrap(): void {
    if (window.virtualBench) return; // idempotent
    window.virtualBench = new VirtualBench();
    window.virtualBench.init();
}

// Guarded on `document` so the pure derivations above can be imported by the
// unit tests, which run in Node with no DOM.
if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bootstrap);
    } else {
        bootstrap();
    }
}

export { VirtualBench };
