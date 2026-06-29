// editor-ui.ts — Editor UI Handler (secondary sidebar, working-set tabs,
// collapsible sidebar sections, JSON viewer, bit/visual track editors).
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

/** The floating inspector's current edges, in viewport pixels. */
export interface FloatRect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

/** New inline dimensions for a float resize. Only the fields the drag direction
 *  changes are present — a pure-horizontal drag returns no top/height, etc. */
export interface FloatResizeResult {
  left?: number;
  top?: number;
  width?: number;
  height?: number;
}

/** Pure resize math for the floating inspector's edge/corner handles. Given the
 *  grabbed direction, the window's edges at drag start, and the pointer delta,
 *  return the new inline dimensions. East/south move the right/bottom edge so
 *  the window grows from a fixed left/top; west/north move left/top so the
 *  opposite edge stays put. The window is clamped to at least minW × minH and to
 *  stay within the viewport (a `margin` gap from each side). */
export function computeFloatResize(
  dir: string,
  start: FloatRect,
  dx: number,
  dy: number,
  viewport: { width: number; height: number },
  limits: { minW: number; minH: number; margin: number }
): FloatResizeResult {
  const { minW, minH, margin } = limits;
  const out: FloatResizeResult = {};

  if (dir.includes('e')) {
    const right = Math.min(viewport.width - margin, Math.max(start.left + minW, start.right + dx));
    out.width = Math.round(right - start.left);
  } else if (dir.includes('w')) {
    const left = Math.max(margin, Math.min(start.right - minW, start.left + dx));
    out.left = Math.round(left);
    out.width = Math.round(start.right - left);
  }

  if (dir.includes('s')) {
    const bottom = Math.min(viewport.height - margin, Math.max(start.top + minH, start.bottom + dy));
    out.height = Math.round(bottom - start.top);
  } else if (dir.includes('n')) {
    const top = Math.max(margin, Math.min(start.bottom - minH, start.top + dy));
    out.top = Math.round(top);
    out.height = Math.round(start.bottom - top);
  }

  return out;
}

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
    const closeBtn = this.editor.$id('close-secondary-sidebar');
    if (closeBtn) {
      closeBtn.addEventListener('click', async () => {
        if (this.activeSecondaryTabId) {
          await this.closeSecondaryTab(this.activeSecondaryTabId);
          return;
        }
        await this.hideSecondarySidebar();
      });
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
        this.secondaryPanelPinned = !this.secondaryPanelPinned;
        this.updateSecondaryNavigationState();
        this.persistSecondaryInspectorState();
      });
    }

    const tearoffBtn = this.editor.$id('secondary-nav-tearoff');
    if (tearoffBtn) {
      tearoffBtn.addEventListener('click', () => this.toggleInspectorFloat());
    }
    this.setupInspectorFloatDrag();
    this.setupInspectorFloatResize();

    const tabsEl: HTMLElement | null = this.editor.$id('secondary-sidebar-tabs');
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

        const pinTabBtn = (event.target as HTMLElement).closest(
          '.secondary-tab-pin[data-tab-id]'
        ) as HTMLElement | null;
        if (pinTabBtn) {
          event.stopPropagation();
          const tabId = pinTabBtn.dataset.tabId;
          if (tabId) {
            this.pinTab(tabId);
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

      // Double-click a preview tab to keep it (pin).
      tabsEl.addEventListener('dblclick', (event) => {
        const tabEl = (event.target as HTMLElement).closest(
          '.secondary-tab[data-tab-id]'
        ) as HTMLElement | null;
        if (tabEl && tabEl.dataset.tabId) {
          this.pinTab(tabEl.dataset.tabId);
        }
      });
    }

    this.updateSecondaryNavigationState();
    this.loadSecondaryInspectorState();
    setTimeout(async () => {
      await this.restoreSecondaryInspectorState();
    }, 0);
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
    const sidebar = this.editor.$id('secondary-sidebar');
    const titleEl = this.editor.$id('secondary-sidebar-title');
    const contentEl = this.editor.$id('secondary-sidebar-content');

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

  /** Toggle the whole inspector between docked (left) and a floating, draggable
   *  window. It stays the same live, editable panel either way — only its
   *  position changes, so every editor and subscription keeps working. */
  toggleInspectorFloat(): void {
    const sidebar = this.editor.$id('secondary-sidebar');
    if (!sidebar) {
      return;
    }
    const floating = sidebar.classList.toggle('secondary-sidebar--floating');
    if (floating) {
      if (!sidebar.style.left) {
        // Open clear of the floating tool capsule on the left (right edge ~152).
        sidebar.style.left = '168px';
        sidebar.style.top = '84px';
      }
      this._clampInspectorFloat();
    } else {
      sidebar.style.left = '';
      sidebar.style.top = '';
      // Drop the floating height so the docked panel flows in the column again;
      // width is left intact (carried over from the floating window's last size).
      sidebar.style.height = '';
      sidebar.style.maxHeight = '';
    }
    this._updateTearoffButton(floating);
    this.persistSecondaryInspectorState();
  }

  /** Drag the floating inspector by its header. No-op while docked. Bound once. */
  setupInspectorFloatDrag(): void {
    const sidebar = this.editor.$id('secondary-sidebar');
    const header = sidebar?.querySelector('.secondary-sidebar-header') as HTMLElement | null;
    if (!sidebar || !header) {
      return;
    }
    let offset: { x: number; y: number } | null = null;
    header.addEventListener('pointerdown', (e: PointerEvent) => {
      if (!sidebar.classList.contains('secondary-sidebar--floating')) return;
      if ((e.target as HTMLElement).closest('button')) return;
      header.setPointerCapture(e.pointerId);
      const r = sidebar.getBoundingClientRect();
      offset = { x: e.clientX - r.left, y: e.clientY - r.top };
    });
    header.addEventListener('pointermove', (e: PointerEvent) => {
      if (!offset) return;
      const x = Math.max(0, Math.min(window.innerWidth - 80, e.clientX - offset.x));
      const y = Math.max(0, Math.min(window.innerHeight - 40, e.clientY - offset.y));
      sidebar.style.left = `${x}px`;
      sidebar.style.top = `${y}px`;
    });
    const end = () => {
      if (offset) {
        offset = null;
        this.persistSecondaryInspectorState();
      }
    };
    header.addEventListener('pointerup', end);
    header.addEventListener('pointercancel', end);
  }

  /** Build the eight edge/corner resize handles for the floating inspector and
   *  wire their drag-to-resize. The handles only act while floating (docked mode
   *  has a fixed width and the handles are hidden by CSS).
   *  East/south grow width/height; west/north also move the left/top edge so the
   *  opposite edge stays put. The window is clamped to stay on screen. Bound once. */
  setupInspectorFloatResize(): void {
    const sidebar = this.editor.$id('secondary-sidebar');
    if (!sidebar) {
      return;
    }
    const DIRECTIONS = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'];
    const MARGIN = 6;
    const MIN_W = 260;
    const MIN_H = 180;

    let active: {
      dir: string;
      startX: number;
      startY: number;
      left: number;
      top: number;
      right: number;
      bottom: number;
    } | null = null;

    const onMove = (e: PointerEvent): void => {
      if (!active) return;
      const res = computeFloatResize(
        active.dir,
        active,
        e.clientX - active.startX,
        e.clientY - active.startY,
        { width: window.innerWidth, height: window.innerHeight },
        { minW: MIN_W, minH: MIN_H, margin: MARGIN }
      );
      if (res.left !== undefined) sidebar.style.left = `${res.left}px`;
      if (res.top !== undefined) sidebar.style.top = `${res.top}px`;
      if (res.width !== undefined) sidebar.style.width = `${res.width}px`;
      if (res.height !== undefined) sidebar.style.height = `${res.height}px`;
    };

    const onEnd = (): void => {
      if (!active) return;
      active = null;
      sidebar.classList.remove('resizing');
      document.body.style.userSelect = '';
      this.persistSecondaryInspectorState();
    };

    for (const dir of DIRECTIONS) {
      const handle = document.createElement('div');
      handle.className = 'secondary-sidebar-resizer';
      handle.dataset.resizeDir = dir;
      handle.addEventListener('pointerdown', (e: PointerEvent) => {
        if (!sidebar.classList.contains('secondary-sidebar--floating')) return;
        e.preventDefault();
        e.stopPropagation();
        handle.setPointerCapture(e.pointerId);
        const r = sidebar.getBoundingClientRect();
        active = {
          dir,
          startX: e.clientX,
          startY: e.clientY,
          left: r.left,
          top: r.top,
          right: r.right,
          bottom: r.bottom,
        };
        // A vertical drag sets an explicit height; lift the auto-height cap so
        // that height — not the CSS max-height — governs from here on.
        if (dir.includes('n') || dir.includes('s')) {
          sidebar.style.maxHeight = 'none';
        }
        sidebar.classList.add('resizing');
        document.body.style.userSelect = 'none';
      });
      handle.addEventListener('pointermove', onMove);
      handle.addEventListener('pointerup', onEnd);
      handle.addEventListener('pointercancel', onEnd);
      sidebar.appendChild(handle);
    }
  }

  _clampInspectorFloat(): void {
    const sidebar = this.editor.$id('secondary-sidebar');
    if (!sidebar || !sidebar.classList.contains('secondary-sidebar--floating')) {
      return;
    }
    const margin = 8;
    const r = sidebar.getBoundingClientRect();
    let x = parseFloat(sidebar.style.left) || r.left;
    let y = parseFloat(sidebar.style.top) || r.top;
    if (x + r.width > window.innerWidth - margin) x = Math.max(margin, window.innerWidth - r.width - margin);
    if (y + r.height > window.innerHeight - margin) y = Math.max(margin, window.innerHeight - r.height - margin);
    sidebar.style.left = `${x}px`;
    sidebar.style.top = `${y}px`;
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
    const sidebar = this.editor.$id('secondary-sidebar');
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
      // A casual single-entity inspect opens as a preview (reuses one slot);
      // lists and editors open as their own kept tab. See pinTab to promote.
      pinned: !PREVIEW_ELIGIBLE_EDITOR_TYPES.has(type || ''),
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

    // Preview reuse: a casual inspect (unpinned) replaces the existing preview
    // slot instead of stacking a new tab, so clicking around the layout never
    // piles up tabs. Pinned tabs always keep their own slot.
    if (!nextTab.pinned) {
      const previewIndex = this.secondaryTabs.findIndex((tab) => !tab.pinned);
      if (previewIndex >= 0) {
        this.secondaryTabs[previewIndex] = nextTab;
        this.activeSecondaryTabId = nextTab.tabId;
        this.persistSecondaryInspectorState();
        return;
      }
    }

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
  getSecondaryTabGroup(editorType: string | null): { key: string; label: string; icon: string } {
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

  renderSecondaryTabs(): void {
    const tabsEl: HTMLElement | null = this.editor.$id('secondary-sidebar-tabs');
    if (!tabsEl) {
      return;
    }

    if (this.secondaryTabs.length === 0) {
      tabsEl.innerHTML = '';
      return;
    }

    const renderRow = (tab: any): string => {
      const { label, tooltip } = this.getSecondaryTabDisplay(tab);
      const safeLabel = this.escapeHtml(label);
      const safeTooltip = this.escapeHtml(tooltip);
      const activeClass = tab.tabId === this.activeSecondaryTabId ? ' active' : '';
      const listClass = LIST_PANEL_EDITOR_TYPES.has(tab.editorType) ? ' secondary-tab--list' : '';
      const previewClass = tab.pinned ? '' : ' secondary-tab--preview';
      const iconClass = this.getTabIconClass(tab.editorType);
      const pinButton = tab.pinned
        ? ''
        : `<button class="secondary-tab-pin" data-tab-id="${tab.tabId}" title="Pin tab (keep open)" aria-label="Pin tab"><i class="ti ti-pin" aria-hidden="true"></i></button>`;
      return (
        `<div class="secondary-tab${activeClass}${listClass}${previewClass}" data-tab-id="${tab.tabId}" title="${safeTooltip}">` +
        `<i class="ti ${iconClass} secondary-tab-icon" aria-hidden="true"></i>` +
        `<button class="secondary-tab-label btn-size-tight" data-tab-id="${tab.tabId}">${safeLabel}</button>` +
        pinButton +
        `<button class="secondary-tab-close" data-tab-id="${tab.tabId}" title="Close tab">×</button>` +
        `</div>`
      );
    };

    // Group the open tabs by top-level entity type: a type → instance tree so
    // many open entities stay legible instead of one mixed flat strip.
    const groups = new Map<string, { label: string; icon: string; tabs: any[] }>();
    for (const tab of this.secondaryTabs) {
      const g = this.getSecondaryTabGroup(tab.editorType);
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

  /** Promote a preview tab to a kept (pinned) tab so the next inspect no longer
   *  replaces it. Triggered by the tab's pin button or a double-click. */
  pinTab(tabId: string): void {
    const tab = this.secondaryTabs.find((t) => t.tabId === tabId);
    if (!tab || tab.pinned) {
      return;
    }
    tab.pinned = true;
    this.renderSecondaryTabs();
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
      pinned: !!tab.pinned,
    }));

    const sidebar = this.editor.$id('secondary-sidebar');
    const isFloating = !!(sidebar && sidebar.classList.contains('secondary-sidebar--floating'));
    const payload = {
      pinned: this.secondaryPanelPinned,
      activeTabId: this.activeSecondaryTabId,
      tabs,
      floating: isFloating,
      floatX: isFloating && sidebar ? sidebar.style.left : '',
      floatY: isFloating && sidebar ? sidebar.style.top : '',
      floatW: isFloating && sidebar ? sidebar.style.width : '',
      floatH: isFloating && sidebar ? sidebar.style.height : '',
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
          // Default to kept: tabs persisted before preview/pin existed have no
          // flag and were deliberately open, so they restore pinned rather than
          // collapsing into the single preview slot. New state is explicit.
          pinned: tab.pinned !== false,
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

      if (parsed?.floating) {
        const sidebar = this.editor.$id('secondary-sidebar');
        if (sidebar) {
          sidebar.classList.add('secondary-sidebar--floating');
          if (parsed.floatX) sidebar.style.left = parsed.floatX;
          if (parsed.floatY) sidebar.style.top = parsed.floatY;
          if (parsed.floatW) sidebar.style.width = parsed.floatW;
          if (parsed.floatH) {
            // Restoring an explicit height means the same auto-height cap lift
            // the live resize applies (see setupInspectorFloatResize).
            sidebar.style.height = parsed.floatH;
            sidebar.style.maxHeight = 'none';
          }
          this._updateTearoffButton(true);
        }
      }
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
    const closeBtn = this.editor.$id('close-secondary-sidebar');
    if (!closeBtn) {
      return;
    }
    closeBtn.title = 'Close';

    // `← Trains`-style parent link: shown only when the open panel is a
    // detail view whose parent list exists (DESIGN_LANGUAGE.md §12 — the back
    // button is labeled with its destination).
    const parentBtn = this.editor.$id('secondary-nav-parent') as HTMLButtonElement | null;
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

    const pinBtn = this.editor.$id('secondary-nav-pin');
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
}
