// TabGroup — an instantiable working set of tabs: preview/pinned semantics,
// the type→instance navigator, parent-link navigation, and stale-tab
// pruning. This was singleton state directly on EditorUIHandler
// (secondaryTabs / activeSecondaryTabId / secondaryPanelPinned / …); pulling
// it into its own class means more than one can exist — the home secondary
// sidebar is the first instance, and splitting a tab out (a later phase)
// creates additional instances, each with the exact same tab/navigator/
// preview-pin/stale-prune behavior, just pointed at different DOM via `dom`.
//
// TabGroup owns no window chrome (drag/resize/dock/float) — that's
// lib/window-manager.ts's FloatingWindow. TabGroup owns no storage I/O
// either — see onPersist. This mirrors DESIGN_LANGUAGE.md §12 "Editor
// navigation": one implementation for every panel's tab/navigator behavior.

import type { EditorHost } from './types';

/** Editor types whose panel is a standalone collection/list — not the detail
 *  view of one entity. Their tabs get a bold label so a list tab (e.g. all
 *  trains) reads differently from an instance tab (e.g. one specific train). */
const LIST_PANEL_EDITOR_TYPES = new Set([
  'trains-list',
  'paths-list',
  'routes-list',
  'route-runs-list',
  'station-group-list',
  'speed-profiles-list',
  'switches-list',
]);

/** Editor types that open as a casual single-entity *preview* — the ones you
 *  reach by inspecting a tile/entity. A preview reuses one shared tab slot (the
 *  next inspect replaces it) until pinned. Everything else — list panels,
 *  route/path editors, setups — opens as its own kept tab. */
const PREVIEW_ELIGIBLE_EDITOR_TYPES = new Set([
  'tile',
  'block',
  'signal',
  'platform',
  'switch',
  'train-control',
  'tunnel',
  'station-group-detail',
  'speed-profile',
  'path',
  'route-run-editor',
]);

export interface SecondaryTab {
  tabId: string;
  key: string;
  title: string;
  editorType: string | null;
  editorId: string | null;
  content: string;
  pinned: boolean;
}

/** Serialized shape for persistence — no `content` (never restored from
 *  storage; the live panel is re-fetched instead, see restoreTabPanel). */
export interface SerializedTabGroup {
  pinned: boolean;
  activeTabId: string | null;
  tabs: Array<Omit<SecondaryTab, 'content'>>;
}

/** The DOM this group renders into, supplied by whoever hosts it — the home
 *  sidebar's static markup ids today; a spawned window's own dynamically
 *  created equivalents in a later phase. `containerEl` is the element whose
 *  width-mode / visibility classes (compact, route-editor-wide, visible,
 *  user-hidden) a panel can drive — generalized here instead of the
 *  hardcoded `#secondary-sidebar` lookup this replaces. */
export interface TabGroupDom {
  titleEl(): HTMLElement | null;
  contentEl(): HTMLElement | null;
  tabsEl(): HTMLElement | null;
  parentBtnEl(): HTMLButtonElement | null;
  pinBtnEl(): HTMLElement | null;
  closeBtnEl(): HTMLElement | null;
  containerEl(): HTMLElement | null;
}

export interface TabGroupOptions {
  /** Unique per group — 'home' for the sidebar; a generated id for a spawned
   *  window (later phase). Used for persistence in a later phase. */
  groupId: string;
  editor: EditorHost;
  dom: TabGroupDom;
  /** Called whenever tab state changes in a way that should be persisted.
   *  TabGroup does no storage I/O itself — the owner combines this with its
   *  own window-geometry persistence into one payload (mirrors how
   *  WindowManager stays storage-agnostic; see lib/window-manager.ts). */
  onPersist?: () => void;
}

export class TabGroup {
  readonly groupId: string;
  private editor: EditorHost;
  private dom: TabGroupDom;
  private onPersist?: () => void;

  currentPanel: any = null;
  private isTabNavigation = false;
  tabs: SecondaryTab[] = [];
  activeTabId: string | null = null;
  pinned = false;

  constructor(opts: TabGroupOptions) {
    this.groupId = opts.groupId;
    this.editor = opts.editor;
    this.dom = opts.dom;
    this.onPersist = opts.onPersist;
  }

  private persist(): void {
    this.onPersist?.();
  }

  // ---- The stable "content" entry point every handler calls ----

  show(title: string, content: string): void {
    const titleEl = this.dom.titleEl();
    const contentEl = this.dom.contentEl();
    if (!titleEl || !contentEl) {
      return;
    }

    titleEl.textContent = title;
    contentEl.innerHTML = content;

    const container = this.dom.containerEl();
    if (container) {
      container.classList.remove('compact');
      // The route editor re-adds this after rendering; clearing it here
      // means every other panel resets to the default width.
      container.classList.remove('route-editor-wide');
      container.classList.add('visible');
      // Opening an editor is an explicit intent to see it, so clear any
      // header-toggle collapse (see App.setupPanelToggles).
      container.classList.remove('user-hidden');
    }

    this.currentPanel = {
      title,
      content,
      editorType: this.editor._currentEditorType,
      editorId: this.editor._currentEditorId,
    };
    this.syncTabsWithCurrentPanel();
    this.renderTabs();
    this.updateNavigationState();
    this.persist();
  }

  /** Clear this group back to empty (no panel, no tabs) — used when the
   *  hosting window closes/hides. */
  clear(): void {
    this.currentPanel = null;
    this.tabs = [];
    this.activeTabId = null;
    this.renderTabs();
    this.updateNavigationState();
  }

  // ---- Tab key / snapshot ----

  private makeKey(panelSnapshot: any): string {
    const type = panelSnapshot?.editorType || '__none__';
    const id = panelSnapshot?.editorId || '__none__';
    return `${type}:${id}`;
  }

  private createTabSnapshot(panelSnapshot: any): SecondaryTab {
    const type = panelSnapshot?.editorType || null;
    const id = panelSnapshot?.editorId || null;
    const key = this.makeKey(panelSnapshot);
    return {
      tabId: `${type || 'panel'}:${id || Date.now()}`,
      key,
      title: panelSnapshot?.title || 'Editor',
      editorType: type,
      editorId: id,
      content: panelSnapshot?.content || '',
      // A casual single-entity inspect opens as a preview (reuses one slot);
      // lists and editors open as their own kept tab. See pinTab to promote.
      pinned: !PREVIEW_ELIGIBLE_EDITOR_TYPES.has(type || ''),
    };
  }

  private syncTabsWithCurrentPanel(): void {
    if (!this.currentPanel || this.isTabNavigation) {
      return;
    }

    const key = this.makeKey(this.currentPanel);
    const existingTab = this.tabs.find((tab) => tab.key === key);

    if (existingTab) {
      existingTab.title = this.currentPanel.title;
      existingTab.editorType = this.currentPanel.editorType;
      existingTab.editorId = this.currentPanel.editorId;
      existingTab.content = this.currentPanel.content;
      this.activeTabId = existingTab.tabId;
      return;
    }

    const nextTab = this.createTabSnapshot(this.currentPanel);

    // Preview reuse: a casual inspect (unpinned) replaces the existing preview
    // slot instead of stacking a new tab, so clicking around the layout never
    // piles up tabs. Pinned tabs always keep their own slot.
    if (!nextTab.pinned) {
      const previewIndex = this.tabs.findIndex((tab) => !tab.pinned);
      if (previewIndex >= 0) {
        this.tabs[previewIndex] = nextTab;
        this.activeTabId = nextTab.tabId;
        this.persist();
        return;
      }
    }

    this.tabs.push(nextTab);
    this.activeTabId = nextTab.tabId;
    this.persist();
  }

  // ---- Display / navigator ----

  /** Display label + tooltip for a tab. Detail tabs are titled by the entity
   *  they hold (`BLK_588D28`, `#1000001`) — not by the editor's name — so two
   *  open blocks stay distinguishable. List tabs keep their list name. The
   *  tooltip carries the full lineage (`Blocks › BLK_588D28`). */
  getTabDisplay(tab: SecondaryTab): { label: string; tooltip: string } {
    const segments = this.editor.getPanelLineage(tab) || [];
    if (segments.length === 0) {
      const fallback = tab?.title || 'Editor';
      return { label: fallback, tooltip: fallback };
    }
    const label = segments[segments.length - 1].label || tab?.title || 'Editor';
    const tooltip = segments.length > 1 ? segments.map((s: any) => s.label).join(' › ') : label;
    return { label, tooltip };
  }

  /** Tabler icon class for the entity-type indicator in the vertical tab strip.
   *  Icon values are registered in static/icon-selections.json. */
  getTabIconClass(editorType: string | null): string {
    const iconMap: Record<string, string> = {
      'tile': 'ti-square',
      'block': 'ti-square',
      'signal': 'ti-traffic-lights',
      'platform': 'ti-building',
      'switch': 'ti-git-branch',
      'switches-list': 'ti-git-branch',
      'tunnel': 'ti-building-tunnel',
      'train-control': 'ti-train',
      'trains-list': 'ti-train',
      'route-editor': 'ti-map-2',
      'route-run-editor': 'ti-player-play',
      'route-runs-list': 'ti-player-play',
      'routes-list': 'ti-map-2',
      'path': 'ti-route',
      'path-setup': 'ti-route',
      'extend-path-setup': 'ti-arrows-horizontal',
      'paths-list': 'ti-route',
      'station-group': 'ti-home',
      'station-group-detail': 'ti-home',
      'station-group-list': 'ti-home',
      'multi-select': 'ti-marquee-2',
      'speed-profile': 'ti-gauge',
      'speed-profiles-list': 'ti-gauge',
    };
    return iconMap[editorType || ''] ?? 'ti-file';
  }

  /** Map a granular editorType to its top-level navigator group (e.g. both
   *  'train-control' and 'trains-list' fall under Trains). The navigator stacks
   *  open tabs under these group headers as a type → instance tree. */
  getNavigatorGroup(editorType: string | null): { key: string; label: string; icon: string } {
    const groups: Record<string, { key: string; label: string; icon: string }> = {
      'tile': { key: 'tile', label: 'Tiles', icon: 'ti-square' },
      'block': { key: 'block', label: 'Blocks', icon: 'ti-square' },
      'signal': { key: 'signal', label: 'Signals', icon: 'ti-traffic-lights' },
      'platform': { key: 'platform', label: 'Platforms', icon: 'ti-building' },
      'switch': { key: 'switch', label: 'Switches', icon: 'ti-git-branch' },
      'switches-list': { key: 'switch', label: 'Switches', icon: 'ti-git-branch' },
      'tunnel': { key: 'tunnel', label: 'Tunnels', icon: 'ti-building-tunnel' },
      'train-control': { key: 'train', label: 'Trains', icon: 'ti-train' },
      'trains-list': { key: 'train', label: 'Trains', icon: 'ti-train' },
      'route-editor': { key: 'route', label: 'Routes', icon: 'ti-map-2' },
      'route-run-editor': { key: 'route', label: 'Routes', icon: 'ti-map-2' },
      'route-runs-list': { key: 'route', label: 'Routes', icon: 'ti-map-2' },
      'routes-list': { key: 'route', label: 'Routes', icon: 'ti-map-2' },
      'path': { key: 'path', label: 'Paths', icon: 'ti-route' },
      'path-setup': { key: 'path', label: 'Paths', icon: 'ti-route' },
      'extend-path-setup': { key: 'path', label: 'Paths', icon: 'ti-route' },
      'paths-list': { key: 'path', label: 'Paths', icon: 'ti-route' },
      'station-group': { key: 'station', label: 'Stations', icon: 'ti-home' },
      'station-group-detail': { key: 'station', label: 'Stations', icon: 'ti-home' },
      'station-group-list': { key: 'station', label: 'Stations', icon: 'ti-home' },
      'speed-profile': { key: 'speed', label: 'Speed profiles', icon: 'ti-gauge' },
      'speed-profiles-list': { key: 'speed', label: 'Speed profiles', icon: 'ti-gauge' },
      'multi-select': { key: 'selection', label: 'Selection', icon: 'ti-marquee-2' },
    };
    return groups[editorType || ''] ?? { key: 'other', label: 'Other', icon: 'ti-file' };
  }

  renderTabs(): void {
    const tabsEl = this.dom.tabsEl();
    if (!tabsEl) {
      return;
    }

    if (this.tabs.length === 0) {
      tabsEl.innerHTML = '';
      return;
    }

    // A lone tab has nothing to leave behind: popping it out just empties this
    // group (which then hides/closes) and respawns the same single tab in a new
    // window at a cascade offset — a no-op "jump". Only show pop-out when there
    // are other tabs that stay in this group.
    const canPopOut = this.tabs.length > 1;

    const renderRow = (tab: SecondaryTab): string => {
      const { label, tooltip } = this.getTabDisplay(tab);
      const safeLabel = this.escapeHtml(label);
      const safeTooltip = this.escapeHtml(tooltip);
      const activeClass = tab.tabId === this.activeTabId ? ' active' : '';
      const listClass = LIST_PANEL_EDITOR_TYPES.has(tab.editorType || '') ? ' secondary-tab--list' : '';
      const previewClass = tab.pinned ? '' : ' secondary-tab--preview';
      const iconClass = this.getTabIconClass(tab.editorType);
      const pinButton = tab.pinned
        ? ''
        : `<button class="secondary-tab-pin" data-tab-id="${tab.tabId}" title="Pin tab (keep open)" aria-label="Pin tab"><i class="ti ti-pin" aria-hidden="true"></i></button>`;
      const popOutButton = canPopOut
        ? `<button class="secondary-tab-popout" data-tab-id="${tab.tabId}" title="Open in new window" aria-label="Open in new window"><i class="ti ti-external-link" aria-hidden="true"></i></button>`
        : '';
      return (
        `<div class="secondary-tab${activeClass}${listClass}${previewClass}" data-tab-id="${tab.tabId}" title="${safeTooltip}">` +
        `<i class="ti ${iconClass} secondary-tab-icon" aria-hidden="true"></i>` +
        `<button class="secondary-tab-label btn-size-tight" data-tab-id="${tab.tabId}">${safeLabel}</button>` +
        pinButton +
        popOutButton +
        `<button class="secondary-tab-close" data-tab-id="${tab.tabId}" title="Close tab">×</button>` +
        `</div>`
      );
    };

    // Group the open tabs by top-level entity type: a type → instance tree so
    // many open entities stay legible instead of one mixed flat strip.
    const groups = new Map<string, { label: string; icon: string; tabs: SecondaryTab[] }>();
    for (const tab of this.tabs) {
      const g = this.getNavigatorGroup(tab.editorType);
      let bucket = groups.get(g.key);
      if (!bucket) {
        bucket = { label: g.label, icon: g.icon, tabs: [] };
        groups.set(g.key, bucket);
      }
      bucket.tabs.push(tab);
    }

    tabsEl.innerHTML = Array.from(groups.values())
      .map(
        (bucket) =>
          `<div class="secondary-nav-group">` +
          `<div class="secondary-nav-group-header">` +
          `<i class="ti ${bucket.icon} secondary-nav-group-icon" aria-hidden="true"></i>` +
          `<span class="secondary-nav-group-label">${this.escapeHtml(bucket.label)}</span>` +
          `<span class="secondary-nav-group-count">${bucket.tabs.length}</span>` +
          `</div>` +
          bucket.tabs.map(renderRow).join('') +
          `</div>`
      )
      .join('');

    // Scroll the active tab into view in the vertical strip.
    const activeEl = tabsEl.querySelector('.secondary-tab.active');
    if (activeEl && typeof activeEl.scrollIntoView === 'function') {
      activeEl.scrollIntoView({ block: 'nearest', inline: 'start' });
    }
  }

  // ---- Tab lifecycle ----

  async activateTab(tabId: string): Promise<void> {
    const tab = this.tabs.find((t) => t.tabId === tabId);
    if (!tab) {
      return;
    }

    if (tab.tabId === this.activeTabId) {
      return;
    }

    if (this.currentPanel) {
      const currentTab = this.tabs.find((t) => t.tabId === this.activeTabId);
      if (currentTab) {
        currentTab.title = this.currentPanel.title;
        currentTab.editorType = this.currentPanel.editorType;
        currentTab.editorId = this.currentPanel.editorId;
        currentTab.content = this.currentPanel.content;
      }
    }

    this.activeTabId = tab.tabId;
    this.renderTabs();

    await this.restoreTabPanel(tab);
    this.persist();
  }

  /** Promote a preview tab to a kept (pinned) tab so the next inspect no longer
   *  replaces it. Triggered by the tab's pin button or a double-click. */
  pinTab(tabId: string): void {
    const tab = this.tabs.find((t) => t.tabId === tabId);
    if (!tab || tab.pinned) {
      return;
    }
    tab.pinned = true;
    this.renderTabs();
    this.persist();
  }

  /** Remove tabId from `this.tabs` and reconcile whatever's left: if another
   *  tab was active, re-point the neighbor and load its live panel (exactly
   *  like a normal tab switch); if the group is now empty, clear it. Shared
   *  by closeTab (discards the removed tab) and extractTab (hands it to
   *  another group) — they differ only in what happens to the tab that came
   *  out, never in how the remaining state settles. Returns the removed tab,
   *  or null if tabId wasn't found. */
  private async removeAndReconcile(tabId: string): Promise<SecondaryTab | null> {
    const index = this.tabs.findIndex((t) => t.tabId === tabId);
    if (index < 0) {
      return null;
    }
    const wasActive = this.tabs[index].tabId === this.activeTabId;
    const [tab] = this.tabs.splice(index, 1);

    if (this.tabs.length === 0) {
      this.cleanupPanelSubscriptions();
      this.activeTabId = null;
      this.currentPanel = null;
      this.renderTabs();
      this.updateNavigationState();
      this.persist();
      return tab;
    }

    if (wasActive) {
      const nextIndex = Math.min(index, this.tabs.length - 1);
      this.activeTabId = this.tabs[nextIndex].tabId;
      this.renderTabs();
      await this.restoreTabPanel(this.tabs[nextIndex]);
      this.persist();
      return tab;
    }

    this.renderTabs();
    this.persist();
    return tab;
  }

  /** Remove a tab from this group without hiding/destroying this group's
   *  window itself — the caller is handing the tab to another group (a new
   *  window, or an existing one), not closing it. If the extracted tab was
   *  active, the group falls back to showing a remaining neighbor tab (same
   *  as closeTab); if it was the last tab, the group clears. The caller
   *  checks `tabs.length` afterward to decide whether *this* group's window
   *  should now hide/close (see EditorUIHandler.splitTabIntoNewWindow /
   *  moveTabToGroup). */
  async extractTab(tabId: string): Promise<SecondaryTab | null> {
    return this.removeAndReconcile(tabId);
  }

  /** Insert a moved-in tab snapshot as a new kept tab and activate it. Does
   *  not itself re-fetch the tab's live panel content — the caller follows
   *  up with restoreTabPanel once this group's window exists. */
  acceptMovedTab(tab: SecondaryTab): void {
    this.tabs.push(tab);
    this.activeTabId = tab.tabId;
    this.renderTabs();
    this.updateNavigationState();
    this.persist();
  }

  /** Close a tab. Returns true when the group is now empty (no tabs left) —
   *  the caller decides what that means for its window (the home window
   *  hides; a spawned window closes itself — see EditorUIHandler). */
  async closeTab(tabId: string): Promise<boolean> {
    const hadTab = this.tabs.some((t) => t.tabId === tabId);
    if (!hadTab) {
      return this.tabs.length === 0;
    }
    await this.removeAndReconcile(tabId);
    return this.tabs.length === 0;
  }

  async restoreTabPanel(tabSnapshot: SecondaryTab): Promise<void> {
    this.cleanupPanelSubscriptions();
    this.isTabNavigation = true;
    try {
      // editor.restoreSecondaryPanel calls into a showXEditor() method that
      // ultimately calls Editor.showSecondarySidebar — which, once a tab can
      // live in a window other than the home one, needs to know *which*
      // group asked so it lands back in this group rather than always the
      // home one. This one-shot ambient flag (read-and-cleared by
      // EditorUIHandler.showSecondarySidebar) carries that — the same shape
      // as isTabNavigation's re-entrancy guard, just shared across groups via
      // the editor host both sides already reference.
      this.editor._activeTabGroupForRestore = this;
      let restored: boolean;
      try {
        restored = await this.editor.restoreSecondaryPanel(tabSnapshot);
      } finally {
        this.editor._activeTabGroupForRestore = null;
      }
      if (!restored) {
        this.editor._currentEditorType = tabSnapshot.editorType || null;
        this.editor._currentEditorId = tabSnapshot.editorId || null;
        this.show(tabSnapshot.title || 'Editor', tabSnapshot.content || '');
      }
    } finally {
      this.isTabNavigation = false;
      this.renderTabs();
      this.updateNavigationState();
    }
  }

  isPinned(): boolean {
    return this.pinned;
  }

  setPinned(pinned: boolean): void {
    this.pinned = pinned;
    this.updateNavigationState();
    this.persist();
  }

  async closeActiveTab(): Promise<boolean> {
    if (!this.activeTabId) {
      this.clear();
      return true;
    }
    return this.closeTab(this.activeTabId);
  }

  // ---- Navigation / lineage ----

  /** The navigable parent list of the current panel, or null when there is
   *  none (list panels; detail panels with no list home, e.g. blocks). The
   *  header's `← Parent` button shows exactly when this is non-null. */
  getCurrentParentSegment(): any | null {
    if (!this.currentPanel) {
      return null;
    }
    const segments = this.editor.getPanelLineage(this.currentPanel) || [];
    if (segments.length < 2) {
      return null;
    }
    const parent = segments[0];
    return parent && parent.editorType ? parent : null;
  }

  /** Open the current detail panel's parent list (header `← Parent` button,
   *  Alt+Left). The list activates its existing tab or opens as a new one. */
  async navigateToParentList(): Promise<void> {
    const parent = this.getCurrentParentSegment();
    if (!parent) {
      return;
    }
    this.cleanupPanelSubscriptions();
    this.editor._activeTabGroupForRestore = this;
    try {
      await this.editor.restoreSecondaryPanel({
        title: parent.label,
        editorType: parent.editorType,
        editorId: parent.editorId || null,
        content: '',
      });
    } finally {
      this.editor._activeTabGroupForRestore = null;
    }
  }

  updateNavigationState(): void {
    const closeBtn = this.dom.closeBtnEl();
    if (!closeBtn) {
      return;
    }
    closeBtn.title = 'Close';

    // `← Trains`-style parent link: shown only when the open panel is a
    // detail view whose parent list exists (DESIGN_LANGUAGE.md §12 — the back
    // button is labeled with its destination).
    const parentBtn = this.dom.parentBtnEl();
    if (parentBtn) {
      const parent = this.getCurrentParentSegment();
      if (parent) {
        parentBtn.hidden = false;
        parentBtn.textContent = `← ${parent.label}`;
        parentBtn.title = `Back to ${parent.label}`;
      } else {
        parentBtn.hidden = true;
        parentBtn.textContent = '';
      }
    }

    const pinBtn = this.dom.pinBtnEl();
    if (pinBtn) {
      pinBtn.classList.toggle('active', this.pinned);
      pinBtn.title = this.pinned ? 'Unpin inspector' : 'Pin inspector';
    }
  }

  // ---- Staleness ----

  /** A persisted tab is stale when it points at an entity absent from the
   *  loaded network. List/tool-setup panels and id-only editors (paths,
   *  routes) aren't tied to a network entity and are always kept. */
  isTabStale(tab: SecondaryTab, network: any): boolean {
    const id = tab?.editorId;
    switch (tab?.editorType) {
      case 'train':
      case 'train-control':
        return !(Array.isArray(network?.trains) && network.trains.some((t: any) => t.id === id));
      case 'block':
        return !(id != null && network?.blocks && id in network.blocks);
      case 'signal':
        return !(id != null && network?.signals && id in network.signals);
      case 'switch':
        return !(id != null && network?.switches && id in network.switches);
      case 'platform':
        return !(Array.isArray(network?.platforms) && network.platforms.some((p: any) => p.platform_id === id));
      case 'station-group':
        return !(Array.isArray(network?.station_groups) && network.station_groups.some((s: any) => s.station_id === id));
      default:
        return false;
    }
  }

  /** Drop persisted tabs whose entity no longer exists in the loaded network,
   *  re-point the active tab, and persist if anything changed. */
  pruneStaleTabs(): void {
    if (this.tabs.length === 0) {
      return;
    }
    const network = this.editor.cachedNetwork;
    const kept = this.tabs.filter((tab) => !this.isTabStale(tab, network));
    if (kept.length === this.tabs.length) {
      return;
    }

    this.tabs = kept;
    if (kept.length === 0) {
      this.activeTabId = null;
    } else if (!kept.some((tab) => tab.tabId === this.activeTabId)) {
      this.activeTabId = kept[0].tabId;
    }
    this.persist();
    this.renderTabs();
    this.updateNavigationState();
  }

  /** Close the tab pointing at a now-missing entity (matched by type + id).
   *  No-op when no such tab is open; if it was this group's current editor,
   *  clear the group so the missing entity doesn't linger on screen. Checks
   *  this group's own currentPanel (not the shared editor._currentEditorType
   *  global) so the check stays scoped to this group once more than one can
   *  exist — otherwise every other open group would also match and clear
   *  itself the moment any one of them happened to share the deleted entity.
   *  Returns true when the group ended up empty (mirrors closeTab's
   *  contract). */
  async closeStaleTab(editorType: string, editorId: string): Promise<boolean> {
    const tab = this.tabs.find((t) => t.editorType === editorType && t.editorId === editorId);
    if (tab) {
      return this.closeTab(tab.tabId);
    }
    if (this.currentPanel?.editorType === editorType && this.currentPanel?.editorId === editorId) {
      this.clear();
      return true;
    }
    return false;
  }

  cleanupPanelSubscriptions(): void {
    if (this.editor.trainHandler?._cleanup) this.editor.trainHandler._cleanup();
    if (this.editor.signalHandler?._cleanup) this.editor.signalHandler._cleanup();
    if (this.editor.switchHandler?._cleanup) this.editor.switchHandler._cleanup();
    if (this.editor.blockHandler?._cleanup) this.editor.blockHandler._cleanup();
    if (this.editor.platformHandler?._cleanup) this.editor.platformHandler._cleanup();
    if (this.editor.stationGroupHandler?._cleanup) this.editor.stationGroupHandler._cleanup();
    if (this.editor.routeRunHandler?._cleanup) this.editor.routeRunHandler._cleanup();
    if (this.editor.routeHandler?._cleanup) this.editor.routeHandler._cleanup();
    if (this.editor.pathHandler?._cleanup) this.editor.pathHandler._cleanup();
    if (this.editor.speedProfileHandler?._cleanup) this.editor.speedProfileHandler._cleanup();
  }

  escapeHtml(text: any): string {
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // ---- Persistence (shape only — I/O stays with the owner) ----

  serialize(): SerializedTabGroup {
    return {
      pinned: this.pinned,
      activeTabId: this.activeTabId,
      tabs: this.tabs.map((tab) => ({
        tabId: tab.tabId,
        key: tab.key,
        title: tab.title,
        editorType: tab.editorType,
        editorId: tab.editorId,
        pinned: !!tab.pinned,
      })),
    };
  }

  restoreTabs(saved: any): void {
    this.pinned = saved?.pinned === true;

    const tabs = Array.isArray(saved?.tabs) ? saved.tabs : [];
    this.tabs = tabs
      .filter((tab: any) => tab && tab.editorType)
      .map((tab: any) => ({
        tabId: tab.tabId || `${tab.editorType}:${tab.editorId || Date.now()}`,
        key: tab.key || this.makeKey(tab),
        title: tab.title || 'Editor',
        editorType: tab.editorType,
        editorId: tab.editorId || null,
        content: '',
        // Default to kept: tabs persisted before preview/pin existed have no
        // flag and were deliberately open, so they restore pinned rather than
        // collapsing into the single preview slot. New state is explicit.
        pinned: tab.pinned !== false,
      }));

    if (this.tabs.length > 0) {
      const activeId = saved?.activeTabId;
      const activeExists = this.tabs.some((tab) => tab.tabId === activeId);
      this.activeTabId = activeExists ? activeId : this.tabs[0].tabId;
    } else {
      this.activeTabId = null;
    }

    this.renderTabs();
    this.updateNavigationState();
  }

  /** Wait for the network to be ready, drop stale tabs, then restore the
   *  active tab's live panel. Persisted tabs are global, not per-scene: an
   *  entity that existed when a tab was saved may be absent from the scene
   *  now loaded — restoring before pruning would fire a request for a
   *  missing entity (e.g. GET /api/trains/DEMO_TRAIN -> 404). */
  async restoreActivePanel(): Promise<void> {
    if (!this.activeTabId || this.tabs.length === 0) {
      return;
    }

    await this.editor.whenNetworkReady();
    this.pruneStaleTabs();

    if (!this.activeTabId || this.tabs.length === 0) {
      return;
    }

    const activeTab = this.tabs.find((tab) => tab.tabId === this.activeTabId);
    if (!activeTab) {
      return;
    }

    await this.restoreTabPanel(activeTab);
  }
}
