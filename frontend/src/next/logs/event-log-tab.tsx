/**
 * The Logs panel's Event log tab: the operator's viewer over the one runtime
 * event stream (design_new.md "Event log"). It replaces today's Event Log
 * window (panels/event-log.ts) under ?ui=next and keeps its behavior: one
 * column per field, filters answered by the server (`GET /api/event-log`),
 * and reads that ask only for events newer than the highest id already shown.
 *
 * What to ask the server and how to merge the answer are the pure functions
 * exported by panels/event-log.ts; this file only draws them. The row and
 * toolbar styles are that panel's `.evl-*` classes (features/event-log.css).
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks';

import { api } from '../../lib/api';
import {
  EMPTY_FILTER,
  LEVEL_OPTIONS,
  MAX_ROWS,
  RATE_OPTIONS,
  buildQuery,
  distinctValues,
  entityLabel,
  formatEventTime,
  highestId,
  levelCounts,
  mergeEvents,
  type EventEntityRef,
  type EventLogFilter,
  type LevelFilter,
  type RuntimeEvent,
} from '../../panels/event-log';
import { readPrefs, writePrefs } from './state';
import { useRatePoll } from './use-rate-poll';

const DEFAULT_HZ = 2;
/** Today's window never read faster than every 200 ms; keep that. */
const MIN_PERIOD_MS = 200;
/** A search that re-reads on every keystroke asks the server a question the
 *  operator has not finished typing yet. */
const SEARCH_DELAY_MS = 250;

function savedFilter(): EventLogFilter {
  const prefs = readPrefs().events ?? {};
  const level = LEVEL_OPTIONS.some((o) => o.value === prefs.level) ? (prefs.level as LevelFilter) : 'all';
  return { ...EMPTY_FILTER, level, persisted: prefs.persisted === true };
}

function savedHz(): number {
  const hz = Number(readPrefs().events?.refreshHz);
  return RATE_OPTIONS.some((o) => o.value === hz) ? hz : DEFAULT_HZ;
}

export function EventLogTab() {
  const [filter, setFilter] = useState<EventLogFilter>(savedFilter);
  const [hz, setHz] = useState<number>(savedHz);
  const [events, setEvents] = useState<RuntimeEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  // Ids whose payload is open. A set, so opening a second event to compare
  // does not close the first.
  const [expanded, setExpanded] = useState<ReadonlySet<number>>(new Set());
  const [searchText, setSearchText] = useState('');

  // What a read needs, kept outside render state so a read the timer starts
  // always sees the latest filter. `generation` counts filter changes: an
  // answer to an older question is dropped instead of merged.
  const view = useRef({
    filter,
    events: [] as RuntimeEvent[],
    watermark: null as number | null,
    generation: 0,
    reading: null as Promise<void> | null,
  });

  const read = (): Promise<void> => {
    const v = view.current;
    if (v.reading) return v.reading;
    const generation = v.generation;
    const run = (async () => {
      try {
        const res = await api.getEventLog(buildQuery(v.filter, MAX_ROWS, v.watermark));
        if (generation !== v.generation) return;
        const incoming: RuntimeEvent[] = Array.isArray(res?.events) ? res.events : [];
        v.events = mergeEvents(v.events, incoming);
        const top = highestId(v.events);
        if (top !== null) v.watermark = top;
        setEvents(v.events);
        setError(null);
      } catch (err: any) {
        if (generation === v.generation) {
          setError(err?.message ? String(err.message) : 'event log unavailable');
        }
      } finally {
        v.reading = null;
      }
      // The filter changed while this read was out: ask the new question now
      // rather than at the next tick (which never comes while Paused).
      if (generation !== v.generation) await read();
    })();
    v.reading = run;
    return run;
  };

  useRatePoll(read, hz, MIN_PERIOD_MS);

  /** Start the view over: empty list, no watermark, read again. */
  const restart = () => {
    const v = view.current;
    v.events = [];
    v.watermark = null;
    v.generation += 1;
    setEvents([]);
    setExpanded(new Set());
    void read();
  };

  /** Any filter change makes the server answer a different question, so the
   *  list and the watermark both start over. */
  const changeFilter = (patch: Partial<EventLogFilter>) => {
    const next = { ...view.current.filter, ...patch };
    view.current.filter = next;
    setFilter(next);
    writePrefs({ events: { level: next.level, persisted: next.persisted } });
    restart();
  };

  useEffect(() => {
    const query = searchText.trim();
    if (query === view.current.filter.query) return undefined;
    const timer = setTimeout(() => changeFilter({ query }), SEARCH_DELAY_MS);
    return () => clearTimeout(timer);
  }, [searchText]);

  const changeRate = (value: string) => {
    const next = Number(value);
    const rate = Number.isFinite(next) ? next : DEFAULT_HZ;
    setHz(rate);
    writePrefs({ events: { refreshHz: rate } });
  };

  const toggleRow = (id: number) => {
    const next = new Set(expanded);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setExpanded(next);
  };

  const pickEntity = (ref: EventEntityRef) =>
    changeFilter({ entityType: ref.entity_type, entityId: ref.entity_id });

  // While the operator reads further down, new events must not pull the list
  // back to the top underneath them.
  const scroll = useRef<HTMLDivElement>(null);
  const pinnedToTop = useRef(true);
  useLayoutEffect(() => {
    if (pinnedToTop.current && scroll.current) scroll.current.scrollTop = 0;
  }, [events]);

  const counts = levelCounts(events);

  return (
    <div class="nlp-pane nlp-events">
      <div class="evl-toolbar">
        <label class="evl-field">
          Level
          <select value={filter.level} onChange={(e) => changeFilter({ level: e.currentTarget.value as LevelFilter })}>
            {LEVEL_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <label class="evl-field">
          Source
          <ValueSelect
            values={distinctValues(events, 'source')}
            allLabel="All sources"
            selected={filter.source}
            onPick={(source) => changeFilter({ source })}
          />
        </label>
        <label class="evl-field">
          Operation
          <ValueSelect
            values={distinctValues(events, 'operation')}
            allLabel="All operations"
            selected={filter.operation}
            onPick={(operation) => changeFilter({ operation })}
          />
        </label>
        <label class="evl-field evl-field-grow">
          Search
          <input
            type="search"
            placeholder="message, error, origin, entity"
            value={searchText}
            onInput={(e) => setSearchText(e.currentTarget.value)}
          />
        </label>
        <div class="evl-entity-slot">
          {filter.entityType && filter.entityId ? (
            <span class="evl-active-entity">
              <span class="evl-entity-kind">{filter.entityType}</span>
              {filter.entityId}
              <button
                type="button"
                aria-label="Show all entities"
                onClick={() => changeFilter({ entityType: '', entityId: '' })}
              >
                ×
              </button>
            </span>
          ) : (
            <span class="evl-entity-empty">All entities</span>
          )}
        </div>
        <label
          class="evl-check"
          title="Read the on-disk stream instead of the in-memory archive, so an earlier run's incidents survive a restart."
        >
          <input
            type="checkbox"
            checked={filter.persisted}
            onChange={(e) => changeFilter({ persisted: e.currentTarget.checked })}
          />{' '}
          Persisted
        </label>
        <label class="evl-field">
          Live
          <select value={String(hz)} onChange={(e) => changeRate(e.currentTarget.value)}>
            {RATE_OPTIONS.map((o) => (
              <option key={o.value} value={String(o.value)}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <button
          class="evl-icon-btn"
          type="button"
          title="Clear this view. The record itself is untouched; the next read refills from the stream."
          aria-label="Clear this view"
          onClick={restart}
        >
          <i class="ti ti-eraser" aria-hidden="true" />
        </button>
      </div>
      <div
        class="evl-scroll"
        ref={scroll}
        onScroll={(e) => (pinnedToTop.current = e.currentTarget.scrollTop <= 4)}
      >
        <div class="evl-head">
          <span class="evl-cell">Time</span>
          <span class="evl-cell">Level</span>
          <span class="evl-cell">Source</span>
          <span class="evl-cell">Operation</span>
          <span class="evl-cell">Message</span>
          <span class="evl-cell">Error</span>
          <span class="evl-cell">Entities</span>
        </div>
        <div class="evl-body">
          {error ? (
            <div class="evl-empty evl-empty-error">{error}</div>
          ) : events.length === 0 ? (
            <div class="evl-empty">No events match this view. Start the simulation, or widen the filters.</div>
          ) : (
            events.map((e) => (
              <EventRow
                key={e.notification_id}
                event={e}
                expanded={expanded.has(e.notification_id)}
                onToggle={toggleRow}
                onPickEntity={pickEntity}
              />
            ))
          )}
        </div>
      </div>
      <div class="evl-footer">
        {events.length} shown
        {counts.error > 0 && (
          <>
            {' · '}
            <span class="evl-count-error">{counts.error} error</span>
          </>
        )}
        {counts.warning > 0 && (
          <>
            {' · '}
            <span class="evl-count-warning">{counts.warning} warning</span>
          </>
        )}
        {' · '}
        {filter.persisted ? 'on-disk stream' : 'in-memory stream'}
        {' · '}
        {hz > 0 ? <span class="evl-live">● live {hz} Hz</span> : <span class="evl-paused">paused</span>}
      </div>
    </div>
  );
}

/** A Source or Operation picker. Its options are the values seen so far; a
 *  picked value that has aged out of the list stays as an option, or the
 *  filter would silently widen underneath the operator. */
function ValueSelect(props: {
  values: string[];
  allLabel: string;
  selected: string;
  onPick: (value: string) => void;
}) {
  const { values, allLabel, selected, onPick } = props;
  const options = selected && !values.includes(selected) ? [...values, selected] : values;
  return (
    <select value={selected} onChange={(e) => onPick(e.currentTarget.value)}>
      <option value="">{allLabel}</option>
      {options.map((v) => (
        <option key={v} value={v}>
          {v}
        </option>
      ))}
    </select>
  );
}

function EventRow(props: {
  event: RuntimeEvent;
  expanded: boolean;
  onToggle: (id: number) => void;
  onPickEntity: (ref: EventEntityRef) => void;
}) {
  const { event: e, expanded, onToggle, onPickEntity } = props;
  const payload =
    e.payload && Object.keys(e.payload).length ? JSON.stringify(e.payload, null, 2) : 'no payload';
  return (
    <div
      class={`evl-row evl-row-${e.kind}${expanded ? ' evl-row-expanded' : ''}`}
      onClick={() => onToggle(e.notification_id)}
    >
      <span class="evl-cell evl-time" title={`#${e.notification_id}`}>
        {formatEventTime(e.event_time)}
      </span>
      <span class="evl-cell evl-level" data-level={e.kind}>
        {e.kind}
      </span>
      <span class="evl-cell evl-source" title={e.source}>
        {e.source}
      </span>
      <span class="evl-cell evl-op" title={e.operation}>
        {e.operation}
      </span>
      <span class="evl-cell evl-message" title={e.message}>
        {e.message}
      </span>
      {/* Origin first: it is the label a reader groups errors by. */}
      <span class="evl-cell evl-error">
        {e.error && (
          <>
            <span class="evl-origin">{e.error.origin}</span>
            <span class="evl-error-text">{e.error.text}</span>
          </>
        )}
      </span>
      <span class="evl-cell evl-entities">
        {(e.entities ?? []).map((ref) => (
          <button
            key={`${ref.entity_type}:${ref.entity_id}`}
            type="button"
            class="evl-chip"
            title={`Filter to this ${ref.entity_type}`}
            onClick={(ev) => {
              ev.stopPropagation();
              onPickEntity(ref);
            }}
          >
            <span class="evl-chip-kind">{ref.entity_type}</span>
            {entityLabel(ref)}
          </button>
        ))}
      </span>
      {expanded && <pre class="evl-payload">{payload}</pre>}
    </div>
  );
}
