/**
 * The event shapes and helper functions behind the Logs screen's Event log tab
 * (frontend/src/next/logs/event-log-tab.tsx), the operator-facing viewer over
 * the one runtime event stream (design_new.md "Event log").
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
 */

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

export const LEVEL_OPTIONS: { value: LevelFilter; label: string }[] = [
    { value: 'all', label: 'All levels' },
    { value: 'error', label: 'Error' },
    { value: 'warning', label: 'Warning' },
    { value: 'info', label: 'Info' },
    { value: 'debug', label: 'Debug' },
];

export const RATE_OPTIONS = [
    { value: 0, label: 'Paused' },
    { value: 1, label: '1 Hz' },
    { value: 2, label: '2 Hz' },
    { value: 4, label: '4 Hz' },
];

/** How many events the panel keeps in its own scrollback. Independent of the
 *  server's retention: this bounds the DOM, not the record. */
export const MAX_ROWS = 1000;

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
