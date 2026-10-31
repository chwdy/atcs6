/**
 * The Logs panel's Diagnostics tab: a read-only, live view of one train and
 * its TrainController, as `GET /api/inspector/entity/train/<id>` reports them
 * in groups. It replaces today's Diagnostics window (panels/diagnostics.ts)
 * under ?ui=next.
 *
 * Kept from today's window: a row whose value changed since the last read
 * flashes; object and array values fold open one level at a time, or all at
 * once; groups fold; rows sort A→Z, Z→A or in the server's order; clicking a
 * name copies it. Changed: the details about where a property comes from
 * (computed_from, writer, consumer, type) and a cut-off value's full text are
 * in the browser's own tooltip instead of a hand-drawn one.
 *
 * The train list comes from the shared network snapshot (`stateManager`), so
 * this tab asks the server only for the train it shows. The styles are
 * today's `.diagnostics-*` classes (features/diagnostics.css).
 */
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';

import { api } from '../../lib/api';
import { stateManager } from '../../lib/state-manager';
import { showToast } from '../../lib/toast';
import {
  REFRESH_OPTIONS,
  containerPaths,
  containerSummary,
  fmtValue,
  indexSnapshot,
  isContainer,
  itemMeta,
  nextSortMode,
  pathKey,
  rowKey,
  shortValue,
  sortItems,
  valueChanged,
  type InspectorGroup,
  type InspectorItem,
  type InspectorSnapshot,
  type SortMode,
} from './diagnostics-model';
import { readPrefs, writePrefs } from './state';
import { singleFlight, useRatePoll } from './use-rate-poll';

const DEFAULT_HZ = 1;

const SORT_BUTTON: Record<'asc' | 'desc' | 'none', { label: string; title: string }> = {
  asc: { label: 'A↓', title: 'Items sorted A→Z. Click for Z→A.' },
  desc: { label: 'Z↓', title: 'Items sorted Z→A. Click to restore default order.' },
  none: { label: '⇅', title: 'Items in default (curated) order. Click to sort A→Z.' },
};

interface Shown {
  snapshot: InspectorSnapshot | null;
  /** The read before `snapshot`, by row key: what a changed row is compared to. */
  previous: Map<string, InspectorItem>;
  /** The server answered that the train no longer exists. */
  gone: boolean;
  error: string | null;
}

const NOTHING_SHOWN: Shown = { snapshot: null, previous: new Map(), gone: false, error: null };

function savedHz(): number {
  const hz = Number(readPrefs().diagnostics?.refreshHz);
  return REFRESH_OPTIONS.some((o) => o.hz === hz) ? hz : DEFAULT_HZ;
}

function savedSortMode(): SortMode {
  const mode = readPrefs().diagnostics?.sortMode;
  return mode === 'asc' || mode === 'desc' || mode === null ? mode : 'asc';
}

function savedCollapsedGroups(): ReadonlySet<string> {
  const groups = readPrefs().diagnostics?.collapsedGroups;
  return new Set(Array.isArray(groups) ? groups.filter((g) => typeof g === 'string') : []);
}

function savedTrainId(): string | null {
  const id = readPrefs().diagnostics?.trainId;
  return typeof id === 'string' && id ? id : null;
}

/** The ids of the trains in the shared network snapshot, in its order. */
function useTrainIds(): string[] {
  const read = () =>
    stateManager
      .getAll('trains')
      .map((t: any) => t?.id)
      .filter((id: unknown): id is string => typeof id === 'string' && !!id);
  const [ids, setIds] = useState<string[]>(read);
  useEffect(() => {
    const update = () => {
      const next = read();
      setIds((prev) => (prev.join('\n') === next.join('\n') ? prev : next));
    };
    update();
    return stateManager.subscribe('trains', '*', update, { immediate: false });
  }, []);
  return ids;
}

function copyName(name: string): void {
  navigator.clipboard
    ?.writeText(name)
    .then(() => showToast('Copied: ' + name, 'success', 2000))
    .catch(() => showToast('Failed to copy name', 'error', 2000));
}

export function DiagnosticsTab() {
  const trainIds = useTrainIds();
  const [trainId, setTrainId] = useState<string | null>(savedTrainId);
  const [hz, setHz] = useState<number>(savedHz);
  const [sortMode, setSortMode] = useState<SortMode>(savedSortMode);
  const [collapsedGroups, setCollapsedGroups] = useState<ReadonlySet<string>>(savedCollapsedGroups);
  // Which objects and arrays inside the values are folded open (by pathKey).
  // Not saved: it only means something for the values on screen now.
  const [openPaths, setOpenPaths] = useState<ReadonlySet<string>>(new Set());
  const [shown, setShown] = useState<Shown>(NOTHING_SHOWN);

  // The train a read is for, outside render state so a timer read sees a new
  // pick at once, and the last snapshot, to compare the next one against.
  const current = useRef({ trainId, last: null as InspectorSnapshot | null });
  // How many times each row's value has changed. It is part of the row's key,
  // so a row that changes on two reads in a row is redrawn and flashes again.
  const changes = useRef(new Map<string, number>());

  const read = useMemo(
    () =>
      singleFlight(async () => {
        const id = current.current.trainId;
        if (!id) return;
        try {
          const data = (await api.getInspectorEntityTrain(id)) as InspectorSnapshot | null;
          if (id !== current.current.trainId) return;
          if (data === null) {
            setShown({ ...NOTHING_SHOWN, gone: true });
            return;
          }
          const previous = indexSnapshot(current.current.last);
          for (const group of data.groups ?? []) {
            for (const item of group.items ?? []) {
              const key = rowKey(group, item);
              if (valueChanged(item, previous.get(key))) {
                changes.current.set(key, (changes.current.get(key) ?? 0) + 1);
              }
            }
          }
          current.current.last = data;
          setShown({ snapshot: data, previous, gone: false, error: null });
        } catch (err) {
          if (id !== current.current.trainId) return;
          const message = (err as { message?: string })?.message || String(err);
          setShown((s) => ({ ...s, error: message }));
        }
      }),
    []
  );

  // A train the server says is gone is not asked about again until another
  // one is picked; with no train picked there is nothing to ask.
  useRatePoll(read, trainId && !shown.gone ? hz : 0);

  const pickTrain = (id: string | null) => {
    current.current = { trainId: id, last: null };
    changes.current.clear();
    setTrainId(id);
    setShown(NOTHING_SHOWN);
    writePrefs({ diagnostics: { trainId: id } });
    if (id) void read();
  };

  const changeRate = (value: string) => {
    const next = Number(value);
    const rate = Number.isFinite(next) ? next : DEFAULT_HZ;
    setHz(rate);
    writePrefs({ diagnostics: { refreshHz: rate } });
  };

  const cycleSort = () => {
    const next = nextSortMode(sortMode);
    setSortMode(next);
    writePrefs({ diagnostics: { sortMode: next } });
  };

  const toggleGroup = (name: string) => {
    const next = new Set(collapsedGroups);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    setCollapsedGroups(next);
    writePrefs({ diagnostics: { collapsedGroups: [...next] } });
  };

  const togglePath = (key: string) => {
    const next = new Set(openPaths);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    setOpenPaths(next);
  };

  /** Fold open (or shut) every object and array in one row, or in all rows. */
  const setRowsOpen = (open: boolean, onlyRow?: string) => {
    const next = new Set(openPaths);
    for (const group of shown.snapshot?.groups ?? []) {
      for (const item of group.items ?? []) {
        const key = rowKey(group, item);
        if (onlyRow !== undefined && key !== onlyRow) continue;
        for (const path of containerPaths(item.value, key)) {
          if (open) next.add(path);
          else next.delete(path);
        }
      }
    }
    setOpenPaths(onlyRow === undefined && !open ? new Set() : next);
  };

  // The picked train stays in the list after it is deleted, so the picker
  // still names the train the "no longer exists" message is about.
  const trainOptions = trainId && !trainIds.includes(trainId) ? [...trainIds, trainId] : trainIds;
  const sortButton = SORT_BUTTON[sortMode ?? 'none'];
  const { snapshot } = shown;

  let body;
  if (!trainId) body = <div class="empty-state">Select a train to begin.</div>;
  else if (shown.gone) body = <div class="empty-state empty-state-warn">Train no longer exists.</div>;
  else if (shown.error) body = <div class="empty-state empty-state-error">Error: {shown.error}</div>;
  else if (!snapshot) body = <div class="empty-state">Reading…</div>;
  else
    body = (snapshot.groups ?? []).map((group) => (
      <DiagnosticsGroup
        key={group.name}
        group={group}
        collapsed={collapsedGroups.has(group.name)}
        sortMode={sortMode}
        previous={shown.previous}
        changes={changes.current}
        openPaths={openPaths}
        onToggleGroup={toggleGroup}
        onTogglePath={togglePath}
        onSetRowOpen={(row, open) => setRowsOpen(open, row)}
      />
    ));

  return (
    <div class="nlp-pane nlp-diagnostics">
      <div class="diagnostics-panel-toolbar">
        <label class="diagnostics-panel-field">
          Train
          <select
            class="diagnostics-panel-train-select"
            value={trainId ?? ''}
            onChange={(e) => pickTrain(e.currentTarget.value || null)}
          >
            <option value="">(select a train)</option>
            {trainOptions.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        </label>
        <label class="diagnostics-panel-field">
          Refresh
          <select class="diagnostics-panel-rate-select" value={String(hz)} onChange={(e) => changeRate(e.currentTarget.value)}>
            {REFRESH_OPTIONS.map((o) => (
              <option key={o.hz} value={String(o.hz)}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <div class="diagnostics-panel-tools">
          <button class="diagnostics-panel-refresh-btn" type="button" title="Refresh now" aria-label="Refresh now" onClick={() => void read()}>
            ↻
          </button>
          <button class="diagnostics-panel-tree-btn" type="button" title="Expand all values" aria-label="Expand all values" onClick={() => setRowsOpen(true)}>
            ⊞
          </button>
          <button class="diagnostics-panel-tree-btn" type="button" title="Collapse all values" aria-label="Collapse all values" onClick={() => setRowsOpen(false)}>
            ⊟
          </button>
          <button
            class={sortMode ? 'diagnostics-panel-sort-btn active' : 'diagnostics-panel-sort-btn'}
            type="button"
            title={sortButton.title}
            onClick={cycleSort}
          >
            {sortButton.label}
          </button>
        </div>
      </div>
      <div class="diagnostics-panel-body">{body}</div>
      <div class="diagnostics-panel-footer">
        {snapshot && !shown.gone && (
          <>
            {snapshot.label || snapshot.id || '?'} · fetched{' '}
            {snapshot.fetched_at ? new Date(snapshot.fetched_at * 1000).toLocaleTimeString() : ''}
          </>
        )}
      </div>
    </div>
  );
}

function DiagnosticsGroup(props: {
  group: InspectorGroup;
  collapsed: boolean;
  sortMode: SortMode;
  previous: Map<string, InspectorItem>;
  changes: ReadonlyMap<string, number>;
  openPaths: ReadonlySet<string>;
  onToggleGroup: (name: string) => void;
  onTogglePath: (key: string) => void;
  onSetRowOpen: (row: string, open: boolean) => void;
}) {
  const { group, collapsed, sortMode, previous, changes, openPaths } = props;
  const items = sortItems(group.items ?? [], sortMode);
  return (
    <div class={collapsed ? 'diagnostics-group collapsed' : 'diagnostics-group'}>
      <div class="diagnostics-group-header" onClick={() => props.onToggleGroup(group.name)}>
        <span class="diagnostics-group-caret">{collapsed ? '▸' : '▾'}</span>
        <span class="diagnostics-group-name">{group.name}</span>
        <span class="chip">{items.length}</span>
      </div>
      <div class="diagnostics-group-items">
        {!collapsed &&
          items.map((item) => {
            const key = rowKey(group, item);
            return (
              <DiagnosticsRow
                key={`${key}#${changes.get(key) ?? 0}`}
                item={item}
                row={key}
                changed={valueChanged(item, previous.get(key))}
                openPaths={openPaths}
                onTogglePath={props.onTogglePath}
                onSetRowOpen={props.onSetRowOpen}
              />
            );
          })}
      </div>
    </div>
  );
}

function DiagnosticsRow(props: {
  item: InspectorItem;
  row: string;
  changed: boolean;
  openPaths: ReadonlySet<string>;
  onTogglePath: (key: string) => void;
  onSetRowOpen: (row: string, open: boolean) => void;
}) {
  const { item, row, changed, openPaths } = props;
  const errored = item.error !== undefined && item.error !== null;
  const structured = !errored && isContainer(item.value);
  const meta = itemMeta(item);
  const classes = ['diagnostics-row'];
  if (errored) classes.push('diagnostics-row-error');
  if (changed) classes.push('diagnostics-row-flash');
  if (structured) classes.push('diagnostics-row-structured');

  let value;
  if (errored) value = item.error;
  else if (structured)
    value = (
      <>
        <button class="diagnostics-row-expand-btn" type="button" title="Expand all in this field" onClick={() => props.onSetRowOpen(row, true)}>
          ⊞
        </button>
        <button class="diagnostics-row-collapse-btn" type="button" title="Collapse all in this field" onClick={() => props.onSetRowOpen(row, false)}>
          ⊟
        </button>
        <JsonValue value={item.value} row={row} path="root" openPaths={openPaths} onToggle={props.onTogglePath} />
      </>
    );
  else value = <span class="diagnostics-json-scalar">{shortValue(item.value)}</span>;

  return (
    <div class={classes.join(' ')} data-source={item.source || ''}>
      <span
        class="diagnostics-row-name"
        title={meta ? `${item.name}\n${meta}\nClick to copy the name.` : `${item.name}\nClick to copy the name.`}
        onClick={() => copyName(item.name)}
      >
        {item.name}
      </span>
      <span class="diagnostics-row-source-badge" data-source={item.source || ''}>
        {item.source || ''}
      </span>
      <span class="diagnostics-row-value" title={!errored && !structured ? fmtValue(item.value) : undefined}>
        {value}
      </span>
    </div>
  );
}

function JsonValue(props: {
  value: unknown;
  row: string;
  path: string;
  openPaths: ReadonlySet<string>;
  onToggle: (key: string) => void;
}) {
  const { value, row, path, openPaths, onToggle } = props;
  if (value === null) return <span class="diagnostics-json-null">null</span>;
  if (value === undefined) return <span class="diagnostics-json-null">undefined</span>;
  if (typeof value === 'string') return <span class="diagnostics-json-string">"{value}"</span>;
  if (typeof value === 'number') return <span class="diagnostics-json-number">{String(value)}</span>;
  if (typeof value === 'boolean') return <span class="diagnostics-json-bool">{String(value)}</span>;
  if (typeof value !== 'object') return <span class="diagnostics-json-scalar">{String(value)}</span>;

  const isArray = Array.isArray(value);
  const entries: [string | number, unknown][] = isArray
    ? (value as unknown[]).map((v, i) => [i, v])
    : Object.entries(value);
  if (entries.length === 0) return <span class="diagnostics-json-empty">{isArray ? '[]' : '{}'}</span>;

  const key = pathKey(row, path);
  const open = openPaths.has(key);
  const caret = (
    <span
      class="diagnostics-json-caret"
      role="button"
      aria-expanded={open}
      onClick={(e) => {
        e.stopPropagation();
        onToggle(key);
      }}
    >
      {open ? '▾' : '▸'}
    </span>
  );
  if (!open) {
    return (
      <span class="diagnostics-json-node">
        {caret}
        <span class="diagnostics-json-summary">{containerSummary(value)}</span>
      </span>
    );
  }
  return (
    <span class="diagnostics-json-node diagnostics-json-node-expanded">
      {caret}
      <span class="diagnostics-json-bracket">{isArray ? '[' : '{'}</span>
      <div class="diagnostics-json-children">
        {entries.map(([k, v]) => (
          <div class="diagnostics-json-child" key={k}>
            {isArray ? (
              <span class="diagnostics-json-index">{k}:</span>
            ) : (
              <span class="diagnostics-json-key">"{k}":</span>
            )}{' '}
            <JsonValue
              value={v}
              row={row}
              path={isArray ? `${path}[${k}]` : `${path}.${k}`}
              openPaths={openPaths}
              onToggle={onToggle}
            />
          </div>
        ))}
      </div>
      <span class="diagnostics-json-bracket">{isArray ? ']' : '}'}</span>
    </span>
  );
}
