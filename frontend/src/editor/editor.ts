// editor.ts — Railway Track Editor (the facade/orchestrator).
//
// Migrated from static/js/editor.js (longterm_todo.md item #7, Wave 3).
// Behavior is unchanged. The Editor owns the toolbar, the canvas mouse/keyboard
// interactions, the EditorMode state machine, the properties panel, and the
// 11 entity handlers — which it imports directly as real modules.
//
// app.ts constructs it via `new Editor(renderer, api)` (imported directly).
// `renderer` is typed `any` (the large Renderer surface); `api` is typed against
// the `FullDataClient` data contract (layout + sim), so the layout handlers and
// the sim handlers each draw from a named, swappable surface.

import type { StateManager } from '../lib/state-manager';
import { showToast } from '../lib/toast';
import { showContextMenu, type ContextMenuItem } from '../lib/context-menu';
import type { FullDataClient } from '../lib/layout-data-client';
import {
  ConstructionPathfinder,
  pathToTracks,
  tileSkipsSegment,
  validateBlockPath,
  getValidSignalDirections,
  traceNodeKey,
  type TraceNode,
} from '../lib/pathfinders';
import { graphSearch } from '../lib/graph-search';
import {
  showConfirmModal,
  renderBitToggle,
  getBlockStateColor,
  applyStructuralLock,
} from '../lib/helpers';
import { FORBIDDEN_TRACK_BITS, isSquareCornerSwitch } from '../lib/constants';
import { BlockEditorHandler } from './editor-blocks';
import { SignalEditorHandler } from './editor-signals';
import { PlatformEditorHandler, StationGroupEditorHandler } from './editor-stations';
// Sim-editor handlers are provided by the SimEditorPlugin (sim-plugin.ts), not
// instantiated by the core editor — imported as types only so a layout-only
// build does not pull in their code.
import type { TrainEditorHandler } from './editor-trains';
import type { PathEditorHandler } from './editor-paths';
import type { RouteEditorHandler } from './editor-routes';
import type { RouteRunEditorHandler } from './editor-route-runs';
import { SwitchEditorHandler } from './editor-switches';
import { TunnelEditorHandler } from './editor-tunnels';
import type { SpeedProfileEditorHandler } from './editor-speed-profiles';
import { EditorUIHandler } from './editor-ui';
import { MultiSelectActionsHandler } from './editor-multi-select';
import { showResourcePicker, dismissResourcePicker } from './resource-picker';
import { renderToolGroups } from './tool-registry';
import type { ToolContribution, ToolMode } from './tool-registry';

/**
 * A plugin that augments the editor with extra handlers and tools. The sim
 * (trains, paths, routes, route-runs, speed profiles) is registered this way
 * (see sim-plugin.ts), keeping the core editor a layout-only editor.
 */
export interface EditorPlugin {
  register(editor: Editor): void;
  /**
   * Tool-palette contributions this plugin adds. Merged with the core editor's
   * tools and rendered into the toolbar (see Editor.renderToolbar). Optional: a
   * behavior-only plugin may register handlers without contributing any tools.
   */
  tools?(editor: Editor): ToolContribution[];
}

/**
 * The host the editor calls back into. The editor never reaches a global
 * (`window.app`) directly — the host injects this so the editor is portable to
 * any host that can re-fetch the network and (optionally) surface sim state.
 */
export interface HostApp {
  refreshNetwork(): Promise<void> | void;
  currentNetwork?: any;
  isSimulationRunning?: boolean;
  selectTrain?(trainId: string): void;
  updateTrainDebugPanel?(train: any): void;
}

export class Editor {
  renderer: any;
  api: FullDataClient;
  selectedTool: string;
  isPanning: boolean;
  isDraggingBuild: boolean;
  dragStart: any;
  dragEnd: any;
  previewPath: any;
  previewTracks: any = null;
  // Any drag tool: set when the current drag has no buildable route, so the
  // preview shows a red attempt-line and commitDragBuild lays nothing.
  previewInvalid: boolean = false;
  lastMouseX: number;
  lastMouseY: number;
  selectedElement: any;
  cachedNetwork: any;
  cachedBlocks: any[];
  selectedBlockId: string | null;
  hoverPosition: any;
  hoverValid: boolean;
  previewSignalDirection: string | null;
  previewSignalDirectionIndex: number;
  validSignalDirections: string[] = [];
  avoidConnectMode: boolean;
  forcedStartSegment: string | null;
  forcedEndSegment: string | null;
  pathToolState: any;
  cachedPaths: any[];
  boxSelectState: any;
  lassoSelectState: any;
  paintSelectState: any;
  traceSelectState: any;
  activeMode: any;
  pathfinder: ConstructionPathfinder;
  blockHandler: BlockEditorHandler;
  signalHandler: SignalEditorHandler;
  platformHandler: PlatformEditorHandler;
  stationGroupHandler: StationGroupEditorHandler;
  // Sim handlers — present only when the SimEditorPlugin is registered.
  trainHandler?: TrainEditorHandler;
  pathHandler?: PathEditorHandler;
  routeHandler?: RouteEditorHandler;
  routeRunHandler?: RouteRunEditorHandler;
  switchHandler: SwitchEditorHandler;
  tunnelEditorHandler: TunnelEditorHandler;
  speedProfileHandler?: SpeedProfileEditorHandler;
  uiHandler: EditorUIHandler;
  multiSelectHandler: MultiSelectActionsHandler;
  _plugins: EditorPlugin[] = [];
  // Tool id → contribution, rebuilt on every renderToolbar(). The delegated
  // toolbar click handler looks up the clicked tool here to dispatch by kind.
  _toolsById: Map<string, ToolContribution> = new Map();
  // Which mode the palette renders for. The host (app.ts) sets this on the
  // editor/operation toggle; tools whose availableIn excludes it are hidden.
  editorMode: ToolMode = 'edit';
  // Injected by the host (app.ts) — the editor calls these instead of reaching
  // the `window.app` / `stateManager` globals, so it is portable to any host.
  host?: HostApp;
  stateManager?: StateManager;
  // Where the editor looks up its DOM. Defaults to the document; a host embedding
  // the editor sets a container element to scope (and isolate) its panels.
  root: ParentNode = document;
  _currentEditorType: string | null;
  _currentEditorId: string | null;
  // Assigned lazily by event handlers.
  hasPanned = false;
  panStart: any = null;
  signalValidationTimeout: any = null;
  _lastValidatedPosition: any = null;
  _pendingTunnelEndpoint: any = null;
  // Resolves on the first setCachedNetwork; deferred init awaits it so it can
  // validate against the loaded scene. See whenNetworkReady.
  _networkReady: Promise<void>;
  _resolveNetworkReady: (() => void) | null = null;

  constructor(renderer: any, api: FullDataClient) {
    this.renderer = renderer;
    this.api = api;
    this.selectedTool = 'inspect';
    this.isPanning = false;
    this.isDraggingBuild = false;
    this.dragStart = null;
    this.dragEnd = null;
    this.previewPath = null;
    this.lastMouseX = 0;
    this.lastMouseY = 0;
    this.selectedElement = null;
    this.cachedNetwork = null;
    // Resolves the first time a network load populates cachedNetwork.
    // Deferred init (restoring persisted inspector tabs) awaits this so it
    // checks each tab's entity against the loaded scene instead of firing a
    // request for something this scene may not have.
    this._networkReady = new Promise<void>((resolve) => {
      this._resolveNetworkReady = resolve;
    });
    this.cachedBlocks = []; // Block Manager cache
    this.selectedBlockId = null; // Currently selected block in Block Manager

    // Hover preview for signal/platform tools
    this.hoverPosition = null;
    this.hoverValid = false;
    this.previewSignalDirection = null;
    this.previewSignalDirectionIndex = 0;

    // Shift-key avoid-connect mode.
    this.avoidConnectMode = false;

    // R/T-key forced-segment override for track drag.
    this.forcedStartSegment = null;
    this.forcedEndSegment = null;

    // Route tool state
    this.pathToolState = {
      active: false,
      startBlockId: null,
      startPosition: null,
      reachableEndpoints: [],
      reachablePositions: new Set(),
      priority: 5,
      trainId: null,
    };
    this.cachedPaths = []; // List of active routes

    // Box/lasso/paint select state.
    this.boxSelectState = {
      selectedKeys: new Set(),
      dragScreenStart: null,
      dragScreenEnd: null,
      isDragging: false,
      shiftRemoving: false,
    };
    this.lassoSelectState = {
      screenPoints: [],
      isDrawing: false,
      shiftRemoving: false,
    };
    this.paintSelectState = {
      isPainting: false,
      shiftRemoving: false,
      lastKey: null,
    };
    // Trace select: remembers the first clicked track while waiting for the second.
    this.traceSelectState = {
      firstPoint: null,
    };

    // Active editor mode (EditorMode contract documented in the original).
    this.activeMode = null;

    this.pathfinder = new ConstructionPathfinder();
    this.blockHandler = new BlockEditorHandler(this);
    this.signalHandler = new SignalEditorHandler(this);
    this.platformHandler = new PlatformEditorHandler(this);
    this.stationGroupHandler = new StationGroupEditorHandler(this);
    // Sim handlers (train/path/route/routeRun/speedProfile) are instantiated by
    // the SimEditorPlugin via registerPlugin(), not here — see sim-plugin.ts.
    this.switchHandler = new SwitchEditorHandler(this);
    this.tunnelEditorHandler = new TunnelEditorHandler(this);
    this.uiHandler = new EditorUIHandler(this);
    this.multiSelectHandler = new MultiSelectActionsHandler(this);

    this.setupEventListeners();
    // Keep the status-bar zoom % in sync with the camera no matter who changes
    // the scale (zoom buttons, wheel, Fit, initial framing). The camera emits
    // onChange on every viewport mutation.
    this.renderer.camera.onChange(() => this._syncZoomLabel());
    this.setupBlockManager();
    this._currentEditorType = null; // 'block', 'signal', or 'platform'
    this._currentEditorId = null; // The ID of the item being edited

    this.setupSecondarySidebar();
  }

  // === Secondary Sidebar Management ===

  /**
   * Register a plugin that augments the editor (e.g. the SimEditorPlugin). The
   * plugin instantiates its handlers and assigns them onto the editor. Call
   * after construction, before the first network refresh.
   */
  registerPlugin(plugin: EditorPlugin): void {
    this._plugins.push(plugin);
    plugin.register(this);
    // A plugin may contribute tools; re-render so they appear. The constructor
    // renders the core tools first, then app.ts registers the sim plugin, which
    // lands here and re-renders with the sim tools merged in. renderToolbar
    // no-ops until its mount exists, so this is safe pre-construction too.
    this.renderToolbar();
  }

  /** Scoped element-by-id lookup within `root` (matches document.getElementById). */
  $id(id: string): HTMLElement | null {
    return this.root.querySelector<HTMLElement>(`#${CSS.escape(id)}`);
  }

  /** Scoped querySelector within `root`. */
  $q<E extends Element = Element>(selector: string): E | null {
    return this.root.querySelector<E>(selector);
  }

  setupSecondarySidebar(): any {
    return this.uiHandler.setupSecondarySidebar();
  }

  // === Collapsible Sidebar Sections ===

  setupCollapsibleSidebarSections(): any {
    return this.uiHandler.setupCollapsibleSidebarSections();
  }

  showSecondarySidebar(title: string, content: string): any {
    // Any editor opening clears a stale block-direction focus and a stale
    // tunnel highlight. The tunnel handler re-sets its highlight right after
    // this call when it is the panel being opened.
    if (this.renderer?.clearBlockDirectionFocus) {
      this.renderer.clearBlockDirectionFocus();
    }
    this.renderer?.setSelectedTunnel?.(null);
    // Opening any panel other than Add Train drops a lingering train-placement
    // ghost and exits the place-train mode, so neither can outlive its panel.
    if (title !== 'Add Train') {
      this.renderer?.setTrainGhostPreview?.(null);
      if (this.activeMode?.toolName === 'place-train') this.exitMode();
    }
    return this.uiHandler.showSecondarySidebar(title, content);
  }

  // Re-run the structural lock on the open inspector after the editor mode
  // flips. Each structural panel already locks itself on render; this catches
  // the case where the user toggles Editor <-> Simulation while a panel stays
  // open. Operational panels (path / route / train / speed profile) are not in
  // the set — the dispatcher edits those freely while simulating.
  reapplyStructuralLock(): void {
    const STRUCTURAL = new Set([
      'block',
      'switch',
      'signal',
      'tunnel',
      'platform',
      'station-group-list',
      'station-group-detail',
    ]);
    if (this._currentEditorType && STRUCTURAL.has(this._currentEditorType)) {
      applyStructuralLock(this.$id('secondary-sidebar-content'));
    }
  }

  hideSecondarySidebar(options: any = {}): any {
    // uiHandler.hideSecondarySidebar() takes no args (it ignored `options` in
    // the classic code too); keep the param on the facade for callers.
    this.renderer?.setSelectedTunnel?.(null);
    return this.uiHandler.hideSecondarySidebar();
  }

  navigateToParentList(): any {
    return this.uiHandler.navigateToParentList();
  }

  closeActiveSecondaryTab(): any {
    return this.uiHandler.closeActiveSecondaryTab();
  }

  async restoreSecondaryPanel(snapshot: any): Promise<boolean> {
    const type = snapshot?.editorType;
    const id = snapshot?.editorId;

    switch (type) {
      case 'block':
        if (!id) return false;
        await this.showBlockEditor(id);
        return true;
      case 'signal':
        if (!id) return false;
        await this.showSignalEditor(id);
        return true;
      case 'platform':
        if (!id) return false;
        await this.showPlatformEditor(id);
        return true;
      case 'switch':
        if (!id) return false;
        await this.showSwitchEditor(id);
        return true;
      case 'switches-list':
        await this.showSwitchesPanel();
        return true;
      case 'train-control':
        if (!id) return false;
        await this.showTrainControlPanel(id);
        return true;
      case 'trains-list':
        await this.showTrainsPanel();
        return true;
      case 'paths-list':
        await this.showPathsPanel();
        return true;
      case 'path':
        if (!id) return false;
        await this.showPathEditor(id);
        return true;
      case 'path-setup':
        this.showRoutePrioritySelector();
        return true;
      case 'extend-path-setup':
        this.showExtendRoutePanel();
        return true;
      case 'station-group-detail':
        if (!id) return false;
        await this.stationGroupHandler.showStationDetail(id);
        return true;
      case 'station-group':
      case 'station-group-list':
        await this.showStationsPanel();
        return true;
      case 'routes-list':
        await this.showRoutesPanel();
        return true;
      case 'route-editor':
        if (!id) return false;
        await this.routeHandler?.showRouteEditor(id);
        return true;
      case 'route-runs-list':
        await this.routeRunHandler?.showRouteRunsPanel();
        return true;
      case 'route-run-editor':
        if (!id) return false;
        await this.routeRunHandler?.showRouteRunEditor(parseInt(id, 10));
        return true;
      case 'speed-profiles-list':
        await this.showSpeedProfilesPanel();
        return true;
      case 'speed-profile':
        if (!id) return false;
        await this.showSpeedProfileEditor(id);
        return true;
      default:
        return false;
    }
  }

  /** Lineage of a panel snapshot, list-first: `[{label, editorType, editorId}]`.
   *  One entry for list panels; `[parent list, entity]` for detail panels. The
   *  last entry's label is the panel's entity-titled tab label; the first
   *  entry, when navigable (non-null editorType), backs the header's
   *  `← Parent` button. A null-editorType parent is context only — blocks /
   *  signals / platforms have no list panel to go to. */
  getPanelLineage(entityView: any): any[] {
    const type = entityView?.editorType || entityView?.type || null;
    const id = entityView?.editorId || entityView?.id || null;

    switch (type) {
      case 'block':
        return [
          { label: 'Blocks', editorType: null, editorId: null },
          { label: id || 'Block', editorType: 'block', editorId: id },
        ];
      case 'signal':
        return [
          { label: 'Signals', editorType: null, editorId: null },
          { label: id || 'Signal', editorType: 'signal', editorId: id },
        ];
      case 'platform':
        return [
          { label: 'Platforms', editorType: null, editorId: null },
          { label: id || 'Platform', editorType: 'platform', editorId: id },
        ];
      case 'switches-list':
        return [{ label: 'Switches', editorType: 'switches-list', editorId: null }];
      case 'switch':
        return [
          { label: 'Switches', editorType: 'switches-list', editorId: null },
          { label: id || 'Switch', editorType: 'switch', editorId: id },
        ];
      case 'trains-list':
        return [{ label: 'Trains', editorType: 'trains-list', editorId: null }];
      case 'train-control':
        return [
          { label: 'Trains', editorType: 'trains-list', editorId: null },
          { label: id || 'Train', editorType: 'train-control', editorId: id },
        ];
      case 'paths-list':
        return [{ label: 'Paths', editorType: 'paths-list', editorId: null }];
      case 'path':
        return [
          { label: 'Paths', editorType: 'paths-list', editorId: null },
          { label: id || 'Path', editorType: 'path', editorId: id },
        ];
      case 'path-setup':
        return [
          { label: 'Paths', editorType: 'paths-list', editorId: null },
          { label: 'Create Path', editorType: 'path-setup', editorId: null },
        ];
      case 'extend-path-setup':
        return [
          { label: 'Paths', editorType: 'paths-list', editorId: null },
          { label: 'Extend Path', editorType: 'extend-path-setup', editorId: id || null },
        ];
      case 'routes-list':
        return [{ label: 'Routes', editorType: 'routes-list', editorId: null }];
      case 'route-editor':
        return [
          { label: 'Routes', editorType: 'routes-list', editorId: null },
          { label: id || 'Route', editorType: 'route-editor', editorId: id },
        ];
      case 'route-runs-list':
        return [{ label: 'Route Runs', editorType: 'route-runs-list', editorId: null }];
      case 'route-run-editor':
        return [
          { label: 'Route Runs', editorType: 'route-runs-list', editorId: null },
          { label: id ? `#${id}` : 'Run', editorType: 'route-run-editor', editorId: id },
        ];
      case 'station-group-detail':
        return [
          { label: 'Stations', editorType: 'station-group-list', editorId: null },
          { label: id || 'Station', editorType: 'station-group-detail', editorId: id },
        ];
      case 'station-group-list':
      case 'station-group':
        return [{ label: 'Stations', editorType: 'station-group-list', editorId: null }];
      case 'speed-profiles-list':
        return [{ label: 'Speed Profiles', editorType: 'speed-profiles-list', editorId: null }];
      case 'speed-profile':
        return [
          { label: 'Speed Profiles', editorType: 'speed-profiles-list', editorId: null },
          { label: id || 'Profile', editorType: 'speed-profile', editorId: id },
        ];
      default:
        return [{ label: entityView?.title || 'Editor', editorType: type, editorId: id }];
    }
  }

  showFullJSONViewer(data: any, title = 'JSON Viewer'): any {
    return this.uiHandler.showFullJSONViewer(data, title);
  }

  syntaxHighlightJSON(jsonString: string): any {
    return this.uiHandler.syntaxHighlightJSON(jsonString);
  }

  updateSecondaryEditorForTile(track: any): any {
    return this.uiHandler.updateSecondaryEditorForTile(track);
  }

  async showSignalsEditorForTile(track: any): Promise<any> {
    return this.signalHandler.showSignalsEditorForTile(track);
  }

  /** Validate that the currently open editor still refers to a valid item. */
  validateCurrentEditor(): void {
    if (!this._currentEditorType) {
      return;
    }

    const id = this._currentEditorId;
    let itemExists: any = false;

    switch (this._currentEditorType) {
      case 'block':
        if (!id) return;
        itemExists = this.cachedNetwork?.blocks && id in this.cachedNetwork.blocks;
        break;
      case 'signal':
        if (!id) return;
        itemExists = this.cachedNetwork?.signals && id in this.cachedNetwork.signals;
        break;
      case 'platform':
        if (!id) return;
        itemExists =
          Array.isArray(this.cachedNetwork?.platforms) &&
          this.cachedNetwork.platforms.some((p: any) => p.platform_id === id);
        break;
      case 'station-group':
      case 'station-group-detail':
        if (!id) return;
        itemExists =
          Array.isArray(this.cachedNetwork?.station_groups) &&
          this.cachedNetwork.station_groups.some((s: any) => s.station_id === id);
        break;
      case 'train':
      case 'train-control':
        if (!id) return;
        itemExists =
          Array.isArray(this.cachedNetwork?.trains) &&
          this.cachedNetwork.trains.some((t: any) => t.id === id);
        break;
      case 'switch':
        if (!id) return;
        itemExists = this.cachedNetwork?.switches && id in this.cachedNetwork.switches;
        break;
      case 'trains-list':
      case 'path-setup':
      case 'extend-path-setup':
      case 'paths-list':
      case 'routes-list':
      case 'route-runs-list':
      case 'speed-profiles-list':
      case 'switches-list':
        itemExists = true;
        break;
      case 'path':
      case 'route-editor':
      case 'route-run-editor':
      case 'speed-profile':
        if (!id) return;
        itemExists = true;
        break;
    }

    if (!itemExists) {
      this.hideSecondarySidebar();
    }
  }

  /** Refresh the secondary editor if it matches the given type and id. */
  refreshEditorIfNeeded(type: string, id: string): void {
    if (this._currentEditorType !== type || this._currentEditorId !== id) {
      return;
    }

    const activeElement = document.activeElement;
    if (
      activeElement &&
      (activeElement.tagName === 'INPUT' ||
        activeElement.tagName === 'TEXTAREA' ||
        activeElement.tagName === 'SELECT')
    ) {
      return;
    }

    switch (type) {
      case 'block':
        this.showBlockEditor(id);
        break;
      case 'signal':
        this.showSignalEditor(id);
        break;
      case 'platform':
        this.showPlatformEditor(id);
        break;
    }
  }

  /** Force refresh the currently open secondary editor. */
  refreshCurrentEditor(): void {
    if (!this._currentEditorType || !this._currentEditorId) {
      return;
    }

    const activeElement = document.activeElement;
    if (
      activeElement &&
      (activeElement.tagName === 'INPUT' ||
        activeElement.tagName === 'TEXTAREA' ||
        activeElement.tagName === 'SELECT')
    ) {
      return;
    }

    if (this._currentEditorType === 'train-control') {
      if (this.host?.isSimulationRunning) return;
      this.trainHandler?.updateTrainControlPanelValues(this._currentEditorId);
      return;
    }

    if (
      this._currentEditorType === 'signal' ||
      this._currentEditorType === 'switch' ||
      this._currentEditorType === 'block' ||
      this._currentEditorType === 'platform' ||
      this._currentEditorType === 'station-group' ||
      this._currentEditorType === 'station-group-detail'
    ) {
      return;
    }
  }

  async showBlockEditor(blockId: any): Promise<any> {
    return this.blockHandler.showEditor(blockId);
  }

  showPlatformEditor(platformId: any): any {
    return this.platformHandler.showEditor(platformId);
  }

  async showSwitchEditor(switchId: any): Promise<any> {
    return this.switchHandler.showEditor(switchId);
  }

  async showStationsPanel(): Promise<any> {
    return this.stationGroupHandler.showPanel();
  }

  async initTrainEditorHandlers(trainId: any): Promise<any> {
    return this.trainHandler?.initTrainControlHandlers(trainId);
  }

  showAddTrainDialog(): any {
    return this.trainHandler?.showAddTrainDialog();
  }

  async showTrainsPanel(): Promise<any> {
    return this.trainHandler?.showTrainsPanel();
  }

  async showSwitchesPanel(): Promise<any> {
    return this.switchHandler.showSwitchesPanel();
  }

  async showSpeedProfilesPanel(): Promise<any> {
    return this.speedProfileHandler?.showProfilesPanel();
  }

  async showSpeedProfileEditor(profileId: string): Promise<any> {
    return this.speedProfileHandler?.showProfileEditor(profileId);
  }

  async showTrainControlPanel(trainId: any): Promise<any> {
    return this.trainHandler?.showTrainControlPanel(trainId);
  }

  // Tools that activate a multi-step EditorMode.
  /**
   * The layout tools the core editor always provides: inspect, the selection
   * tools, and the network / platform / block build tools. Simulation tools
   * (path, train, route) are contributed by the SimEditorPlugin, not here, so a
   * layout-only host gets a clean palette.
   */
  coreTools(): ToolContribution[] {
    return [
      {
        id: 'inspect', label: 'Inspect', icon: 'ti ti-pointer',
        title: 'Inspect — click a tile to view its properties',
        group: null, availableIn: ['edit', 'sim'], kind: { type: 'tool' },
      },

      {
        id: 'trace', label: 'Trace', icon: 'ti ti-line',
        title: 'Trace (click two tracks to select the connecting path. Space clears, Delete removes tracks)',
        group: 'Selection', availableIn: ['edit'],
        kind: { type: 'mode', factory: () => this._buildTraceSelectMode() },
      },
      {
        id: 'box-select', label: 'Box', icon: 'ti ti-box-multiple',
        title: 'Box Select (drag a rectangle to mark tracks. Shift = remove. Space clears, Delete removes tracks)',
        group: 'Selection', availableIn: ['edit'],
        kind: { type: 'mode', factory: () => this._buildBoxSelectMode() },
      },
      {
        id: 'lasso-select', label: 'Lasso', icon: 'ti ti-lasso',
        title: 'Lasso Select (draw a shape around tracks to mark them. Shift = remove. Space clears, Delete removes tracks)',
        group: 'Selection', availableIn: ['edit'],
        kind: { type: 'mode', factory: () => this._buildLassoSelectMode() },
      },
      {
        id: 'paint-select', label: 'Brush', icon: 'ti ti-brush',
        title: 'Brush Select (click or drag to paint tracks into the selection. Shift = remove. Space clears, Delete removes tracks)',
        group: 'Selection', availableIn: ['edit'],
        kind: { type: 'mode', factory: () => this._buildPaintSelectMode() },
      },
      {
        id: 'entity-select', label: 'Entity', icon: 'ti ti-click',
        title: 'Entity Select (click a tile, then pick a block / platform / station — or all of them — to add its tracks to the selection. Space clears, Delete removes tracks)',
        group: 'Selection', availableIn: ['edit'],
        kind: { type: 'mode', factory: () => this._buildEntitySelectMode() },
      },

      {
        id: 'track', label: 'Track', icon: 'ti ti-track', title: 'Track',
        group: 'Network', availableIn: ['edit'], kind: { type: 'tool' },
      },
      {
        id: 'signal', label: 'Signal', icon: 'ti ti-traffic-lights', title: 'Signal',
        group: 'Network', availableIn: ['edit'], kind: { type: 'tool' },
      },
      {
        id: 'tunnel', label: 'Tunnel', icon: 'ti ti-arrows-join',
        title: 'Tunnel — click two dead-end edges to pair them',
        group: 'Network', availableIn: ['edit'], kind: { type: 'tool' },
      },
      {
        id: 'switches-panel', label: 'Switches', icon: 'ti ti-arrows-split-2',
        title: 'List All Switches',
        group: 'Network', availableIn: ['edit', 'sim'],
        kind: { type: 'action', run: () => this.showSwitchesPanel() },
      },

      {
        id: 'platform', label: 'New', icon: 'ti ti-plus',
        title: 'Place a Platform (click one tile, or drag across several to span multiple tiles)',
        group: 'Platform', availableIn: ['edit'], kind: { type: 'tool' },
      },
      {
        id: 'extend-platform', label: 'Extend', icon: 'ti ti-arrows-horizontal',
        title: 'Extend Existing Platform',
        group: 'Platform', availableIn: ['edit'], kind: { type: 'tool' },
      },
      {
        id: 'remove-platform-tile', label: 'Remove', icon: 'ti ti-minus',
        title: 'Remove Platform Tile',
        group: 'Platform', availableIn: ['edit'], kind: { type: 'tool' },
      },
      {
        id: 'stations-panel', label: 'Stations', icon: 'ti ti-home',
        title: 'Logical Station Groups',
        group: 'Platform', availableIn: ['edit', 'sim'],
        kind: { type: 'action', run: () => this.showStationsPanel() },
      },

      {
        id: 'block', label: 'New', icon: 'ti ti-plus', title: 'Create New Block',
        group: 'Block', availableIn: ['edit'], kind: { type: 'tool' },
      },
      {
        id: 'extend-block', label: 'Extend', icon: 'ti ti-arrows-horizontal',
        title: 'Extend Existing Block',
        group: 'Block', availableIn: ['edit'], kind: { type: 'tool' },
      },
      {
        id: 'remove-block-tile', label: 'Remove', icon: 'ti ti-minus',
        title: 'Remove Block Tile',
        group: 'Block', availableIn: ['edit'], kind: { type: 'tool' },
      },
    ];
  }

  /** Core tools plus every registered plugin's tools, in registration order. */
  _collectToolContributions(): ToolContribution[] {
    return [
      ...this.coreTools(),
      ...this._plugins.flatMap((p) => p.tools?.(this) ?? []),
    ];
  }

  /**
   * (Re)render the tool palette from the registry into `#tool-groups`, filtered
   * to the current editor mode, and rebuild the id→contribution lookup. The rail
   * shows Inspect plus one main button per group; a group button opens a flyout
   * popover of that group's tools. Tools whose `availableIn` excludes the current
   * mode are not rendered, and a group with no remaining tools shows no button.
   * Safe to call repeatedly; no-ops if the mount is absent.
   */
  renderToolbar(): void {
    const mount = this.$id('tool-groups');
    if (!mount) return;
    const contributions = this._collectToolContributions();
    this._toolsById = new Map(contributions.map((c) => [c.id, c]));
    renderToolGroups(contributions, mount, {
      activeToolId: this.selectedTool || 'inspect',
      mode: this.editorMode,
    });
  }

  /**
   * Delegated click handler for the tool palette. A group button toggles its
   * flyout popover; a tool button (in the rail or a popover) is dispatched and
   * the popover is closed.
   */
  async _onToolbarClick(e: Event): Promise<void> {
    const t = e.target as HTMLElement;

    const groupBtn = t.closest<HTMLElement>('.tool-group-btn');
    if (groupBtn) {
      const group = groupBtn.closest<HTMLElement>('.tool-group');
      const wasOpen = group?.classList.contains('open');
      this._closeToolPopovers();
      if (group && !wasOpen) this._openGroupPopover(group);
      return;
    }

    const toolBtn = t.closest<HTMLElement>('[data-tool]');
    if (toolBtn) {
      await this._dispatchTool(toolBtn);
      this._closeToolPopovers();
    }
  }

  /**
   * Dispatch a tool button by its contribution `kind`: action tools run and
   * leave the active tool alone; tool/mode tools become the active tool (mode
   * tools also enter their editor mode).
   */
  async _dispatchTool(btn: HTMLElement): Promise<void> {
    const newTool = btn.dataset.tool || '';
    const contribution = this._toolsById.get(newTool);
    const oldTool = this.selectedTool;

    // Action tools (panels/dialogs): run and leave the active tool unchanged.
    if (contribution?.kind.type === 'action') {
      contribution.kind.run();
      return;
    }

    // Exit any active mode before switching tools.
    if (this.activeMode) {
      await this.exitMode();
    }

    // Auto-toggle block debug when entering/leaving block tools.
    const isBlockTool = ['block', 'extend-block'].includes(newTool);
    const wasBlockTool = ['block', 'extend-block'].includes(oldTool);
    if (isBlockTool !== wasBlockTool) {
      this.renderer.showBlockColorsOnly = isBlockTool;
      this.host?.refreshNetwork();
    }

    if (contribution?.kind.type === 'mode') {
      // enterMode sets the active tool via _setSelectedTool(mode.toolName).
      await this.enterMode(contribution.kind.factory());
    } else {
      this._setSelectedTool(newTool);
    }

    this.previewPath = null;
    this.previewTracks = null;
    // Drop any lingering build/hover ghost from the previous tool.
    this.renderer.setBuildPreview(null);
    this.renderer.setHoverPreview(null);
    this.hoverPosition = null;
    this.hoverValid = false;
    this.renderer.requestRedraw();
  }

  /** Open a group's flyout popover, positioned beside its button (clamped to the viewport). */
  _openGroupPopover(group: HTMLElement): void {
    const btn = group.querySelector<HTMLElement>('.tool-group-btn');
    const pop = group.querySelector<HTMLElement>('.tool-popover');
    if (!btn || !pop) return;
    group.classList.add('open');
    btn.setAttribute('aria-expanded', 'true');
    // The popover is position:fixed (so the sidebar's overflow can't clip it);
    // place it just right of the button, then nudge up if it would overflow.
    const r = btn.getBoundingClientRect();
    pop.style.left = `${Math.round(r.right + 6)}px`;
    const margin = 8;
    let top = r.top;
    const h = pop.offsetHeight;
    if (top + h + margin > window.innerHeight) {
      top = Math.max(margin, window.innerHeight - h - margin);
    }
    pop.style.top = `${Math.round(top)}px`;
  }

  /** Close any open tool-group popover. */
  _closeToolPopovers(): void {
    this.$id('tool-groups')
      ?.querySelectorAll<HTMLElement>('.tool-group.open')
      .forEach((g) => {
        g.classList.remove('open');
        g.querySelector('.tool-group-btn')?.setAttribute('aria-expanded', 'false');
      });
  }

  _buildBoxSelectMode(): any {
    const resetState = () => {
      this.boxSelectState.selectedKeys = new Set();
      this.boxSelectState.dragScreenStart = null;
      this.boxSelectState.dragScreenEnd = null;
      this.boxSelectState.isDragging = false;
      this.boxSelectState.shiftRemoving = false;
      this._applyBoxSelection(new Set());
      this.renderer.setBoxSelectRect(null);
    };
    return {
      name: 'Box select',
      toolName: 'box-select',
      cancelMessage: 'Box select cancelled',
      onEnter: () => {
        resetState();
        const status = this.$id('status-state');
        if (status)
          status.textContent =
            'Box: drag to add, Shift+drag to remove, Space/right-click clears, Delete removes tracks';
      },
      onClick: () => true,
      onExit: () => {
        resetState();
        const status = this.$id('status-state');
        if (status) status.textContent = 'Ready';
        this.renderer.requestRedraw();
      },
    };
  }

  // Even-odd ray cast: returns true when (px, py) is inside the polygon.
  _pointInPolygon(px: number, py: number, polygon: any[]): boolean {
    let inside = false;
    for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
      const xi = polygon[i].x,
        yi = polygon[i].y;
      const xj = polygon[j].x,
        yj = polygon[j].y;
      const intersects =
        yi > py !== yj > py && px < ((xj - xi) * (py - yi)) / (yj - yi || 1e-9) + xi;
      if (intersects) inside = !inside;
    }
    return inside;
  }

  _buildLassoSelectMode(): any {
    const resetState = () => {
      this.boxSelectState.selectedKeys = new Set();
      this.lassoSelectState.screenPoints = [];
      this.lassoSelectState.isDrawing = false;
      this.lassoSelectState.shiftRemoving = false;
      this._applyBoxSelection(new Set());
      this.renderer.setLassoPath(null);
    };
    return {
      name: 'Lasso select',
      toolName: 'lasso-select',
      cancelMessage: 'Lasso select cancelled',
      onEnter: () => {
        resetState();
        const status = this.$id('status-state');
        if (status)
          status.textContent =
            'Lasso: draw a shape to add, Shift to remove, Space/right-click clears, Delete removes tracks';
      },
      onClick: () => true,
      onExit: () => {
        resetState();
        const status = this.$id('status-state');
        if (status) status.textContent = 'Ready';
        this.renderer.requestRedraw();
      },
    };
  }

  _buildPaintSelectMode(): any {
    const resetState = () => {
      this.boxSelectState.selectedKeys = new Set();
      this.paintSelectState.isPainting = false;
      this.paintSelectState.shiftRemoving = false;
      this.paintSelectState.lastKey = null;
      this._applyBoxSelection(new Set());
    };
    return {
      name: 'Brush select',
      toolName: 'paint-select',
      cancelMessage: 'Brush select cancelled',
      onEnter: () => {
        resetState();
        const status = this.$id('status-state');
        if (status)
          status.textContent =
            'Brush: click or drag to add, Shift to remove, Space/right-click clears, Delete removes tracks';
      },
      onClick: () => true,
      onExit: () => {
        resetState();
        const status = this.$id('status-state');
        if (status) status.textContent = 'Ready';
        this.renderer.requestRedraw();
      },
    };
  }

  // Entity select: click a tile, then choose which entity there — a block,
  // platform, or station — to add ALL of that entity's tiles to the shared
  // selection set. A tile with one entity selects it directly; a tile carrying
  // several opens the resource picker (the same menu the route waypoint flow
  // uses) with an extra "select all of them" row. Delete / Space / ESC act on
  // the selection exactly as they do for box / lasso / brush / trace.
  _buildEntitySelectMode(): any {
    const resetState = () => {
      this.boxSelectState.selectedKeys = new Set();
      this._applyBoxSelection(new Set());
    };
    const setStatus = (text: string) => {
      const status = this.$id('status-state');
      if (status) status.textContent = text;
    };
    // Union a set of tiles into the selection and repaint.
    const addTiles = (tiles: Set<string>) => {
      tiles.forEach((k) => this.boxSelectState.selectedKeys.add(k));
      this._applyBoxSelection(this.boxSelectState.selectedKeys);
      this.renderer.requestRedraw();
    };
    return {
      name: 'Entity select',
      toolName: 'entity-select',
      cancelMessage: 'Entity select cancelled',
      onEnter: () => {
        resetState();
        setStatus(
          'Entity: click a tile, then pick a block / platform / station — or all. Space/right-click clears, Delete removes tracks'
        );
      },
      onClick: ({ gridPos, screenX, screenY }: any) => {
        const track = this.cachedNetwork?.tracks?.find(
          (t: any) => t.x === gridPos.x && t.y === gridPos.y
        );
        // _resourceOptionsAtTile resolves the block/platform/station at a tile —
        // a layout op that currently lives on the route handler. Without the sim
        // plugin there are no resolvable options (helper relocation is a follow-up).
        const options = this.routeHandler?._resourceOptionsAtTile(gridPos, track) ?? [];
        if (options.length === 0) {
          setStatus('Entity: nothing here — click a track, platform, or station tile');
          return true;
        }
        const selectOne = (opt: any) => {
          const tiles = this._entityTileSet(opt);
          addTiles(tiles);
          showToast(
            `Selected ${opt.label} — ${tiles.size} tile${tiles.size === 1 ? '' : 's'}`,
            'success',
            1800
          );
        };
        // One entity on the tile: take it directly. Several: open the picker
        // with each entity plus a "select all of them" row.
        if (options.length === 1) {
          selectOne(options[0]);
          return true;
        }
        const pickerOptions = [
          ...options,
          { type: '__all__', label: 'Select all of them', variant: 'all' },
        ];
        showResourcePicker(screenX, screenY, pickerOptions, (opt: any) => {
          if (opt.type !== '__all__') {
            selectOne(opt);
            return;
          }
          const union = new Set<string>();
          options.forEach((o: any) => this._entityTileSet(o).forEach((k) => union.add(k)));
          addTiles(union);
          showToast(
            `Selected all here — ${union.size} tile${union.size === 1 ? '' : 's'}`,
            'success',
            1800
          );
        });
        return true;
      },
      onExit: () => {
        dismissResourcePicker();
        resetState();
        setStatus('Ready');
        this.renderer.requestRedraw();
      },
    };
  }

  // All "x,y" tiles that belong to a {type,id} resource, for entity-select.
  // Reuses the route handler's position-set helpers so there is one source of
  // truth for what a block / platform / station covers.
  _entityTileSet(opt: any): Set<string> {
    const rh = this.routeHandler;
    if (!rh) return new Set();
    if (opt.type === 'block') return rh.getBlockPositionSet(opt.id);
    if (opt.type === 'platform') return rh.getPlatformPositionSet([opt.id]);
    if (opt.type === 'station') {
      const group = rh.getStationGroups().find((g: any) => g.station_id === opt.id);
      return rh.getPlatformPositionSet(group?.platform_ids || []);
    }
    return new Set();
  }

  // Trace select: click a first track, then a second; selects every existing
  // track on the path between them (found via the construction pathfinder) and
  // adds them to the shared box-select selection set. Delete / Space / ESC act
  // on that set exactly as they do for box / lasso / brush.
  _buildTraceSelectMode(): any {
    const resetState = () => {
      this.boxSelectState.selectedKeys = new Set();
      this._resetTraceAnchor();
      this._applyBoxSelection(new Set());
    };
    const setStatus = (text: string) => {
      const status = this.$id('status-state');
      if (status) status.textContent = text;
    };
    return {
      name: 'Trace select',
      toolName: 'trace',
      cancelMessage: 'Trace select cancelled',
      onEnter: () => {
        resetState();
        setStatus('Trace: click the first track, then the second. Space/right-click clears, Delete removes tracks');
      },
      onClick: ({ gridPos }: any) => {
        // If the selection was cleared (Space / ESC), the pending anchor is stale.
        if (this.boxSelectState.selectedKeys.size === 0) {
          this._resetTraceAnchor();
        }
        const tracks = this.cachedNetwork?.tracks || [];
        const trackKeys = new Set(tracks.map((t: any) => `${t.x},${t.y}`));
        const here = `${gridPos.x},${gridPos.y}`;

        // Both endpoints must sit on an existing track.
        if (!trackKeys.has(here)) {
          setStatus(
            this.traceSelectState.firstPoint
              ? 'Trace: click the second track (must be on a track)'
              : 'Trace: click a track to start'
          );
          return true;
        }

        if (!this.traceSelectState.firstPoint) {
          // First endpoint: anchor it (yellow box-select) and light up every
          // track tile reachable from it (orange) so the operator can see which
          // tiles are valid second endpoints — the same "reachable" cue the
          // Create / Extend Path tools paint via setReachableHighlights.
          this.traceSelectState.firstPoint = { x: gridPos.x, y: gridPos.y };
          this.boxSelectState.selectedKeys.add(here);
          this._applyBoxSelection(this.boxSelectState.selectedKeys);
          const reachable = this._traceReachableTiles(this.traceSelectState.firstPoint);
          if (this.renderer.setReachableHighlights) {
            this.renderer.setReachableHighlights(reachable);
          }
          this.renderer.requestRedraw();
          setStatus(
            reachable.size === 0
              ? 'Trace: this track has no connected neighbours — Space to pick another'
              : `Trace: click a highlighted track to select the path (${reachable.size} reachable)`
          );
          return true;
        }

        // Second endpoint: trace the path and add every existing track on it.
        const a = this.traceSelectState.firstPoint;
        const path = this._traceTrackPath(a, { x: gridPos.x, y: gridPos.y });
        this.traceSelectState.firstPoint = null;
        // The pick is resolved — drop the orange reachable overlay either way.
        if (this.renderer.setReachableHighlights) {
          this.renderer.setReachableHighlights(new Set());
        }
        if (path && path.length) {
          for (const p of path) {
            // Every tile on the path is an existing track by construction.
            this.boxSelectState.selectedKeys.add(`${p.x},${p.y}`);
          }
          this._applyBoxSelection(this.boxSelectState.selectedKeys);
          this.renderer.requestRedraw();
          setStatus('Trace: click the first track, then the second');
        } else {
          showToast('No track path between those two tiles', 'info', 2500);
          setStatus('Trace: no path found — click a new first track');
        }
        return true;
      },
      onExit: () => {
        resetState();
        setStatus('Ready');
        this.renderer.requestRedraw();
      },
    };
  }

  // "x,y" → track map over cachedNetwork.tracks, rebuilt only when the tracks
  // array reference changes. cachedNetwork is replaced wholesale on every
  // network update, so identity comparison is exact; during a drag it is stable,
  // so the trace/preview methods (updatePreviewPath runs on every mousemove)
  // reuse this map instead of rebuilding it over all tracks each call.
  private _tracksByKeyCache: Map<string, any> | null = null;
  private _tracksByKeySource: any[] | null = null;
  private _tracksByKey(): Map<string, any> {
    const tracks = this.cachedNetwork?.tracks || [];
    if (this._tracksByKeyCache && this._tracksByKeySource === tracks) {
      return this._tracksByKeyCache;
    }
    const byKey = new Map<string, any>(tracks.map((t: any) => [`${t.x},${t.y}`, t]));
    this._tracksByKeyCache = byKey;
    this._tracksByKeySource = tracks;
    return byKey;
  }

  // Walk the existing-track network from `start` to `goal`, following real
  // segment connectivity (switch-aware), and return the ordered list of track
  // tiles on the shortest connecting path — or null if they are not connected.
  // Directed BFS keyed on (tile, entry edge): a tile is left only through the
  // exits its track bits actually bind for the edge it was entered by, so the
  // path cannot cheat across the disconnected segments of a switch.
  _traceTrackPath(start: any, goal: any): Array<{ x: number; y: number }> | null {
    const byKey = this._tracksByKey();
    if (!byKey.has(`${start.x},${start.y}`) || !byKey.has(`${goal.x},${goal.y}`)) return null;

    const { path } = graphSearch<TraceNode>({
      seeds: [{ x: start.x, y: start.y, entry: '' }],
      key: traceNodeKey,
      neighbors: this.pathfinder.followExistingNeighbors(byKey),
      goal: (n) => n.x === goal.x && n.y === goal.y,
    });
    return path ? path.map((n) => ({ x: n.x, y: n.y })) : null;
  }

  // Flood the existing-track network from `start` — same switch-aware directed
  // BFS as _traceTrackPath, but without a goal — and return the set of "x,y"
  // keys for every track tile reachable from it. These are exactly the valid
  // second endpoints of a trace; the start tile is excluded since it already
  // carries the yellow anchor highlight.
  _traceReachableTiles(start: any): Set<string> {
    const byKey = this._tracksByKey();
    const startKey = `${start.x},${start.y}`;
    const reachable = new Set<string>();
    if (!byKey.has(startKey)) return reachable;

    // No goal → graphSearch floods, settling every reachable (tile, entry)
    // node. Collapse those to tile keys; the start tile is excluded since it
    // already carries the yellow anchor highlight.
    const { visited } = graphSearch<TraceNode>({
      seeds: [{ x: start.x, y: start.y, entry: '' }],
      key: traceNodeKey,
      neighbors: this.pathfinder.followExistingNeighbors(byKey),
    });
    for (const node of visited.values()) {
      const k = `${node.x},${node.y}`;
      if (k !== startKey) reachable.add(k);
    }
    return reachable;
  }

  // Drop the pending trace anchor and clear the orange reachable-tile overlay.
  // Safe to call from the shared selection-clear paths (Space / Esc): for the
  // other mark-select tools the anchor is already null and the overlay empty.
  _resetTraceAnchor(): void {
    this.traceSelectState.firstPoint = null;
    if (this.renderer.setReachableHighlights) {
      this.renderer.setReachableHighlights(new Set());
    }
  }

  // Apply a single tile to the brush-select set.
  _applyPaintTile(gridPos: any, removing: boolean): boolean {
    const tracks = this.cachedNetwork?.tracks || [];
    const track = tracks.find((t: any) => t.x === gridPos.x && t.y === gridPos.y);
    if (!track) return false;
    const key = `${gridPos.x},${gridPos.y}`;
    if (removing) {
      if (!this.boxSelectState.selectedKeys.has(key)) return false;
      this.boxSelectState.selectedKeys.delete(key);
    } else {
      if (this.boxSelectState.selectedKeys.has(key)) return false;
      this.boxSelectState.selectedKeys.add(key);
    }
    return true;
  }

  // Tools that just open a panel or run a one-shot action.
  /** Sync the status-bar zoom percentage to the camera's current scale. */
  _syncZoomLabel(): void {
    const zoomLabel = this.$id('status-zoom');
    if (zoomLabel) zoomLabel.textContent = `${Math.round(this.renderer.camera.scale * 100)}%`;
  }

  /** Zoom toward a target absolute scale, keeping the world point anchored. */
  _zoomAt(screenX: number, screenY: number, targetScale: number): void {
    this.renderer.camera.zoomAt(screenX, screenY, targetScale);
    this.renderer.requestRedraw({ reason: 'zoom' });
    this._syncZoomLabel();
  }

  /** Reset zoom to 1.0 and re-center all tracks if available. */
  _resetZoom(): void {
    this.renderer.camera.scale = 1.0;
    if (this.cachedNetwork?.tracks?.length) {
      this.renderer.camera.centerOnContent(this.cachedNetwork.tracks);
    }
    this.renderer.requestRedraw({ reason: 'reset-zoom' });
    this._syncZoomLabel();
  }

  setupEventListeners(): void {
    // Render the tool palette from the registry, then dispatch clicks via event
    // delegation on the mount so a re-render (plugin registration, mode change)
    // never drops the listener. See renderToolbar and _onToolbarClick.
    this.renderToolbar();
    const toolMount = this.$id('tool-groups');
    toolMount?.addEventListener('click', (e) => {
      void this._onToolbarClick(e);
    });
    // Hover-intent: open a group's popover on hover (after a short delay so a
    // mouse merely crossing the rail doesn't pop menus), keeping click working
    // for touch. The popover is a DOM child of its .tool-group, so moving from
    // the button onto the popover stays "inside" the group and won't close it.
    if (toolMount) {
      let openTimer: ReturnType<typeof setTimeout> | null = null;
      let closeTimer: ReturnType<typeof setTimeout> | null = null;
      const HOVER_OPEN_MS = 130;
      const HOVER_CLOSE_MS = 220;
      toolMount.addEventListener('mouseover', (e) => {
        const group = (e.target as HTMLElement).closest<HTMLElement>('.tool-group');
        if (!group) return;
        if (closeTimer) {
          clearTimeout(closeTimer);
          closeTimer = null;
        }
        if (group.classList.contains('open')) return;
        if (openTimer) clearTimeout(openTimer);
        openTimer = setTimeout(() => {
          openTimer = null;
          this._closeToolPopovers();
          this._openGroupPopover(group);
        }, HOVER_OPEN_MS);
      });
      toolMount.addEventListener('mouseout', (e) => {
        const group = (e.target as HTMLElement).closest<HTMLElement>('.tool-group');
        if (!group) return;
        const related = e.relatedTarget as Node | null;
        if (related && group.contains(related)) return;
        if (openTimer) {
          clearTimeout(openTimer);
          openTimer = null;
        }
        if (closeTimer) clearTimeout(closeTimer);
        closeTimer = setTimeout(() => {
          closeTimer = null;
          this._closeToolPopovers();
        }, HOVER_CLOSE_MS);
      });
    }
    // Close the open group popover on an outside click or Escape.
    document.addEventListener('click', (e) => {
      if (toolMount && !toolMount.contains(e.target as Node)) this._closeToolPopovers();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') this._closeToolPopovers();
    });
    // Number keys 1-9 activate the Nth tool tile (or open its group) — the badge
    // shown on each rail tile. Ignored while typing in a field or with a modifier.
    document.addEventListener('keydown', (e) => {
      if (e.key < '1' || e.key > '9' || e.metaKey || e.ctrlKey || e.altKey) return;
      const el = document.activeElement as HTMLElement | null;
      const tag = (el?.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || el?.isContentEditable) return;
      const tiles = this.$id('tool-groups')?.querySelectorAll<HTMLElement>('.tool-launch');
      const tile = tiles?.[Number(e.key) - 1];
      if (tile) {
        e.preventDefault();
        tile.click();
      }
    });

    // Collapsible sidebar sections (Track Bits, etc.).
    this.setupCollapsibleSidebarSections();

    const canvas = this.renderer.canvas;

    // Mouse down - start drag or pan
    canvas.addEventListener('mousedown', (e: any) => {
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const gridPos = this.renderer.screenToGrid(x, y);

      // Middle mouse, select tool, or route-related tools = pan
      const pathTools = ['path', 'extend-path', 'cancel-path'];
      if (
        e.button === 1 ||
        (e.button === 0 && this.selectedTool === 'inspect') ||
        (e.button === 0 && pathTools.includes(this.selectedTool))
      ) {
        this.isPanning = true;
        this.panStart = { x, y };
        this.hasPanned = false;
        this.lastMouseX = e.clientX;
        this.lastMouseY = e.clientY;
      }
      // Left mouse + path-based tools = start drag
      else if (
        e.button === 0 &&
        ['track', 'block', 'extend-block', 'platform', 'extend-platform', 'remove-platform-tile', 'remove-block-tile'].includes(
          this.selectedTool
        )
      ) {
        this.isDraggingBuild = true;
        this.dragStart = gridPos;
        this.dragEnd = gridPos;
        this.previewInvalid = false;
      }
      // Left mouse + box-select = start screen-space rubber-band rectangle
      else if (e.button === 0 && this.selectedTool === 'box-select') {
        this.boxSelectState.isDragging = true;
        this.boxSelectState.shiftRemoving = e.shiftKey;
        this.boxSelectState.dragScreenStart = { x, y };
        this.boxSelectState.dragScreenEnd = { x, y };
        this.renderer.setBoxSelectRect({ startX: x, startY: y, endX: x, endY: y });
      }
      // Left mouse + lasso-select = start free-form polyline
      else if (e.button === 0 && this.selectedTool === 'lasso-select') {
        this.lassoSelectState.isDrawing = true;
        this.lassoSelectState.shiftRemoving = e.shiftKey;
        this.lassoSelectState.screenPoints = [{ x, y }];
        this.renderer.setLassoPath(this.lassoSelectState.screenPoints);
      }
      // Left mouse + paint-select = start tile-by-tile brush stroke.
      else if (e.button === 0 && this.selectedTool === 'paint-select') {
        this.paintSelectState.isPainting = true;
        this.paintSelectState.shiftRemoving = e.shiftKey;
        this.paintSelectState.lastKey = null;
        const removing = this.paintSelectState.shiftRemoving;
        if (this._applyPaintTile(gridPos, removing)) {
          this.paintSelectState.lastKey = `${gridPos.x},${gridPos.y}`;
          this._applyBoxSelection(this.boxSelectState.selectedKeys);
          this.renderer.requestRedraw();
        }
      }
      // Left click with signal/tunnel = single click placement
      else if (e.button === 0 && ['signal', 'tunnel'].includes(this.selectedTool)) {
        this.handleSingleClickPlace(gridPos);
      }
    });

    // Mouse move - update drag preview or pan
    canvas.addEventListener('mousemove', (e: any) => {
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const gridPos = this.renderer.screenToGrid(x, y);

      const coordsEl = this.$id('status-coords');
      if (coordsEl) coordsEl.textContent = `${gridPos.x}, ${gridPos.y}`;

      if (this.isPanning) {
        const dx = e.clientX - this.lastMouseX;
        const dy = e.clientY - this.lastMouseY;

        if (Math.abs(dx) > 2 || Math.abs(dy) > 2) {
          this.hasPanned = true;
        }

        this.renderer.camera.panBy(dx, dy);
        this.renderer.requestRedraw({ reason: 'pan' });
        this.lastMouseX = e.clientX;
        this.lastMouseY = e.clientY;
      } else if (this.isDraggingBuild && this.dragStart) {
        this.dragEnd = gridPos;
        this.updatePreviewPath();
      } else if (this.boxSelectState.isDragging) {
        this.boxSelectState.dragScreenEnd = { x, y };
        this.renderer.setBoxSelectRect({
          startX: this.boxSelectState.dragScreenStart.x,
          startY: this.boxSelectState.dragScreenStart.y,
          endX: x,
          endY: y,
        });
        this.renderer.requestRedraw();
      } else if (this.lassoSelectState.isDrawing) {
        const pts = this.lassoSelectState.screenPoints;
        const last = pts[pts.length - 1];
        if (!last || Math.abs(last.x - x) > 2 || Math.abs(last.y - y) > 2) {
          pts.push({ x, y });
          this.renderer.setLassoPath(pts);
          this.renderer.requestRedraw();
        }
      } else if (this.paintSelectState.isPainting) {
        const key = `${gridPos.x},${gridPos.y}`;
        if (key !== this.paintSelectState.lastKey) {
          const removing = this.paintSelectState.shiftRemoving;
          if (this._applyPaintTile(gridPos, removing)) {
            this._applyBoxSelection(this.boxSelectState.selectedKeys);
            this.renderer.requestRedraw();
          }
          this.paintSelectState.lastKey = key;
        }
      }
      // Hover preview for signal/platform tools
      else if (['signal', 'platform'].includes(this.selectedTool)) {
        this.updateHoverPreview(gridPos);
      }
      // Any active mode with an onHover hook (e.g. train placement) gets the
      // hovered tile so it can update its own canvas preview.
      else if (this.activeMode?.onHover) {
        this.activeMode.onHover(gridPos);
      }
    });

    // Mouse leaves the canvas - drop the signal/platform hover ghost so it
    // doesn't stick at its last position (the renderer holds preview state
    // now, so it won't clear on its own). A build drag keeps going via the
    // document-level mouseup, so its preview is intentionally left alone.
    canvas.addEventListener('mouseleave', () => {
      if (this.hoverPosition) {
        this.hoverPosition = null;
        this.hoverValid = false;
        this.renderer.setHoverPreview(null);
        this.renderer.requestRedraw();
      }
    });

    // Mouse up - commit drag build. Listen on document so an off-canvas
    // release still resets the drag/pan state.
    document.addEventListener('mouseup', async (e) => {
      const onCanvas = e.target === canvas;

      if (this.isPanning) {
        this.isPanning = false;
      } else if (this.boxSelectState.isDragging) {
        this.boxSelectState.isDragging = false;
        const start = this.boxSelectState.dragScreenStart;
        const end = this.boxSelectState.dragScreenEnd;
        const removing = this.boxSelectState.shiftRemoving;
        this.boxSelectState.dragScreenStart = null;
        this.boxSelectState.dragScreenEnd = null;
        this.boxSelectState.shiftRemoving = false;
        this.renderer.setBoxSelectRect(null);

        if (onCanvas && start && end) {
          const tracks = this.cachedNetwork?.tracks || [];
          const isClick = Math.abs(start.x - end.x) <= 2 && Math.abs(start.y - end.y) <= 2;
          if (isClick) {
            const gp = this.renderer.screenToGrid(start.x, start.y);
            const track = tracks.find((t: any) => t.x === gp.x && t.y === gp.y);
            if (track) {
              const key = `${gp.x},${gp.y}`;
              if (removing) this.boxSelectState.selectedKeys.delete(key);
              else this.boxSelectState.selectedKeys.add(key);
            }
          } else {
            const minX = Math.min(start.x, end.x);
            const maxX = Math.max(start.x, end.x);
            const minY = Math.min(start.y, end.y);
            const maxY = Math.max(start.y, end.y);
            for (const t of tracks) {
              const sx = this.renderer.gridToScreenX(t.x, t.y);
              const sy = this.renderer.gridToScreenY(t.x, t.y);
              if (sx >= minX && sx <= maxX && sy >= minY && sy <= maxY) {
                const key = `${t.x},${t.y}`;
                if (removing) this.boxSelectState.selectedKeys.delete(key);
                else this.boxSelectState.selectedKeys.add(key);
              }
            }
          }
        }

        this._applyBoxSelection(this.boxSelectState.selectedKeys);
        this.renderer.requestRedraw();
      } else if (this.lassoSelectState.isDrawing) {
        this.lassoSelectState.isDrawing = false;
        const pts = this.lassoSelectState.screenPoints;
        const removing = this.lassoSelectState.shiftRemoving;
        this.lassoSelectState.screenPoints = [];
        this.lassoSelectState.shiftRemoving = false;
        this.renderer.setLassoPath(null);

        if (onCanvas && pts.length >= 3) {
          const tracks = this.cachedNetwork?.tracks || [];
          for (const t of tracks) {
            const sx = this.renderer.gridToScreenX(t.x, t.y);
            const sy = this.renderer.gridToScreenY(t.x, t.y);
            if (this._pointInPolygon(sx, sy, pts)) {
              const key = `${t.x},${t.y}`;
              if (removing) this.boxSelectState.selectedKeys.delete(key);
              else this.boxSelectState.selectedKeys.add(key);
            }
          }
        }

        this._applyBoxSelection(this.boxSelectState.selectedKeys);
        this.renderer.requestRedraw();
      } else if (this.paintSelectState.isPainting) {
        this.paintSelectState.isPainting = false;
        this.paintSelectState.shiftRemoving = false;
        this.paintSelectState.lastKey = null;
      } else if (e.button === 0 && this.isDraggingBuild && this.dragStart && this.dragEnd) {
        // Left-button release commits the build. A right-button release must
        // NOT land here — it falls through to the contextmenu handler, which
        // cancels the in-progress drag instead of committing it.
        this.isDraggingBuild = false;

        if (onCanvas) {
          const isSingleClick = this.dragStart.x === this.dragEnd.x && this.dragStart.y === this.dragEnd.y;

          if (
            ['remove-platform-tile', 'remove-block-tile'].includes(this.selectedTool) &&
            isSingleClick
          ) {
            await this.handleRemovalClick(this.dragStart);
          } else if (
            ['block', 'track', 'extend-block', 'platform', 'extend-platform', 'remove-platform-tile', 'remove-block-tile'].includes(
              this.selectedTool
            )
          ) {
            await this.commitDragBuild();
          }
        }

        this.dragStart = null;
        this.dragEnd = null;
        this.forcedStartSegment = null;
        this.forcedEndSegment = null;
        const hadPreview = this.previewPath !== null || this.previewInvalid;
        this.previewPath = null;
        this.previewTracks = null;
        this.previewInvalid = false;
        this.renderer.setBuildPreview(null);

        if (hadPreview && this.cachedNetwork) {
          this.renderer.requestRedraw();
        }
      }
    });

    // Right click - cancel active drag, or context actions
    canvas.addEventListener('contextmenu', async (e: any) => {
      e.preventDefault();

      if (this.isDraggingBuild) {
        this.isDraggingBuild = false;
        this.dragStart = null;
        this.dragEnd = null;
        this.forcedStartSegment = null;
        this.forcedEndSegment = null;
        this.previewPath = null;
        this.previewTracks = null;
        this.previewInvalid = false;
        this.renderer.setBuildPreview(null);
        if (this.cachedNetwork) {
          this.renderer.requestRedraw();
        }
        return;
      }

      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const gridPos = this.renderer.screenToGrid(x, y);

      await this.handleRightClick(gridPos, e.clientX, e.clientY);
    });

    // Zoom with mouse-centered zooming and pan compensation.
    canvas.addEventListener('wheel', (e: any) => {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const mouseScreenX = e.clientX - rect.left;
      const mouseScreenY = e.clientY - rect.top;
      // Zoom multiplicatively so each notch changes the scale by a fixed
      // percentage (feels even at every zoom level). ZOOM_SENSITIVITY tunes
      // the speed; deltaMode!=0 (line/page scrolls) report far larger deltas,
      // so normalize those down. Clamp per-event so a fast flick or trackpad
      // burst can't jump several steps at once.
      const ZOOM_SENSITIVITY = 0.001;
      const unit = e.deltaMode === 0 ? 1 : 16; // pixels vs lines/pages
      const step = Math.max(-0.2, Math.min(0.2, (-e.deltaY / unit) * ZOOM_SENSITIVITY));
      const targetScale = this.renderer.scale * (1 + step);
      this._zoomAt(mouseScreenX, mouseScreenY, targetScale);
    });

    // Click to select element for properties panel
    canvas.addEventListener('click', async (e: any) => {
      if (this.hasPanned) {
        this.hasPanned = false;
        return;
      }

      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;

      // Station labels float above the grid — give them first shot.
      const stationId = this.renderer.stationHandler?.stationAt?.(x, y);
      if (stationId) {
        await this.stationGroupHandler.showStationDetail(stationId);
        return;
      }

      const gridPos = this.renderer.screenToGrid(x, y);

      // Any active EditorMode gets the first shot at the click.
      if (this.activeMode) {
        const handled = await this.activeMode.onClick({
          gridPos,
          screenX: e.clientX,
          screenY: e.clientY,
          event: e,
        });
        if (handled) return;
      }

      // Only handle selection with select tool
      if (this.selectedTool !== 'inspect') return;

      // Tunnel mouths are overlay glyphs sitting on a tile's edge — give them
      // first shot before tile selection, mirroring station labels above.
      const tunnelPair = this.renderer.tunnelHandler?.tunnelAt?.(x, y);
      if (tunnelPair) {
        await this.tunnelEditorHandler.showEditor(tunnelPair);
        return;
      }

      this.selectElement(gridPos);
    });

    // Shift key toggles avoid-connect mode for the track pathfinder.
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Shift' && !this.avoidConnectMode) {
        this.avoidConnectMode = true;
        if (this.isDraggingBuild) {
          this.updatePreviewPath();
        }
      }
    });

    document.addEventListener('keyup', (e) => {
      if (e.key === 'Shift' && this.avoidConnectMode) {
        this.avoidConnectMode = false;
        if (this.isDraggingBuild) {
          this.updatePreviewPath();
        }
      }
    });

    // R / T cycle the forced segment for the start (R) and end (T) tile.
    const SEGMENT_CYCLE = ['NS', 'EW', 'NW', 'SW', 'SE', 'NE'];
    const cycleSegment = (current: string | null): string | null => {
      if (current === null) return SEGMENT_CYCLE[0];
      const i = SEGMENT_CYCLE.indexOf(current);
      return i === SEGMENT_CYCLE.length - 1 ? null : SEGMENT_CYCLE[i + 1];
    };
    document.addEventListener('keydown', (e) => {
      if (this.selectedTool !== 'track') return;
      const active = document.activeElement;
      if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.tagName === 'SELECT'))
        return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const key = e.key.toLowerCase();
      if (key === 'r') {
        this.forcedStartSegment = cycleSegment(this.forcedStartSegment);
        showToast(`Start segment: ${this.forcedStartSegment || 'auto'}`, 'info', 800);
        if (this.isDraggingBuild) this.updatePreviewPath();
      } else if (key === 't') {
        this.forcedEndSegment = cycleSegment(this.forcedEndSegment);
        showToast(`End segment: ${this.forcedEndSegment || 'auto'}`, 'info', 800);
        if (this.isDraggingBuild) this.updatePreviewPath();
      }
    });

    // Inspector navigation shortcuts
    document.addEventListener('keydown', async (e) => {
      const activeElement = document.activeElement;
      const inInput =
        activeElement &&
        (activeElement.tagName === 'INPUT' ||
          activeElement.tagName === 'TEXTAREA' ||
          activeElement.tagName === 'SELECT');

      // Alt+Left mirrors the header's `← Parent` button: detail → its list.
      if (!inInput && e.altKey && !e.metaKey && !e.ctrlKey && e.key === 'ArrowLeft') {
        e.preventDefault();
        await this.navigateToParentList();
        return;
      }

      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && e.key.toLowerCase() === 'w') {
        if (inInput) {
          return;
        }
        e.preventDefault();
        await this.closeActiveSecondaryTab();
      }
    });

    // ESC key - exit extend mode or return to select tool
    document.addEventListener('keydown', async (e) => {
      if (e.key === 'Escape') {
        const inModal = this.$q('.block-modal-overlay, .modal-overlay') !== null;
        const inInput =
          document.activeElement?.tagName === 'INPUT' ||
          document.activeElement?.tagName === 'TEXTAREA' ||
          document.activeElement?.tagName === 'SELECT';

        if (inModal || inInput) {
          return; // Let modal/input handlers deal with it
        }

        // Box/lasso/brush select: first Esc clears any selection.
        const isMarkSelectTool =
          this.selectedTool === 'box-select' ||
          this.selectedTool === 'lasso-select' ||
          this.selectedTool === 'paint-select' ||
          this.selectedTool === 'entity-select' ||
          this.selectedTool === 'trace';
        if (isMarkSelectTool && this.boxSelectState.selectedKeys.size > 0) {
          this.boxSelectState.selectedKeys = new Set();
          this._applyBoxSelection(new Set());
          this._resetTraceAnchor();
          this.renderer.requestRedraw();
          return;
        }

        // Exit the active EditorMode if any, then revert to select.
        if (this.activeMode) {
          const m = this.activeMode;
          await this.exitMode();
          if (this.selectedTool !== 'inspect') this._setSelectedTool('inspect');
          showToast(m.cancelMessage ?? `${m.name} cancelled`, 'info', 3000);
          return;
        }

        // Otherwise revert any non-mode tool back to select.
        if (this.selectedTool !== 'inspect') {
          this._setSelectedTool('inspect');
        }
      }
    });

    // Delete / Backspace key - remove the box/lasso/brush-select selection.
    document.addEventListener('keydown', async (e) => {
      if (e.key !== 'Delete' && e.key !== 'Backspace') return;
      if (
        this.selectedTool !== 'box-select' &&
        this.selectedTool !== 'lasso-select' &&
        this.selectedTool !== 'paint-select' &&
        this.selectedTool !== 'entity-select' &&
        this.selectedTool !== 'trace'
      )
        return;
      const inInput =
        document.activeElement &&
        (document.activeElement.tagName === 'INPUT' ||
          document.activeElement.tagName === 'TEXTAREA' ||
          document.activeElement.tagName === 'SELECT');
      const inModal = this.$q('.block-modal-overlay, .modal-overlay') !== null;
      if (inInput || inModal) return;
      e.preventDefault();

      if (this.boxSelectState.selectedKeys.size === 0) {
        showToast('Nothing selected', 'info', 2000);
        return;
      }

      const positions = [...this.boxSelectState.selectedKeys].map((k: any) => {
        const [xs, ys] = k.split(',');
        return [Number(xs), Number(ys)];
      });

      try {
        const result = await this._bulkDeleteWithConfirm(positions);
        if (result) {
          this.boxSelectState.selectedKeys = new Set();
          this._applyBoxSelection(new Set());
          await this.refreshAndSelect(null);
        }
      } catch (err: any) {
        console.error('[Editor] Box-select delete failed:', err);
        showToast('Failed to delete: ' + err.message, 'error', 5000);
      }
    });

    // Delete / Backspace key - remove the tunnel selected with the Inspect tool.
    document.addEventListener('keydown', async (e) => {
      if (e.key !== 'Delete' && e.key !== 'Backspace') return;
      if (this.selectedTool !== 'inspect') return;
      const pair = this.renderer?.selectedTunnel;
      if (!pair) return;
      const inInput =
        document.activeElement &&
        (document.activeElement.tagName === 'INPUT' ||
          document.activeElement.tagName === 'TEXTAREA' ||
          document.activeElement.tagName === 'SELECT');
      const inModal = this.$q('.block-modal-overlay, .modal-overlay') !== null;
      if (inInput || inModal) return;
      e.preventDefault();
      await this.tunnelEditorHandler.delete(pair, { confirm: true });
    });

    // Space key - clear the current selection without exiting the tool.
    // Right-click does the same thing; see handleRightClick.
    document.addEventListener('keydown', (e) => {
      if (e.key !== ' ' && e.code !== 'Space') return;
      if (!this._isSelectionTool(this.selectedTool)) return;
      const inInput =
        document.activeElement &&
        (document.activeElement.tagName === 'INPUT' ||
          document.activeElement.tagName === 'TEXTAREA' ||
          document.activeElement.tagName === 'SELECT');
      const inModal = this.$q('.block-modal-overlay, .modal-overlay') !== null;
      if (inInput || inModal) return;
      e.preventDefault();

      if (this.boxSelectState.selectedKeys.size === 0) return;
      this.clearMultiSelection();
    });
  }

  // === ModeManager ===

  async enterMode(mode: any): Promise<void> {
    if (this.activeMode) await this.exitMode();
    this._setSelectedTool(mode.toolName ?? 'inspect');
    this.activeMode = mode;
    if (mode.onEnter) {
      try {
        await mode.onEnter();
      } catch (e) {
        console.error('[ModeManager] onEnter threw', e);
      }
    }
  }

  // Exits the active mode and runs its onExit. Does NOT change selectedTool.
  async exitMode(): Promise<void> {
    if (!this.activeMode) return;
    const m = this.activeMode;
    this.activeMode = null; // clear first so onExit can re-enter another mode
    try {
      await m.onExit();
    } catch (e) {
      console.error('[ModeManager] onExit threw', e);
    }
  }

  /**
   * Hard-reset transient editor state for a destructive "New Layout" clear.
   * Exits any active mode, returns to the inspect tool, and drops the build /
   * hover preview and the block-color overlay. This guarantees isToolActive()
   * is false so the follow-up refreshNetwork() takes the full-redraw path and
   * the renderer actually paints the cleared (empty) network. Without it, the
   * tool-active fast path in refreshNetwork() updates the caches but skips the
   * world redraw, so the renderer keeps painting the stale tracks / blocks /
   * switches over the now-empty layout.
   */
  async resetForBlankLayout(): Promise<void> {
    if (this.activeMode) await this.exitMode();
    this._setSelectedTool('inspect');
    this.previewPath = null;
    this.previewTracks = null;
    this.isDraggingBuild = false;
    this.hoverPosition = null;
    this.hoverValid = false;
    this.renderer.showBlockColorsOnly = false;
    this.renderer.setBuildPreview(null);
    this.renderer.setHoverPreview(null);
  }

  // Pure DOM + state update. Does NOT call enter/exitMode (no recursion).
  _setSelectedTool(toolName: string): void {
    this.selectedTool = toolName;
    this._syncToolbarActive();
  }

  /**
   * Reflect the active tool in the palette: highlight the matching tool button
   * (rail or popover) and the group button that contains it.
   */
  _syncToolbarActive(): void {
    const root = this.$id('tool-groups');
    if (!root) return;
    root.querySelectorAll<HTMLElement>('[data-tool]').forEach((b) =>
      b.classList.toggle('active', b.dataset.tool === this.selectedTool),
    );
    root.querySelectorAll<HTMLElement>('.tool-group').forEach((g) => {
      const owns = !!g.querySelector(`[data-tool="${this.selectedTool}"]`);
      g.querySelector('.tool-group-btn')?.classList.toggle('active', owns);
    });
  }

  // Wrapper around renderer.setBoxSelection that also syncs the sidebar panel.
  _applyBoxSelection(keys: any): void {
    this.renderer.setBoxSelection(keys);
    this.renderer.setSelectionGroups(this._buildSelectionOutlineGroups(keys));
    this.multiSelectHandler?.update(this.boxSelectState.selectedKeys);
  }

  // Group the selected tiles by the entity each belongs to, so the renderer can
  // trace one coloured perimeter per entity (block / platform / switch / signal)
  // instead of a field of tiles. A tile can land in several groups (a switch
  // tile is also in a block); each group carries only its own tiles, so its
  // outline is exactly that entity's footprint. Colours match the
  // selection-panel chips. The block outline is thin, light, and drawn last
  // (on top) so it stays a delicate border; platform / switch / signal are a
  // touch wider and drawn first, so they still peek around the block when they
  // share its tiles.
  _buildSelectionOutlineGroups(keys: Set<string>): any[] {
    if (!keys || keys.size === 0) return [];
    const tracks = this.cachedNetwork?.tracks || [];
    const byKey = new Map<string, any>();
    for (const t of tracks) byKey.set(`${t.x},${t.y}`, t);

    const blocks = new Map<string, Set<string>>();
    const platforms = new Map<string, Set<string>>();
    const switches = new Map<string, Set<string>>();
    const signals = new Map<string, Set<string>>();
    const add = (m: Map<string, Set<string>>, id: string, key: string): void => {
      let s = m.get(id);
      if (!s) {
        s = new Set();
        m.set(id, s);
      }
      s.add(key);
    };
    for (const key of keys) {
      const t = byKey.get(key);
      if (!t) continue;
      if (t.block_section_id) add(blocks, t.block_section_id, key);
      // A tile's `station_id` holds the platform it belongs to (historically
      // named; see _summarize in editor-multi-select.ts).
      if (t.station_id) add(platforms, t.station_id, key);
      if (t.switch_id) add(switches, t.switch_id, key);
      for (const sid of t.signal_ids || []) add(signals, sid, key);
    }

    const hue = (token: string, fallback: string): string => {
      try {
        const v = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
        return v || fallback;
      } catch {
        return fallback;
      }
    };
    // Mix a hex colour toward white so the block border reads light, not bold.
    const lighten = (hex: string, amt: number): string => {
      const m = hex.trim().replace('#', '');
      const full = m.length === 3 ? m.split('').map((c) => c + c).join('') : m;
      if (full.length !== 6) return hex;
      const ch = (i: number): number => parseInt(full.slice(i, i + 2), 16);
      const mix = (c: number): number => Math.round(c + (255 - c) * amt);
      return `rgb(${mix(ch(0))}, ${mix(ch(2))}, ${mix(ch(4))})`;
    };
    const blockColor = lighten(hue('--accent-hover', '#9466ff'), 0.45);
    const platformColor = hue('--info-color', '#00d4ff');
    const switchColor = hue('--warning-color', '#ffcd1e');
    const signalColor = hue('--success-color', '#04d361');

    // Draw platform / switch / signal first (a touch wider) and the block last
    // (thin + light, on top) so the block stays a delicate border that never
    // hides the entities sharing its tiles.
    const groups: any[] = [];
    for (const s of platforms.values()) groups.push({ keys: s, color: platformColor, width: 3.5, dash: [7, 5] });
    for (const s of switches.values()) groups.push({ keys: s, color: switchColor, width: 3.5, dash: [2, 4] });
    for (const s of signals.values()) groups.push({ keys: s, color: signalColor, width: 3.5, dash: [7, 4, 2, 4] });
    for (const s of blocks.values()) groups.push({ keys: s, color: blockColor, width: 2, dash: [] });
    return groups;
  }

  // The tools that share the box-select selection set. Space and right-click
  // both clear that set without leaving the active tool.
  _isSelectionTool(tool: string): boolean {
    return (
      tool === 'box-select' ||
      tool === 'lasso-select' ||
      tool === 'paint-select' ||
      tool === 'entity-select' ||
      tool === 'trace'
    );
  }

  clearMultiSelection(): void {
    this.boxSelectState.selectedKeys = new Set();
    this._applyBoxSelection(new Set());
    this._resetTraceAnchor();
    this.renderer.requestRedraw();
  }

  setupBlockManager(): void {
    this.blockHandler.setup();
  }

  async refreshBlocksList(): Promise<any> {
    return this.blockHandler.refreshList();
  }

  async deleteBlock(blockId: any): Promise<any> {
    return this.blockHandler.delete(blockId);
  }

  getBlockColor(blockId: any): any {
    return this.blockHandler.getColor(blockId);
  }

  showBlockModal(options: any): any {
    return this.blockHandler.showModal(options);
  }

  showPlatformModal(options: any): any {
    return this.platformHandler.showModal(options);
  }

  renderSignalList(signals: any): any {
    return this.signalHandler.renderSignalList(signals);
  }

  async invalidateSignalsInPath(signalsInPath: any): Promise<any> {
    return this.signalHandler.invalidateSignalsInPath(signalsInPath);
  }

  // === End Block Manager Methods ===

  // Path for a block/platform/eraser drag: these operate ON existing track, so
  // they follow existing connectivity (never an empty tile) via graphSearch.
  // Blocks additionally cannot contain switches or crossings (see
  // validateBlockTile), so the block tools stop at them — a drag that can only
  // be completed by crossing one finds no path, which surfaces as the red line.
  private _followExistingDragPath(): Array<{ x: number; y: number }> | null {
    const byKey = this._tracksByKey();
    const s = this.dragStart;
    const g = this.dragEnd;
    if (!byKey.has(`${s.x},${s.y}`) || !byKey.has(`${g.x},${g.y}`)) return null;

    const isBlock = this.selectedTool === 'block' || this.selectedTool === 'extend-block';
    const { path } = graphSearch<TraceNode>({
      seeds: [{ x: s.x, y: s.y, entry: '' }],
      key: traceNodeKey,
      neighbors: this.pathfinder.followExistingNeighbors(
        byKey,
        isBlock ? { stopAtSwitches: true, stopAtCrossings: true } : {}
      ),
      goal: (n) => n.x === g.x && n.y === g.y,
    });
    return path ? path.map((n) => ({ x: n.x, y: n.y })) : null;
  }

  updatePreviewPath(): void {
    if (!this.dragStart || !this.dragEnd) return;

    const dragToolSet = [
      'track', 'block', 'extend-block', 'platform', 'extend-platform', 'remove-platform-tile', 'remove-block-tile',
    ];
    if (dragToolSet.includes(this.selectedTool)) {
      const isTrackTool = this.selectedTool === 'track';
      // The track tool LAYS new track, so it routes with the placement A* (it may
      // cross empty tiles). Every other drag tool operates ON existing track, so
      // it follows existing connectivity and never proposes an empty tile.
      const foundPath = isTrackTool
        ? this.pathfinder.findPath(
            this.dragStart.x,
            this.dragStart.y,
            this.dragEnd.x,
            this.dragEnd.y,
            this.cachedNetwork ? this.cachedNetwork.tracks : [],
            {
              avoidConnect: this.avoidConnectMode,
              forcedStartSegment: this.forcedStartSegment ?? undefined,
              forcedEndSegment: this.forcedEndSegment ?? undefined,
            }
          )
        : this._followExistingDragPath();

      if (foundPath) {
        this.previewPath = foundPath;
        this.previewTracks = isTrackTool
          ? pathToTracks(this.previewPath, this.cachedNetwork, {
              avoidConnect: this.avoidConnectMode,
              forcedStartSegment: this.forcedStartSegment,
              forcedEndSegment: this.forcedEndSegment,
            })
          : null;
        this.previewInvalid = false;

        // Platform/signal tiles keep their own rail, so pathToTracks lays nothing
        // on them — a path passing through one would show a gap. The path now
        // follows that rail (rail-follow rule in the pathfinder), so draw the
        // tile's existing rail into the preview to bridge the gap. Display only:
        // commitDragBuild re-runs pathToTracks, which still skips these tiles.
        if (isTrackTool && this.previewTracks && this.cachedNetwork?.tracks) {
          const shown = new Set(this.previewTracks.map((t: any) => `${t.x},${t.y}`));
          const skipTiles = new Map<string, any>();
          for (const t of this.cachedNetwork.tracks) {
            if (tileSkipsSegment(t)) {
              skipTiles.set(`${t.x},${t.y}`, t);
            }
          }
          for (const p of this.previewPath) {
            const key = `${p.x},${p.y}`;
            if (shown.has(key)) continue;
            const ex = skipTiles.get(key);
            if (ex && ex.track_bits) {
              this.previewTracks.push({ x: p.x, y: p.y, track_bits: ex.track_bits });
              shown.add(key);
            }
          }
        }
      } else {
        // Any drag tool with no legal route: show the red attempt-line (an
        // off-track gap, a switch/crossing a block can't cross, a platform/signal
        // in the way, or a forced segment that can't turn) and leave previewPath
        // null so commitDragBuild lays nothing — never freeze on a stale shape.
        this.previewPath = null;
        this.previewTracks = null;
        this.previewInvalid = true;
      }
    } else {
      // Single placement for other tools
      this.previewPath = [this.dragEnd];
      this.previewTracks = null;
    }

    // Push the preview overlay to the renderer and request a redraw. The
    // renderer paints it in _drawLive, so it survives the rAF-coalesced flush.
    // (Drawing it synchronously here would be wiped when the flush clears the
    // live canvas — see renderer setBuildPreview / _drawLive.)
    if (this.cachedNetwork) {
      if (this.previewInvalid && this.dragStart && this.dragEnd) {
        this.renderer.setBuildPreview({ kind: 'invalid', path: [this.dragStart, this.dragEnd] });
      } else if (this.previewTracks && this.previewTracks.length > 0) {
        this.renderer.setBuildPreview({ kind: 'tracks', tracks: this.previewTracks });
      } else if (
        ['remove-platform-tile', 'remove-block-tile'].includes(this.selectedTool) &&
        this.previewPath
      ) {
        const tracks = this.cachedNetwork?.tracks || [];
        const trackKeys = new Set(tracks.map((t: any) => `${t.x},${t.y}`));
        const filtered = this.previewPath.filter((p: any) => trackKeys.has(`${p.x},${p.y}`));
        this.renderer.setBuildPreview({ kind: 'eraser', path: filtered });
      } else if (['block', 'extend-block', 'platform', 'extend-platform'].includes(this.selectedTool) && this.previewPath) {
        this.renderer.setBuildPreview({ kind: 'block', path: this.previewPath });
      } else if (this.previewPath) {
        this.renderer.setBuildPreview({ kind: 'path', path: this.previewPath });
      } else {
        this.renderer.setBuildPreview(null);
      }
      this.renderer.requestRedraw();
    }
  }

  async commitDragBuild(): Promise<void> {
    // The platform tool also supports a plain click (no drag) to create a
    // single-tile platform: a click never builds a previewPath, so seed it with
    // the tile under the cursor. Other build tools require an actual drag and
    // bail when there is no path.
    if (!this.previewPath || this.previewPath.length === 0) {
      if (this.selectedTool === 'platform' && this.dragStart) {
        this.previewPath = [this.dragStart];
      } else {
        return;
      }
    }

    try {
      if (this.selectedTool === 'track') {
        const tracks = pathToTracks(this.previewPath, this.cachedNetwork, {
          avoidConnect: this.avoidConnectMode,
          forcedStartSegment: this.forcedStartSegment,
          forcedEndSegment: this.forcedEndSegment,
        });
        await this.api.addTracksBulk(tracks);
      } else if (this.selectedTool === 'block') {
        if (!this.previewPath || this.previewPath.length === 0) return;

        const tracks = this.cachedNetwork?.tracks || [];
        const validation = validateBlockPath(this.previewPath, tracks, 'create');

        if (!validation.valid) {
          const errorMsg = 'Cannot create block:\n' + validation.errors.join('\n');
          showToast(errorMsg, 'error', 5000);
          console.error('[Editor] Block creation validation failed:', validation.errors);
          return;
        }

        if (validation.warnings.length > 0) {
          const proceed = await showConfirmModal({
            title: 'Proceed with Warnings',
            message: 'Block creation reported warnings.',
            detail: `${validation.warnings.join('\n')}\n\nProceed anyway?`,
            confirmText: 'Proceed',
            confirmVariant: 'primary',
          });
          if (!proceed) return;
        }

        const result = await this.showBlockModal({
          mode: 'create',
          trackCount: this.previewPath.length,
          anchorTiles: this.previewPath.map((p: any) => ({ x: p.x, y: p.y })),
        });

        if (!result) return; // Cancelled

        try {
          const blockOptions: Record<string, unknown> = {};
          if (result.maxSpeed) blockOptions.speed_limit_kmh = result.maxSpeed;
          if (result.minRadius != null) blockOptions.min_radius_cm = result.minRadius;
          if (result.slope != null) blockOptions.grade_percent = result.slope;
          await this.api.createBlock(result.blockId, result.name || '', blockOptions);

          const positions = this.previewPath.map((p: any) => [p.x, p.y]);
          await this.api.addTracksToBlock(result.blockId, positions);

          await this.refreshBlocksList();
        } catch (err: any) {
          console.error('[Editor] Failed to create block:', err);
          showToast('Failed to create block: ' + err.message, 'error', 5000);
          return;
        }
      } else if (this.selectedTool === 'extend-block') {
        if (!this.previewPath || this.previewPath.length === 0) return;

        const tracks = this.cachedNetwork?.tracks || [];
        const validation = validateBlockPath(this.previewPath, tracks, 'extend');

        if (!validation.valid) {
          const errorMsg = 'Cannot extend block:\n' + validation.errors.join('\n');
          showToast(errorMsg, 'error', 5000);
          console.error('[Editor] Block extension validation failed:', validation.errors);
          return;
        }

        const hasSignals = validation.signalsInPath.length > 0;
        if (hasSignals) {
          const signalCount = validation.signalsInPath.length;
          const proceed = await showConfirmModal({
            title: 'Signals Will Be Invalidated',
            message: `Path contains ${signalCount} signal(s).`,
            detail:
              'These signals will be invalidated and need reconfiguration.\n(They will show an orange warning triangle)\n\nProceed anyway?',
            confirmText: 'Proceed',
            confirmVariant: 'primary',
          });
          if (!proceed) return;
        }

        const trackMap = new Map<string, any>();
        tracks.forEach((t: any) => trackMap.set(`${t.x},${t.y}`, t));

        const targetBlockId = validation.blocksInPath.length > 0 ? validation.blocksInPath[0] : null;
        let newTileCount = this.previewPath.length;
        if (targetBlockId) {
          newTileCount = this.previewPath.filter((pos: any) => {
            const track = trackMap.get(`${pos.x},${pos.y}`);
            return !track || track.block_section_id !== targetBlockId;
          }).length;
        }

        const result = await this.showBlockModal({
          mode: 'extend',
          trackCount: newTileCount,
          existingBlocks: validation.blocksInPath,
        });

        if (!result) return; // Cancelled

        try {
          const positions = this.previewPath.map((p: any) => [p.x, p.y]);
          const hasOtherBlocks =
            validation.blocksInPath.length > 1 ||
            (validation.blocksInPath.length === 1 && validation.blocksInPath[0] !== result.blockId);
          const apiResult = await this.api.extendBlock(result.blockId, positions, hasSignals, hasOtherBlocks);

          if (apiResult.invalidated_signals && apiResult.invalidated_signals.length > 0) {
            const count = apiResult.invalidated_signals.length;
            showToast(`Block extended. ${count} signal(s) need reconfiguration.`, 'warning');
          }

          await this.refreshBlocksList();
        } catch (err: any) {
          console.error('[Editor] Failed to extend block:', err);
          showToast('Failed to extend block: ' + err.message, 'error', 5000);
          return;
        }
      } else if (this.selectedTool === 'platform') {
        if (!this.previewPath || this.previewPath.length === 0) return;

        const positions = this.previewPath.map((p: any) => [p.x, p.y]);

        const platformData = await this.showPlatformModal({
          mode: 'create',
          anchorTiles: this.previewPath.map((p: any) => ({ x: p.x, y: p.y })),
        });
        if (!platformData) return; // User cancelled

        try {
          await this.api.createPlatform({
            positions,
            name: platformData.name,
            length_cm: platformData.lengthCm,
            platform_side: platformData.platformSide,
            top_speed_kmh: platformData.topSpeedKmh,
            platform_id: platformData.platformId,
          });
          showToast(`Platform created with ${positions.length} tile(s)`, 'success');
        } catch (err: any) {
          console.error('[Editor] Platform creation failed:', err);
          showToast('Failed to create platform: ' + err.message, 'error', 5000);
          return;
        }
      } else if (this.selectedTool === 'extend-platform') {
        if (!this.previewPath || this.previewPath.length === 0) return;

        const tracks = this.cachedNetwork?.tracks || [];
        const platforms = this.cachedNetwork?.platforms || [];
        const platformsInPath = new Set<any>();

        for (const pos of this.previewPath) {
          const track = tracks.find((t: any) => t.x === pos.x && t.y === pos.y);
          if (track && track.station_id) {
            platformsInPath.add(track.station_id);
          }
        }

        if (platformsInPath.size === 0) {
          showToast('No platform found in path. Start the drag on an existing platform.', 'error', 5000);
          return;
        }

        let platformId;
        if (platformsInPath.size === 1) {
          platformId = Array.from(platformsInPath)[0];
        } else {
          const startTrack = tracks.find((t: any) => t.x === this.dragStart.x && t.y === this.dragStart.y);
          if (startTrack && startTrack.station_id) {
            platformId = startTrack.station_id;
          } else {
            platformId = Array.from(platformsInPath)[0];
          }
        }

        try {
          const platform = platforms.find((p: any) => p.platform_id === platformId);
          const platformPositions = new Set(platform?.positions?.map((p: any) => `${p[0]},${p[1]}`) || []);

          const newPositions = this.previewPath
            .filter((p: any) => !platformPositions.has(`${p.x},${p.y}`))
            .map((p: any) => [p.x, p.y]);

          if (newPositions.length === 0) {
            showToast('All tiles are already part of the platform.', 'error', 5000);
            return;
          }

          await this.api.extendPlatform(platformId, newPositions);

          showToast(`Platform extended with ${newPositions.length} tile(s)`, 'success');
        } catch (err: any) {
          console.error('[Editor] Failed to extend platform:', err);
          showToast('Failed to extend platform: ' + err.message, 'error', 5000);
          return;
        }
      } else if (this.selectedTool === 'remove-platform-tile') {
        if (!this.previewPath || this.previewPath.length === 0) return;

        const tracks = this.cachedNetwork?.tracks || [];
        let successCount = 0;
        let errorCount = 0;
        const errors = [];

        for (const pos of this.previewPath) {
          const track = tracks.find((t: any) => t.x === pos.x && t.y === pos.y);
          if (!track || !track.station_id) {
            errors.push(`No platform at (${pos.x}, ${pos.y})`);
            errorCount++;
            continue;
          }

          try {
            await this.api.removePlatformTile(track.station_id, pos.x, pos.y);
            successCount++;
          } catch (err: any) {
            errors.push(`(${pos.x}, ${pos.y}): ${err.message}`);
            errorCount++;
          }
        }

        if (successCount > 0) {
          showToast(`Removed ${successCount} platform tile(s)`, 'success');
        }
        if (errorCount > 0) {
          showToast(`Failed to remove ${errorCount} tile(s):\n${errors.slice(0, 3).join('\n')}`, 'error', 5000);
        }
      } else if (this.selectedTool === 'remove-block-tile') {
        if (!this.previewPath || this.previewPath.length === 0) return;

        const tracks = this.cachedNetwork?.tracks || [];

        const blockGroups = new Map<string, any[]>();
        for (const pos of this.previewPath) {
          const track = tracks.find((t: any) => t.x === pos.x && t.y === pos.y);
          if (track && track.block_section_id) {
            if (!blockGroups.has(track.block_section_id)) {
              blockGroups.set(track.block_section_id, []);
            }
            blockGroups.get(track.block_section_id)!.push([pos.x, pos.y]);
          }
        }

        if (blockGroups.size === 0) {
          showToast('No block tiles in selected path', 'error', 3000);
          return;
        }

        let totalSuccess = 0;
        const totalErrors = [];

        for (const [blockId, positions] of blockGroups.entries()) {
          try {
            const result = await this.api.removeBlockTiles(blockId, positions);
            totalSuccess += result.removed || 0;
            if (result.errors && result.errors.length > 0) {
              totalErrors.push(...result.errors);
            }
          } catch (err: any) {
            totalErrors.push(`Block ${blockId}: ${err.message}`);
          }
        }

        if (totalSuccess > 0) {
          showToast(`Removed ${totalSuccess} block tile(s)`, 'success');
        }
        if (totalErrors.length > 0) {
          showToast(`Errors:\n${totalErrors.slice(0, 3).join('\n')}`, 'error', 5000);
        }
      }

      await this.refreshAndSelect(null); // Refresh without selecting specific element
    } catch (err) {
      console.error('[Editor] Failed to commit build:', err);
    }
  }

  /** Unified refresh helper. */
  async refreshAndSelect(gridPos: any): Promise<void> {
    await this.host?.refreshNetwork();
    if (gridPos) {
      await this.selectElement(gridPos);
    }
  }

  // Wraps deleteTracksBulk with the server's impact pre-check / confirmation.
  async _bulkDeleteWithConfirm(positions: any): Promise<any> {
    if (!positions || positions.length === 0) return null;
    try {
      return await this.api.deleteTracksBulk(positions, { force: false });
    } catch (err: any) {
      if (err.status !== 409 || !err.requires_confirmation) throw err;
      const impact = err.impact || {};
      const switches = (impact.removes_switches || []).length;
      const signals = (impact.removes_signals || []).length;
      const platforms = (impact.removes_platforms || []).length;
      const parts = [];
      if (switches) parts.push(`${switches} switch${switches !== 1 ? 'es' : ''}`);
      if (signals) parts.push(`${signals} signal${signals !== 1 ? 's' : ''}`);
      if (platforms) parts.push(`${platforms} platform${platforms !== 1 ? 's' : ''}`);
      const detail = parts.length ? `This will also remove ${parts.join(', ')}.` : '';
      const confirmed = await showConfirmModal({
        title: 'Confirm bulk delete',
        message: `Delete ${positions.length} track${positions.length !== 1 ? 's' : ''}?`,
        detail,
        confirmText: 'Delete',
        confirmVariant: 'danger',
      });
      if (!confirmed) return null;
      return await this.api.deleteTracksBulk(positions, { force: true });
    }
  }

  async handleRemovalClick(gridPos: any): Promise<void> {
    try {
      const existingTrack = this.cachedNetwork?.tracks?.find(
        (t: any) => t.x === gridPos.x && t.y === gridPos.y
      );

      if (!existingTrack) {
        showToast('No track at this position', 'error', 3000);
        return;
      }

      if (this.selectedTool === 'remove-platform-tile') {
        if (!existingTrack.station_id) {
          showToast('No platform at this position', 'error', 3000);
          return;
        }

        try {
          await this.api.removePlatformTile(existingTrack.station_id, gridPos.x, gridPos.y);
          showToast('Platform tile removed', 'success');
        } catch (err: any) {
          showToast(`Failed to remove platform tile: ${err.message}`, 'error', 5000);
        }
      } else if (this.selectedTool === 'remove-block-tile') {
        if (!existingTrack.block_section_id) {
          showToast('No block at this position', 'error', 3000);
          return;
        }

        try {
          const result = await this.api.removeBlockTiles(existingTrack.block_section_id, [
            [gridPos.x, gridPos.y],
          ]);

          if (result.removed > 0) {
            showToast('Block tile removed', 'success');
          } else if (result.errors && result.errors.length > 0) {
            showToast(`Cannot remove: ${result.errors[0]}`, 'error', 5000);
          }
        } catch (err: any) {
          showToast(`Failed to remove block tile: ${err.message}`, 'error', 5000);
        }
      }

      await this.refreshAndSelect(null);
    } catch (err) {
      console.error('[Editor] Failed to handle removal click:', err);
    }
  }

  // Single click placement for signal/platform/tunnel tools
  async handleSingleClickPlace(gridPos: any): Promise<void> {
    const existingTrack = this.cachedNetwork?.tracks?.find(
      (t: any) => t.x === gridPos.x && t.y === gridPos.y
    );

    if (!existingTrack) {
      showToast(`Cannot place ${this.selectedTool}: No track at this position`, 'error', 5000);
      return;
    }

    if (this.selectedTool === 'tunnel') {
      await this.handleTunnelClick(gridPos, existingTrack);
      return;
    }

    if (this.selectedTool === 'signal') {
      if (existingTrack.station_name) {
        showToast('Cannot place signal on a platform', 'error', 5000);
        return;
      }

      try {
        const validation = await this.api.validateSignalPlacement(gridPos.x, gridPos.y);

        if (!validation.can_place) {
          showToast(validation.error || 'Cannot place signal at this position', 'error', 5000);
          return;
        }

        let detectionDirection;
        if (
          this.previewSignalDirection &&
          this._lastValidatedPosition?.x === gridPos.x &&
          this._lastValidatedPosition?.y === gridPos.y &&
          validation.available_directions?.includes(this.previewSignalDirection)
        ) {
          detectionDirection = this.previewSignalDirection;
        } else {
          detectionDirection = validation.available_directions?.[0] || validation.valid_directions?.[0];
        }

        if (!detectionDirection) {
          showToast('No valid detection direction available for signal placement', 'error', 5000);
          return;
        }

        await this.api.createSignal(gridPos.x, gridPos.y, detectionDirection, 'block', null, true);

        this.previewSignalDirection = null;
        this.previewSignalDirectionIndex = 0;
        this._lastValidatedPosition = null;
      } catch (err: any) {
        console.error('[Editor] Signal creation failed:', err);
        showToast('Failed to create signal: ' + err.message, 'error', 5000);
        return;
      }
    }

    // Clear the placement ghost before the refresh redraw repaints the live
    // layer, otherwise the stale hover preview would survive the flush.
    this.hoverPosition = null;
    this.hoverValid = false;
    this.renderer.setHoverPreview(null);
    await this.refreshAndSelect(gridPos);
  }

  // Update hover preview for signal/platform tools
  updateHoverPreview(gridPos: any): void {
    if (!['signal', 'platform'].includes(this.selectedTool)) {
      this.hoverPosition = null;
      this.hoverValid = false;
      this.renderer.setHoverPreview(null);
      return;
    }

    this.hoverPosition = gridPos;

    const existingTrack = this.cachedNetwork?.tracks?.find(
      (t: any) => t.x === gridPos.x && t.y === gridPos.y
    );

    if (!existingTrack) {
      this.hoverValid = false;
      this.previewSignalDirection = null;
      this.validSignalDirections = [];
      this.drawWithHoverPreview();
      return;
    }

    if (this.selectedTool === 'signal') {
      this.validateSignalPreviewAsync(gridPos, existingTrack);
    } else if (this.selectedTool === 'platform') {
      this.hoverValid = !existingTrack.is_switch && !existingTrack.signal_state;
      this.previewSignalDirection = null;
      this.drawWithHoverPreview();
    }
  }

  // Async API validation for signal preview - debounced.
  async validateSignalPreviewAsync(gridPos: any, existingTrack: any): Promise<void> {
    if (this.signalValidationTimeout) {
      clearTimeout(this.signalValidationTimeout);
    }

    const trackBits = existingTrack.track_bits || 0;
    const localDirections = getValidSignalDirections(trackBits);
    const hasStation = !!existingTrack.station_name;

    if (localDirections.length === 0 || hasStation) {
      this.hoverValid = false;
      this.previewSignalDirection = null;
      this.validSignalDirections = [];
      this.drawWithHoverPreview();
      return;
    }

    if (
      this.validSignalDirections.length === 0 ||
      this._lastValidatedPosition?.x !== gridPos.x ||
      this._lastValidatedPosition?.y !== gridPos.y
    ) {
      this.validSignalDirections = localDirections;
      this.hoverValid = localDirections.length > 0;
      if (this.hoverValid && !this.previewSignalDirection) {
        this.previewSignalDirectionIndex = 0;
        this.previewSignalDirection = localDirections[0];
      }
      this.drawWithHoverPreview();
    }

    // Debounce the API call (50ms)
    this.signalValidationTimeout = setTimeout(async () => {
      try {
        const validation = await this.api.validateSignalPlacement(gridPos.x, gridPos.y);

        if (!this.hoverPosition || this.hoverPosition.x !== gridPos.x || this.hoverPosition.y !== gridPos.y) {
          return; // User moved away, ignore result
        }

        this._lastValidatedPosition = { x: gridPos.x, y: gridPos.y };

        if (!validation.can_place) {
          this.hoverValid = false;
          this.previewSignalDirection = null;
          this.validSignalDirections = [];
        } else {
          const availableDirections = validation.available_directions || [];

          this.validSignalDirections = availableDirections;
          this.hoverValid = availableDirections.length > 0;

          if (this.hoverValid && availableDirections.length > 0) {
            if (!this.previewSignalDirection || !availableDirections.includes(this.previewSignalDirection)) {
              this.previewSignalDirectionIndex = 0;
              this.previewSignalDirection = availableDirections[0];
            }
          } else {
            this.previewSignalDirection = null;
          }
        }

        this.drawWithHoverPreview();
      } catch (err) {
        console.warn('[Editor] Signal validation API failed, using local validation:', err);
      }
    }, 50);
  }

  // Check if a tool is actively being used that needs hover preview preservation
  isToolActive(): boolean {
    const previewTools = ['signal', 'platform'];
    if (previewTools.includes(this.selectedTool) && this.hoverPosition) {
      return true;
    }
    if (this.isDraggingBuild && this.previewPath && this.previewPath.length > 0) {
      return true;
    }
    return false;
  }

  // Push the hover preview overlay to the renderer (painted in _drawLive so
  // it survives the rAF-coalesced flush) and request a redraw.
  drawWithHoverPreview(): void {
    if (!this.cachedNetwork) return;

    if (this.hoverPosition) {
      let trackBits = null;
      if (this.selectedTool === 'signal') {
        const existingTrack = this.cachedNetwork?.tracks?.find(
          (t: any) => t.x === this.hoverPosition.x && t.y === this.hoverPosition.y
        );
        trackBits = existingTrack?.track_bits || null;
      }

      this.renderer.setHoverPreview({
        gridPos: this.hoverPosition,
        toolType: this.selectedTool,
        isValid: this.hoverValid,
        signalDirection: this.previewSignalDirection,
        trackBits,
      });
    } else {
      this.renderer.setHoverPreview(null);
    }
    this.renderer.requestRedraw();
  }

  async handleRightClick(gridPos: any, clientX = 0, clientY = 0): Promise<void> {
    // Selection tools: right-click clears the current selection without leaving
    // the tool — same as the Space key (see _setupEventListeners). It does not
    // fall through to switch toggling while a selection tool is active.
    if (this._isSelectionTool(this.selectedTool)) {
      if (this.boxSelectState.selectedKeys.size > 0) this.clearMultiSelection();
      return;
    }

    if (this.activeMode?.name === 'Route creation' && this.pathToolState?.active) {
      await this.resetRouteToolState();
      showToast('Route creation cancelled', 'info', 3000);
      return;
    }

    if (
      this.selectedTool === 'signal' &&
      this.hoverPosition &&
      this.hoverPosition.x === gridPos.x &&
      this.hoverPosition.y === gridPos.y &&
      this.hoverValid
    ) {
      const validDirections = this.validSignalDirections || [];
      if (validDirections.length > 1) {
        this.previewSignalDirectionIndex = (this.previewSignalDirectionIndex + 1) % validDirections.length;
        this.previewSignalDirection = validDirections[this.previewSignalDirectionIndex];
        this.drawWithHoverPreview();
        return;
      }
    }

    try {
      const track = await this.api.getTrack(gridPos.x, gridPos.y);
      if (!track || track.error) return;
      this.showTileContextMenu(gridPos, track, clientX, clientY);
    } catch (err) {
      console.error('Right-click action failed:', err);
    }
  }

  /**
   * Build and show the right-click menu for a tile: one entry per entity on the
   * tile (train / switch / signal / block / platform), each opening that
   * entity's editor, plus a Throw action for switches and an "Inspect tile"
   * fallback. Entity resolution mirrors the properties panel.
   */
  showTileContextMenu(gridPos: any, track: any, clientX: number, clientY: number): void {
    const entityItems: ContextMenuItem[] = [];

    const trainsAtTile = (this.cachedNetwork?.trains || []).filter((t: any) => {
      const span = this.stateManager?.getController(t.id)?.span;
      return span?.occupied_tiles?.some((tile: any) => tile[0] === track.x && tile[1] === track.y);
    });
    for (const train of trainsAtTile) {
      entityItems.push({
        label: train.id,
        detail: 'Train',
        icon: 'ti ti-train',
        onSelect: () => {
          (window as any).app?.selectTrain?.(train.id);
        },
      });
    }
    if (track.is_switch && track.switch_id) {
      entityItems.push({
        label: track.switch_id,
        detail: 'Switch',
        icon: 'ti ti-arrows-split-2',
        onSelect: () => this.switchHandler.showEditor(track.switch_id),
      });
    }
    for (const signal of track.signals || []) {
      entityItems.push({
        label: signal.signal_id,
        detail: 'Signal',
        icon: 'ti ti-traffic-lights',
        onSelect: () => this.signalHandler.showSignalEditor(signal.signal_id),
      });
    }
    if (track.block_section_id) {
      entityItems.push({
        label: track.block_section_id,
        detail: 'Block',
        icon: 'ti ti-square-dashed',
        onSelect: () => this.blockHandler.showEditor(track.block_section_id),
      });
    }
    if (track.station_id) {
      entityItems.push({
        label: track.station_id,
        detail: 'Platform',
        icon: 'ti ti-building',
        onSelect: () => this.platformHandler.showEditor(track.station_id),
      });
    }

    const actionItems: ContextMenuItem[] = [];
    if (track.is_switch) {
      const toDiverge = track.switch_state === 0;
      actionItems.push({
        label: toDiverge ? 'Throw to diverge' : 'Throw to main',
        detail: this.editorMode === 'sim' ? 'operational' : undefined,
        icon: 'ti ti-switch-horizontal',
        emphasis: 'accent',
        onSelect: () => {
          void this.throwSwitchByMode(gridPos, track);
        },
      });
    }
    actionItems.push({
      label: 'Inspect tile',
      icon: 'ti ti-info-circle',
      onSelect: () => {
        void this.selectElement(gridPos);
      },
    });

    showContextMenu(clientX, clientY, [
      { heading: 'On this tile', items: entityItems },
      { items: actionItems },
    ]);
  }

  /**
   * Throw the switch on a tile, routed by mode: sim mode goes through the
   * operational SwitchManager command (validated, may be queued); edit mode is a
   * direct tile write. Mirrors the design's edit-vs-operational split.
   */
  async throwSwitchByMode(gridPos: any, track: any): Promise<void> {
    const newState = track.switch_state === 0 ? 1 : 0;
    try {
      if (this.editorMode === 'sim' && track.switch_id) {
        await this.api.setSwitchState(track.switch_id, newState);
        showToast(
          `Switch ${track.switch_id} → ${newState === 1 ? 'diverge' : 'main'} requested`,
          'info',
          2500,
        );
      } else {
        await this.api.updateTrack(gridPos.x, gridPos.y, { switch_state: newState });
      }
    } catch (switchErr) {
      showToast(this._parseApiError(switchErr, 'Cannot throw switch'), 'error', 5000);
      return;
    }
    await this.host?.refreshNetwork();
    await this.selectElement(gridPos);
  }

  /** Parse API error message from exception. */
  _parseApiError(err: any, fallbackPrefix: string): string {
    try {
      const msg = err.message || String(err);
      const jsonMatch = msg.match(/\{[\s\S]*\}/);
      if (jsonMatch) {
        const parsed = JSON.parse(jsonMatch[0]);
        if (parsed.error) {
          return `${fallbackPrefix}: ${parsed.error}`;
        }
      }
      return `${fallbackPrefix}: ${msg}`;
    } catch {
      return `${fallbackPrefix}: ${err.message || String(err)}`;
    }
  }

  async selectElement(gridPos: any): Promise<void> {
    // Selecting a tile entity drops any tunnel highlight from a prior click.
    this.tunnelEditorHandler?.clearSelection();
    try {
      const track = await this.api.getTrack(gridPos.x, gridPos.y);
      if (track && !track.error) {
        if (track.signals && track.signals.length > 0) {
          // The signals array contains {signal_id, direction, state, etc.}
        }

        if (track.signals && track.signals.length > 0) {
          await this.refreshBlocksList();
        }

        this.selectedElement = track;
        this.updatePropertiesPanel(track);

        if (!this.uiHandler.isSecondaryPanelPinned()) {
          this.updateSecondaryEditorForTile(track);
        }
      } else {
        this.selectedElement = {
          x: gridPos.x,
          y: gridPos.y,
          track_bits: 0,
          directed_paths: [],
          edges: [],
          isVirtual: true,
        };
        this.updatePropertiesPanel(this.selectedElement);

        if (!this.uiHandler.isSecondaryPanelPinned()) {
          this.updateSecondaryEditorForTile(this.selectedElement);
        }
      }
    } catch (err) {
      this.selectedElement = {
        x: gridPos.x,
        y: gridPos.y,
        track_bits: 0,
        directed_paths: [],
        edges: [],
        isVirtual: true,
      };
      this.updatePropertiesPanel(this.selectedElement);

      if (!this.uiHandler.isSecondaryPanelPinned()) {
        this.updateSecondaryEditorForTile(this.selectedElement);
      }
    }
  }

  updatePropertiesPanel(track: any): void {
    const panel = this.$q('.properties-panel');
    if (!panel) return;

    const trackBits: number = track.track_bits ?? 0;
    const edges = track.edges || [];

    const SEGMENT_NAMES: Record<number, string> = {
      1: 'NS', 2: 'EW', 4: 'NE', 8: 'NW', 16: 'SE', 32: 'SW',
    };

    const activeSegments = [];
    for (const [bit, name] of Object.entries(SEGMENT_NAMES)) {
      if (trackBits & parseInt(bit)) {
        activeSegments.push(name);
      }
    }

    const numSegments = activeSegments.length;
    let shapeDesc = 'Empty';
    if (numSegments === 1) {
      if (trackBits === 1 || trackBits === 2) shapeDesc = 'Straight';
      else shapeDesc = 'Diagonal';
    } else if (numSegments === 2) {
      if (trackBits === 3) shapeDesc = 'Crossing (NS+EW)';
      else if (trackBits === 36) shapeDesc = 'Diamond (NE+SW)';
      else if (trackBits === 24) shapeDesc = 'Diamond (NW+SE)';
      else shapeDesc = 'Switch';
    } else if (numSegments >= 3) {
      shapeDesc = 'Complex Junction';
    }

    const isForbidden = FORBIDDEN_TRACK_BITS.has(trackBits) || isSquareCornerSwitch(trackBits);

    // Binary string for the read-only Track Bits summary in the Properties
    // panel below (active bits get an accent span). The Track Bits editor panel
    // computes its own plain binary independently.
    const binaryHTML = trackBits
      .toString(2)
      .padStart(6, '0')
      .split('')
      .map((bit) => (bit === '1' ? `<span class="text-track-bit-active">${bit}</span>` : bit))
      .join('');

    const editorId = `track-editor-${Date.now()}`;

    panel.innerHTML = `
            <h3>Properties</h3>

            <div class="compact-header info-display-medium">
                <span>(${track.x}, ${track.y}) - ${track.isVirtual ? 'Empty' : track.type}</span>
                ${track.isVirtual ? '<span class="badge badge-new">NEW</span>' : ''}
            </div>

            <div class="info-box info-display-large">
                <label class="text-secondary font-bold block mb-4">Track Bits</label>
                <div class="text-2xl font-bold mb-2 text-primary font-mono">${trackBits} <span class="text-lg text-muted">(0b${binaryHTML})</span></div>
                <div class="text-muted text-base mb-8 info-meta">
                    ${shapeDesc} • ${numSegments} segment${numSegments !== 1 ? 's' : ''} • ${activeSegments.length > 0 ? activeSegments.join(', ') : 'No segments'} ${edges.length > 0 ? '• Edges: ' + edges.join(', ') : ''}
                </div>
                ${isForbidden ? '<div class="text-xs text-danger mt-8 font-bold">⚠ FORBIDDEN CONFIGURATION</div>' : ''}
            </div>

            ${(() => {
              const trainsAtTile =
                this.cachedNetwork && this.cachedNetwork.trains
                  ? this.cachedNetwork.trains.filter((t: any) => {
                      const span = this.stateManager?.getController(t.id)?.span;
                      if (span && span.occupied_tiles) {
                        return span.occupied_tiles.some((tile: any) => tile[0] === track.x && tile[1] === track.y);
                      }
                      return false;
                    })
                  : [];
              if (trainsAtTile.length === 0) return '';
              return `
                <div class="property-group-header">Train</div>
                ${trainsAtTile
                  .map((train: any) => {
                    const controller: any = this.stateManager?.getController(train.id);
                    const dotColor =
                      controller?.control_mode === 'manual'
                        ? 'var(--warning-color)'
                        : controller?.state === 'running_moving'
                          ? 'var(--success-color)'
                          : controller?.state === 'running_at_station'
                            ? 'var(--info-color)'
                            : controller?.state === 'idle'
                              ? 'var(--text-secondary)'
                              : 'var(--danger-color)';
                    return `
                <div class="property-item-row">
                    <div class="entity-card" style="--card-accent: var(--accent-color)">
                        <div class="entity-card-dot" style="background-color: ${dotColor}"></div>
                        <div class="entity-card-body">
                            <span class="entity-card-id">${train.id}</span>
                            <span class="entity-card-state">${controller?.state || 'unknown'}</span>
                        </div>
                        <button class="edit-btn edit-train-btn" data-train-id="${train.id}">Edit</button>
                    </div>
                </div>
                `;
                  })
                  .join('')}
                `;
            })()}

            ${
              track.switch_points && Object.keys(track.switch_points).length > 0
                ? (() => {
                    const switchStateColor = track.switch_state === 0 ? 'var(--success-color)' : 'var(--warning-color)';
                    const switchStateText = track.switch_state === 0 ? 'MAIN' : 'DIVERGE';
                    const lockSwitchEntity = this.cachedNetwork?.switches?.[track.switch_id] || null;
                    const isLocked = !!(lockSwitchEntity && Array.isArray(lockSwitchEntity.locks) && lockSwitchEntity.locks.length > 0);
                    const hasSwitchId = track.switch_id && track.switch_id !== '';
                    return `
                <div class="property-group-header">Switch</div>
                <div class="property-item-row">
                    <div class="entity-card" style="--card-accent: ${switchStateColor}">
                        <div class="entity-card-dot" style="background-color: ${switchStateColor};"></div>
                        <div class="entity-card-body">
                            <span class="entity-card-id">${track.switch_id || 'Switch'}</span>
                            <span class="entity-card-state" style="color: ${switchStateColor}">${switchStateText}${isLocked ? ' <i class="ti ti-lock"></i>' : ''}</span>
                        </div>
                        ${
                          hasSwitchId
                            ? `<button class="edit-btn edit-switch-btn" data-switch-id="${track.switch_id}">Edit</button>`
                            : `<span class="text-muted text-xs">No switch entity</span>`
                        }
                    </div>
                </div>
                `;
                  })()
                : ''
            }
            ${
              track.signals && track.signals.length > 0
                ? `
            <div class="property-group-header">Signals</div>
            ${track.signals
              .map((signal: any) => {
                const signalColor =
                  signal.signal_state === 'green' || signal.signal_state === 'GREEN'
                    ? 'var(--success-color)'
                    : signal.signal_state === 'yellow' || signal.signal_state === 'YELLOW'
                      ? 'var(--warning-color)'
                      : 'var(--danger-color)';
                return `
            <div class="property-item-row">
                <div class="entity-card" style="--card-accent: ${signalColor}">
                    <div class="entity-card-dot" style="background-color: ${signalColor}"></div>
                    <div class="entity-card-body">
                        <span class="entity-card-id">${signal.signal_id}</span>
                        <span class="entity-card-meta">→${signal.detection_direction}</span>
                    </div>
                    <button class="edit-btn edit-signal-btn" data-signal-id="${signal.signal_id}">Edit</button>
                </div>
            </div>
            `;
              })
              .join('')}
            `
                : ''
            }

            ${
              track.block_section_id
                ? (() => {
                    const block = this.cachedBlocks && this.cachedBlocks.find((b: any) => b.block_id === track.block_section_id);
                    const blockColor = this.getBlockColor(track.block_section_id);
                    const stateColor = block ? getBlockStateColor(block.state) : 'var(--text-secondary)';
                    return `
                <div class="property-group-header">Block</div>
                <div class="property-item-row">
                    <div class="entity-card" style="--card-accent: ${blockColor}">
                        <div class="entity-card-body">
                            <span class="entity-card-id">${track.block_section_id}</span>
                            <span class="entity-card-state" style="color: ${stateColor}">${block ? block.state : 'UNKNOWN'}</span>
                        </div>
                        <button class="edit-btn edit-block-btn" data-block-id="${track.block_section_id}">Edit</button>
                    </div>
                </div>
                `;
                  })()
                : ''
            }

            ${
              track.station_id
                ? (() => {
                    const platform =
                      this.cachedNetwork && this.cachedNetwork.platforms
                        ? this.cachedNetwork.platforms.find((p: any) => p.platform_id === track.station_id)
                        : null;
                    return `
                <div class="property-group-header">Platform</div>
                <div class="property-item-row">
                    <div class="entity-card" style="--card-accent: var(--info-color)">
                        <div class="entity-card-body">
                            <span class="entity-card-id">${track.station_id}</span>
                            ${platform && platform.name ? `<span class="entity-card-meta" style="color: var(--info-color)">${platform.name}</span>` : ''}
                        </div>
                        <button class="edit-btn edit-platform-btn" data-platform-id="${track.station_id}">Edit</button>
                    </div>
                </div>
                `;
                  })()
                : ''
            }

            ${
              track.station_id
                ? (() => {
                    const groups = Array.isArray(this.cachedNetwork?.station_groups)
                      ? this.cachedNetwork.station_groups
                      : [];
                    const group = groups.find((g: any) =>
                      (g.platform_ids || []).includes(track.station_id)
                    );
                    if (!group) return '';
                    return `
                <div class="property-group-header">Station</div>
                <div class="property-item-row">
                    <div class="entity-card" style="--card-accent: var(--info-color)">
                        <div class="entity-card-dot" style="background-color: var(--info-color)"></div>
                        <div class="entity-card-body">
                            <span class="entity-card-id">${group.station_id}</span>
                            ${group.name ? `<span class="entity-card-meta" style="color: var(--info-color)">${group.name}</span>` : ''}
                        </div>
                        <button class="edit-btn edit-station-btn" data-station-id="${group.station_id}">Edit</button>
                    </div>
                </div>
                `;
                  })()
                : ''
            }

            ${
              track.occupied_by
                ? `
            <div class="property-item">
                <label>Occupied By:</label>
                <span class="text-danger">${track.occupied_by}</span>
            </div>
            `
                : ''
            }
            ${
              track.dead_end_ports && track.dead_end_ports.length > 0
                ? `
            <div class="property-item">
                <div class="info-display-row info-display-compact">
                    <span class="info-label">Dead Ends</span>
                    <span class="info-value text-warning">${track.dead_end_ports.join(', ')}</span>
                </div>
            </div>
            `
                : ''
            }

            <button id="view-full-json-btn" class="btn btn-outline btn-full text-sm mt-12">
                <i class="ti ti-braces"></i> View Full JSON
            </button>
        `;

    // Render bit editor into its dedicated sidebar section
    this.updateTrackBitsPanel(track, editorId, trackBits, isForbidden);

    setTimeout(() => this.initBitEditor(track), 0);
    setTimeout(() => this.initVisualTrackEditor(editorId, track), 0);
    setTimeout(() => this.initSignalEditor(track), 0);
    setTimeout(() => this.initBlockEditor(track), 0);
    setTimeout(() => this.initSwitchEditor(track), 0);
    setTimeout(() => this.initStationCardButton(), 0);
    setTimeout(() => this.initPlatformButtonHandlers(), 0);
    setTimeout(() => this.initTrainEditor(track), 0);
    setTimeout(() => this.initViewFullJSONButton(track), 0);
  }

  initViewFullJSONButton(track: any): any {
    return this.uiHandler.initViewFullJSONButton(track);
  }

  initPlatformButtonHandlers(): any {
    return this.uiHandler.initPlatformButtonHandlers();
  }

  initSignalEditor(track: any): any {
    return this.signalHandler.initSignalEditor(track);
  }

  async showSignalEditor(signalId: any): Promise<any> {
    return this.signalHandler.showSignalEditor(signalId);
  }

  // Initialize block summary edit button handler
  initBlockEditor(track: any): void {
    if (!track.block_section_id) return;

    const editBtn = this.$q(
      `.edit-block-btn[data-block-id="${track.block_section_id}"]`
    ) as HTMLElement | null;
    if (editBtn) {
      editBtn.onclick = () => {
        this.showBlockEditor(track.block_section_id);
      };
    }
  }

  // Initialize switch edit button handler
  initSwitchEditor(track: any): void {
    if (!track.switch_id) return;

    const editBtn = this.$q(
      `.edit-switch-btn[data-switch-id="${track.switch_id}"]`
    ) as HTMLElement | null;
    if (editBtn) {
      editBtn.onclick = () => {
        this.showSwitchEditor(track.switch_id);
      };
    }
  }

  // Initialize the logical-station card Edit button shown on a platform tile —
  // jumps to the Station Editor for the station that owns the platform.
  initStationCardButton(): void {
    document.querySelectorAll<HTMLElement>('.edit-station-btn').forEach((btn) => {
      btn.onclick = () => {
        const sid = btn.dataset.stationId;
        if (sid) this.stationGroupHandler.showStationDetail(sid);
      };
    });
  }

  // Populate the dedicated Track Bits sidebar section.
  updateTrackBitsPanel(track: any, editorId: string, trackBits: number, isForbidden: boolean): void {
    const body = this.$id('track-bits-panel-body');
    if (!body) return;

    // Two views of one 6-bit value: the clickable compass and a single vertical
    // column of segment checkboxes beside it, with the readout full-width below.
    // The Track Bits section is the only container — no sub-boxes, no sub-headings,
    // no Apply button (every toggle/click auto-applies).
    const binary = trackBits.toString(2).padStart(6, '0');
    body.innerHTML = `
            <div class="track-bits-editor">
                <div class="track-bits-main">
                    <canvas id="${editorId}" class="track-bits-canvas" width="200" height="200"></canvas>
                    <div class="track-bits-grid">
                        ${renderBitToggle(1, 'NS', trackBits)}
                        ${renderBitToggle(2, 'EW', trackBits)}
                        ${renderBitToggle(4, 'NE', trackBits)}
                        ${renderBitToggle(8, 'NW', trackBits)}
                        ${renderBitToggle(16, 'SE', trackBits)}
                        ${renderBitToggle(32, 'SW', trackBits)}
                    </div>
                </div>
                <div class="track-bits-readout">
                    <span class="tb-raw">bits <b id="bit-total">${trackBits}</b> · 0b<span id="bit-binary">${binary}</span></span>
                    <span class="track-bits-forbidden" id="bit-forbidden" style="display: ${isForbidden ? 'inline' : 'none'};">forbidden</span>
                </div>
            </div>
        `;
  }

  // Reset the Track Bits sidebar section to the empty placeholder.
  clearTrackBitsPanel(): void {
    const body = this.$id('track-bits-panel-body');
    if (body) {
      body.innerHTML = `<p class="properties-empty">Select a track to edit bits</p>`;
    }
  }

  // Initialize train summary edit button handler
  initTrainEditor(track: any): void {
    const trainsAtTile =
      this.cachedNetwork && this.cachedNetwork.trains
        ? this.cachedNetwork.trains.filter((t: any) => {
            const span = this.stateManager?.getController(t.id)?.span;
            if (span && span.occupied_tiles) {
              return span.occupied_tiles.some((tile: any) => tile[0] === track.x && tile[1] === track.y);
            }
            return false;
          })
        : [];

    if (trainsAtTile.length === 0) return;

    trainsAtTile.forEach((train: any) => {
      const editBtn = this.$q(
        `.edit-train-btn[data-train-id="${train.id}"]`
      ) as HTMLElement | null;
      if (editBtn) {
        editBtn.onclick = () => {
          this.host?.selectTrain?.(train.id);
        };
      }
    });
  }

  initBitEditor(track: any): any {
    return this.uiHandler.initBitEditor(track);
  }

  initVisualTrackEditor(canvasId: string, track: any): any {
    return this.uiHandler.initVisualTrackEditor(canvasId, track);
  }

  pointToSegmentDistance(px: number, py: number, x1: number, y1: number, x2: number, y2: number): any {
    return this.uiHandler.pointToSegmentDistance(px, py, x1, y1, x2, y2);
  }

  clearPropertiesPanel(): any {
    return this.uiHandler.clearPropertiesPanel();
  }

  setCachedNetwork(network: any): void {
    this.cachedNetwork = network;

    // First load satisfies anything awaiting the network (see whenNetworkReady).
    if (this._resolveNetworkReady) {
      this._resolveNetworkReady();
      this._resolveNetworkReady = null;
    }

    if (this.hoverPosition && ['signal', 'platform'].includes(this.selectedTool)) {
      this.updateHoverPreview(this.hoverPosition);
    }
  }

  /** Resolves once the first network load has populated cachedNetwork, so
   *  deferred init can validate entities against the loaded scene. */
  whenNetworkReady(): Promise<void> {
    return this._networkReady;
  }

  /** Close the secondary-inspector tab for a now-missing entity (matched by
   *  type + id). Used when a panel-open request finds its entity gone, so a
   *  stale restored tab clears itself instead of surfacing an error. */
  closeStaleEditorTab(editorType: string, id: string): Promise<void> {
    return this.uiHandler.closeStaleSecondaryTab(editorType, id);
  }

  enterRouteToolMode(): any {
    return this.pathHandler?.enterRouteToolMode();
  }

  async exitRouteToolMode(): Promise<any> {
    return await this.pathHandler?.exitRouteToolMode();
  }

  async resetRouteToolState(): Promise<any> {
    return await this.pathHandler?.resetRouteToolState();
  }

  async handleRouteToolClick(gridPos: any): Promise<any> {
    return this.pathHandler?.handleRouteToolClick(gridPos);
  }

  async handleRouteFirstClick(gridPos: any, track: any): Promise<any> {
    return this.pathHandler?.handleRouteFirstClick(gridPos, track);
  }

  buildReachablePositionsSet(): any {
    return this.pathHandler?.buildReachablePositionsSet();
  }

  async handleRouteSecondClick(gridPos: any, track: any): Promise<any> {
    return this.pathHandler?.handleRouteSecondClick(gridPos, track);
  }

  showRoutePrioritySelector(): any {
    return this.pathHandler?.showRoutePrioritySelector();
  }

  async showPathsPanel(): Promise<any> {
    return this.pathHandler?.showPathsPanel();
  }

  renderPathListItem(path: any): any {
    return this.pathHandler?.renderPathListItem(path);
  }

  getPathStatusColor(status: any): any {
    return this.pathHandler?.getPathStatusColor(status);
  }

  initRoutesPanelHandlers(): any {
    return this.pathHandler?.initRoutesPanelHandlers();
  }

  async showPathEditor(pathId: any): Promise<any> {
    return this.pathHandler?.showPathEditor(pathId);
  }

  initPathEditorHandlers(pathId: any, path: any): any {
    return this.pathHandler?.initPathEditorHandlers(pathId, path);
  }

  enterExtendRouteMode(): any {
    return this.pathHandler?.enterExtendRouteMode();
  }

  async exitExtendRouteMode(): Promise<any> {
    return await this.pathHandler?.exitExtendRouteMode();
  }

  async resetExtendRouteState(): Promise<any> {
    return await this.pathHandler?.resetExtendRouteState();
  }

  async handleExtendRouteToolClick(gridPos: any): Promise<any> {
    return this.pathHandler?.handleExtendRouteToolClick(gridPos);
  }

  async handleExtendRouteFirstClick(gridPos: any, track: any): Promise<any> {
    return this.pathHandler?.handleExtendRouteFirstClick(gridPos, track);
  }

  buildExtendRoutePositionsSet(): any {
    return this.pathHandler?.buildExtendRoutePositionsSet();
  }

  async handleExtendRouteSecondClick(gridPos: any, track: any): Promise<any> {
    return this.pathHandler?.handleExtendRouteSecondClick(gridPos, track);
  }

  showExtendRoutePanel(): any {
    return this.pathHandler?.showExtendRoutePanel();
  }

  getDirectionName(dir: any): any {
    return this.pathHandler?.getDirectionName(dir);
  }

  enterCancelRouteMode(): any {
    return this.pathHandler?.enterCancelRouteMode();
  }

  exitCancelRouteMode(): any {
    return this.pathHandler?.exitCancelRouteMode();
  }

  async handleCancelRouteToolClick(gridPos: any): Promise<any> {
    return this.pathHandler?.handleCancelRouteToolClick(gridPos);
  }

  // === Train Routes Panel Methods ===

  async showRoutesPanel(): Promise<any> {
    return this.routeHandler?.showRoutesPanel();
  }

  // === Tunnel Tool ===

  async handleTunnelClick(gridPos: any, existingTrack: any): Promise<void> {
    const deadEndPorts = Array.isArray(existingTrack.dead_end_ports)
      ? existingTrack.dead_end_ports.map((p: any) => String(p).toUpperCase())
      : [];
    if (deadEndPorts.length === 0) {
      showToast(
        'Tunnel: clicked tile has no dead-end edge. Tunnels can only pair dead-ends.',
        'error',
        5000
      );
      return;
    }

    const edge = deadEndPorts.slice().sort()[0];
    const endpoint = { x: gridPos.x, y: gridPos.y, edge };

    if (!this._pendingTunnelEndpoint) {
      this._pendingTunnelEndpoint = endpoint;
      this.renderer.setPendingTunnelEndpoint(endpoint);
      showToast(
        `Tunnel: first endpoint (${endpoint.x}, ${endpoint.y}).${endpoint.edge} selected. Click another dead-end to pair.`,
        'info',
        4000
      );
      return;
    }

    const first = this._pendingTunnelEndpoint;
    this._pendingTunnelEndpoint = null;
    this.renderer.setPendingTunnelEndpoint(null);
    try {
      await this.api.createTunnel(first, endpoint);
      showToast(
        `Tunnel created: (${first.x}, ${first.y}).${first.edge} ↔ (${endpoint.x}, ${endpoint.y}).${endpoint.edge}`,
        'success',
        4000
      );
      await this.refreshAndSelect(null);
    } catch (err: any) {
      showToast(`Failed to create tunnel: ${err.message}`, 'error', 6000);
    }
  }
}
