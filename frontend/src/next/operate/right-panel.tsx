/**
 * The Operate screen's right panel: it shows the selection (a block, a
 * switch, or one of today's panels shown as it is) and hides while
 * nothing is selected, giving the map room.
 *
 * Its pop-out button moves it into a floating window and back. That is the
 * only floating window this screen keeps (docs/ui-layout-rewrite-plan.md §12
 * decision 2). It uses FloatingWindow (lib/window-manager.ts): the window
 * adopts this panel's own element, so
 * popping out moves nothing and redraws nothing. Whether it floats, and
 * where, is remembered.
 */
import { useLayoutEffect, useRef, useState } from 'preact/hooks';

import { stateManager } from '../../lib/state-manager';
import { FloatingWindow, windowManager } from '../../lib/window-manager';
import { browserWindowPersistenceEnv, loadWindowState, saveWindowState } from '../../lib/window-persistence';
import { BlockPanel } from './block-panel';
import { selection, type OperateHost, type OperateSelection } from './operate-state';
import { setSelection, wrappedPanelElement } from './panel-host';
import { SwitchPanel } from './switch-panel';

const WINDOW_ID = 'operate-panel';

function panelTitle(sel: OperateSelection): string {
  if (!sel) return '';
  if (sel.kind === 'panel') return sel.title;
  const kind = sel.kind === 'block' ? 'blocks' : 'switches';
  const name = String((stateManager.get(kind, sel.id) as any)?.name || '').trim();
  return `${sel.kind === 'block' ? 'Block' : 'Switch'} ${name || sel.id}`;
}

export function RightPanel({ host }: { host: OperateHost }) {
  const sel = selection.value;
  const root = useRef<HTMLElement>(null);
  const header = useRef<HTMLDivElement>(null);
  const wrappedSlot = useRef<HTMLDivElement>(null);
  const win = useRef<FloatingWindow | null>(null);
  const [floating, setFloating] = useState(false);

  // Today's panels draw into this element and look their elements up by id
  // straight away, so it stays in the page while the screen is mounted.
  useLayoutEffect(() => {
    wrappedSlot.current?.appendChild(wrappedPanelElement());
    return () => {
      // Leaving the screen: one of today's panels makes no sense to come back to.
      if (selection.peek()?.kind === 'panel') setSelection(host, null);
      wrappedPanelElement().remove();
    };
  }, []);

  useLayoutEffect(() => {
    if (!root.current || !header.current) return;
    const env = browserWindowPersistenceEnv();
    const saved = loadWindowState(env, WINDOW_ID);
    const w = new FloatingWindow({
      id: WINDOW_ID,
      kind: 'operate-panel',
      manager: windowManager,
      title: 'Selection',
      dockable: true,
      adopt: { root: root.current, header: header.current },
      initialFloating: !!saved?.floating,
      initialGeometry: saved?.floating ? saved.geometry : undefined,
      minW: 300,
      minH: 240,
      defaultFloatPosition: ({ width }) => ({ left: Math.max(8, window.innerWidth - width - 24), top: 72 }),
      onGeometryChange: (geometry) => saveWindowState(env, WINDOW_ID, { geometry, floating: w.isFloating() }),
    });
    win.current = w;
    setFloating(w.isFloating());
    return () => {
      win.current = null;
      w.destroy();
    };
  }, []);

  const togglePopOut = () => {
    if (win.current) setFloating(win.current.toggleDock());
  };

  return (
    // The class never changes after the first draw: FloatingWindow adds its
    // own classes to this element, and a class change here would wipe them.
    <aside class="op-panel" ref={root} data-empty={sel ? 'false' : 'true'} aria-label="Selection">
      <div class="secondary-sidebar-header op-panel-header" ref={header}>
        <h3 title={panelTitle(sel)}>{panelTitle(sel)}</h3>
        <button
          type="button"
          class="secondary-nav-btn"
          title={floating ? 'Dock the panel' : 'Pop out into a window'}
          aria-label={floating ? 'Dock the panel' : 'Pop out into a window'}
          onClick={togglePopOut}
        >
          <i class={floating ? 'ti ti-layout-sidebar-right' : 'ti ti-external-link'} aria-hidden="true" />
        </button>
        <button type="button" class="close-btn" title="Close" aria-label="Close" onClick={() => setSelection(host, null)}>
          ×
        </button>
      </div>
      <div class="op-panel-body">
        {sel && sel.kind !== 'panel' && (
          <div class="secondary-sidebar-content op-native-panel">
            {sel.kind === 'block' && <BlockPanel host={host} blockId={sel.id} />}
            {sel.kind === 'switch' && <SwitchPanel host={host} switchId={sel.id} />}
          </div>
        )}
        <div class="op-wrapped-slot" ref={wrappedSlot} hidden={sel?.kind !== 'panel'} />
      </div>
    </aside>
  );
}
