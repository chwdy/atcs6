// editor-ui.ts — Editor UI Handler (secondary sidebar, tabs/history/breadcrumb,
// collapsible categories, JSON viewer, bit/visual track editors).
//
// Migrated from static/js/editor-ui.js (longterm_todo.md item #7, Wave 2).
// Behavior is unchanged. Owned by the Editor (`new EditorUIHandler(this)`);
// bridged onto window.EditorUIHandler for the still-classic editor.js.

import type { EditorHost } from './types';
import { showConfirmModal } from '../lib/helpers';
import { FORBIDDEN_TRACK_BITS, isSquareCornerSwitch } from '../lib/constants';

export class EditorUIHandler {
  editor: EditorHost;
  secondaryPanelHistoryBack: any[];
  secondaryPanelHistoryForward: any[];
  currentSecondaryPanel: any;
  isHistoryNavigation: boolean;
  isTabNavigation: boolean;
  maxSecondaryHistory: number;
  currentBreadcrumbSegments: any[];
  secondaryTabs: any[];
  activeSecondaryTabId: string | null;
  maxSecondaryTabs: number;
  secondaryPanelPinned: boolean;
  secondaryInspectorStorageKey: string;
  sidebarSectionStorageKey: string;

  constructor(editor: EditorHost) {
    this.editor = editor;
    this.secondaryPanelHistoryBack = [];
    this.secondaryPanelHistoryForward = [];
    this.currentSecondaryPanel = null;
    this.isHistoryNavigation = false;
    this.isTabNavigation = false;
    this.maxSecondaryHistory = 50;
    this.currentBreadcrumbSegments = [];
    this.secondaryTabs = [];
    this.activeSecondaryTabId = null;
    this.maxSecondaryTabs = 8;
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

    const backBtn = document.getElementById('secondary-nav-back');
    if (backBtn) {
      backBtn.addEventListener('click', async () => {
        await this.goBackSecondaryPanel();
      });
    }

    const forwardBtn = document.getElementById('secondary-nav-forward');
    if (forwardBtn) {
      forwardBtn.addEventListener('click', async () => {
        await this.goForwardSecondaryPanel();
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

    const breadcrumb = document.getElementById('secondary-sidebar-breadcrumb');
    if (breadcrumb) {
      breadcrumb.addEventListener('click', async (event) => {
        const target = (event.target as HTMLElement).closest(
          '.secondary-breadcrumb-segment[data-breadcrumb-index]'
        ) as HTMLElement | null;
        if (!target) {
          return;
        }
        const index = parseInt(target.dataset.breadcrumbIndex ?? '', 10);
        if (Number.isNaN(index)) {
          return;
        }
        await this.navigateBreadcrumbSegment(index);
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
      if (
        !this.isHistoryNavigation &&
        !this.isTabNavigation &&
        sidebar.classList.contains('visible') &&
        this.currentSecondaryPanel
      ) {
        const currentKey = `${this.currentSecondaryPanel.editorType || '__none__'}:${this.currentSecondaryPanel.editorId || '__none__'}`;
        const nextKey = `${this.editor._currentEditorType || '__none__'}:${this.editor._currentEditorId || '__none__'}`;
        if (currentKey !== nextKey) {
          this.pushSecondaryHistoryBack(this.currentSecondaryPanel);
          this.secondaryPanelHistoryForward = [];
        }
      }

      titleEl.textContent = title;
      contentEl.innerHTML = content;

      sidebar.classList.remove('compact');
      // The route editor re-adds this after rendering; clearing it here
      // means every other panel resets to the default width.
      sidebar.classList.remove('route-editor-wide');

      sidebar.classList.add('visible');

      this.currentSecondaryPanel = {
        title,
        content,
        editorType: this.editor._currentEditorType,
        editorId: this.editor._currentEditorId,
      };
      this.syncSecondaryTabsWithCurrentPanel();
      this.renderSecondaryTabs();
      this.renderSecondaryBreadcrumb();
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
    }

    this.cleanupSecondaryPanelSubscriptions();

    this.secondaryPanelHistoryBack = [];
    this.secondaryPanelHistoryForward = [];
    this.currentSecondaryPanel = null;
    this.currentBreadcrumbSegments = [];
    this.secondaryTabs = [];
    this.activeSecondaryTabId = null;
    this.renderSecondaryTabs();
    this.renderSecondaryBreadcrumb();
    this.updateSecondaryNavigationState();
    this.persistSecondaryInspectorState();

    this.editor._currentEditorType = null;
    this.editor._currentEditorId = null;

    // Drop the block-direction arrows when the sidebar closes.
    if (this.editor.renderer?.clearBlockDirectionOverlay) {
      this.editor.renderer.clearBlockDirectionOverlay();
    }
  }

  async goBackSecondaryPanel(): Promise<void> {
    if (this.secondaryPanelHistoryBack.length === 0) {
      return;
    }

    const previousPanel = this.secondaryPanelHistoryBack.pop();
    if (this.currentSecondaryPanel) {
      this.secondaryPanelHistoryForward.push(this.currentSecondaryPanel);
    }
    await this.restoreSecondaryHistoryPanel(previousPanel);
    this.persistSecondaryInspectorState();
  }

  async goForwardSecondaryPanel(): Promise<void> {
    if (this.secondaryPanelHistoryForward.length === 0) {
      return;
    }

    const nextPanel = this.secondaryPanelHistoryForward.pop();
    if (this.currentSecondaryPanel) {
      this.pushSecondaryHistoryBack(this.currentSecondaryPanel);
    }
    await this.restoreSecondaryHistoryPanel(nextPanel);
    this.persistSecondaryInspectorState();
  }

  async navigateBreadcrumbSegment(index: number): Promise<void> {
    if (
      !this.currentSecondaryPanel ||
      index < 0 ||
      index >= this.currentBreadcrumbSegments.length
    ) {
      return;
    }
    if (index === this.currentBreadcrumbSegments.length - 1) {
      return;
    }

    const segment = this.currentBreadcrumbSegments[index];
    if (!segment || !segment.editorType) {
      return;
    }

    const targetPanel = {
      title: segment.label,
      editorType: segment.editorType,
      editorId: segment.editorId || null,
      content: '',
    };

    this.pushSecondaryHistoryBack(this.currentSecondaryPanel);
    this.secondaryPanelHistoryForward = [];
    await this.restoreSecondaryHistoryPanel(targetPanel);
    this.persistSecondaryInspectorState();
  }

  pushSecondaryHistoryBack(panelSnapshot: any): void {
    this.secondaryPanelHistoryBack.push(panelSnapshot);
    if (this.secondaryPanelHistoryBack.length > this.maxSecondaryHistory) {
      this.secondaryPanelHistoryBack.shift();
    }
  }

  async restoreSecondaryHistoryPanel(panelSnapshot: any): Promise<void> {
    this.cleanupSecondaryPanelSubscriptions();
    this.isHistoryNavigation = true;
    try {
      const restored = await this.editor.restoreSecondaryPanel(panelSnapshot);
      if (!restored) {
        this.editor._currentEditorType = panelSnapshot.editorType || null;
        this.editor._currentEditorId = panelSnapshot.editorId || null;
        this.showSecondarySidebar(panelSnapshot.title || 'Editor', panelSnapshot.content || '');
      }
    } finally {
      this.isHistoryNavigation = false;
      this.updateSecondaryNavigationState();
    }
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
    if (this.secondaryTabs.length > this.maxSecondaryTabs) {
      const removeIndex = this.secondaryTabs.findIndex(
        (tab) => tab.tabId !== this.activeSecondaryTabId
      );
      if (removeIndex >= 0) {
        this.secondaryTabs.splice(removeIndex, 1);
      } else {
        this.secondaryTabs.shift();
      }
    }
    this.activeSecondaryTabId = nextTab.tabId;
    this.persistSecondaryInspectorState();
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
        const safeTitle = this.escapeHtml(tab.title || 'Editor');
        const activeClass = tab.tabId === this.activeSecondaryTabId ? ' active' : '';
        return (
          `<div class="secondary-tab${activeClass}" title="${safeTitle}">` +
          `<button class="secondary-tab-label btn-size-tight" data-tab-id="${tab.tabId}">${safeTitle}</button>` +
          `<button class="secondary-tab-close" data-tab-id="${tab.tabId}" title="Close tab">×</button>` +
          `</div>`
        );
      })
      .join('');
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
        .slice(0, this.maxSecondaryTabs)
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

    const activeTab = this.secondaryTabs.find((tab) => tab.tabId === this.activeSecondaryTabId);
    if (!activeTab) {
      return;
    }

    await this.restoreSecondaryTabPanel(activeTab);
  }

  clearSecondaryInspectorState(): void {
    this.secondaryTabs = [];
    this.activeSecondaryTabId = null;
    this.currentSecondaryPanel = null;
    this.secondaryPanelHistoryBack = [];
    this.secondaryPanelHistoryForward = [];
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
  }

  renderSecondaryBreadcrumb(): void {
    const breadcrumbEl = document.getElementById('secondary-sidebar-breadcrumb');
    if (!breadcrumbEl) {
      return;
    }

    if (!this.currentSecondaryPanel) {
      breadcrumbEl.innerHTML = '';
      this.currentBreadcrumbSegments = [];
      return;
    }

    const segments = this.editor.getBreadcrumbForEntityView(this.currentSecondaryPanel) || [];
    this.currentBreadcrumbSegments = segments;

    if (segments.length === 0) {
      breadcrumbEl.innerHTML = '';
      return;
    }

    let html = '';
    for (let i = 0; i < segments.length; i++) {
      const segment = segments[i];
      const isLast = i === segments.length - 1;
      const segmentLabel = this.escapeHtml(segment.label || 'Editor');

      if (isLast || !segment.editorType) {
        html += `<span class="secondary-breadcrumb-current">${segmentLabel}</span>`;
      } else {
        html += `<button class="secondary-breadcrumb-segment" data-breadcrumb-index="${i}">${segmentLabel}</button>`;
        html += `<span class="secondary-breadcrumb-separator">></span>`;
      }
    }

    breadcrumbEl.innerHTML = html;
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

    const backBtn = document.getElementById('secondary-nav-back') as HTMLButtonElement | null;
    if (backBtn) {
      backBtn.disabled = this.secondaryPanelHistoryBack.length === 0;
    }

    const forwardBtn = document.getElementById('secondary-nav-forward') as HTMLButtonElement | null;
    if (forwardBtn) {
      forwardBtn.disabled = this.secondaryPanelHistoryForward.length === 0;
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
                        📋 Copy to Clipboard
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
              window.showToast?.('JSON copied to clipboard', 'success', 2000);
              copyBtn.textContent = '✓ Copied!';
              copyBtn.classList.remove('btn-primary');
              copyBtn.classList.add('btn-success');
              setTimeout(() => {
                copyBtn.textContent = '📋 Copy to Clipboard';
                copyBtn.classList.remove('btn-success');
                copyBtn.classList.add('btn-primary');
              }, 2000);
            })
            .catch((err) => {
              console.error('Failed to copy JSON:', err);
              window.showToast?.('Failed to copy JSON', 'error', 3000);
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

      case 'block':
        if (track.block_section_id) {
          this.editor.showBlockEditor(track.block_section_id);
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
        this.showFullJSONViewer(track, `📄 JSON: (${track.x}, ${track.y})`);
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
    const bitEditor = document.getElementById('bit-editor');
    if (!bitEditor) return;

    const api: any = window.api;

    const calculateBitsFromCheckboxes = () => {
      let bits = 0;
      document
        .querySelectorAll<HTMLInputElement>('#bit-editor input[type="checkbox"]')
        .forEach((cb) => {
          if (cb.checked) {
            bits |= parseInt(cb.dataset.bit ?? '');
          }
        });
      return bits;
    };

    const updateBitDisplay = () => {
      const bits = calculateBitsFromCheckboxes();
      const totalSpan = document.getElementById('bit-total');
      const binarySpan = document.getElementById('bit-binary');
      if (totalSpan) totalSpan.textContent = String(bits);
      if (binarySpan) binarySpan.textContent = bits.toString(2).padStart(6, '0');

      document
        .querySelectorAll<HTMLInputElement>('#bit-editor input[type="checkbox"]')
        .forEach((cb) => {
          const label = cb.parentElement as HTMLElement;
          const span = label.querySelector('span') as HTMLElement | null;
          const isChecked = cb.checked;
          label.style.background = isChecked ? '#2a4a3e' : '#2a2a34';
          label.style.borderColor = isChecked ? '#00ff88' : 'transparent';
          if (span) span.style.color = isChecked ? '#00ff88' : '#ccc';
        });

      const warning = document.querySelector('.bit-warning') as HTMLElement | null;
      if (warning) {
        warning.style.display =
          FORBIDDEN_TRACK_BITS.has(bits) || isSquareCornerSwitch(bits) ? 'block' : 'none';
      }
    };

    const checkboxes = bitEditor.querySelectorAll<HTMLInputElement>('input[type="checkbox"]');
    checkboxes.forEach((cb) => {
      cb.addEventListener('change', () => updateBitDisplay());
    });

    const applyButton = document.getElementById('apply-bits') as HTMLButtonElement | null;
    if (applyButton) {
      const applyBits = async (newBits: number, force: boolean) => {
        if (track.isVirtual) {
          const newTrack = {
            x: track.x,
            y: track.y,
            track_bits: newBits,
            directed_paths: [],
          };
          await api.addTracksBulk([newTrack]);
        } else {
          const body: Record<string, unknown> = { track_bits: newBits };
          if (force) body.force = true;
          await api.updateTrack(track.x, track.y, body);
        }

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

        await window.app?.refreshNetwork();
        await this.editor.selectElement({ x: track.x, y: track.y });
      };

      applyButton.onclick = async () => {
        const newBits = calculateBitsFromCheckboxes();

        if (FORBIDDEN_TRACK_BITS.has(newBits)) {
          window.showToast?.('Cannot apply forbidden configuration', 'error', 3000);
          return;
        }
        if (isSquareCornerSwitch(newBits)) {
          window.showToast?.(
            'Invalid switch: a branch is 90° to a connecting segment ' +
              '(square corner). Add a diagonal (NS/EW) leg to ease it.',
            'error',
            5000
          );
          return;
        }

        try {
          await applyBits(newBits, false);
        } catch (err: any) {
          const data = err && err.data;
          if (data && data.requires_confirmation) {
            const proceed = await this._confirmBitChangeImpact(data.impact);
            if (!proceed) return;
            try {
              await applyBits(newBits, true);
            } catch (err2: any) {
              console.error('Failed to update track bits (forced):', err2);
              window.showToast?.('Failed to update track bits: ' + err2.message, 'error', 5000);
            }
            return;
          }
          console.error('Failed to update track bits:', err);
          window.showToast?.('Failed to update track bits: ' + err.message, 'error', 5000);
        }
      };
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
    canvas.width = 200;
    canvas.height = 200;
    const size = 200;
    const half = size / 2;
    const quarter = size / 4;

    const segments: Record<number, { edges: [string, string]; label: string; color: string }> = {
      1: { edges: ['N', 'S'], label: 'NS', color: '#00ff88' },
      2: { edges: ['E', 'W'], label: 'EW', color: '#00ff88' },
      4: { edges: ['N', 'E'], label: 'NE', color: '#00d4ff' },
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

      ctx.fillStyle = '#111';
      ctx.fillRect(0, 0, size, size);

      ctx.strokeStyle = '#333';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(half, 10);
      ctx.lineTo(size - 10, half);
      ctx.lineTo(half, size - 10);
      ctx.lineTo(10, half);
      ctx.closePath();
      ctx.stroke();

      ctx.setLineDash([2, 4]);
      ctx.strokeStyle = '#222';
      ctx.lineWidth = 1;
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

        ctx.fillStyle = isConnected ? '#00ff88' : '#444';
        ctx.strokeStyle = isConnected ? '#fff' : '#666';
        ctx.lineWidth = isConnected ? 3 : 2;
        ctx.beginPath();
        ctx.arc(pos.x, pos.y, 12, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();

        ctx.fillStyle = isConnected ? '#000' : '#ccc';
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

      const numSegments = Object.keys(segments).filter((bit) => currentBits & parseInt(bit)).length;
      ctx.fillStyle =
        FORBIDDEN_TRACK_BITS.has(currentBits) || isSquareCornerSwitch(currentBits)
          ? '#e83f5b'
          : '#aaa';
      ctx.font = 'bold 12px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(
        `${numSegments} segment${numSegments !== 1 ? 's' : ''} (bits: ${currentBits})`,
        half,
        size - 10
      );
    };

    const saveToServer = async () => {
      if (FORBIDDEN_TRACK_BITS.has(currentBits)) {
        console.warn('Cannot save forbidden configuration');
        return;
      }
      if (isSquareCornerSwitch(currentBits)) {
        window.showToast?.(
          'Invalid switch: a branch is 90° to a connecting segment ' +
            '(square corner). Add a diagonal (NS/EW) leg to ease it.',
          'error',
          5000
        );
        return;
      }

      try {
        await this.editor.api.updateTrack(track.x, track.y, { track_bits: currentBits });
        await window.app?.refreshNetwork();
        await this.editor.selectElement({ x: track.x, y: track.y });
      } catch (err) {
        console.error('Failed to update track bits:', err);
      }
    };

    drawEditor();

    canvas.addEventListener('click', async (e) => {
      const rect = canvas.getBoundingClientRect();
      const scaleX = canvas.width / rect.width;
      const scaleY = canvas.height / rect.height;
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

        const checkbox = document.querySelector(
          `#bit-editor input[data-bit="${closestBit}"]`
        ) as HTMLInputElement | null;
        if (checkbox) {
          checkbox.checked = !!(currentBits & closestBit);
          checkbox.dispatchEvent(new Event('change'));
        }

        drawEditor();
        await saveToServer();
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

// Browser-global bridge so the still-classic editor.js can `new
// EditorUIHandler(this)`. Guarded for Node (Vitest). Removed in Wave 3.
if (typeof window !== 'undefined') {
  Object.assign(window as unknown as Record<string, unknown>, { EditorUIHandler });
}
