/**
 * Lets the Operate screen's right panel take over from today's inspector.
 *
 * While the Operate screen shows, the Editor hands it (through
 * `Editor.nextPanelHost`) what would have gone to today's secondary sidebar:
 *
 * - a click on the map: a train under it opens that train, otherwise the
 *   switch or the block the tile belongs to. The tile itself is an Editor
 *   matter (plan §5).
 * - a request for a train's controls, which this screen draws itself.
 * - any other panel's HTML (a route, a path, a route run, a station, Add
 *   Train, …). Those are not rebuilt yet, so their HTML is shown as it is,
 *   inside the right panel. They look their own elements up by id straight
 *   after they are drawn, so the element they are drawn into is kept in the
 *   page for as long as the Operate screen is mounted.
 */
import { signal } from '@preact/signals';

import { applyStructuralLock } from '../../lib/helpers';
import { stateManager } from '../../lib/state-manager';
import type { NextPanelHost } from '../../editor/editor';
import { screen } from '../state';
import { trainsOnTile } from './live';
import { selection, type OperateHost, type OperateSelection } from './operate-state';

/** Today's panels that edit layout structure. In Operate they show
 *  read-only; the same list as Editor.reapplyStructuralLock. */
const STRUCTURAL = new Set([
  'segment',
  'switch',
  'signal',
  'tunnel',
  'platform',
  'station-group-list',
  'station-group-detail',
  'block-list',
  'block-detail',
]);

let wrappedEl: HTMLDivElement | null = null;
/** The editor type and id of the panel shown as it is, as its handler set
 *  them on the Editor before drawing it. The left list marks its row. */
export const wrappedPanelKey = signal<{ type: string | null; id: string | null }>({ type: null, id: null });

/** The element today's panels are drawn into while they sit in the right
 *  panel. It lives outside Preact: those panels write its HTML themselves. */
export function wrappedPanelElement(): HTMLDivElement {
  if (!wrappedEl) {
    wrappedEl = document.createElement('div');
    wrappedEl.className = 'op-wrapped-panel secondary-sidebar-content';
  }
  return wrappedEl;
}

/** Stop the timers and subscriptions of the panel shown as it is, and empty
 *  it, the way today's inspector does when one of its tabs closes. */
function closeWrappedPanel(host: OperateHost): void {
  host.editor.uiHandler.homeTabGroup?.cleanupPanelSubscriptions();
  host.editor._currentEditorType = null;
  host.editor._currentEditorId = null;
  host.editor.renderer?.clearBlockDirectionOverlay?.();
  wrappedPanelKey.value = { type: null, id: null };
  if (wrappedEl) wrappedEl.innerHTML = '';
}

/** Show `next` in the right panel. Leaving a train lets App drop it as the
 *  selected train (and stop fetching its look-ahead); leaving one of today's
 *  panels stops its timers. */
export function setSelection(host: OperateHost, next: OperateSelection): void {
  const prev = selection.peek();
  if (prev?.kind === 'panel' && next?.kind !== 'panel') closeWrappedPanel(host);
  if (prev?.kind === 'train' && next?.kind !== 'train') host.deselectTrain();
  selection.value = next;
}

/** The Editor hook (see the file comment). Every method does nothing and
 *  returns false unless the Operate screen is the one showing. */
export function operatePanelHost(host: OperateHost): NextPanelHost {
  const active = () => screen.peek() === 'operate';
  return {
    selectTile(tile) {
      if (!active()) return false;
      const trains = trainsOnTile(tile.x, tile.y);
      if (trains.length > 0) host.selectTrain(trains[0]);
      else if (tile.switch_id) setSelection(host, { kind: 'switch', id: String(tile.switch_id) });
      else if (tile.block_section_id) setSelection(host, { kind: 'block', id: String(tile.block_section_id) });
      else setSelection(host, null);
      return true;
    },
    showTrain(trainId) {
      if (!active()) return false;
      // An unidentified train offers no driving controls, only adoption
      // (design_new.md "Unidentified trains"); today's panel for it is shown
      // as it is, through showPanel.
      if (stateManager.get('trains', trainId)?.is_unidentified) return false;
      setSelection(host, { kind: 'train', id: String(trainId) });
      return true;
    },
    showPanel(title, content) {
      const el = wrappedEl;
      if (!active() || !el?.isConnected) return false;
      // Today's inspector restoring its own saved tabs (at boot) keeps them.
      if (host.editor._activeTabGroupForRestore) return false;
      el.innerHTML = content;
      el.classList.remove('sim-locked');
      const id = host.editor._currentEditorId;
      wrappedPanelKey.value = { type: host.editor._currentEditorType, id: id == null ? null : String(id) };
      if (host.editor.editorMode === 'sim' && STRUCTURAL.has(host.editor._currentEditorType ?? '')) {
        applyStructuralLock(el);
      }
      setSelection(host, { kind: 'panel', title });
      return true;
    },
    hidePanel() {
      if (!active()) return false;
      if (selection.peek()?.kind === 'panel') setSelection(host, null);
      return true;
    },
    closePanel(editorType, editorId) {
      if (!active() || selection.peek()?.kind !== 'panel') return;
      const key = wrappedPanelKey.peek();
      if (key.type === editorType && key.id === (editorId == null ? null : String(editorId))) setSelection(host, null);
    },
  };
}
