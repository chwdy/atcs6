/**
 * The Operate screen's left list (docs/ui-layout-rewrite-plan.md §5): the
 * operational lists, each in its own folding section. The whole list folds
 * away to give the map room.
 *
 * Blocks and switches come from the network poll. A block or switch opens in
 * this screen's own right panel. The trains are on the cards row and the map.
 */
import { signal } from '@preact/signals';

import type { ComponentChildren } from 'preact';

import { stateManager } from '../../lib/state-manager';
import { liveVersion } from './live';
import { listOpen, selection, type OperateHost } from './operate-state';
import { setSelection } from './panel-host';

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
