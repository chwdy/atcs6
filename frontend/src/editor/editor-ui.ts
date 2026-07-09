// editor-ui.ts — Editor UI Handler (secondary sidebar, JSON viewer, bit/visual
// track editors).
//
// The tab/navigator working-set logic (preview/pinned tabs, the type→instance
// navigator, stale-tab pruning) lives in TabGroup (tab-group.ts) —
// instantiable, so more than one can exist once a tab is split into its own
// window (a later phase). The window chrome (dock/float toggle, drag,
// 8-direction resize, double-click-to-fit) lives in FloatingWindow
// (lib/window-manager.ts). This file wires the home sidebar's one TabGroup +
// FloatingWindow to the static markup, and is the stable facade `Editor`
// composes against — every method here keeps its original name and signature
// so none of Editor's ~26 `showSecondarySidebar` callers (or its other
// `uiHandler.*` calls) needed to change for this split.
//
// See DESIGN_LANGUAGE.md §12 "Editor navigation".

import { showToast } from '../lib/toast';
import type { EditorHost } from './types';
import { showConfirmModal } from '../lib/helpers';
import { FORBIDDEN_TRACK_BITS, isSquareCornerSwitch } from '../lib/constants';
import { TabGroup } from './tab-group';
import { FloatingWindow, windowManager, type WindowGeometry } from '../lib/window-manager';
import { showContextMenu, type ContextMenuSection } from '../lib/context-menu';
import { wireTabDrag } from './tab-drag';
import {
  browserWindowPersistenceEnv,
  loadRegistry,
  loadWindowState,
  saveWindowState,
  ensureRegistered,
  unregister,
  type WindowPersistenceEnv,
} from '../lib/window-persistence';

export class EditorUIHandler {
  editor: EditorHost;
  homeTabGroup!: TabGroup;
  homeWindow!: FloatingWindow;
  sidebarSectionStorageKey: string;

  private persistenceEnv: WindowPersistenceEnv = browserWindowPersistenceEnv();

  /** Windows spawned by splitting a tab out of some group — floating-only,
   *  each a full independent TabGroup + FloatingWindow. Keyed by groupId. */
  private spawnedGroups = new Map<string, { window: FloatingWindow; tabGroup: TabGroup }>();
  private spawnCounter = 0;

  /** The group whose window was most recently focused (any pointerdown
   *  inside it, including on an embedded action button — FloatingWindow
   *  bumps z-order on every pointerdown, not just header drags). Content
   *  actions embedded in a panel (an "Edit" button in a list row) call a
   *  generic editor.showXEditor() with no idea which window they live in —
   *  this is showSecondarySidebar's fallback so that action lands back in
   *  the window the user was actually looking at, not always home. */
  private lastFocusedTabGroup: TabGroup | null = null;

  constructor(editor: EditorHost) {
    this.editor = editor;
    this.sidebarSectionStorageKey = 'sidebarSectionStatesV1';
  }

  get selectedTool(): any {
    return this.editor.selectedTool;
  }

  setupSecondarySidebar(): void {
    const sidebar = this.editor.$id('secondary-sidebar');
    const header = sidebar?.querySelector('.secondary-sidebar-header') as HTMLElement | null;
    if (!sidebar || !header) {
      return;
    }

    this.homeTabGroup = new TabGroup({
      groupId: 'home',
      editor: this.editor,
      dom: {
        titleEl: () => this.editor.$id('secondary-sidebar-title'),
        contentEl: () => this.editor.$id('secondary-sidebar-content'),
        tabsEl: () => this.editor.$id('secondary-sidebar-tabs'),
        parentBtnEl: () => this.editor.$id('secondary-nav-parent') as HTMLButtonElement | null,
        pinBtnEl: () => this.editor.$id('secondary-nav-pin'),
        closeBtnEl: () => this.editor.$id('close-secondary-sidebar'),
        containerEl: () => this.editor.$id('secondary-sidebar'),
      },
      onPersist: () => this.persistSecondaryInspectorState(),
    });

    // The home sidebar is the one dockable window — it adopts the static
    // markup (index.html) instead of FloatingWindow creating fresh DOM, and
    // its header keeps its own richer button set (parent-link, pin, tearoff,
    // close) instead of FloatingWindow's generic title + close.
    this.homeWindow = new FloatingWindow({
      id: 'home',
      kind: 'home',
      manager: windowManager,
      title: 'Editor',
      dockable: true,
      adopt: { root: sidebar, header },
      minW: 260,
      minH: 180,
      // Clear of the floating tool capsule on the left (right edge ~152).
      defaultFloatPosition: { left: 168, top: 84 },
      onGeometryChange: () => this.persistSecondaryInspectorState(),
    });

    windowManager.subscribe(() => this.updateLastFocusedTabGroup());
    this.lastFocusedTabGroup = this.homeTabGroup;

    const closeBtn = this.editor.$id('close-secondary-sidebar');
    if (closeBtn) {
      closeBtn.addEventListener('click', () => this.closeActiveSecondaryTab());
    }

    const parentBtn = this.editor.$id('secondary-nav-parent');
    if (parentBtn) {
      parentBtn.addEventListener('click', async () => {
        await this.navigateToParentList();
      });
    }

    const pinBtn = this.editor.$id('secondary-nav-pin');
    if (pinBtn) {
      pinBtn.addEventListener('click', () => {
        this.homeTabGroup.setPinned(!this.homeTabGroup.isPinned());
      });
    }

    const tearoffBtn = this.editor.$id('secondary-nav-tearoff');
    if (tearoffBtn) {
      tearoffBtn.addEventListener('click', () => this.toggleInspectorFloat());
    }

    const tabsEl: HTMLElement | null = this.editor.$id('secondary-sidebar-tabs');
    if (tabsEl) {
      this.wireTabGroupInteractions(this.homeTabGroup, tabsEl);
      wireTabDrag(tabsEl, {
        group: this.homeTabGroup,
        listDropTargets: () => this.listTabDropTargets(),
        onDropOnGroup: (tabId, source, target) => {
          void this.moveTabToGroup(source, tabId, target);
        },
        onDropOutside: (tabId, source, point) => {
          void this.splitTabIntoNewWindow(source, tabId, point);
        },
      });
    }

    this.homeTabGroup.updateNavigationState();
    this.restorePersistedState();
    this.persistWindowRegistry();
    setTimeout(async () => {
      // restoreActivePanel prunes tabs whose entity is absent from the loaded
      // scene (persisted tabs are global, not per-scene). Pruning can empty a
      // group, and unlike the close/move paths it doesn't itself hide/destroy
      // the window — so an all-stale group would restore as an empty frame.
      // Enforce the "no empty group stays visible" rule here. Snapshot the
      // spawned map first: closeGroupWindowIfEmpty deletes from it when it
      // destroys a window, which would mutate the Map mid-iteration.
      await this.homeTabGroup.restoreActivePanel();
      this.closeGroupWindowIfEmpty(this.homeTabGroup);
      for (const { tabGroup } of [...this.spawnedGroups.values()]) {
        await tabGroup.restoreActivePanel();
        this.closeGroupWindowIfEmpty(tabGroup);
      }
    }, 0);
  }

  // ---- Tab-strip interactions (shared by the home group and every spawned
  // window's group — a spawned window's tab strip behaves identically) ----

  private wireTabGroupInteractions(group: TabGroup, tabsEl: HTMLElement): void {
    tabsEl.addEventListener('click', async (event) => {
      const target = event.target as HTMLElement;

      const popOutBtn = target.closest('.secondary-tab-popout[data-tab-id]') as HTMLElement | null;
      if (popOutBtn) {
        event.stopPropagation();
        const tabId = popOutBtn.dataset.tabId;
        if (tabId) await this.splitTabIntoNewWindow(group, tabId);
        return;
      }

      const closeTabBtn = target.closest('.secondary-tab-close[data-tab-id]') as HTMLElement | null;
      if (closeTabBtn) {
        event.stopPropagation();
        const tabId = closeTabBtn.dataset.tabId;
        if (tabId) await this.closeTabInGroup(group, tabId);
        return;
      }

      const pinTabBtn = target.closest('.secondary-tab-pin[data-tab-id]') as HTMLElement | null;
      if (pinTabBtn) {
        event.stopPropagation();
        const tabId = pinTabBtn.dataset.tabId;
        if (tabId) group.pinTab(tabId);
        return;
      }

      const labelBtn = target.closest('.secondary-tab-label[data-tab-id]') as HTMLElement | null;
      if (labelBtn) {
        const tabId = labelBtn.dataset.tabId;
        if (tabId) await group.activateTab(tabId);
      }
    });

    // Double-click a preview tab to keep it (pin).
    tabsEl.addEventListener('dblclick', (event) => {
      const tabEl = (event.target as HTMLElement).closest('.secondary-tab[data-tab-id]') as HTMLElement | null;
      if (tabEl?.dataset.tabId) {
        group.pinTab(tabEl.dataset.tabId);
      }
    });

    // Right-click a tab: "Open in new window", plus "Move to <window>" for
    // every other window currently open.
    tabsEl.addEventListener('contextmenu', (event) => {
      const tabEl = (event.target as HTMLElement).closest('.secondary-tab[data-tab-id]') as HTMLElement | null;
      if (!tabEl?.dataset.tabId) return;
      event.preventDefault();
      this.showTabContextMenu(group, tabEl.dataset.tabId, event as MouseEvent);
    });
  }

  private showTabContextMenu(group: TabGroup, tabId: string, event: MouseEvent): void {
    const others = this.allTabGroups().filter((entry) => entry.group !== group);
    const sections: ContextMenuSection[] = [];
    // Popping a lone tab out just relocates its window (this group empties and
    // closes), so only offer it when there are other tabs to leave behind —
    // matches the tab-strip pop-out button being hidden in the same case.
    if (group.tabs.length > 1) {
      sections.push({
        items: [
          {
            label: 'Open in new window',
            icon: 'ti ti-external-link',
            onSelect: () => this.splitTabIntoNewWindow(group, tabId),
          },
        ],
      });
    }
    if (others.length > 0) {
      sections.push({
        heading: 'Move to',
        items: others.map((entry) => ({
          label: entry.title,
          icon: 'ti ti-arrow-right',
          onSelect: () => this.moveTabToGroup(group, tabId, entry.group),
        })),
      });
    }
    if (sections.length === 0) {
      return;
    }
    showContextMenu(event.clientX, event.clientY, sections);
  }

  /** Every open tab group — the home one plus every spawned window's —
   *  labeled by its current panel's title. Used to enumerate "Move to"
   *  targets and to look up which spawned window owns a group. */
  private allTabGroups(): Array<{ id: string; title: string; group: TabGroup }> {
    const list = [{ id: 'home', title: 'Editor', group: this.homeTabGroup }];
    for (const [id, entry] of this.spawnedGroups) {
      list.push({ id, title: entry.tabGroup.currentPanel?.title || 'Window', group: entry.tabGroup });
    }
    return list;
  }

  /** Every currently open tab strip, as a cross-window tab drag
   *  (editor/tab-drag.ts) drop candidate — home plus every spawned window's,
   *  ordered topmost-first (resolveDropTarget's documented contract) so that
   *  when two windows visually overlap, the drop resolves to whichever one
   *  the user can actually see/click, not an arbitrary fixed order.
   *  Recomputed on each drag rather than cached, since windows can open/
   *  close while one is in flight. */
  private listTabDropTargets(): Array<{ group: TabGroup; tabsEl: HTMLElement }> {
    const list: Array<{ group: TabGroup; tabsEl: HTMLElement }> = [];
    const openWindows = windowManager.all(); // back-to-front; last = topmost
    for (let i = openWindows.length - 1; i >= 0; i--) {
      const win = openWindows[i];
      const group = win.id === 'home' ? this.homeTabGroup : this.spawnedGroups.get(win.id)?.tabGroup;
      if (!group) {
        continue; // a non-tab-group window (diagnostics) isn't a drop target
      }
      const tabsEl = win.getRootElement().querySelector<HTMLElement>('.secondary-sidebar-tabs');
      if (tabsEl) {
        list.push({ group, tabsEl });
      }
    }
    return list;
  }

  /** Re-derive lastFocusedTabGroup from WindowManager's current z-order
   *  (topmost = last). Called on every WindowManager change (open/close/
   *  focus) — cheap, and simpler than threading a "which group is this"
   *  callback through FloatingWindowOptions for every window kind. */
  private updateLastFocusedTabGroup(): void {
    const open = windowManager.all();
    const top = open[open.length - 1];
    if (!top) {
      return;
    }
    if (top.id === 'home') {
      this.lastFocusedTabGroup = this.homeTabGroup;
      return;
    }
    const entry = this.spawnedGroups.get(top.id);
    if (entry) {
      this.lastFocusedTabGroup = entry.tabGroup;
    }
    // A non-tab-group window (diagnostics) being topmost doesn't change
    // which *tab group* content actions should land in — leave it as-is.
  }

  /** Close one tab in a group; if that group is now empty, hide (home) or
   *  destroy (spawned) its window. */
  private async closeTabInGroup(group: TabGroup, tabId: string): Promise<void> {
    await group.closeTab(tabId);
    this.closeGroupWindowIfEmpty(group);
  }

  private closeGroupWindowIfEmpty(group: TabGroup): void {
    if (group.tabs.length > 0) {
      return;
    }
    if (group === this.homeTabGroup) {
      void this.hideSecondarySidebar();
      return;
    }
    for (const [id, entry] of this.spawnedGroups) {
      if (entry.tabGroup === group) {
        entry.tabGroup.cleanupPanelSubscriptions();
        entry.window.destroy();
        this.spawnedGroups.delete(id);
        unregister(this.persistenceEnv, id);
        return;
      }
    }
  }

  /** Split a tab out of its current group into a brand-new floating window
   *  with its own full tab strip/navigator — the button/context-menu "Open
   *  in new window" action, and where a drag-out (editor/tab-drag.ts) lands
   *  when dropped outside every open window. `atPoint` positions the new
   *  window under the drop point (drag-out); omitted, it cascades from a
   *  default corner (button/menu trigger, which has no drop point of its
   *  own). The new window is floating-only (there is only one dockable
   *  slot, owned by the home window) and reuses the same secondary-sidebar-*
   *  CSS as the home sidebar, so no new tab-strip visuals are needed. */
  async splitTabIntoNewWindow(
    sourceGroup: TabGroup,
    tabId: string,
    atPoint?: { x: number; y: number }
  ): Promise<void> {
    const tab = await sourceGroup.extractTab(tabId);
    if (!tab) {
      return;
    }

    const groupId = `tabgroup-${++this.spawnCounter}`;
    const geometry = atPoint
      ? { left: Math.max(8, atPoint.x - 60), top: Math.max(8, atPoint.y - 12) }
      : // Cascade so repeated splits don't stack exactly on top of each
        // other; clamped back on screen by FloatingWindow's own reclamp.
        { left: 200 + 28 * (this.spawnCounter % 8), top: 120 + 28 * (this.spawnCounter % 8) };
    const { tabGroup } = this.createSpawnedWindow(groupId, geometry);

    tabGroup.acceptMovedTab(tab);
    tabGroup.updateNavigationState();
    void tabGroup.restoreTabPanel(tab);

    this.closeGroupWindowIfEmpty(sourceGroup);
    this.persistWindowRegistry();
  }

  /** Move a tab from one already-open group to another — the context-menu
   *  "Move to <window>" action once more than one window is open. */
  async moveTabToGroup(sourceGroup: TabGroup, tabId: string, targetGroup: TabGroup): Promise<void> {
    if (sourceGroup === targetGroup) {
      return;
    }
    const tab = await sourceGroup.extractTab(tabId);
    if (!tab) {
      return;
    }
    targetGroup.acceptMovedTab(tab);
    await targetGroup.restoreTabPanel(tab);
    this.closeGroupWindowIfEmpty(sourceGroup);
  }

  /** Build a spawned window's DOM + TabGroup + FloatingWindow, wire its
   *  chrome interactions, and register it — shared by splitTabIntoNewWindow
   *  (seeds the new TabGroup with one moved-in tab) and
   *  restorePersistedWindows (seeds it by replaying a saved tab list on
   *  boot): the chrome construction is identical either way, they differ
   *  only in how the group's initial tabs get there. */
  private createSpawnedWindow(
    groupId: string,
    initialGeometry: Partial<WindowGeometry>
  ): { tabGroup: TabGroup; window: FloatingWindow } {
    const dom = this.buildSpawnedWindowDom();

    const tabGroup = new TabGroup({
      groupId,
      editor: this.editor,
      dom: {
        titleEl: () => dom.titleEl,
        contentEl: () => dom.contentEl,
        tabsEl: () => dom.tabsEl,
        parentBtnEl: () => dom.parentBtnEl,
        pinBtnEl: () => dom.pinBtnEl,
        closeBtnEl: () => dom.closeBtnEl,
        containerEl: () => dom.root,
      },
      onPersist: () => this.persistSpawnedWindow(groupId),
    });
    this.wireTabGroupInteractions(tabGroup, dom.tabsEl);
    wireTabDrag(dom.tabsEl, {
      group: tabGroup,
      listDropTargets: () => this.listTabDropTargets(),
      onDropOnGroup: (draggedTabId, source, target) => {
        void this.moveTabToGroup(source, draggedTabId, target);
      },
      onDropOutside: (draggedTabId, source, point) => {
        void this.splitTabIntoNewWindow(source, draggedTabId, point);
      },
    });
    dom.parentBtnEl.addEventListener('click', async () => {
      await tabGroup.navigateToParentList();
    });
    dom.pinBtnEl.addEventListener('click', () => {
      tabGroup.setPinned(!tabGroup.isPinned());
    });
    dom.closeBtnEl.addEventListener('click', async () => {
      if (tabGroup.activeTabId) {
        await this.closeTabInGroup(tabGroup, tabGroup.activeTabId);
      } else {
        this.closeGroupWindowIfEmpty(tabGroup);
      }
    });

    const win = new FloatingWindow({
      id: groupId,
      kind: 'tab-group',
      manager: windowManager,
      title: 'Editor',
      dockable: false,
      adopt: { root: dom.root, header: dom.header },
      minW: 260,
      minH: 180,
      initialGeometry,
      onCloseRequested: () => this.closeGroupWindowIfEmpty(tabGroup),
      onGeometryChange: () => this.persistSpawnedWindow(groupId),
    });

    this.spawnedGroups.set(groupId, { window: win, tabGroup });
    return { tabGroup, window: win };
  }

  /** Build the DOM for a spawned window: the same three-part shape as the
   *  home sidebar's static markup (header with parent-link/pin/close + tabs +
   *  content), generated since there's no bound on how many might exist.
   *  Reuses the exact same CSS classes, so no new styling is needed. */
  private buildSpawnedWindowDom(): {
    root: HTMLElement;
    header: HTMLElement;
    titleEl: HTMLElement;
    parentBtnEl: HTMLButtonElement;
    pinBtnEl: HTMLElement;
    closeBtnEl: HTMLElement;
    tabsEl: HTMLElement;
    contentEl: HTMLElement;
  } {
    const root = document.createElement('aside');
    root.className = 'secondary-sidebar visible';

    const header = document.createElement('div');
    header.className = 'secondary-sidebar-header';

    const parentBtnEl = document.createElement('button');
    parentBtnEl.className = 'secondary-nav-btn secondary-nav-parent';
    parentBtnEl.hidden = true;

    const titleEl = document.createElement('h3');
    titleEl.textContent = 'Editor';

    const pinBtnEl = document.createElement('button');
    pinBtnEl.className = 'secondary-nav-btn';
    pinBtnEl.title = 'Pin inspector';
    pinBtnEl.setAttribute('aria-label', 'Pin inspector');
    pinBtnEl.innerHTML = '<i class="ti ti-pin"></i>';

    const closeBtnEl = document.createElement('button');
    closeBtnEl.className = 'close-btn';
    closeBtnEl.title = 'Close';
    closeBtnEl.textContent = '×';

    header.appendChild(parentBtnEl);
    header.appendChild(titleEl);
    header.appendChild(pinBtnEl);
    header.appendChild(closeBtnEl);

    const tabsEl = document.createElement('div');
    tabsEl.className = 'secondary-sidebar-tabs';

    const contentEl = document.createElement('div');
    contentEl.className = 'secondary-sidebar-content';

    root.appendChild(header);
    root.appendChild(tabsEl);
    root.appendChild(contentEl);
    document.body.appendChild(root);

    return { root, header, titleEl, parentBtnEl, pinBtnEl, closeBtnEl, tabsEl, contentEl };
  }

  setupCollapsibleSidebarSections(): void {
    const sections = document.querySelectorAll<HTMLElement>('.collapsible-sidebar-section');

    sections.forEach((section) => {
      const toggle = section.querySelector('.sidebar-section-toggle');
      if (!toggle) return;

      toggle.addEventListener('click', (e) => {
        e.stopPropagation();
        this.toggleSidebarSection(section);
      });
    });

    this.loadSidebarSectionStates();
  }

  toggleSidebarSection(section: HTMLElement): void {
    const isCollapsed = section.classList.toggle('collapsed');
    const toggle = section.querySelector('.sidebar-section-toggle');
    if (toggle) {
      toggle.setAttribute('aria-expanded', (!isCollapsed).toString());
    }
    this.saveSidebarSectionStates();
  }

  saveSidebarSectionStates(): void {
    const states: Record<string, boolean> = {};
    const sections = document.querySelectorAll<HTMLElement>('.collapsible-sidebar-section');

    sections.forEach((section, index) => {
      const sectionName = section.dataset.section || `section-${index}`;
      states[sectionName] = section.classList.contains('collapsed');
    });

    try {
      localStorage.setItem(this.sidebarSectionStorageKey, JSON.stringify(states));
    } catch (e) {}
  }

  loadSidebarSectionStates(): void {
    try {
      const saved = localStorage.getItem(this.sidebarSectionStorageKey);
      if (!saved) return;

      const states = JSON.parse(saved);
      const sections = document.querySelectorAll<HTMLElement>('.collapsible-sidebar-section');
      sections.forEach((section, index) => {
        const sectionName = section.dataset.section || `section-${index}`;
        const shouldCollapse =
          states[sectionName] !== undefined
            ? states[sectionName]
            : section.classList.contains('collapsed');
        section.classList.toggle('collapsed', shouldCollapse);
        const toggle = section.querySelector('.sidebar-section-toggle');
        if (toggle) {
          toggle.setAttribute('aria-expanded', (!shouldCollapse).toString());
        }
      });
    } catch (e) {}
  }

  showSecondarySidebar(title: string, content: string): void {
    // A tab living in a spawned window that's mid-restore (TabGroup.
    // restoreTabPanel / navigateToParentList) marks itself as the target
    // first, so that takes priority. Otherwise this is a fresh open — most
    // are canvas-driven (inspecting a tile/entity) with no window context of
    // their own, but a panel-embedded action button (an "Edit" row inside an
    // already-open list) has no way to say which window it lives in either,
    // so falling back to the last-focused window (rather than always home)
    // means that action lands back in the window the user was looking at.
    const target: TabGroup = this.editor._activeTabGroupForRestore || this.lastFocusedTabGroup || this.homeTabGroup;
    // Reopening into the home window after it was closed while floating must
    // clear the `.floating-window--hidden` that hideSecondarySidebar set —
    // `.visible` alone can't reveal a floating window. Gated on isHidden so an
    // ordinary inspect into an already-open window doesn't re-focus it every
    // time. Spawned windows are never hidden (they're destroyed when empty),
    // so this only concerns the home window.
    if (target === this.homeTabGroup && this.homeWindow.isHidden()) {
      this.homeWindow.setHidden(false);
    }
    target.show(title, content);
  }

  /** Toggle the whole inspector between docked (left) and a floating,
   *  draggable window. It stays the same live, editable panel either way —
   *  only its position changes, so every editor and subscription keeps
   *  working. */
  toggleInspectorFloat(): void {
    const floating = this.homeWindow.toggleDock();
    this._updateTearoffButton(floating);
  }

  _updateTearoffButton(floating: boolean): void {
    const btn = this.editor.$id('secondary-nav-tearoff');
    if (!btn) {
      return;
    }
    const label = floating ? 'Dock the inspector' : 'Float the inspector';
    btn.title = label;
    btn.setAttribute('aria-label', label);
    const icon = btn.querySelector('i');
    if (icon) {
      icon.className = floating ? 'ti ti-layout-sidebar' : 'ti ti-external-link';
    }
  }

  async hideSecondarySidebar(): Promise<void> {
    const container = this.editor.$id('secondary-sidebar');
    if (container) {
      container.classList.remove('visible');
      container.classList.remove('compact');
      container.classList.remove('route-editor-wide');
      // Reset the header-toggle collapse so a reopened panel starts shown.
      container.classList.remove('user-hidden');
    }
    // `.visible` is the docked-mode show/hide lever; it does nothing once the
    // window is floating, because `.floating-window` (position: fixed;
    // display: flex) governs a floating window's visibility instead. The
    // floating lever is FloatingWindow.setHidden — call it so the frame
    // actually disappears when floating. Harmless when docked (the
    // `.floating-window--hidden` class it toggles only matches a floating
    // window), so this needs no mode check. showSecondarySidebar clears it
    // again on reopen.
    this.homeWindow.setHidden(true);

    this.homeTabGroup.cleanupPanelSubscriptions();
    this.homeTabGroup.clear();
    this.persistSecondaryInspectorState();

    this.editor._currentEditorType = null;
    this.editor._currentEditorId = null;

    // Drop the block-direction arrows when the sidebar closes.
    if (this.editor.renderer?.clearBlockDirectionOverlay) {
      this.editor.renderer.clearBlockDirectionOverlay();
    }
  }

  async navigateToParentList(): Promise<void> {
    return this.homeTabGroup.navigateToParentList();
  }

  isSecondaryPanelPinned(): boolean {
    return this.homeTabGroup.isPinned();
  }

  async closeActiveSecondaryTab(): Promise<void> {
    if (!this.homeTabGroup.activeTabId) {
      await this.hideSecondarySidebar();
      return;
    }
    await this.closeTabInGroup(this.homeTabGroup, this.homeTabGroup.activeTabId);
  }

  /** Persist the home window's own geometry/dock-state/tabs under the
   *  unified 'home' key. Does not touch the top-level registry — the home
   *  window always exists regardless of the registry's contents (see
   *  restorePersistedState), so it doesn't need to be discovered through it;
   *  only spawned windows do (persistWindowRegistry). */
  persistSecondaryInspectorState(): void {
    saveWindowState(this.persistenceEnv, 'home', {
      geometry: this.homeWindow.getGeometry(),
      floating: this.homeWindow.isFloating(),
      tabGroup: this.homeTabGroup.serialize(),
    });
  }

  /** Persist one spawned window's own geometry/tabs under its own key. */
  private persistSpawnedWindow(groupId: string): void {
    const entry = this.spawnedGroups.get(groupId);
    if (!entry) {
      return;
    }
    saveWindowState(this.persistenceEnv, groupId, {
      geometry: entry.window.getGeometry(),
      floating: true,
      tabGroup: entry.tabGroup.serialize(),
    });
  }

  /** Persist which windows currently exist — called at the handful of
   *  moments a window is created (splitTabIntoNewWindow); closing one
   *  updates the registry via closeGroupWindowIfEmpty's own unregister call
   *  instead of a full rewrite here. Not called on every tab-level change
   *  (persistSecondaryInspectorState / persistSpawnedWindow already fire for
   *  those) — the registry only needs to change when a window itself
   *  appears or disappears. */
  private persistWindowRegistry(): void {
    ensureRegistered(this.persistenceEnv, 'home', 'home');
    for (const id of this.spawnedGroups.keys()) {
      ensureRegistered(this.persistenceEnv, id, 'tab-group');
    }
  }

  /** Boot-time restore: the home window's own state, plus every spawned
   *  window the registry remembers. Diagnostics restores itself
   *  independently (panels/diagnostics.ts) — it isn't a tab group and always
   *  exists regardless of the registry, same as home. */
  private restorePersistedState(): void {
    const homeState = loadWindowState(this.persistenceEnv, 'home');
    if (homeState) {
      if (homeState.tabGroup) {
        this.homeTabGroup.restoreTabs(homeState.tabGroup);
      }
      this.homeWindow.setGeometry(homeState.geometry);
      if (homeState.floating && !this.homeWindow.isFloating()) {
        this.homeWindow.toggleDock();
      }
      if (this.homeWindow.isFloating()) {
        this._updateTearoffButton(true);
      }
    }

    const registry = loadRegistry(this.persistenceEnv);
    for (const entry of registry.windows) {
      if (entry.kind !== 'tab-group' || entry.id === 'home') {
        continue;
      }
      const state = loadWindowState(this.persistenceEnv, entry.id);
      if (!state?.tabGroup || state.tabGroup.tabs.length === 0) {
        continue; // nothing to restore — don't spawn an empty window
      }
      const { tabGroup } = this.createSpawnedWindow(entry.id, state.geometry);
      tabGroup.restoreTabs(state.tabGroup);

      const n = parseInt(entry.id.replace('tabgroup-', ''), 10);
      if (!isNaN(n) && n > this.spawnCounter) {
        this.spawnCounter = n;
      }
    }
  }

  showFullJSONViewer(data: any, title = 'JSON Viewer'): void {
    const jsonString = JSON.stringify(data, null, 2);

    const content = `
            <div class="json-viewer-panel">
                <div class="json-viewer-toolbar">
                    <button id="copy-json-btn" class="btn btn-primary btn-full text-sm">
                        <i class="ti ti-copy"></i> Copy to Clipboard
                    </button>
                </div>

                <div class="json-viewer-content">
                    <pre id="json-content" class="json-content-view">${this.syntaxHighlightJSON(jsonString)}</pre>
                </div>
            </div>
        `;

    this.showSecondarySidebar(title, content);

    setTimeout(() => {
      const copyBtn = this.editor.$id('copy-json-btn') as HTMLButtonElement | null;
      if (copyBtn) {
        copyBtn.onclick = () => {
          navigator.clipboard
            .writeText(jsonString)
            .then(() => {
              showToast('JSON copied to clipboard', 'success', 2000);
              copyBtn.innerHTML = '<i class="ti ti-check"></i> Copied!';
              copyBtn.classList.remove('btn-primary');
              copyBtn.classList.add('btn-success');
              setTimeout(() => {
                copyBtn.innerHTML = '<i class="ti ti-copy"></i> Copy to Clipboard';
                copyBtn.classList.remove('btn-success');
                copyBtn.classList.add('btn-primary');
              }, 2000);
            })
            .catch((err) => {
              console.error('Failed to copy JSON:', err);
              showToast('Failed to copy JSON', 'error', 3000);
            });
        };
      }
    }, 0);
  }

  syntaxHighlightJSON(jsonString: string): string {
    return jsonString
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(
        /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g,
        (match) => {
          let cls = 'json-number';
          if (/^"/.test(match)) {
            if (/:$/.test(match)) {
              cls = 'json-key';
            } else {
              cls = 'json-string';
            }
          } else if (/true|false/.test(match)) {
            cls = 'json-boolean';
          } else if (/null/.test(match)) {
            cls = 'json-null';
          }

          const colors: Record<string, string> = {
            'json-key': '#00d4ff',
            'json-string': '#ffd93d',
            'json-number': '#ff79c6',
            'json-boolean': '#04d361',
            'json-null': '#888',
          };

          return `<span style="color: ${colors[cls]};">${match}</span>`;
        }
      );
  }

  /** Keep the secondary inspector in step with the newly selected tile. A
   *  signal / platform / station editor that is already open follows the tile
   *  (or steps aside to the Tile Editor when the tile no longer carries that
   *  entity). When nothing more specific is open — or the Tile Editor itself is
   *  showing — the tile's own bits show in the Tile Editor. Any other entity
   *  editor (block, switch, train, path, …) is left in place; those are reached
   *  through their own Edit affordances. */
  updateSecondaryEditorForTile(track: any): void {
    const type = this.editor._currentEditorType;

    switch (type) {
      case 'signal':
        if (track.signals && track.signals.length > 0) {
          this.editor.showSignalsEditorForTile(track);
        } else {
          this.editor.showTileEditor(track);
        }
        return;

      case 'platform':
        if (track.station_id) {
          this.editor.showPlatformEditor(track.station_id);
        } else {
          this.editor.showTileEditor(track);
        }
        return;

      case 'station-group':
      case 'station-group-list':
        this.editor.showStationsPanel();
        return;
    }

    if (!type || type === 'tile') {
      this.editor.showTileEditor(track);
    }
  }

  initViewFullJSONButton(track: any): void {
    const viewJsonBtn = this.editor.$id('view-full-json-btn') as HTMLButtonElement | null;
    if (viewJsonBtn) {
      viewJsonBtn.onclick = () => {
        this.showFullJSONViewer(track, `JSON: (${track.x}, ${track.y})`);
      };
    }
  }

  initPlatformButtonHandlers(): void {
    const editPlatformBtns = document.querySelectorAll<HTMLElement>('.edit-platform-btn');
    editPlatformBtns.forEach((btn) => {
      btn.onclick = () => {
        const platformId = btn.dataset.platformId;
        if (platformId) {
          this.editor.showPlatformEditor(platformId);
        }
      };
    });
  }

  initBitEditor(track: any): void {
    // The bit grid lives in the Tile Editor (secondary sidebar) now.
    const body: HTMLElement | null = this.editor.$id('secondary-sidebar-content');
    if (!body) return;

    const checkboxes = body.querySelectorAll<HTMLInputElement>('input[type="checkbox"][data-bit]');

    const calculateBits = () => {
      let bits = 0;
      checkboxes.forEach((cb) => {
        if (cb.checked) bits |= parseInt(cb.dataset.bit ?? '0');
      });
      return bits;
    };

    // Optimistic readout update for instant feedback; the authoritative state
    // lands when _commitTrackBits re-renders the panel from the server. The
    // checked/unchecked chrome is pure CSS (.checkbox-custom :checked) now, so
    // there is nothing to repaint here beyond the numeric readout.
    const updateReadout = () => {
      const bits = calculateBits();
      const total = this.editor.$id('bit-total');
      const binary = this.editor.$id('bit-binary');
      const forbidden = this.editor.$id('bit-forbidden');
      if (total) total.textContent = String(bits);
      if (binary) binary.textContent = bits.toString(2).padStart(6, '0');
      if (forbidden) {
        forbidden.style.display =
          FORBIDDEN_TRACK_BITS.has(bits) || isSquareCornerSwitch(bits) ? 'inline' : 'none';
      }
    };

    // Every toggle auto-applies through the one validated commit path — there is
    // no Apply button. Forbidden / square-corner / destructive combinations are
    // all handled (toast, confirm-modal, or revert) inside _commitTrackBits.
    checkboxes.forEach((cb) => {
      cb.addEventListener('change', async () => {
        updateReadout();
        await this._commitTrackBits(track, calculateBits());
      });
    });
  }

  // The single validated path that writes track_bits to the server. Both the
  // checkbox grid and the visual compass route through here so the forbidden /
  // square-corner guards and the "this removes a switch/signal/platform"
  // confirmation modal apply identically to both. On any rejection or failure
  // the panel is re-rendered from server state, which reverts the optimistic UI.
  async _commitTrackBits(track: any, newBits: number): Promise<void> {
    const api: any = this.editor.api;
    const resync = () => this.editor.selectElement({ x: track.x, y: track.y });

    if (FORBIDDEN_TRACK_BITS.has(newBits)) {
      showToast('Cannot apply forbidden configuration', 'error', 3000);
      await resync();
      return;
    }
    if (isSquareCornerSwitch(newBits)) {
      showToast(
        'Invalid switch: a branch is 90° to a connecting segment ' +
          '(square corner). Add a diagonal (NS/EW) leg to ease it.',
        'error',
        5000
      );
      await resync();
      return;
    }

    const applyBits = async (force: boolean) => {
      // Removing the last segment (newBits === 0, no dead-end ports) deletes
      // the whole tile. The server reports that with a removed/empty result
      // that carries no `track` payload, so there is nothing left to re-fetch
      // — getTrack on the removed tile would just 404 to null. Track removal
      // so we skip the pointless re-fetch and still fall through to
      // refreshNetwork + reselect, which leaves the now empty tile selected.
      let tileRemoved = false;
      if (track.isVirtual) {
        await api.addTracksBulk([
          { x: track.x, y: track.y, track_bits: newBits, directed_paths: [] },
        ]);
      } else {
        const reqBody: Record<string, unknown> = { track_bits: newBits };
        if (force) reqBody.force = true;
        const updateResult = await api.updateTrack(track.x, track.y, reqBody);
        tileRemoved = !updateResult?.track;
      }

      if (!tileRemoved) {
        const updatedTrack = await api.getTrack(track.x, track.y);
        if (updatedTrack && updatedTrack.is_switch && !updatedTrack.switch_id) {
          try {
            await api.createSwitchAt(track.x, track.y, {
              main_length_cm: 0,
              diverge_length_cm: 0,
              main_radius_cm: null,
              diverge_radius_cm: null,
            });
            console.log(`[Editor] Auto-created switch entity at (${track.x}, ${track.y})`);
          } catch (switchErr) {
            console.warn('[Editor] Failed to auto-create switch:', switchErr);
          }
        }
      }

      await this.editor.host?.refreshNetwork();
      await this.editor.selectElement({ x: track.x, y: track.y });
    };

    try {
      await applyBits(false);
    } catch (err: any) {
      const data = err && err.data;
      if (data && data.requires_confirmation) {
        const proceed = await this._confirmBitChangeImpact(data.impact);
        if (!proceed) {
          await resync();
          return;
        }
        try {
          await applyBits(true);
        } catch (err2: any) {
          console.error('Failed to update track bits (forced):', err2);
          showToast('Failed to update track bits: ' + err2.message, 'error', 5000);
          await resync();
        }
        return;
      }
      console.error('Failed to update track bits:', err);
      showToast('Failed to update track bits: ' + err.message, 'error', 5000);
      await resync();
    }
  }

  async _confirmBitChangeImpact(impact: any): Promise<boolean> {
    const lines = [];
    if (impact.removes_switch) {
      lines.push(`• Switch ${impact.removes_switch} (this tile is no longer a switch)`);
    }
    for (const sid of impact.removes_signals || []) {
      lines.push(`• Signal ${sid} (its detection direction loses its segment)`);
    }
    if (impact.removes_platform) {
      lines.push(`• Platform ${impact.removes_platform} (tile would be removed)`);
    }
    const message = impact.tile_will_be_removed
      ? 'This change removes all segments and will delete the tile. The entities below will also be removed:'
      : 'This change will remove the following entities so the layout stays consistent:';
    return await showConfirmModal({
      title: 'Remove dependent entities?',
      message,
      detail: lines.join('\n'),
      confirmText: 'Remove and apply',
      cancelText: 'Cancel',
      confirmVariant: 'danger',
    });
  }

  initVisualTrackEditor(canvasId: string, track: any): void {
    const canvas = this.editor.$id(canvasId) as HTMLCanvasElement | null;
    if (!canvas) return;

    const ctx = canvas.getContext('2d')!;
    const size = 200; // logical drawing units
    // Render at device resolution so the (now larger) compass stays crisp on
    // retina. All drawing below stays in logical 0..200 coordinates.
    const dpr = window.devicePixelRatio || 1;
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    ctx.scale(dpr, dpr);
    const half = size / 2;
    const quarter = size / 4;

    // Segment color follows the editor (chrome) theme: straight = success,
    // diagonal = info. Resolved from the token at draw time in drawEditor().
    const segments: Record<number, { edges: [string, string]; label: string; token: string }> = {
      1: { edges: ['N', 'S'], label: 'NS', token: '--success-color' }, // straight
      2: { edges: ['E', 'W'], label: 'EW', token: '--success-color' },
      4: { edges: ['N', 'E'], label: 'NE', token: '--info-color' }, // diagonal
      8: { edges: ['N', 'W'], label: 'NW', token: '--info-color' },
      16: { edges: ['S', 'E'], label: 'SE', token: '--info-color' },
      32: { edges: ['S', 'W'], label: 'SW', token: '--info-color' },
    };

    const edgePositions: Record<string, { x: number; y: number }> = {
      N: { x: half + quarter - 5, y: half - quarter + 5 },
      E: { x: half + quarter - 5, y: half + quarter - 5 },
      S: { x: half - quarter + 5, y: half + quarter - 5 },
      W: { x: half - quarter + 5, y: half - quarter + 5 },
    };

    let currentBits = track.track_bits ?? 0;

    const drawEditor = () => {
      ctx.clearRect(0, 0, size, size);

      // Editor-chrome tokens, resolved at draw time so the mini-grid follows the
      // light/dark editor theme. (#000/#fff and #10101a stay literal: legibility
      // marks on saturated fills, correct in both themes.)
      const cs = getComputedStyle(document.documentElement);
      const v = (token: string, fallback: string) => cs.getPropertyValue(token).trim() || fallback;

      ctx.fillStyle = v('--bg-dark', '#121214');
      ctx.fillRect(0, 0, size, size);

      ctx.strokeStyle = v('--border-color', '#333340'); // outer diamond, the quietest line
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(half, 10);
      ctx.lineTo(size - 10, half);
      ctx.lineTo(half, size - 10);
      ctx.lineTo(10, half);
      ctx.closePath();
      ctx.stroke();

      // Ghost (inactive) segments are the click affordance, so they must read
      // clearly — a visible muted gray, a step brighter than the diamond above.
      ctx.setLineDash([3, 4]);
      ctx.strokeStyle = v('--text-tertiary', '#5a5a66');
      ctx.lineWidth = 1.5;
      for (const seg of Object.values(segments)) {
        const [e1, e2] = seg.edges;
        const start = edgePositions[e1];
        const end = edgePositions[e2];
        ctx.beginPath();
        ctx.moveTo(start.x, start.y);
        ctx.lineTo(end.x, end.y);
        ctx.stroke();
      }
      ctx.setLineDash([]);

      for (const [bit, seg] of Object.entries(segments)) {
        if (currentBits & parseInt(bit)) {
          const [e1, e2] = seg.edges;
          const start = edgePositions[e1];
          const end = edgePositions[e2];

          ctx.strokeStyle = v(seg.token, '#04d361');
          ctx.lineWidth = 3;
          ctx.beginPath();
          ctx.moveTo(start.x, start.y);
          ctx.lineTo(end.x, end.y);
          ctx.stroke();

          const midX = (start.x + end.x) / 2;
          const midY = (start.y + end.y) / 2;
          ctx.font = 'bold 12px sans-serif';
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          ctx.strokeStyle = '#000';
          ctx.lineWidth = 3;
          ctx.strokeText(seg.label, midX, midY);
          ctx.fillStyle = '#fff';
          ctx.fillText(seg.label, midX, midY);
        }
      }

      for (const [name, pos] of Object.entries(edgePositions)) {
        let isConnected = false;
        for (const [bit, seg] of Object.entries(segments)) {
          if (currentBits & parseInt(bit) && seg.edges.includes(name)) {
            isConnected = true;
            break;
          }
        }

        ctx.fillStyle = isConnected ? v('--success-color', '#04d361') : v('--bg-hover', '#2a2a35');
        ctx.strokeStyle = isConnected ? v('--text-primary', '#e1e1e6') : v('--text-tertiary', '#5a5a66');
        ctx.lineWidth = isConnected ? 3 : 2;
        ctx.beginPath();
        ctx.arc(pos.x, pos.y, 12, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();

        ctx.fillStyle = isConnected ? '#10101a' : v('--text-secondary', '#a8a8b3'); // #10101a = dark text on the saturated success dot (both themes)
        ctx.font = 'bold 10px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(name, pos.x, pos.y);
      }

      if (FORBIDDEN_TRACK_BITS.has(currentBits) || isSquareCornerSwitch(currentBits)) {
        ctx.fillStyle = v('--danger-color', '#e83f5b');
        ctx.beginPath();
        ctx.arc(half, half, 8, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = '#fff';
        ctx.font = 'bold 10px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText('!', half, half);
      }
    };

    drawEditor();

    // Click the nearest segment to toggle it. The compass shares the one
    // validated commit path with the checkbox grid (_commitTrackBits): the
    // optimistic redraw is instant, then the commit re-renders both views from
    // server state — which also pops the confirmation modal when the change
    // would remove a switch / signal / platform.
    canvas.addEventListener('click', async (e) => {
      const rect = canvas.getBoundingClientRect();
      // Map the click into logical 0..200 space (independent of dpr / display size).
      const scaleX = size / rect.width;
      const scaleY = size / rect.height;
      const x = (e.clientX - rect.left) * scaleX;
      const y = (e.clientY - rect.top) * scaleY;

      let closestBit = null;
      let minDist = 12;

      for (const [bit, seg] of Object.entries(segments)) {
        const [e1, e2] = seg.edges;
        const start = edgePositions[e1];
        const end = edgePositions[e2];
        const dist = this.pointToSegmentDistance(x, y, start.x, start.y, end.x, end.y);
        if (dist < minDist) {
          minDist = dist;
          closestBit = parseInt(bit);
        }
      }

      if (closestBit !== null) {
        currentBits ^= closestBit;
        drawEditor();
        await this._commitTrackBits(track, currentBits);
      }
    });
  }

  pointToSegmentDistance(
    px: number,
    py: number,
    x1: number,
    y1: number,
    x2: number,
    y2: number
  ): number {
    const dx = x2 - x1;
    const dy = y2 - y1;
    const lenSq = dx * dx + dy * dy;

    if (lenSq === 0) return Math.sqrt((px - x1) * (px - x1) + (py - y1) * (py - y1));

    let t = ((px - x1) * dx + (py - y1) * dy) / lenSq;
    t = Math.max(0, Math.min(1, t));

    const nearX = x1 + t * dx;
    const nearY = y1 + t * dy;

    return Math.sqrt((px - nearX) * (px - nearX) + (py - nearY) * (py - nearY));
  }

  /** Close the tab pointing at a now-missing entity in every open group —
   *  the home sidebar and any spawned windows alike. */
  async closeStaleSecondaryTab(editorType: string, editorId: string): Promise<void> {
    for (const { group } of this.allTabGroups()) {
      await group.closeStaleTab(editorType, editorId);
      this.closeGroupWindowIfEmpty(group);
    }
  }
}
