// editor-ui.ts — Editor UI Handler (secondary sidebar, working-set tabs,
// collapsible categories, JSON viewer, bit/visual track editors).
//
// Owned by the Editor (`new EditorUIHandler(this)`). The sidebar chrome is
// header (parent link · title · pin · close) + one row of entity-titled tabs;
// see DESIGN_LANGUAGE.md §12 "Editor navigation".

import { showToast } from '../lib/toast';
import type { EditorHost } from './types';
import { showConfirmModal } from '../lib/helpers';
import { FORBIDDEN_TRACK_BITS, isSquareCornerSwitch } from '../lib/constants';

/** Editor types whose panel is a standalone collection/list — not the detail
 *  view of one entity. Their secondary-sidebar tabs get a bold label so a list
 *  tab (e.g. all trains) reads differently from an instance tab (e.g. one
 *  specific train). */
const LIST_PANEL_EDITOR_TYPES = new Set([
  'trains-list',
  'paths-list',
  'routes-list',
  'route-runs-list',
  'station-group-list',
  'speed-profiles-list',
  'switches-list',
]);

export class EditorUIHandler {
  editor: EditorHost;
  currentSecondaryPanel: any;
  isTabNavigation: boolean;
  secondaryTabs: any[];
  activeSecondaryTabId: string | null;
  secondaryPanelPinned: boolean;
  secondaryInspectorStorageKey: string;
  sidebarSectionStorageKey: string;

  constructor(editor: EditorHost) {
    this.editor = editor;
    this.currentSecondaryPanel = null;
    this.isTabNavigation = false;
    this.secondaryTabs = [];
    this.activeSecondaryTabId = null;
    this.secondaryPanelPinned = false;
    this.secondaryInspectorStorageKey = 'secondaryInspectorStateV1';
    this.sidebarSectionStorageKey = 'sidebarSectionStatesV1';
  }

  get selectedTool(): any {
    return this.editor.selectedTool;
  }

  setupSecondarySidebar(): void {
    const closeBtn = document.getElementById('close-secondary-sidebar');
    if (closeBtn) {
      closeBtn.addEventListener('click', async () => {
        if (this.activeSecondaryTabId) {
          await this.closeSecondaryTab(this.activeSecondaryTabId);
          return;
        }
        await this.hideSecondarySidebar();
      });
    }

    const parentBtn = document.getElementById('secondary-nav-parent');
    if (parentBtn) {
      parentBtn.addEventListener('click', async () => {
        await this.navigateToParentList();
      });
    }

    const pinBtn = document.getElementById('secondary-nav-pin');
    if (pinBtn) {
      pinBtn.addEventListener('click', () => {
        this.secondaryPanelPinned = !this.secondaryPanelPinned;
        this.updateSecondaryNavigationState();
        this.persistSecondaryInspectorState();
      });
    }

    const tabsEl = document.getElementById('secondary-sidebar-tabs');
    if (tabsEl) {
      tabsEl.addEventListener('click', async (event) => {
        const closeBtn = (event.target as HTMLElement).closest(
          '.secondary-tab-close[data-tab-id]'
        ) as HTMLElement | null;
        if (closeBtn) {
          event.stopPropagation();
          const tabId = closeBtn.dataset.tabId;
          if (tabId) {
            await this.closeSecondaryTab(tabId);
          }
          return;
        }

        const labelBtn = (event.target as HTMLElement).closest(
          '.secondary-tab-label[data-tab-id]'
        ) as HTMLElement | null;
        if (labelBtn) {
          const tabId = labelBtn.dataset.tabId;
          if (tabId) {
            await this.activateSecondaryTab(tabId);
          }
        }
      });
    }

    this.updateSecondaryNavigationState();
    this.loadSecondaryInspectorState();
    setTimeout(async () => {
      await this.restoreSecondaryInspectorState();
    }, 0);
  }

  setupCollapsibleCategories(): void {
    const categories = document.querySelectorAll<HTMLElement>('.tool-category');

    categories.forEach((category) => {
      const label = category.querySelector('.tool-category-label');
      if (!label) return;

      label.addEventListener('click', (e) => {
        e.stopPropagation();
        this.toggleCategory(category);
      });
    });

    this.loadCategoryStates();

    this.expandCategoryForTool(this.selectedTool);
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

  toggleCategory(category: HTMLElement): void {
    const wasCollapsed = category.classList.contains('collapsed');

    if (wasCollapsed) {
      category.classList.remove('collapsed');
    } else {
      category.classList.add('collapsed');
    }

    this.saveCategoryStates();
  }

  expandCategoryForTool(toolName: any): void {
    if (!toolName) return;

    const toolBtn = document.querySelector(`.tool-btn[data-tool="${toolName}"]`);
    if (!toolBtn) return;

    const category = toolBtn.closest('.tool-category');
    if (category && category.classList.contains('collapsed')) {
      category.classList.remove('collapsed');
      this.saveCategoryStates();
    }
  }

  saveCategoryStates(): void {
    const categories = document.querySelectorAll<HTMLElement>('.tool-category');
    const states: Record<string, boolean> = {};

    categories.forEach((category, index) => {
      const label = category.querySelector('.tool-category-label');
      const categoryName = label ? (label.textContent || '').trim() : `category-${index}`;
      states[categoryName] = category.classList.contains('collapsed');
    });

    try {
      localStorage.setItem('toolCategoryStates', JSON.stringify(states));
    } catch (e) {}
  }

  loadCategoryStates(): void {
    try {
      const saved = localStorage.getItem('toolCategoryStates');
      if (!saved) return;

      const states = JSON.parse(saved);
      const categories = document.querySelectorAll<HTMLElement>('.tool-category');

      categories.forEach((category, index) => {
        const label = category.querySelector('.tool-category-label');
        const categoryName = label ? (label.textContent || '').trim() : `category-${index}`;

        if (states[categoryName] === true) {
          category.classList.add('collapsed');
        }
      });
    } catch (e) {}
  }

  showSecondarySidebar(title: string, content: string): void {
    const sidebar = document.getElementById('secondary-sidebar');
    const titleEl = document.getElementById('secondary-sidebar-title');
    const contentEl = document.getElementById('secondary-sidebar-content');

    if (sidebar && titleEl && contentEl) {
      titleEl.textContent = title;
      contentEl.innerHTML = content;

      sidebar.classList.remove('compact');
      // The route editor re-adds this after rendering; clearing it here
      // means every other panel resets to the default width.
      sidebar.classList.remove('route-editor-wide');

      sidebar.classList.add('visible');
      // Opening an editor is an explicit intent to see it, so clear any
      // header-toggle collapse (see App.setupPanelToggles).
      sidebar.classList.remove('user-hidden');

      this.currentSecondaryPanel = {
        title,
        content,
        editorType: this.editor._currentEditorType,
        editorId: this.editor._currentEditorId,
      };
      this.syncSecondaryTabsWithCurrentPanel();
      this.renderSecondaryTabs();
      this.updateSecondaryNavigationState();
      this.persistSecondaryInspectorState();
    }
  }

  async hideSecondarySidebar(): Promise<void> {
    const sidebar = document.getElementById('secondary-sidebar');
    if (sidebar) {
      sidebar.classList.remove('visible');
      sidebar.classList.remove('compact');
      sidebar.classList.remove('route-editor-wide');
      // Reset the header-toggle collapse so a reopened panel starts shown.
      sidebar.classList.remove('user-hidden');
    }

    this.cleanupSecondaryPanelSubscriptions();

    this.currentSecondaryPanel = null;
    this.secondaryTabs = [];
    this.activeSecondaryTabId = null;
    this.renderSecondaryTabs();
    this.updateSecondaryNavigationState();
    this.persistSecondaryInspectorState();

    this.editor._currentEditorType = null;
    this.editor._currentEditorId = null;

    // Drop the block-direction arrows when the sidebar closes.
    if (this.editor.renderer?.clearBlockDirectionOverlay) {
      this.editor.renderer.clearBlockDirectionOverlay();
    }
  }

  /** The navigable parent list of the current panel, or null when there is
   *  none (list panels; detail panels with no list home, e.g. blocks). The
   *  header's `← Parent` button shows exactly when this is non-null. */
  getCurrentParentSegment(): any | null {
    if (!this.currentSecondaryPanel) {
      return null;
    }
    const segments = this.editor.getPanelLineage(this.currentSecondaryPanel) || [];
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
    this.cleanupSecondaryPanelSubscriptions();
    await this.editor.restoreSecondaryPanel({
      title: parent.label,
      editorType: parent.editorType,
      editorId: parent.editorId || null,
      content: '',
    });
  }

  makeSecondaryPanelKey(panelSnapshot: any): string {
    const type = panelSnapshot?.editorType || '__none__';
    const id = panelSnapshot?.editorId || '__none__';
    return `${type}:${id}`;
  }

  createSecondaryTabSnapshot(panelSnapshot: any): any {
    const type = panelSnapshot?.editorType || null;
    const id = panelSnapshot?.editorId || null;
    const key = this.makeSecondaryPanelKey(panelSnapshot);
    return {
      tabId: `${type || 'panel'}:${id || Date.now()}`,
      key,
      title: panelSnapshot?.title || 'Editor',
      editorType: type,
      editorId: id,
      content: panelSnapshot?.content || '',
    };
  }

  syncSecondaryTabsWithCurrentPanel(): void {
    if (!this.currentSecondaryPanel || this.isTabNavigation) {
      return;
    }

    const key = this.makeSecondaryPanelKey(this.currentSecondaryPanel);
    const existingTab = this.secondaryTabs.find((tab) => tab.key === key);

    if (existingTab) {
      existingTab.title = this.currentSecondaryPanel.title;
      existingTab.editorType = this.currentSecondaryPanel.editorType;
      existingTab.editorId = this.currentSecondaryPanel.editorId;
      existingTab.content = this.currentSecondaryPanel.content;
      this.activeSecondaryTabId = existingTab.tabId;
      return;
    }

    const nextTab = this.createSecondaryTabSnapshot(this.currentSecondaryPanel);
    this.secondaryTabs.push(nextTab);
    this.activeSecondaryTabId = nextTab.tabId;
    this.persistSecondaryInspectorState();
  }

  /** Display label + tooltip for a tab. Detail tabs are titled by the entity
   *  they hold (`BLK_588D28`, `#1000001`) — not by the editor's name — so two
   *  open blocks stay distinguishable. List tabs keep their list name. The
   *  tooltip carries the full lineage (`Blocks › BLK_588D28`). */
  getSecondaryTabDisplay(tab: any): { label: string; tooltip: string } {
    const segments = this.editor.getPanelLineage(tab) || [];
    if (segments.length === 0) {
      const fallback = tab?.title || 'Editor';
      return { label: fallback, tooltip: fallback };
    }
    const label = segments[segments.length - 1].label || tab?.title || 'Editor';
    const tooltip =
      segments.length > 1 ? segments.map((s: any) => s.label).join(' › ') : label;
    return { label, tooltip };
  }

  /** Tabler icon class for the entity-type indicator in the vertical tab strip.
   *  Icon values are registered in static/icon-selections.json. */
  getTabIconClass(editorType: string | null): string {
    const iconMap: Record<string, string> = {
      'block': 'ti-square',
      'signal': 'ti-traffic-lights',
      'platform': 'ti-building',
      'switch': 'ti-git-branch',
      'switches-list': 'ti-git-branch',
      'tunnel': 'ti-building-tunnel',
      'train-control': 'ti-train',
      'trains-list': 'ti-train',
      'route-editor': 'ti-map-route',
      'route-run-editor': 'ti-player-play',
      'route-runs-list': 'ti-player-play',
      'routes-list': 'ti-map-route',
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

  renderSecondaryTabs(): void {
    const tabsEl = document.getElementById('secondary-sidebar-tabs');
    if (!tabsEl) {
      return;
    }

    if (this.secondaryTabs.length === 0) {
      tabsEl.innerHTML = '';
      return;
    }

    tabsEl.innerHTML = this.secondaryTabs
      .map((tab) => {
        const { label, tooltip } = this.getSecondaryTabDisplay(tab);
        const safeLabel = this.escapeHtml(label);
        const safeTooltip = this.escapeHtml(tooltip);
        const activeClass = tab.tabId === this.activeSecondaryTabId ? ' active' : '';
        const listClass = LIST_PANEL_EDITOR_TYPES.has(tab.editorType) ? ' secondary-tab--list' : '';
        const iconClass = this.getTabIconClass(tab.editorType);
        return (
          `<div class="secondary-tab${activeClass}${listClass}" title="${safeTooltip}">` +
          `<i class="ti ${iconClass} secondary-tab-icon" aria-hidden="true"></i>` +
          `<button class="secondary-tab-label btn-size-tight" data-tab-id="${tab.tabId}">${safeLabel}</button>` +
          `<button class="secondary-tab-close" data-tab-id="${tab.tabId}" title="Close tab">×</button>` +
          `</div>`
        );
      })
      .join('');

    // Scroll the active tab into view in the vertical strip.
    const activeEl = tabsEl.querySelector('.secondary-tab.active');
    if (activeEl && typeof activeEl.scrollIntoView === 'function') {
      activeEl.scrollIntoView({ block: 'nearest', inline: 'start' });
    }
  }

  async activateSecondaryTab(tabId: string): Promise<void> {
    const tab = this.secondaryTabs.find((t) => t.tabId === tabId);
    if (!tab) {
      return;
    }

    if (tab.tabId === this.activeSecondaryTabId) {
      return;
    }

    if (this.currentSecondaryPanel) {
      const currentTab = this.secondaryTabs.find((t) => t.tabId === this.activeSecondaryTabId);
      if (currentTab) {
        currentTab.title = this.currentSecondaryPanel.title;
        currentTab.editorType = this.currentSecondaryPanel.editorType;
        currentTab.editorId = this.currentSecondaryPanel.editorId;
        currentTab.content = this.currentSecondaryPanel.content;
      }
    }

    this.activeSecondaryTabId = tab.tabId;
    this.renderSecondaryTabs();

    await this.restoreSecondaryTabPanel(tab);
    this.persistSecondaryInspectorState();
  }

  async closeSecondaryTab(tabId: string): Promise<void> {
    const index = this.secondaryTabs.findIndex((t) => t.tabId === tabId);
    if (index < 0) {
      return;
    }

    const wasActive = this.secondaryTabs[index].tabId === this.activeSecondaryTabId;
    this.secondaryTabs.splice(index, 1);

    if (this.secondaryTabs.length === 0) {
      this.activeSecondaryTabId = null;
      this.renderSecondaryTabs();
      await this.hideSecondarySidebar();
      return;
    }

    if (wasActive) {
      const nextIndex = Math.min(index, this.secondaryTabs.length - 1);
      this.activeSecondaryTabId = this.secondaryTabs[nextIndex].tabId;
      this.renderSecondaryTabs();
      await this.restoreSecondaryTabPanel(this.secondaryTabs[nextIndex]);
      this.persistSecondaryInspectorState();
      return;
    }

    this.renderSecondaryTabs();
    this.persistSecondaryInspectorState();
  }

  async restoreSecondaryTabPanel(tabSnapshot: any): Promise<void> {
    this.cleanupSecondaryPanelSubscriptions();
    this.isTabNavigation = true;
    try {
      const restored = await this.editor.restoreSecondaryPanel(tabSnapshot);
      if (!restored) {
        this.editor._currentEditorType = tabSnapshot.editorType || null;
        this.editor._currentEditorId = tabSnapshot.editorId || null;
        this.showSecondarySidebar(tabSnapshot.title || 'Editor', tabSnapshot.content || '');
      }
    } finally {
      this.isTabNavigation = false;
      this.renderSecondaryTabs();
      this.updateSecondaryNavigationState();
    }
  }

  isSecondaryPanelPinned(): boolean {
    return this.secondaryPanelPinned;
  }

  closeActiveSecondaryTab(): Promise<void> {
    if (!this.activeSecondaryTabId) {
      return this.hideSecondarySidebar();
    }
    return this.closeSecondaryTab(this.activeSecondaryTabId);
  }

  persistSecondaryInspectorState(): void {
    const tabs = this.secondaryTabs.map((tab) => ({
      tabId: tab.tabId,
      key: tab.key,
      title: tab.title,
      editorType: tab.editorType,
      editorId: tab.editorId,
    }));

    const payload = {
      pinned: this.secondaryPanelPinned,
      activeTabId: this.activeSecondaryTabId,
      tabs,
    };

    try {
      localStorage.setItem(this.secondaryInspectorStorageKey, JSON.stringify(payload));
    } catch (e) {}
  }

  loadSecondaryInspectorState(): void {
    try {
      const raw = localStorage.getItem(this.secondaryInspectorStorageKey);
      if (!raw) {
        return;
      }

      const parsed = JSON.parse(raw);
      this.secondaryPanelPinned = parsed?.pinned === true;

      const tabs = Array.isArray(parsed?.tabs) ? parsed.tabs : [];
      this.secondaryTabs = tabs
        .filter((tab: any) => tab && tab.editorType)
        .map((tab: any) => ({
          tabId: tab.tabId || `${tab.editorType}:${tab.editorId || Date.now()}`,
          key: tab.key || this.makeSecondaryPanelKey(tab),
          title: tab.title || 'Editor',
          editorType: tab.editorType,
          editorId: tab.editorId || null,
          content: '',
        }));

      if (this.secondaryTabs.length > 0) {
        const activeId = parsed?.activeTabId;
        const activeExists = this.secondaryTabs.some((tab) => tab.tabId === activeId);
        this.activeSecondaryTabId = activeExists ? activeId : this.secondaryTabs[0].tabId;
      } else {
        this.activeSecondaryTabId = null;
      }

      this.renderSecondaryTabs();
      this.updateSecondaryNavigationState();
    } catch (e) {}
  }

  async restoreSecondaryInspectorState(): Promise<void> {
    if (!this.activeSecondaryTabId || this.secondaryTabs.length === 0) {
      return;
    }

    // Persisted tabs are global, not per-scene: an entity that existed when a
    // tab was saved may be absent from the scene now loaded. Wait for the
    // first network load, then drop tabs whose entity is gone before
    // restoring — otherwise restoring fires a request for a missing entity
    // (e.g. GET /api/trains/DEMO_TRAIN -> 404 in a scene without that train).
    await this.editor.whenNetworkReady();
    this.pruneStaleSecondaryTabs();

    if (!this.activeSecondaryTabId || this.secondaryTabs.length === 0) {
      return;
    }

    const activeTab = this.secondaryTabs.find((tab) => tab.tabId === this.activeSecondaryTabId);
    if (!activeTab) {
      return;
    }

    await this.restoreSecondaryTabPanel(activeTab);
  }

  /** A persisted tab is stale when it points at an entity absent from the
   *  loaded network. List/tool-setup panels and id-only editors (paths,
   *  routes) aren't tied to a network entity and are always kept. */
  isSecondaryTabStale(tab: any, network: any): boolean {
    const id = tab?.editorId;
    switch (tab?.editorType) {
      case 'train':
      case 'train-control':
        return !(
          Array.isArray(network?.trains) && network.trains.some((t: any) => t.id === id)
        );
      case 'block':
        return !(network?.blocks && id in network.blocks);
      case 'signal':
        return !(network?.signals && id in network.signals);
      case 'switch':
        return !(network?.switches && id in network.switches);
      case 'platform':
        return !(
          Array.isArray(network?.platforms) &&
          network.platforms.some((p: any) => p.platform_id === id)
        );
      case 'station-group':
        return !(
          Array.isArray(network?.station_groups) &&
          network.station_groups.some((s: any) => s.station_id === id)
        );
      default:
        return false;
    }
  }

  /** Drop persisted tabs whose entity no longer exists in the loaded network,
   *  re-point the active tab, and persist if anything changed. */
  pruneStaleSecondaryTabs(): void {
    if (this.secondaryTabs.length === 0) {
      return;
    }
    const network = this.editor.cachedNetwork;
    const kept = this.secondaryTabs.filter((tab) => !this.isSecondaryTabStale(tab, network));
    if (kept.length === this.secondaryTabs.length) {
      return;
    }

    this.secondaryTabs = kept;
    if (kept.length === 0) {
      this.activeSecondaryTabId = null;
    } else if (!kept.some((tab) => tab.tabId === this.activeSecondaryTabId)) {
      this.activeSecondaryTabId = kept[0].tabId;
    }
    this.persistSecondaryInspectorState();
    this.renderSecondaryTabs();
    this.updateSecondaryNavigationState();
  }

  /** Close the tab pointing at a now-missing entity (matched by type + id).
   *  No-op when no such tab is open; if it was the current editor, hide the
   *  panel so the missing entity doesn't linger on screen. */
  async closeStaleSecondaryTab(editorType: string, editorId: string): Promise<void> {
    const tab = this.secondaryTabs.find(
      (t) => t.editorType === editorType && t.editorId === editorId
    );
    if (tab) {
      await this.closeSecondaryTab(tab.tabId);
      return;
    }
    if (
      this.editor._currentEditorType === editorType &&
      this.editor._currentEditorId === editorId
    ) {
      await this.hideSecondarySidebar();
    }
  }

  clearSecondaryInspectorState(): void {
    this.secondaryTabs = [];
    this.activeSecondaryTabId = null;
    this.currentSecondaryPanel = null;
    try {
      localStorage.removeItem(this.secondaryInspectorStorageKey);
    } catch (e) {}
    this.hideSecondarySidebar();
  }

  cleanupSecondaryPanelSubscriptions(): void {
    // Phase 3-4: Cleanup subscriptions for all handlers
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

  updateSecondaryNavigationState(): void {
    const closeBtn = document.getElementById('close-secondary-sidebar');
    if (!closeBtn) {
      return;
    }
    closeBtn.title = 'Close';

    // `← Trains`-style parent link: shown only when the open panel is a
    // detail view whose parent list exists (DESIGN_LANGUAGE.md §12 — the back
    // button is labeled with its destination).
    const parentBtn = document.getElementById('secondary-nav-parent') as HTMLButtonElement | null;
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

    const pinBtn = document.getElementById('secondary-nav-pin');
    if (pinBtn) {
      pinBtn.classList.toggle('active', this.secondaryPanelPinned);
      pinBtn.title = this.secondaryPanelPinned ? 'Unpin inspector' : 'Pin inspector';
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
      const copyBtn = document.getElementById('copy-json-btn') as HTMLButtonElement | null;
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

  updateSecondaryEditorForTile(track: any): void {
    if (!this.editor._currentEditorType) {
      return;
    }

    switch (this.editor._currentEditorType) {
      case 'signal':
        if (track.signals && track.signals.length > 0) {
          this.editor.showSignalsEditorForTile(track);
        } else {
          this.hideSecondarySidebar();
        }
        break;

      case 'platform':
        if (track.station_id) {
          this.editor.showPlatformEditor(track.station_id);
        } else {
          this.hideSecondarySidebar();
        }
        break;

      case 'station-group':
      case 'station-group-list':
        this.editor.showStationsPanel();
        break;
    }
  }

  initViewFullJSONButton(track: any): void {
    const viewJsonBtn = document.getElementById('view-full-json-btn') as HTMLButtonElement | null;
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
    const body = document.getElementById('track-bits-panel-body');
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
      const total = document.getElementById('bit-total');
      const binary = document.getElementById('bit-binary');
      const forbidden = document.getElementById('bit-forbidden');
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

      await window.app?.refreshNetwork();
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
    const canvas = document.getElementById(canvasId) as HTMLCanvasElement | null;
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

    const segments: Record<number, { edges: [string, string]; label: string; color: string }> = {
      1: { edges: ['N', 'S'], label: 'NS', color: '#04d361' }, // --success-color (straight)
      2: { edges: ['E', 'W'], label: 'EW', color: '#04d361' },
      4: { edges: ['N', 'E'], label: 'NE', color: '#00d4ff' }, // --info-color (diagonal)
      8: { edges: ['N', 'W'], label: 'NW', color: '#00d4ff' },
      16: { edges: ['S', 'E'], label: 'SE', color: '#00d4ff' },
      32: { edges: ['S', 'W'], label: 'SW', color: '#00d4ff' },
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

      ctx.fillStyle = '#121214'; // --bg-dark
      ctx.fillRect(0, 0, size, size);

      ctx.strokeStyle = '#333340'; // --border-color — outer diamond, the quietest line
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
      ctx.strokeStyle = '#5a5a66';
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

          ctx.strokeStyle = seg.color;
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

        ctx.fillStyle = isConnected ? '#04d361' : '#2a2a35'; // --success-color / --bg-hover
        ctx.strokeStyle = isConnected ? '#e1e1e6' : '#5a5a66'; // --text-primary / muted gray
        ctx.lineWidth = isConnected ? 3 : 2;
        ctx.beginPath();
        ctx.arc(pos.x, pos.y, 12, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();

        ctx.fillStyle = isConnected ? '#10101a' : '#a8a8b3'; // dark-on-green / --text-secondary
        ctx.font = 'bold 10px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(name, pos.x, pos.y);
      }

      if (FORBIDDEN_TRACK_BITS.has(currentBits) || isSquareCornerSwitch(currentBits)) {
        ctx.fillStyle = '#e83f5b';
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

  clearPropertiesPanel(): void {
    const panel = document.querySelector('.properties-panel');
    if (panel) {
      panel.innerHTML = `
                <h3>Properties</h3>
                <p class="properties-empty">Select an element to view properties</p>
            `;
    }
    // Also reset the dedicated Track Bits sidebar section.
    this.editor.clearTrackBitsPanel?.();
  }
}
