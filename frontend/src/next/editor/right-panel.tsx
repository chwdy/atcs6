/**
 * The Editor screen's right panel: it edits the selection.
 *
 * One docked panel. Its header holds the selection's title, a list button
 * (all switches, all blocks — they used to be tool-rail
 * buttons), the pop-out button that moves the panel into a floating window
 * and back (§12 decision 2; the window chrome is lib/window-manager.ts), and
 * close. Its body hosts today's entity panels (legacy-panel.ts) while the
 * Editor screen shows; with nothing selected it says how to select.
 */
import { useEffect, useRef } from 'preact/hooks';

import { FloatingWindow, windowManager } from '../../lib/window-manager';
import { placeClearOfLayout } from '../../lib/window-placement';
import {
  browserWindowPersistenceEnv,
  loadWindowState,
  saveWindowState,
  type WindowPersistenceEnv,
} from '../../lib/window-persistence';
import { MenuButton } from '../menu';
import { screen } from '../state';
import { adoptLegacyPanel, watchLegacyPanelOpen, watchTileBits } from './legacy-panel';
import { panelFloating, panelOpen } from './state';

/** The window id the pop-out's position and size are saved under. */
export const PANEL_WINDOW_ID = 'next-editor-panel';

export interface PanelActions {
  /** Clear the selection (close today's panel). */
  close(): void;
  showSwitches(): void;
  showBlocks(): void;
  /** The open tile panel's "x,y", or null when another panel is open. */
  openTileKey(): string | null;
}

export function EditorPanel({
  actions,
  persistence = browserWindowPersistenceEnv(),
}: {
  actions: PanelActions;
  persistence?: WindowPersistenceEnv;
}) {
  const root = useRef<HTMLElement>(null);
  const header = useRef<HTMLDivElement>(null);
  const titleSlot = useRef<HTMLDivElement>(null);
  const bodySlot = useRef<HTMLDivElement>(null);
  const win = useRef<FloatingWindow | null>(null);
  const onEditor = screen.value === 'editor';
  const open = panelOpen.value;
  const floating = panelFloating.value;

  // The pop-out chrome: drag, resize, and docking back. Created once; the
  // panel stays mounted on every screen and CSS hides it off the Editor screen.
  useEffect(() => {
    const saved = loadWindowState(persistence, PANEL_WINDOW_ID);
    const w = new FloatingWindow({
      id: PANEL_WINDOW_ID,
      kind: PANEL_WINDOW_ID,
      manager: windowManager,
      title: 'Selection',
      dockable: true,
      adopt: { root: root.current!, header: header.current! },
      minW: 280,
      minH: 200,
      initialFloating: saved?.floating ?? false,
      initialGeometry: saved?.floating ? saved.geometry : undefined,
      defaultFloatPosition: (size) => placeClearOfLayout(size),
      onGeometryChange: (geometry) =>
        saveWindowState(persistence, PANEL_WINDOW_ID, { geometry, floating: w.isFloating() }),
    });
    win.current = w;
    panelFloating.value = w.isFloating();
    return () => {
      w.destroy();
      win.current = null;
    };
  }, []);

  // Host today's panels only while the Editor screen shows, so another
  // screen can take the same nodes.
  useEffect(() => {
    if (!onEditor) return;
    const release = adoptLegacyPanel({ header: titleSlot.current!, body: bodySlot.current! });
    const stopOpen = watchLegacyPanelOpen((isOpen) => {
      panelOpen.value = isOpen;
    });
    const content = bodySlot.current!.querySelector<HTMLElement>('#secondary-sidebar-content');
    const stopBits = content ? watchTileBits(content, actions.openTileKey) : () => {};
    return () => {
      stopBits();
      stopOpen();
      release();
    };
  }, [onEditor]);

  const togglePopOut = () => {
    const w = win.current;
    if (!w) return;
    const nowFloating = w.toggleDock();
    // Docked, the layout sets the width; drop the size the window had.
    if (!nowFloating) w.getRootElement().style.width = '';
    panelFloating.value = nowFloating;
  };

  return (
    <aside class="ned-panel" ref={root} aria-label="Selection">
      <div class="ned-panel-header" ref={header}>
        {/* Today's back link and title land here (legacy-panel.ts). */}
        <div class="ned-panel-title" ref={titleSlot} hidden={!open} />
        {!open && <h3 class="ned-panel-title-empty">Selection</h3>}
        <MenuButton
          icon="ti ti-list"
          title="Lists: switches, blocks"
          align="right"
          sections={() => [
            {
              items: [
                { label: 'Switches', icon: 'ti ti-arrows-split-2', onSelect: actions.showSwitches },
                { label: 'Blocks', icon: 'ti ti-stack-2', onSelect: actions.showBlocks },
              ],
            },
          ]}
        />
        <button
          type="button"
          class="btn btn-icon-only"
          title={floating ? 'Dock the panel' : 'Pop out into a window'}
          aria-label={floating ? 'Dock the panel' : 'Pop out into a window'}
          onClick={togglePopOut}
        >
          <i class={floating ? 'ti ti-layout-sidebar-right' : 'ti ti-external-link'} aria-hidden="true" />
        </button>
        {open && (
          <button type="button" class="btn btn-icon-only" title="Close" aria-label="Close" onClick={actions.close}>
            <i class="ti ti-x" aria-hidden="true" />
          </button>
        )}
      </div>
      <div class="ned-panel-body">
        {!open && (
          <div class="empty-state ned-panel-empty">
            Nothing selected. Click a piece of track to edit its segment, or right-click it for everything on that tile.
          </div>
        )}
        {/* Today's panel body lands here (legacy-panel.ts). */}
        <div class="ned-panel-legacy" ref={bodySlot} hidden={!open} />
      </div>
    </aside>
  );
}
