import { describe, it, expect } from 'vitest';
import {
    buildQuery,
    mergeEvents,
    highestId,
    levelCounts,
    distinctValues,
    entityLabel,
    formatEventTime,
    EMPTY_FILTER,
    type RuntimeEvent,
} from './event-log';

const ev = (over: Partial<RuntimeEvent> = {}): RuntimeEvent => ({
    notification_id: 1,
    event_time: 1_700_000_000,
    source: 'RouteExecutor',
    operation: 'tick_train_phase1',
    kind: 'info',
    entities: [],
    message: 'planned',
    error: null,
    payload: {},
    ...over,
});

describe('buildQuery', () => {
    it('sends only the filters that are set', () => {
        // A default view must ask the cheapest question the endpoint answers —
        // an empty filter that serialised every key would make the server run
        // every branch of its loop for nothing.
        const qs = buildQuery(EMPTY_FILTER, 500, null);
        expect(qs).toBe('limit=500');
    });

    it('pairs entity_type with entity_id, never one alone', () => {
        // The endpoint only filters when both are present, so sending a lone
        // entity_type would silently widen the view instead of narrowing it.
        const half = buildQuery({ ...EMPTY_FILTER, entityType: 'train' }, 10, null);
        expect(half).not.toContain('entity_type');

        const both = buildQuery(
            { ...EMPTY_FILTER, entityType: 'train', entityId: 'T1' },
            10,
            null
        );
        expect(both).toContain('entity_type=train');
        expect(both).toContain('entity_id=T1');
    });

    it('carries the watermark so a poll transfers only what is new', () => {
        const qs = buildQuery(EMPTY_FILTER, 500, 42);
        expect(qs).toContain('since_notification_id=42');
    });

    it('omits the watermark when it is null, so a reset re-reads from the top', () => {
        expect(buildQuery(EMPTY_FILTER, 500, null)).not.toContain('since_notification_id');
    });

    it("maps the level filter onto the endpoint's kind parameter", () => {
        expect(buildQuery({ ...EMPTY_FILTER, level: 'error' }, 10, null)).toContain('kind=error');
        expect(buildQuery({ ...EMPTY_FILTER, level: 'all' }, 10, null)).not.toContain('kind=');
    });
});

describe('mergeEvents', () => {
    it('keeps the list newest first by notification_id', () => {
        const merged = mergeEvents([ev({ notification_id: 2 })], [ev({ notification_id: 5 }), ev({ notification_id: 3 })]);
        expect(merged.map((e) => e.notification_id)).toEqual([5, 3, 2]);
    });

    it('drops duplicates by id', () => {
        // A filter change resets the watermark and re-reads from the top, so the
        // same events arrive again; without the id dedupe they would double up.
        const existing = [ev({ notification_id: 2 }), ev({ notification_id: 1 })];
        const merged = mergeEvents(existing, [ev({ notification_id: 2 }), ev({ notification_id: 1 })]);
        expect(merged.map((e) => e.notification_id)).toEqual([2, 1]);
    });

    it('returns the same array when nothing is new', () => {
        const existing = [ev({ notification_id: 1 })];
        expect(mergeEvents(existing, [])).toBe(existing);
        expect(mergeEvents(existing, [ev({ notification_id: 1 })])).toBe(existing);
    });

    it('trims to the row cap, keeping the newest', () => {
        const incoming = [1, 2, 3, 4, 5].map((i) => ev({ notification_id: i }));
        const merged = mergeEvents([], incoming, 3);
        expect(merged.map((e) => e.notification_id)).toEqual([5, 4, 3]);
    });
});

describe('highestId', () => {
    it('is null for an empty buffer, so the first read asks from the top', () => {
        expect(highestId([])).toBeNull();
    });

    it('finds the top id regardless of order', () => {
        expect(highestId([ev({ notification_id: 3 }), ev({ notification_id: 9 })])).toBe(9);
    });
});

describe('levelCounts', () => {
    it('counts each level and ignores unknown ones', () => {
        const counts = levelCounts([
            ev({ kind: 'error' }),
            ev({ kind: 'error' }),
            ev({ kind: 'warning' }),
            ev({ kind: 'nonsense' }),
        ]);
        expect(counts.error).toBe(2);
        expect(counts.warning).toBe(1);
        expect(counts.info).toBe(0);
    });
});

describe('distinctValues', () => {
    it('derives the dropdown contents from what has been seen', () => {
        // `operation` is a function name, so the set grows whenever a new call
        // site starts reporting — it cannot be a hardcoded list.
        const events = [
            ev({ operation: 'cancel_path' }),
            ev({ operation: 'set_handle' }),
            ev({ operation: 'cancel_path' }),
        ];
        expect(distinctValues(events, 'operation')).toEqual(['cancel_path', 'set_handle']);
    });
});

describe('entityLabel', () => {
    it('shows a walk position as its index, not the packed run#index id', () => {
        // The run is already named by its own sibling chip on the same event, so
        // repeating it in the task chip is noise.
        expect(entityLabel({ entity_type: 'walk_position', entity_id: 'RUN7#3' })).toBe('task 3');
    });

    it('passes an ordinary ref through unchanged', () => {
        expect(entityLabel({ entity_type: 'train', entity_id: 'T1' })).toBe('T1');
    });
});

describe('formatEventTime', () => {
    it('renders a placeholder for a missing timestamp rather than 1970', () => {
        expect(formatEventTime(0)).toBe('—');
        expect(formatEventTime(Number.NaN)).toBe('—');
    });

    it('keeps milliseconds, because a tick emits several events inside one second', () => {
        expect(formatEventTime(1_700_000_000.123)).toMatch(/^\d{2}:\d{2}:\d{2}\.\d{3}$/);
    });
});
