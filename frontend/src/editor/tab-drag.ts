// tab-drag.ts — pick up a tab and drag it out: drop it onto another open
// window's tab strip (moves it there) or onto empty space (spawns a new
// window at the drop point). The deferred half of the split feature — the
// button/context-menu trigger (editor-ui.ts's splitTabIntoNewWindow /
// moveTabToGroup) shipped first; this is the "grab and drag" trigger, and it
// routes through those exact same primitives — there is no parallel
// implementation to keep in sync, this file only supplies the gesture.
//
// Pointer-event based, matching every other drag interaction in this
// codebase (FloatingWindow's own drag/resize, the box-select tool) — never
// HTML5 draggable/dragstart.

import type { TabGroup } from './tab-group';

export interface Rect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

/** Pure — whether (x, y) falls within rect. */
export function pointInRect(x: number, y: number, rect: Rect): boolean {
  return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
}

/** Pure — the first candidate whose rect contains the point, or null. Order
 *  candidates topmost-first when rects can overlap. */
export function resolveDropTarget<T>(x: number, y: number, candidates: Array<{ rect: Rect; value: T }>): T | null {
  for (const c of candidates) {
    if (pointInRect(x, y, c.rect)) {
      return c.value;
    }
  }
  return null;
}

/** Pure — has the pointer moved far enough from its down-point to count as a
 *  drag rather than a click? */
export function exceedsDragThreshold(dx: number, dy: number, threshold: number): boolean {
  return Math.hypot(dx, dy) >= threshold;
}

export interface TabDragOptions {
  /** Minimum pointer travel (px) before a pointerdown-on-a-tab becomes a
   *  drag — below this it's an ordinary click, left for the tab strip's own
   *  click handler (wireTabGroupInteractions) to activate the tab. */
  dragThreshold?: number;
  /** The group this specific tab strip belongs to. */
  group: TabGroup;
  /** Every currently open tab-strip drop candidate (home + every spawned
   *  window) — recomputed on each drag since windows can open/close while
   *  the app runs. Supplied by EditorUIHandler, the only place that knows
   *  the FloatingWindow-to-TabGroup mapping. */
  listDropTargets(): Array<{ group: TabGroup; tabsEl: HTMLElement }>;
  /** Released over a different group's tab strip. */
  onDropOnGroup(tabId: string, sourceGroup: TabGroup, targetGroup: TabGroup): void;
  /** Released outside every open window's tab strip. */
  onDropOutside(tabId: string, sourceGroup: TabGroup, dropPoint: { x: number; y: number }): void;
}

/** Wire the drag-out gesture onto one tab strip. Call once per TabGroup's
 *  tabsEl (home and every spawned window), alongside wireTabGroupInteractions
 *  for that same strip. Returns an unsubscribe/teardown function. */
export function wireTabDrag(tabsEl: HTMLElement, opts: TabDragOptions): () => void {
  const threshold = opts.dragThreshold ?? 6;

  let start: { x: number; y: number; tabId: string; pointerId: number } | null = null;
  let dragging = false;
  let ghostEl: HTMLElement | null = null;
  let highlightedEl: HTMLElement | null = null;

  const currentCandidates = (): Array<{ rect: Rect; value: { group: TabGroup; tabsEl: HTMLElement } }> =>
    opts.listDropTargets().map((c) => ({ rect: c.tabsEl.getBoundingClientRect(), value: c }));

  const clearHighlight = (): void => {
    highlightedEl?.classList.remove('secondary-sidebar-tabs--drop-target');
    highlightedEl = null;
  };

  const removeGhost = (): void => {
    ghostEl?.remove();
    ghostEl = null;
  };

  const reset = (): void => {
    start = null;
    dragging = false;
    clearHighlight();
    removeGhost();
  };

  const onPointerDown = (e: PointerEvent): void => {
    const tabEl = (e.target as HTMLElement).closest('.secondary-tab[data-tab-id]') as HTMLElement | null;
    if (!tabEl?.dataset.tabId) {
      return;
    }
    // The tab's own pin/pop-out/close buttons handle their own clicks.
    if ((e.target as HTMLElement).closest('button')) {
      return;
    }
    start = { x: e.clientX, y: e.clientY, tabId: tabEl.dataset.tabId, pointerId: e.pointerId };
  };

  const onPointerMove = (e: PointerEvent): void => {
    if (!start || e.pointerId !== start.pointerId) {
      return;
    }

    if (!dragging) {
      if (!exceedsDragThreshold(e.clientX - start.x, e.clientY - start.y, threshold)) {
        return;
      }
      dragging = true;
      tabsEl.setPointerCapture(e.pointerId);
      const sourceTab = opts.group.tabs.find((t) => t.tabId === start!.tabId);
      ghostEl = document.createElement('div');
      ghostEl.className = 'secondary-tab-drag-ghost';
      ghostEl.textContent = sourceTab?.title || 'Tab';
      document.body.appendChild(ghostEl);
    }

    if (ghostEl) {
      ghostEl.style.left = `${e.clientX + 12}px`;
      ghostEl.style.top = `${e.clientY + 12}px`;
    }

    const target = resolveDropTarget(e.clientX, e.clientY, currentCandidates());
    clearHighlight();
    if (target && target.group !== opts.group) {
      target.tabsEl.classList.add('secondary-sidebar-tabs--drop-target');
      highlightedEl = target.tabsEl;
    }
  };

  const onPointerUp = (e: PointerEvent): void => {
    if (!start || e.pointerId !== start.pointerId) {
      return;
    }
    if (!dragging) {
      start = null;
      return; // a plain click — the strip's own click handler activates the tab
    }

    const target = resolveDropTarget(e.clientX, e.clientY, currentCandidates());
    const { tabId } = start;
    reset();

    if (target && target.group !== opts.group) {
      opts.onDropOnGroup(tabId, opts.group, target.group);
    } else if (!target) {
      opts.onDropOutside(tabId, opts.group, { x: e.clientX, y: e.clientY });
    }
    // Dropped back on the strip it came from: no-op, the tab stays put.
  };

  const onKeyDown = (e: KeyboardEvent): void => {
    if (e.key === 'Escape' && dragging) {
      reset();
    }
  };

  tabsEl.addEventListener('pointerdown', onPointerDown);
  tabsEl.addEventListener('pointermove', onPointerMove);
  tabsEl.addEventListener('pointerup', onPointerUp);
  tabsEl.addEventListener('pointercancel', reset);
  document.addEventListener('keydown', onKeyDown);

  return () => {
    tabsEl.removeEventListener('pointerdown', onPointerDown);
    tabsEl.removeEventListener('pointermove', onPointerMove);
    tabsEl.removeEventListener('pointerup', onPointerUp);
    tabsEl.removeEventListener('pointercancel', reset);
    document.removeEventListener('keydown', onKeyDown);
    reset();
  };
}
