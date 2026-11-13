/**
 * The Operate screen's left list (docs/ui-layout-rewrite-plan.md §5): the
 * operational lists, each in its own folding section. The whole list folds
 * away to give the map room.
 *
 * Blocks and switches come from the network poll; stations have their own
 * endpoint, read only while their section is open. A block or switch opens in
 * this screen's own right panel; a station opens today's panel for it, shown
 * inside the right panel (panel-host.ts). The trains are on the cards row and
 * the map.
 */
import { signal } from '@preact/signals';
import { useState } from 'preact/hooks';

import type { ComponentChildren } from 'preact';

import { api } from '../../lib/api';
import { stateManager } from '../../lib/state-manager';
import { usePoll } from '../poll';
import { liveVersion } from './live';
import { listOpen, selection, type OperateHost } from './operate-state';
import { setSelection, wrappedPanelKey } from './panel-host';

/** Which sections are open; kept while the screen comes and goes. */
const openSections = signal<Record<string, boolean>>({ blocks: true });

function Section({
  id,
  title,
  count,
  action,
  children,
}: {
  id: string;
  title: string;
  count?: number;
  action?: { label: string; title: string; run: () => void };
  children: ComponentChildren;
}) {
  const open = !!openSections.value[id];
  return (
    <details
      class="op-list-section"
      open={open}
      onToggle={(e) => {
        const next = e.currentTarget.open;
        if (next !== open) openSections.value = { ...openSections.value, [id]: next };
      }}
    >
      <summary>
        <span class="op-list-title">{title}</span>
        {count !== undefined && <span class="chip">{count}</span>}
        {action && (
          <button
            type="button"
            class="rr-flat-btn op-list-action"
            title={action.title}
            onClick={(e) => {
              e.preventDefault();
              action.run();
            }}
          >
            <i class="ti ti-plus" aria-hidden="true" /> {action.label}
          </button>
        )}
      </summary>
      {open && <div class="op-list-body">{children}</div>}
    </details>
  );
}

function Row({
  label,
  meta,
  chip,
  selected,
  onClick,
}: {
  label: string;
  meta?: string;
  chip?: ComponentChildren;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button type="button" class={selected ? 'op-list-row row-selected' : 'op-list-row'} onClick={onClick}>
      <span class="op-list-row-text">
        <span class="op-list-row-label">{label}</span>
        {meta && <span class="op-list-row-meta">{meta}</span>}
      </span>
      {chip}
    </button>
  );
}

/** An empty list (DESIGN_LANGUAGE.md §10): one plain line, and the button
 *  that fills the list when there is one. */
function Empty({
  children,
  action,
}: {
  children: ComponentChildren;
  action?: { label: string; run: () => void };
}) {
  if (!action) return <div class="empty-state">{children}</div>;
  return (
    <div class="empty-state-action">
      <span>{children}</span>
      <button type="button" class="btn btn-accent btn-sm" onClick={action.run}>
        <i class="ti ti-plus" aria-hidden="true" /> {action.label}
      </button>
    </div>
  );
}

/** A list read from its own endpoint: the last list read, and why the latest
 *  read failed, if it did. */
export interface PolledList<T> {
  items: T[] | null;
  error: string | null;
}

/** Read a list from its endpoint every `periodMs` while the section shows. A
 *  failed read keeps the last list and says so, so a server that stopped
 *  answering never looks like an empty layout. */
function usePolledList<T>(read: () => Promise<T[]>, periodMs: number): PolledList<T> {
  const [list, setList] = useState<PolledList<T>>({ items: null, error: null });
  usePoll(async () => {
    try {
      const items = await read();
      setList({ items, error: null });
    } catch (err: any) {
      const error = String(err?.message || err || 'no answer');
      setList((prev) => ({ items: prev.items, error }));
    }
  }, periodMs);
  return list;
}

/**
 * What a list read from its endpoint shows: "Loading…" before the first
 * answer, the reason when no read has worked yet, the empty line when the
 * list is empty, and otherwise the rows, under a note while the latest read
 * failed.
 */
export function PolledBody<T>({
  list,
  what,
  empty,
  children,
}: {
  list: PolledList<T>;
  /** The list's name in a sentence: "routes", "route runs". */
  what: string;
  empty: ComponentChildren;
  children: (items: T[]) => ComponentChildren;
}) {
  const { items, error } = list;
  if (items === null) {
    if (error === null) return <div class="empty-state">Loading…</div>;
    return (
      <div class="empty-state empty-state-error" role="status">
        Could not read the {what}: {error}. Trying again.
      </div>
    );
  }
  if (items.length === 0 && error === null) return <>{empty}</>;
  return (
    <>
      {error !== null && (
        <div class="banner banner-warn op-list-stale" role="status">
          Could not read the {what} just now ({error}). This is the last list read; trying again.
        </div>
      )}
      {items.length === 0 ? empty : children(items)}
    </>
  );
}

/** Whether today's panel shown in the right panel is this one. */
function showing(type: string, id: unknown): boolean {
  const key = wrappedPanelKey.value;
  return selection.value?.kind === 'panel' && key.type === type && key.id === String(id);
}

function StationsList({ host }: { host: OperateHost }) {
  const stations = usePolledList<any>(async () => (await api.listStationGroups())?.stations ?? [], 10000);
  return (
    <PolledBody
      list={stations}
      what="stations"
      empty={<Empty>No stations on this layout. Draw platforms in the Editor, then group them into a station.</Empty>}
    >
      {(items) =>
        items.map((s) => {
          const platforms = Array.isArray(s.platform_ids) ? s.platform_ids.length : 0;
          return (
            <Row
              key={s.station_id}
              label={s.name || s.station_id}
              meta={`${platforms} platform${platforms === 1 ? '' : 's'}`}
              selected={showing('station-group-detail', s.station_id)}
              onClick={() => void host.editor.stationGroupHandler?.showStationDetail(s.station_id)}
            />
          );
        })
      }
    </PolledBody>
  );
}

/** Blocks and switches: today's tool rail lists them in Operate too, so the
 *  rail can go only if they stay reachable here. */
function EntityList({ host, kind }: { host: OperateHost; kind: 'block' | 'switch' }) {
  liveVersion.value;
  const items = stateManager
    .getAll(kind === 'block' ? 'blocks' : 'switches')
    .map((e: any) => ({ id: String(kind === 'block' ? e.block_id : e.switch_id), name: String(e.name || '').trim(), e }))
    .sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id));
  const sel = selection.value;
  if (items.length === 0) {
    return (
      <Empty>
        {kind === 'block'
          ? 'No blocks yet. Each segment drawn in the Editor gets its own block.'
          : 'No switches. Draw a branch off existing track in the Editor to make one.'}
      </Empty>
    );
  }
  return (
    <>
      {items.map(({ id, name, e }) => (
        <Row
          key={id}
          label={name || id}
          meta={
            kind === 'block'
              ? e.occupied_by
                ? `Occupied by ${e.occupied_by}`
                : undefined
              : e.position === 1
                ? 'Diverge'
                : 'Main'
          }
          selected={sel?.kind === kind && sel.id === id}
          onClick={() => setSelection(host, { kind, id })}
        />
      ))}
    </>
  );
}

export function OperateLists({ host }: { host: OperateHost }) {
  liveVersion.value;
  if (!listOpen.value) {
    return (
      <aside class="op-left is-folded" aria-label="Lists">
        <button
          type="button"
          class="secondary-nav-btn"
          title="Show the lists"
          aria-label="Show the lists"
          onClick={() => (listOpen.value = true)}
        >
          <i class="ti ti-layout-sidebar-left-expand" aria-hidden="true" />
        </button>
      </aside>
    );
  }
  return (
    <aside class="op-left" aria-label="Lists">
      <div class="op-left-header">
        <span class="op-left-title">Lists</span>
        <button
          type="button"
          class="secondary-nav-btn"
          title="Fold the lists away"
          aria-label="Fold the lists away"
          onClick={() => (listOpen.value = false)}
        >
          <i class="ti ti-layout-sidebar-left-collapse" aria-hidden="true" />
        </button>
      </div>
      <div class="op-left-body">
        <Section id="stations" title="Stations">
          <StationsList host={host} />
        </Section>
        <Section id="blocks" title="Blocks" count={stateManager.getAll('blocks').length}>
          <EntityList host={host} kind="block" />
        </Section>
        <Section id="switches" title="Switches" count={stateManager.getAll('switches').length}>
          <EntityList host={host} kind="switch" />
        </Section>
      </div>
    </aside>
  );
}
