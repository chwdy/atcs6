/**
 * The Operate screen's left list (docs/ui-layout-rewrite-plan.md §5): the
 * operational lists, each in its own folding section. The whole list folds
 * away to give the map room.
 *
 * Trains, blocks and switches come from the network poll. Routes, paths,
 * route runs and stations have their own endpoints, read only while their
 * section is open. A train, block or switch opens in this screen's own
 * right panel; a route, path, route run or station opens today's panel for
 * it, shown inside the right panel (panel-host.ts).
 */
import { signal } from '@preact/signals';
import { useState } from 'preact/hooks';

import type { ComponentChildren } from 'preact';

import { api } from '../../lib/api';
import { stateManager } from '../../lib/state-manager';
import { usePoll } from '../poll';
import { liveVersion, trainViews } from './live';
import { listOpen, selection, type OperateHost } from './operate-state';
import { setSelection, wrappedPanelKey } from './panel-host';
import { speedText, trainStatus, trainStatusChip } from './train-display';

/** Which sections are open; kept while the screen comes and goes. */
const openSections = signal<Record<string, boolean>>({ trains: true, routes: true });

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
    <button type="button" class={selected ? 'op-list-row is-selected' : 'op-list-row'} onClick={onClick}>
      <span class="op-list-row-text">
        <span class="op-list-row-label">{label}</span>
        {meta && <span class="op-list-row-meta">{meta}</span>}
      </span>
      {chip}
    </button>
  );
}

function Empty({ children }: { children: ComponentChildren }) {
  return <div class="op-list-empty">{children}</div>;
}

/** Read a list from its endpoint every `periodMs` while the section shows. */
function usePolledList<T>(read: () => Promise<T[]>, periodMs: number): T[] | null {
  const [items, setItems] = useState<T[] | null>(null);
  usePoll(async () => {
    try {
      setItems(await read());
    } catch {
      /* keep what was shown; the next read tries again */
    }
  }, periodMs);
  return items;
}

/** Whether today's panel shown in the right panel is this one. */
function showing(type: string, id: unknown): boolean {
  const key = wrappedPanelKey.value;
  return selection.value?.kind === 'panel' && key.type === type && key.id === String(id);
}

// A component that reads a signal re-draws only when that signal changes or
// its props do (@preact/signals), so each list of live entities reads
// `liveVersion` itself rather than relying on its parent re-drawing.
function TrainsList({ host }: { host: OperateHost }) {
  liveVersion.value;
  const trains = trainViews();
  const sel = selection.value;
  if (trains.length === 0) return <Empty>No trains yet. Place one on the map.</Empty>;
  return (
    <>
      {trains.map((v) => {
        const status = trainStatus(v);
        return (
          <Row
            key={v.id}
            label={v.train?.name || v.id}
            meta={`${speedText(v.controller?.current_speed ?? 0)} km/h`}
            chip={<span class={`chip chip-state ${trainStatusChip(status)}`}>{status.toUpperCase()}</span>}
            selected={sel?.kind === 'train' && sel.id === v.id}
            onClick={() => host.selectTrain(v.id)}
          />
        );
      })}
    </>
  );
}

function RoutesList({ host }: { host: OperateHost }) {
  const routes = usePolledList<any>(async () => (await api.getRoutes())?.routes ?? [], 5000);
  if (routes === null) return <Empty>Loading…</Empty>;
  if (routes.length === 0) return <Empty>No routes yet. Press New to make one.</Empty>;
  return (
    <>
      {routes.map((r) => (
        <Row
          key={r.route_id}
          label={r.name || r.route_id}
          meta={r.name ? r.route_id : undefined}
          selected={showing('route-editor', r.route_id)}
          onClick={() => void host.editor.routeHandler?.showRouteEditor(r.route_id)}
        />
      ))}
    </>
  );
}

function PathsList({ host }: { host: OperateHost }) {
  const paths = usePolledList<any>(() => api.getPaths(), 3000);
  if (paths === null) return <Empty>Loading…</Empty>;
  if (paths.length === 0) return <Empty>No paths. Draw one from a train's Route section.</Empty>;
  return (
    <>
      {paths.map((p) => (
        <Row
          key={p.path_id}
          label={p.path_id}
          meta={[p.train_id, p.status].filter(Boolean).join(' · ')}
          selected={showing('path', p.path_id)}
          onClick={() => void host.editor.showPathEditor(p.path_id)}
        />
      ))}
    </>
  );
}

function RouteRunsList({ host }: { host: OperateHost }) {
  const runs = usePolledList<any>(() => api.getRouteRuns(), 3000);
  if (runs === null) return <Empty>Loading…</Empty>;
  if (runs.length === 0) return <Empty>No route runs yet. Start a route to make one.</Empty>;
  return (
    <>
      {runs.map((run) => (
        <Row
          key={run.run_number}
          label={`#${run.run_number} ${run.route_id ?? ''}`.trim()}
          meta={[run.train_id, run.display_state ?? run.state].filter(Boolean).join(' · ')}
          selected={showing('route-run-editor', run.run_number)}
          onClick={() => void host.editor.routeRunHandler?.showRouteRunEditor(Number(run.run_number))}
        />
      ))}
    </>
  );
}

function StationsList({ host }: { host: OperateHost }) {
  const stations = usePolledList<any>(async () => (await api.listStationGroups())?.stations ?? [], 10000);
  if (stations === null) return <Empty>Loading…</Empty>;
  if (stations.length === 0) {
    return <Empty>No stations on this layout. Draw platforms in the Editor, then group them into a station.</Empty>;
  }
  return (
    <>
      {stations.map((s) => {
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
      })}
    </>
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
        <Section id="trains" title="Trains" count={stateManager.getAll('trains').length}>
          <TrainsList host={host} />
        </Section>
        <Section
          id="routes"
          title="Routes"
          action={{ label: 'New', title: 'Make a new route', run: () => void host.editor.routeHandler?.createNewRoute() }}
        >
          <RoutesList host={host} />
        </Section>
        <Section id="paths" title="Paths">
          <PathsList host={host} />
        </Section>
        <Section id="runs" title="Route runs">
          <RouteRunsList host={host} />
        </Section>
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
