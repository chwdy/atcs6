/**
 * EventLog — the operator-facing viewer over the one runtime event stream
 * (design_new.md "Event log").
 *
 * Every component that has something to report calls `NotificationCenter.emit`,
 * so there is exactly one stream and this panel is a filter over it. Nothing
 * here mutates runtime state: it presents the record only.
 *
 * One column per field, aligned down the whole list, so a value can be compared
 * between rows by eye instead of being hunted for inside a paragraph:
 *
 *   TIME      — `event_time`, wall-clock. The stream is ordered by
 *               `notification_id`, not by this, so two events in the same
 *               millisecond still read back in emit order.
 *   LEVEL     — `kind`. The only field runtime code branches on, and the first
 *               thing an operator scans for, so it is a colour before a word.
 *   SOURCE    — the emitting class.
 *   OPERATION — the emitting function's own name. Its own column because it is
 *               what makes "who produced this" answerable without a
 *               per-event-type vocabulary to memorise, and because scanning one
 *               column for a repeated function name is how a loop is spotted.
 *   MESSAGE   — what the operation did.
 *   ERROR     — `origin` + `error.text`. A separate column from MESSAGE because
 *               they answer different questions: message says what was
 *               attempted, error says what went wrong, and stacking them made
 *               every failure row twice as tall as the rows around it.
 *   ENTITIES  — the `entities` refs, as chips. They are the drill-down: clicking
 *               one filters the stream to that entity, which is how "show me
 *               this train's last error" is asked.
 *
 * Columns are a CSS grid on a shared template rather than a <table>, so the
 * header and every row line up while an expanded payload can still span the full
 * width underneath its row.
 *
 * Filtering is server-side (`GET /api/event-log`), not client-side over a
 * downloaded buffer: retention is 10k rows and the operator may be asking about
 * something that scrolled out of any window we would have kept locally.
 *
 * Polling is incremental. Each read passes the highest `notification_id` seen as
 * `since_notification_id`, so a steady session transfers only what is new. Ids
 * keep counting past a retention drop, so a watermark is never re-served an
 * event it already saw. Changing any filter resets the watermark, because the
 * server is then answering a different question.
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
// The event shape returned by GET /api/event-log. Read defensively: the
// endpoint answers `{events: []}` before a runtime exists.

export interface EventEntityRef {
    entity_type: string;
    entity_id: string;
}

export interface EventErrorRecord {
    /** The human-readable failure detail. */
    text: string;
    /** The free-form label naming what produced the error. Diagnostic only —
     *  it is what a reader groups errors by when `operation` is too coarse. */
    origin: string;
}

export interface RuntimeEvent {
    notification_id: number;
    event_time: number;
    source: string;
    /** The emitting function's own name. */
    operation: string;
    /** The level: 'debug' | 'info' | 'warning' | 'error'. */
    kind: string;
    entities: EventEntityRef[];
    message: string;
    error: EventErrorRecord | null;
    payload: Record<string, unknown>;
}

export type LevelFilter = 'all' | 'debug' | 'info' | 'warning' | 'error';

export interface EventLogFilter {
    level: LevelFilter;
    source: string;
    operation: string;
    entityType: string;
    entityId: string;
    query: string;
    persisted: boolean;
}

export const EMPTY_FILTER: EventLogFilter = {
    level: 'all',
    source: '',
    operation: '',
    entityType: '',
    entityId: '',
    query: '',
    persisted: false,
};

const LEVEL_OPTIONS: { value: LevelFilter; label: string }[] = [
    { value: 'all', label: 'All levels' },
    { value: 'error', label: 'Error' },
    { value: 'warning', label: 'Warning' },
    { value: 'info', label: 'Info' },
    { value: 'debug', label: 'Debug' },
];

const RATE_OPTIONS = [
    { value: 0, label: 'Paused' },
    { value: 1, label: '1 Hz' },
    { value: 2, label: '2 Hz' },
    { value: 4, label: '4 Hz' },
];

const WINDOW_ID = 'event-log';
// Seven columns need room: at 720 the error column collapsed to a few pixels
// and the one field that explains a failure was the one you could not read.
const DEFAULT_WIDTH = 1040;
const DEFAULT_HEIGHT = 460;
const MIN_WIDTH = 620;
const MAX_WIDTH = 1400;
/** Left inset that keeps the panel clear of the editor's tool rail. */
const TOOL_RAIL_CLEARANCE = 110;

/** How many events the panel keeps in its own scrollback. Independent of the
 *  server's retention: this bounds the DOM, not the record. */
const MAX_ROWS = 1000;

// ---------------------------------------------------------------------------
// Pure derivations — exported for the unit tests, which run in Node with no DOM.

/** Build the query string for one read. Only non-empty filters are sent, so a
 *  default view asks the cheapest question the endpoint answers. */
export function buildQuery(
    filter: EventLogFilter,
    limit: number,
    sinceNotificationId: number | null
): string {
    const params = new URLSearchParams();
    if (filter.level !== 'all') params.set('kind', filter.level);
    if (filter.source) params.set('source', filter.source);
    if (filter.operation) params.set('operation', filter.operation);
    if (filter.entityType && filter.entityId) {
        params.set('entity_type', filter.entityType);
        params.set('entity_id', filter.entityId);
    }
    if (filter.query) params.set('q', filter.query);
    if (filter.persisted) params.set('persisted', 'true');
    if (sinceNotificationId !== null) {
        params.set('since_notification_id', String(sinceNotificationId));
    }
    params.set('limit', String(limit));
    return params.toString();
}

/** Merge a read's events into the existing scrollback, newest first, dropping
 *  duplicates by `notification_id` and trimming to `MAX_ROWS`.
 *
 *  Dedupe by id rather than trusting the watermark alone: a filter change resets
 *  the watermark to null and re-reads from the top, and the same events must not
 *  appear twice in the list. */
export function mergeEvents(
    existing: RuntimeEvent[],
    incoming: RuntimeEvent[],
    max = MAX_ROWS
): RuntimeEvent[] {
    if (!incoming.length) return existing;
    const seen = new Set(existing.map((e) => e.notification_id));
    const fresh = incoming.filter((e) => !seen.has(e.notification_id));
    if (!fresh.length) return existing;
    const merged = [...fresh, ...existing];
    merged.sort((a, b) => b.notification_id - a.notification_id);
    return merged.length > max ? merged.slice(0, max) : merged;
}

/** The highest id in a list, or null when empty — the next read's watermark. */
export function highestId(events: RuntimeEvent[]): number | null {
    let top: number | null = null;
    for (const e of events) {
        if (top === null || e.notification_id > top) top = e.notification_id;
    }
    return top;
}

/** Per-level counts for the footer, so an operator can see there are errors in
 *  the buffer without scrolling for them. */
export function levelCounts(events: RuntimeEvent[]): Record<string, number> {
    const counts: Record<string, number> = { debug: 0, info: 0, warning: 0, error: 0 };
    for (const e of events) {
        if (e.kind in counts) counts[e.kind] += 1;
    }
    return counts;
}

/** Distinct values of one field across the buffer, sorted — the contents of the
 *  source / operation dropdowns. They are derived from what has actually been
 *  seen rather than hardcoded, because `operation` is a function name and the
 *  set grows whenever a new call site starts reporting. */
export function distinctValues(events: RuntimeEvent[], field: 'source' | 'operation'): string[] {
    const out = new Set<string>();
    for (const e of events) {
        if (e[field]) out.add(e[field]);
    }
    return [...out].sort();
}

/** Wall-clock time of day, to the millisecond. Ordering is by id, so this is
 *  read for "when did this happen", not for sequencing. */
export function formatEventTime(seconds: number): string {
    if (!Number.isFinite(seconds) || seconds <= 0) return '—';
    const d = new Date(seconds * 1000);
    const pad = (n: number, w = 2) => String(n).padStart(w, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(
        d.getMilliseconds(),
        3
    )}`;
}

/** Short label for an entity chip. The walk-position ref packs its run id and
 *  index as `run#index`, so show just the index — the run is already named by
 *  its own sibling chip on the same event. */
export function entityLabel(ref: EventEntityRef): string {
    if (ref.entity_type === 'walk_position') {
        const hash = ref.entity_id.lastIndexOf('#');
        if (hash >= 0) return `task ${ref.entity_id.slice(hash + 1)}`;
    }
    return ref.entity_id;
}

function escapeHtml(value: unknown): string {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// ---------------------------------------------------------------------------

class EventLog {
    private _window!: FloatingWindow;
    private _persistenceEnv = browserWindowPersistenceEnv();
    private _pollHandle: ReturnType<typeof setInterval> | null = null;
    /** The read currently out, so an interval tick can skip rather than stack
     *  reads when the server is slow. */
    private _inflight: Promise<void> | null = null;

    private _events: RuntimeEvent[] = [];
    private _watermark: number | null = null;
    private _filter: EventLogFilter = { ...EMPTY_FILTER };
    private _refreshHz = 2;
    private _visible = false;
    private _error: string | null = null;
    /** Ids whose payload row is expanded. Kept as a set rather than a single id
     *  so comparing two events side by side does not close the first. */
    private _expanded = new Set<number>();
    /** Set while the operator has scrolled off the top. New events still arrive
     *  and merge, but the list must not yank itself back to the newest row
     *  underneath them — that makes reading a burst impossible. */
    private _pinnedToTop = true;

    private _bodyEl!: HTMLElement;
    private _scrollEl!: HTMLElement;
    private _footerEl!: HTMLElement;
    private _levelSelectEl!: HTMLSelectElement;
    private _sourceSelectEl!: HTMLSelectElement;
    private _opSelectEl!: HTMLSelectElement;
    private _rateSelectEl!: HTMLSelectElement;
    private _searchEl!: HTMLInputElement;
    private _persistedEl!: HTMLInputElement;
    private _entityChipEl!: HTMLElement;

    init(): void {
        const saved = loadWindowState(this._persistenceEnv, WINDOW_ID);
        this._visible = saved ? !saved.hidden : false;
        const settings = saved?.eventLog;
        if (settings) {
            if (Number.isFinite(Number(settings.refreshHz))) {
                this._refreshHz = Number(settings.refreshHz);
            }
            if (LEVEL_OPTIONS.some((o) => o.value === settings.level)) {
                this._filter.level = settings.level as LevelFilter;
            }
            this._filter.persisted = !!settings.persisted;
        }

        this._buildFloatingWindow(saved?.geometry);
        ensureRegistered(this._persistenceEnv, WINDOW_ID, 'event-log');
        this._wireToggleButton();
        this._wireControls();

        this._levelSelectEl.value = this._filter.level;
        this._rateSelectEl.value = String(this._refreshHz);
        this._persistedEl.checked = this._filter.persisted;
        this._renderEntityChip();
        this._render();
        if (this._visible) {
            void this._pollOnce();
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
            // Clear the left tool rail. Centring alone puts a wide panel under
            // it on a narrow viewport, and the rail draws above floating
            // windows — which clips the time column, the one thing every row
            // starts with.
            geometry.left = Math.max(
                TOOL_RAIL_CLEARANCE,
                Math.round((window.innerWidth - DEFAULT_WIDTH) / 2)
            );
            geometry.top = 120;
        }
        geometry.width = saved?.width && saved.width > 0 ? saved.width : DEFAULT_WIDTH;
        geometry.height = saved?.height && saved.height > 0 ? saved.height : DEFAULT_HEIGHT;

        this._window = new FloatingWindow({
            id: WINDOW_ID,
            kind: 'event-log',
            manager: windowManager,
            title: 'Event Log',
            minW: MIN_WIDTH,
            maxW: MAX_WIDTH,
            initialGeometry: geometry,
            initialHidden: !this._visible,
            content: { mount: (el) => this._buildContentDom(el) },
            // A singleton toggled from the toolbar, so its × hides rather than
            // destroys — the same contract as diagnostics and the occupancy
            // monitor.
            onCloseRequested: () => this.hide(),
            onGeometryChange: () => this._persist(),
        });
    }

    private _buildContentDom(contentEl: HTMLElement): void {
        contentEl.innerHTML = `
            <div class="evl-toolbar">
                <label class="evl-field">
                    Level
                    <select data-role="level"></select>
                </label>
                <label class="evl-field">
                    Source
                    <select data-role="source"></select>
                </label>
                <label class="evl-field">
                    Operation
                    <select data-role="operation"></select>
                </label>
                <label class="evl-field evl-field-grow">
                    Search
                    <input type="search" data-role="search" placeholder="message, error, origin, entity" />
                </label>
            </div>
            <div class="evl-toolbar evl-toolbar-secondary">
                <div class="evl-entity-slot" data-role="entity-chip"></div>
                <label class="evl-check" title="Read the on-disk stream instead of the in-memory archive, so an earlier run's incidents survive a restart.">
                    <input type="checkbox" data-role="persisted" /> Persisted
                </label>
                <label class="evl-field">
                    Live
                    <select data-role="rate"></select>
                </label>
                <button class="evl-icon-btn" type="button" data-role="clear" title="Clear this view. The record itself is untouched — the next read refills from the stream.">
                    <i class="ti ti-eraser" aria-hidden="true"></i>
                </button>
            </div>
            <div class="evl-scroll" data-role="scroll">
                <div class="evl-head">
                    <span class="evl-cell">Time</span>
                    <span class="evl-cell">Level</span>
                    <span class="evl-cell">Source</span>
                    <span class="evl-cell">Operation</span>
                    <span class="evl-cell">Message</span>
                    <span class="evl-cell">Error</span>
                    <span class="evl-cell">Entities</span>
                </div>
                <div class="evl-body" data-role="body"></div>
            </div>
            <div class="evl-footer" data-role="footer"></div>
        `;

        this._levelSelectEl = contentEl.querySelector('[data-role="level"]') as HTMLSelectElement;
        this._sourceSelectEl = contentEl.querySelector('[data-role="source"]') as HTMLSelectElement;
        this._opSelectEl = contentEl.querySelector('[data-role="operation"]') as HTMLSelectElement;
        this._rateSelectEl = contentEl.querySelector('[data-role="rate"]') as HTMLSelectElement;
        this._searchEl = contentEl.querySelector('[data-role="search"]') as HTMLInputElement;
        this._persistedEl = contentEl.querySelector('[data-role="persisted"]') as HTMLInputElement;
        this._entityChipEl = contentEl.querySelector('[data-role="entity-chip"]') as HTMLElement;
        this._bodyEl = contentEl.querySelector('[data-role="body"]') as HTMLElement;
        this._scrollEl = contentEl.querySelector('[data-role="scroll"]') as HTMLElement;
        this._footerEl = contentEl.querySelector('[data-role="footer"]') as HTMLElement;

        this._levelSelectEl.innerHTML = LEVEL_OPTIONS.map(
            (o) => `<option value="${o.value}">${o.label}</option>`
        ).join('');
        this._rateSelectEl.innerHTML = RATE_OPTIONS.map(
            (o) => `<option value="${o.value}">${o.label}</option>`
        ).join('');
        this._syncSelect(this._sourceSelectEl, [], 'All sources');
        this._syncSelect(this._opSelectEl, [], 'All operations');
    }

    private _wireToggleButton(): void {
        const btn = document.getElementById('btn-event-log-toggle');
        if (btn) btn.addEventListener('click', () => this.toggle());
    }

    private _wireControls(): void {
        this._levelSelectEl.addEventListener('change', () => {
            this._filter.level = this._levelSelectEl.value as LevelFilter;
            this._onFilterChanged();
        });
        this._sourceSelectEl.addEventListener('change', () => {
            this._filter.source = this._sourceSelectEl.value;
            this._onFilterChanged();
        });
        this._opSelectEl.addEventListener('change', () => {
            this._filter.operation = this._opSelectEl.value;
            this._onFilterChanged();
        });
        this._persistedEl.addEventListener('change', () => {
            this._filter.persisted = this._persistedEl.checked;
            this._onFilterChanged();
        });

        // Debounced: a search box that re-reads on every keystroke asks the
        // server a question the operator has not finished typing yet.
        let searchTimer: ReturnType<typeof setTimeout> | null = null;
        this._searchEl.addEventListener('input', () => {
            if (searchTimer) clearTimeout(searchTimer);
            searchTimer = setTimeout(() => {
                this._filter.query = this._searchEl.value.trim();
                this._onFilterChanged();
            }, 250);
        });

        this._rateSelectEl.addEventListener('change', () => {
            const hz = Number(this._rateSelectEl.value);
            this._refreshHz = Number.isFinite(hz) ? hz : 2;
            this._persist();
            this._stopPolling();
            if (this._visible && this._refreshHz > 0) {
                void this._pollOnce();
                this._startPolling();
            }
            this._renderFooter();
        });

        const clearBtn = this._scrollEl.parentElement?.querySelector('[data-role="clear"]');
        clearBtn?.addEventListener('click', () => {
            this._events = [];
            this._watermark = null;
            this._expanded.clear();
            this._render();
            void this._pollOnce();
        });

        // One delegated listener rather than per-row handlers: rows are replaced
        // wholesale on every read, and re-binding hundreds of listeners each
        // time is what makes a log panel stutter.
        this._bodyEl.addEventListener('click', (ev) => {
            const target = ev.target as HTMLElement;
            const chip = target.closest('[data-entity-type]') as HTMLElement | null;
            if (chip) {
                this._filter.entityType = chip.dataset.entityType || '';
                this._filter.entityId = chip.dataset.entityId || '';
                this._renderEntityChip();
                this._onFilterChanged();
                return;
            }
            const row = target.closest('[data-event-id]') as HTMLElement | null;
            if (!row) return;
            const id = Number(row.dataset.eventId);
            if (this._expanded.has(id)) this._expanded.delete(id);
            else this._expanded.add(id);
            this._renderBody();
        });

        this._scrollEl.addEventListener('scroll', () => {
            this._pinnedToTop = this._scrollEl.scrollTop <= 4;
        });
    }

    /** Any filter change makes the server answer a different question, so the
     *  watermark and the buffer both reset — otherwise the next read would be
     *  merged into rows that the new filter excludes. */
    private _onFilterChanged(): void {
        this._events = [];
        this._watermark = null;
        this._expanded.clear();
        this._persist();
        this._render();
        void this._pollOnce();
    }

    toggle(): void {
        if (this._visible) this.hide();
        else this.show();
    }

    show(): void {
        this._visible = true;
        this._window.setHidden(false);
        this._window.focus();
        this._persist();
        void this._pollOnce();
        this._startPolling();
    }

    hide(): void {
        this._visible = false;
        this._window.setHidden(true);
        this._stopPolling();
        this._persist();
    }

    // ----- polling ---------------------------------------------------------

    private _startPolling(): void {
        this._stopPolling();
        if (this._refreshHz <= 0) return;
        const period = Math.max(200, Math.round(1000 / this._refreshHz));
        this._pollHandle = setInterval(() => void this._pollOnce(), period);
    }

    private _stopPolling(): void {
        if (this._pollHandle !== null) {
            clearInterval(this._pollHandle);
            this._pollHandle = null;
        }
    }

    private _pollOnce(): Promise<void> {
        if (this._inflight) return this._inflight;
        const run = (async () => {
            try {
                const qs = buildQuery(this._filter, MAX_ROWS, this._watermark);
                const res = await api.getEventLog(qs);
                const incoming: RuntimeEvent[] = Array.isArray(res?.events) ? res.events : [];
                this._events = mergeEvents(this._events, incoming);
                const top = highestId(this._events);
                if (top !== null) this._watermark = top;
                this._error = null;
            } catch (err: any) {
                this._error = err?.message ? String(err.message) : 'event log unavailable';
            } finally {
                this._inflight = null;
                this._render();
            }
        })();
        this._inflight = run;
        return run;
    }

    // ----- rendering -------------------------------------------------------

    private _render(): void {
        this._syncSelect(this._sourceSelectEl, distinctValues(this._events, 'source'), 'All sources', this._filter.source);
        this._syncSelect(this._opSelectEl, distinctValues(this._events, 'operation'), 'All operations', this._filter.operation);
        this._renderBody();
        this._renderFooter();
    }

    /** Refill a dropdown only when its option set actually changed, so the open
     *  menu is not rebuilt underneath a pointer mid-selection. */
    private _syncSelect(
        el: HTMLSelectElement,
        values: string[],
        allLabel: string,
        selected = ''
    ): void {
        const shape = `${allLabel}|${values.join('|')}`;
        if (el.dataset.shape !== shape) {
            el.dataset.shape = shape;
            el.innerHTML = [
                `<option value="">${allLabel}</option>`,
                ...values.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`),
            ].join('');
        }
        // A value that has aged out of the buffer must stay selected, or the
        // filter would silently widen underneath the operator.
        if (selected && !values.includes(selected)) {
            el.insertAdjacentHTML(
                'beforeend',
                `<option value="${escapeHtml(selected)}">${escapeHtml(selected)}</option>`
            );
        }
        if (el.value !== selected) el.value = selected;
    }

    private _renderEntityChip(): void {
        if (!this._filter.entityType || !this._filter.entityId) {
            this._entityChipEl.innerHTML = '<span class="evl-entity-empty">All entities</span>';
            return;
        }
        this._entityChipEl.innerHTML = `
            <span class="evl-active-entity">
                <span class="evl-entity-kind">${escapeHtml(this._filter.entityType)}</span>
                ${escapeHtml(this._filter.entityId)}
                <button type="button" data-role="clear-entity" aria-label="Show all entities">×</button>
            </span>`;
        this._entityChipEl
            .querySelector('[data-role="clear-entity"]')
            ?.addEventListener('click', () => {
                this._filter.entityType = '';
                this._filter.entityId = '';
                this._renderEntityChip();
                this._onFilterChanged();
            });
    }

    private _renderBody(): void {
        if (this._error) {
            this._bodyEl.innerHTML = `<div class="evl-empty evl-empty-error">${escapeHtml(
                this._error
            )}</div>`;
            return;
        }
        if (!this._events.length) {
            this._bodyEl.innerHTML =
                '<div class="evl-empty">No events match this view. Start the simulation, or widen the filters.</div>';
            return;
        }
        const wasPinned = this._pinnedToTop;
        this._bodyEl.innerHTML = this._events.map((e) => this._rowHtml(e)).join('');
        if (wasPinned) this._scrollEl.scrollTop = 0;
    }

    private _rowHtml(e: RuntimeEvent): string {
        const expanded = this._expanded.has(e.notification_id);
        const chips = e.entities
            .map(
                (ref) =>
                    `<button type="button" class="evl-chip" data-entity-type="${escapeHtml(
                        ref.entity_type
                    )}" data-entity-id="${escapeHtml(ref.entity_id)}" title="Filter to this ${escapeHtml(
                        ref.entity_type
                    )}"><span class="evl-chip-kind">${escapeHtml(
                        ref.entity_type
                    )}</span>${escapeHtml(entityLabel(ref))}</button>`
            )
            .join('');
        // The error cell carries both halves of the diagnostic, origin first —
        // it is the label a reader groups by, so it leads the cell the way the
        // level leads the row.
        const errorCell = e.error
            ? `<span class="evl-origin">${escapeHtml(e.error.origin)}</span><span class="evl-error-text">${escapeHtml(
                  e.error.text
              )}</span>`
            : '';
        // The payload spans every column beneath its row rather than sitting in
        // one of them: it is the whole event's detail, not one field's.
        const payloadBlock = expanded
            ? `<pre class="evl-payload">${escapeHtml(
                  e.payload && Object.keys(e.payload).length
                      ? JSON.stringify(e.payload, null, 2)
                      : 'no payload'
              )}</pre>`
            : '';
        return `
            <div class="evl-row evl-row-${escapeHtml(e.kind)}${
                expanded ? ' evl-row-expanded' : ''
            }" data-event-id="${e.notification_id}">
                <span class="evl-cell evl-time" title="#${e.notification_id}">${formatEventTime(
                    e.event_time
                )}</span>
                <span class="evl-cell evl-level" data-level="${escapeHtml(e.kind)}">${escapeHtml(
                    e.kind
                )}</span>
                <span class="evl-cell evl-source" title="${escapeHtml(e.source)}">${escapeHtml(
                    e.source
                )}</span>
                <span class="evl-cell evl-op" title="${escapeHtml(e.operation)}">${escapeHtml(
                    e.operation
                )}</span>
                <span class="evl-cell evl-message" title="${escapeHtml(e.message)}">${escapeHtml(
                    e.message
                )}</span>
                <span class="evl-cell evl-error">${errorCell}</span>
                <span class="evl-cell evl-entities">${chips}</span>
                ${payloadBlock}
            </div>`;
    }

    private _renderFooter(): void {
        const counts = levelCounts(this._events);
        const live =
            this._refreshHz > 0
                ? `<span class="evl-live">● live ${this._refreshHz} Hz</span>`
                : '<span class="evl-paused">paused</span>';
        const parts = [
            `${this._events.length} shown`,
            counts.error ? `<span class="evl-count-error">${counts.error} error</span>` : '',
            counts.warning ? `<span class="evl-count-warning">${counts.warning} warning</span>` : '',
            this._filter.persisted ? 'on-disk stream' : 'in-memory stream',
            live,
        ].filter(Boolean);
        this._footerEl.innerHTML = parts.join(' · ');
    }

    // ----- persistence -----------------------------------------------------

    private _persist(): void {
        saveWindowState(this._persistenceEnv, WINDOW_ID, {
            geometry: this._window.getGeometry(),
            floating: true,
            hidden: !this._visible,
            eventLog: {
                refreshHz: this._refreshHz,
                level: this._filter.level,
                persisted: this._filter.persisted,
            },
        });
    }
}

function bootstrap(): void {
    if ((window as any).eventLog) return; // idempotent
    (window as any).eventLog = new EventLog();
    (window as any).eventLog.init();
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

export { EventLog };
