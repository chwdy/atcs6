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
import { trainDisplayStatus } from '../lib/train-status';
import { showToast } from '../lib/toast';
import { showContextMenu, type ContextMenuItem } from '../lib/context-menu';
import { copyIdButton } from '../lib/copy-id';
import type { FullDataClient } from '../lib/layout-data-client';
import {
  ConstructionPathfinder,
  pathToTracks,
  tileSkipsSegment,
  validateSegmentPath,
  getValidSignalDirections,
} from '../lib/pathfinders';
import {
  showConfirmModal,
  renderBitToggle,
  getBlockStateColor,
  applyStructuralLock,
} from '../lib/helpers';
import { FORBIDDEN_TRACK_BITS, isSquareCornerSwitch } from '../lib/constants';
import { blockColorCss } from '../lib/block-color';
import { SegmentEditorHandler } from './editor-segments';
import { SignalEditorHandler } from './editor-signals';
import { PlatformEditorHandler, StationGroupEditorHandler } from './editor-stations';
import { BlockGroupingEditorHandler } from './editor-block-grouping';
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
import { SelectModesHandler } from './editor-select-modes';
import { DragBuildHandler } from './editor-drag-build';
import { ToolbarHandler } from './editor-toolbar';
import { LayoutHighlighter } from './layout-highlighter';
import type { ToolContribution, ToolMode } from './tool-registry';

/**
 * A plugin that augments the editor with extra handlers and tools. The sim
 * (trains, paths, routes, route-runs, speed profiles) is registered this way
 * (see sim-plugin.ts), keeping the core editor a layout-only editor.
 */
export interface EditorPlugin {
  register(editor: Editor): void;
  /**
   * Tool contributions this plugin adds. Merged with the core editor's tools
   * (see Editor.refreshTools). Optional: a behavior-only plugin may register
   * handlers without contributing any tools.
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

/**
 * A screen that shows the selection and today's panels in its own right
 * panel (the Operate screen, frontend/src/next/operate/panel-host.ts). Each
 * method returns true when the screen took the request, and false to leave
 * it to the panel host in editor-ui.ts (which the Editor screen shows).
 */
export interface NextPanelHost {
  /** A tile was picked on the map. */
  selectTile(tile: any): boolean;
  /** A train's controls were asked for. */
  showTrain(trainId: string): boolean;
  /** A panel's HTML, as today's inspector would show it. */
  showPanel(title: string, content: string): boolean;
  /** The open panel was asked to close. */
  hidePanel(): boolean;
  /** The panel with this editor type and id should close if it is open. */
  closePanel(editorType: string, editorId: string | null): void;
}

/**
 * Identity text for one entity row: its display name, with its stable id on the
 * second line.
 *
 * A name alone does not identify a row, because names repeat across kinds by
 * construction — creating a segment builds its owning block with that same name
 * (segment_geometry_manager.create_segment), so a tile's Segment and Block rows
 * would otherwise read as one label twice. The id line is what separates them.
 * An unnamed entity has no second line: its label is already the id.
 */
function entityIdentity(name: unknown, id: string): { label: string; sublabel?: string } {
  const trimmed = typeof name === 'string' ? name.trim() : '';
  return trimmed ? { label: trimmed, sublabel: id } : { label: id };
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
  cachedSegments: any[];
  selectedSegmentId: string | null;
  // Lazy per-block cache on the OTHER axis (power / occupancy). Signals monitor
  // blocks, so the signal editor fills this from `getBlock` on demand; it is
  // deliberately separate from `cachedSegments` so a block id and a segment id
  // can never be looked up in the same map.
  cachedBlocks: any[];
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
  segmentHandler: SegmentEditorHandler;
  signalHandler: SignalEditorHandler;
  platformHandler: PlatformEditorHandler;
  stationGroupHandler: StationGroupEditorHandler;
  blockGroupingHandler: BlockGroupingEditorHandler;
  // The block the assign tool targets (set when a block detail is open).
  _activeBlockId: string | null = null;
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
  selectModesHandler: SelectModesHandler;
  dragBuildHandler: DragBuildHandler;
  toolbarHandler: ToolbarHandler;
  // Single front door for "show me where this is on the layout" highlights.
  // Wired here; call sites migrate onto it phase by phase.
  highlighter: LayoutHighlighter;
  _plugins: EditorPlugin[] = [];
  // Tool id → contribution, rebuilt on every refreshTools(). Picking a tool
  // (ToolbarHandler.selectTool) looks it up here to dispatch by kind.
  _toolsById: Map<string, ToolContribution> = new Map();
  // Editor mode or Operate mode. The host (app.ts) sets this when the screen
  // changes; a tool whose availableIn excludes it stops being the active tool.
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
  // Set while a screen owns the right panel (see NextPanelHost).
  nextPanelHost: NextPanelHost | null = null;
  // Assigned lazily by event handlers.
  hasPanned = false;
  signalValidationTimeout: any = null;
  _lastValidatedPosition: any = null;
  _pendingTunnelEndpoint: any = null;
  // Set by the layout (frontend/src/next/). onToolChange hears every change
  // of the active tool, so the Editor tool bar can show it. tileClickPanel decides what a plain Select click on a tile
  // opens on the screen showing; it returns false to fall back to today's
  // behavior (see updateSecondaryEditorForTile).
  onToolChange: ((toolId: string) => void) | null = null;
  tileClickPanel: ((tile: any) => boolean) | null = null;

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
    this.cachedSegments = []; // Segment geometry cache
    this.selectedSegmentId = null; // Currently selected segment
    this.cachedBlocks = []; // Lazy block cache (power / occupancy axis)

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
    this.segmentHandler = new SegmentEditorHandler(this);
    this.signalHandler = new SignalEditorHandler(this);
    this.platformHandler = new PlatformEditorHandler(this);
    this.stationGroupHandler = new StationGroupEditorHandler(this);
    this.blockGroupingHandler = new BlockGroupingEditorHandler(this);
    // Sim handlers (train/path/route/routeRun/speedProfile) are instantiated by
    // the SimEditorPlugin via registerPlugin(), not here — see sim-plugin.ts.
    this.switchHandler = new SwitchEditorHandler(this);
    this.tunnelEditorHandler = new TunnelEditorHandler(this);
    this.uiHandler = new EditorUIHandler(this);
    this.multiSelectHandler = new MultiSelectActionsHandler(this);
    this.selectModesHandler = new SelectModesHandler(this);
    this.dragBuildHandler = new DragBuildHandler(this);
    this.toolbarHandler = new ToolbarHandler(this);

    // Lazy `renderer` getter so highlighter construction never depends on field
    // ordering. resolveEntityKeys is added when a call site needs entity
    // targets; tiles/keys targets work without it.
    const self = this;
    this.highlighter = new LayoutHighlighter({
      get renderer() {
        return self.renderer;
      },
      buildOutlineGroups: (keys) => self._buildSelectionOutlineGroups(keys),
    });

    this.setupEventListeners();
    // Keep the status-bar zoom % in sync with the camera no matter who changes
    // the scale (zoom buttons, wheel, Fit, initial framing). The camera emits
    // onChange on every viewport mutation.
    this.renderer.camera.onChange(() => this._syncZoomLabel());
    this.setupSegmentManager();
    this._currentEditorType = null; // 'segment', 'signal', or 'platform'
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
    // A plugin may contribute tools; rebuild the lookup so they can be picked.
    this.refreshTools();
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

  showSecondarySidebar(title: string, content: string): any {
    // Any editor opening clears a stale block-direction focus and a stale
    // tunnel highlight. The tunnel handler re-sets its highlight right after
    // this call when it is the panel being opened.
    if (this.renderer?.clearBlockDirectionFocus) {
      this.renderer.clearBlockDirectionFocus();
    }
    // A new panel makes the old panel's pointer highlights stale — the inspect
    // peek and the selected tunnel pair both go.
    this.highlighter.clear(['peek', 'pair']);
    // Opening any panel other than Add Train drops a lingering train-placement
    // ghost and exits the place-train mode, so neither can outlive its panel.
    if (title !== 'Add Train') {
      this.renderer?.setTrainGhostPreview?.(null);
      if (this.activeMode?.toolName === 'place-train') this.exitMode();
    }
    if (this.nextPanelHost?.showPanel(title, content)) return;
    return this.uiHandler.showSecondarySidebar(title, content);
  }

  // Re-run the structural lock on the open inspector after the editor mode
  // flips. Each structural panel already locks itself on render; this catches
  // the case where the user toggles Editor <-> Simulation while a panel stays
  // open. Operational panels (path / route / train / speed profile) are not in
  // the set — the dispatcher edits those freely while simulating.
  reapplyStructuralLock(): void {
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
    if (this._currentEditorType && STRUCTURAL.has(this._currentEditorType)) {
      applyStructuralLock(this.$id('secondary-sidebar-content'));
    }
  }

  hideSecondarySidebar(options: any = {}): any {
    // uiHandler.hideSecondarySidebar() takes no args (it ignored `options` in
    // the classic code too); keep the param on the facade for callers.
    this.highlighter.clear(['peek', 'pair']);
    if (this.nextPanelHost?.hidePanel()) return;
    return this.uiHandler.hideSecondarySidebar();
  }

  navigateToParentList(): any {
    return this.uiHandler.navigateToParentList();
  }

  async restoreSecondaryPanel(snapshot: any): Promise<boolean> {
    const type = snapshot?.editorType;
    const id = snapshot?.editorId;

    switch (type) {
      case 'tile': {
        if (!id) return false;
        const [xs, ys] = String(id).split(',');
        const x = parseInt(xs, 10);
        const y = parseInt(ys, 10);
        if (Number.isNaN(x) || Number.isNaN(y)) return false;
        let tile: any;
        try {
          const fetched = await this.api.getTrack(x, y);
          tile =
            fetched && !fetched.error
              ? fetched
              : { x, y, track_bits: 0, directed_paths: [], edges: [], isVirtual: true };
        } catch {
          tile = { x, y, track_bits: 0, directed_paths: [], edges: [], isVirtual: true };
        }
        this.showTileEditor(tile);
        return true;
      }
      case 'segment':
        if (!id) return false;
        await this.showSegmentEditor(id);
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
      case 'block-detail':
        if (!id) return false;
        await this.blockGroupingHandler.showDetail(id);
        return true;
      case 'block-list':
        await this.showBlocksPanel();
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
   *  `← Parent` button. A null-editorType parent is context only — signals and
   *  platforms have no list panel to go to. */
  getPanelLineage(entityView: any): any[] {
    const type = entityView?.editorType || entityView?.type || null;
    const id = entityView?.editorId || entityView?.id || null;
    const title = String(entityView?.title || '').trim();

    switch (type) {
      case 'segment':
        return [
          { label: 'Segments', editorType: null, editorId: null },
          { label: title && title !== 'Segment Editor' ? title : id || 'Segment', editorType: 'segment', editorId: id },
        ];
      case 'block-list':
        return [{ label: 'Blocks', editorType: 'block-list', editorId: null }];
      case 'block-detail':
        return [
          { label: 'Blocks', editorType: 'block-list', editorId: null },
          { label: title && title !== 'Block' ? title : id || 'Block', editorType: 'block-detail', editorId: id },
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
    if (this.tileClickPanel?.(track)) return;
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
      case 'segment':
        if (!id) return;
        itemExists = Object.values(this.cachedNetwork?.blocks || {}).some((b: any) =>
          (b?.segment_ids || []).includes(id)
        );
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
      case 'block-detail':
        if (!id) return;
        itemExists = !!this.cachedNetwork?.blocks?.[id];
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
      default:
        // Any panel that is not one of the delete-by-id entity editors above
        // (tile, multi-select, tunnel, the *-list panels, …) has no entity that
        // a refresh could have deleted, so keep it open. Treating an
        // unrecognized type as "gone" hid the whole sidebar on every
        // refreshNetwork() and the follow-up re-select reopened it — a visible
        // flash of the panel (and, while docked, a layout reflow as the canvas
        // widened then narrowed). Throwing a switch hit this directly:
        // throwSwitchByMode() calls refreshNetwork() then selectElement(). These
        // panels close only through their explicit close paths.
        return;
    }

    if (!itemExists) {
      this.hideSecondarySidebar();
    }
  }

  async showSegmentEditor(segmentId: any): Promise<any> {
    return this.segmentHandler.showEditor(segmentId);
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

  async showBlocksPanel(): Promise<any> {
    return this.blockGroupingHandler.showPanel();
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
    if (this.nextPanelHost?.showTrain(trainId)) return;
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
        kind: { type: 'mode', factory: () => this.selectModesHandler._buildTraceSelectMode() },
      },
      {
        id: 'box-select', label: 'Box', icon: 'ti ti-box-multiple',
        title: 'Box Select (drag a rectangle to mark tracks. Shift = remove. Space clears, Delete removes tracks)',
        group: 'Selection', availableIn: ['edit'],
        kind: { type: 'mode', factory: () => this.selectModesHandler._buildBoxSelectMode() },
      },
      {
        id: 'lasso-select', label: 'Lasso', icon: 'ti ti-lasso',
        title: 'Lasso Select (draw a shape around tracks to mark them. Shift = remove. Space clears, Delete removes tracks)',
        group: 'Selection', availableIn: ['edit'],
        kind: { type: 'mode', factory: () => this.selectModesHandler._buildLassoSelectMode() },
      },
      {
        id: 'paint-select', label: 'Brush', icon: 'ti ti-brush',
        title: 'Brush Select (click or drag to paint tracks into the selection. Shift = remove. Space clears, Delete removes tracks)',
        group: 'Selection', availableIn: ['edit'],
        kind: { type: 'mode', factory: () => this.selectModesHandler._buildPaintSelectMode() },
      },
      {
        id: 'entity-select', label: 'Entity', icon: 'ti ti-click',
        title: 'Entity Select (click a tile, then pick a block / platform / station — or all of them — to add its tracks to the selection. Space clears, Delete removes tracks)',
        group: 'Selection', availableIn: ['edit'],
        kind: { type: 'mode', factory: () => this.selectModesHandler._buildEntitySelectMode() },
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
        id: 'segment', label: 'New', icon: 'ti ti-plus', title: 'Create New Segment',
        group: 'Segment', availableIn: ['edit'], kind: { type: 'tool' },
      },
      {
        id: 'extend-segment', label: 'Extend', icon: 'ti ti-arrows-horizontal',
        title: 'Extend Existing Segment',
        group: 'Segment', availableIn: ['edit'], kind: { type: 'tool' },
      },
      {
        id: 'remove-segment-tile', label: 'Remove', icon: 'ti ti-minus',
        title: 'Remove Segment Tile',
        group: 'Segment', availableIn: ['edit'], kind: { type: 'tool' },
      },

      {
        id: 'blocks-panel', label: 'Blocks', icon: 'ti ti-bolt',
        title: 'Blocks — group segments onto one power feed',
        group: 'Power', availableIn: ['edit', 'sim'],
        kind: { type: 'action', run: () => this.showBlocksPanel() },
      },
      {
        id: 'block-assign', label: 'Assign', icon: 'ti ti-click',
        title: 'Assign segments to the open block (click a segment to add it, click a member again to remove it)',
        group: 'Power', availableIn: ['edit'], kind: { type: 'tool' },
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

  // Picking a tool lives on ToolbarHandler (editor-toolbar.ts).

  refreshTools(): void {
    this.toolbarHandler.refreshTools();
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

  // "x,y" → track map over cachedNetwork.tracks, rebuilt only when the tracks
  // array reference changes. cachedNetwork is replaced wholesale on every
  // network update, so identity comparison is exact; during a drag it is stable,
  // so the trace/preview methods (updatePreviewPath runs on every mousemove)
  // reuse this map instead of rebuilding it over all tracks each call.
  private _tracksByKeyCache: Map<string, any> | null = null;
  private _tracksByKeySource: any[] | null = null;
  // Public because the drag-build and select-mode handlers depend on it (it is
  // part of their host surface — see DragBuildHost / SelectModesHost in types.ts).
  _tracksByKey(): Map<string, any> {
    const tracks = this.cachedNetwork?.tracks || [];
    if (this._tracksByKeyCache && this._tracksByKeySource === tracks) {
      return this._tracksByKeyCache;
    }
    const byKey = new Map<string, any>(tracks.map((t: any) => [`${t.x},${t.y}`, t]));
    this._tracksByKeyCache = byKey;
    this._tracksByKeySource = tracks;
    return byKey;
  }

  // Drop the pending trace anchor and clear the orange reachable-tile overlay.
  // Safe to call from the shared selection-clear paths (Space / Esc): for the
  // other mark-select tools the anchor is already null and the overlay empty.
  _resetTraceAnchor(): void {
    this.traceSelectState.firstPoint = null;
    this.highlighter.clear('reachable');
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
    this.refreshTools();

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
        this.hasPanned = false;
        this.lastMouseX = e.clientX;
        this.lastMouseY = e.clientY;
      }
      // Left mouse + path-based tools = start drag
      else if (
        e.button === 0 &&
        ['track', 'segment', 'extend-segment', 'platform', 'extend-platform', 'remove-platform-tile', 'remove-segment-tile'].includes(
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
        this.handleSingleClickPlace(gridPos, e);
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
            ['remove-platform-tile', 'remove-segment-tile'].includes(this.selectedTool) &&
            isSingleClick
          ) {
            await this.handleRemovalClick(this.dragStart);
          } else if (
            ['segment', 'track', 'extend-segment', 'platform', 'extend-platform', 'remove-platform-tile', 'remove-segment-tile'].includes(
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

      // Block assign tool: click a segment to toggle its membership in the
      // block open in the detail panel.
      if (this.selectedTool === 'block-assign') {
        await this.blockGroupingHandler.handleAssignClick(gridPos);
        return;
      }

      // Only handle selection with select tool
      if (this.selectedTool !== 'inspect') return;

      // Tunnel mouths are overlay glyphs sitting on a tile's edge — give them
      // first shot before tile selection, mirroring station labels above.
      const tunnelHit = this.renderer.tunnelHandler?.tunnelMouthAt?.(x, y);
      if (tunnelHit) {
        const current = this.renderer.selectedTunnel;
        if (current && this.tunnelEditorHandler._samePair(current, tunnelHit.pair)) {
          // Clicking the already-selected tunnel toggles it off — no re-flash.
          this.tunnelEditorHandler.clearSelection();
          this.hideSecondarySidebar();
          return;
        }
        await this.tunnelEditorHandler.showEditor(tunnelHit.pair);
        // Reveal where this mouth connects: flash the far end (and pan to it if
        // it's off-screen) so you can find the partner among many tunnels.
        this._revealTunnelPartner(tunnelHit.partner);
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
        await this.hideSecondarySidebar();
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
    this.renderer.showSegmentColorsOnly = false;
    this.renderer.setBuildPreview(null);
    this.renderer.setHoverPreview(null);
  }

  // Pure DOM + state update. Does NOT call enter/exitMode (no recursion).
  _setSelectedTool(toolName: string): void {
    this.selectedTool = toolName;
    // A tool change is a fresh gesture — drop any inspect peek highlight so it
    // can't linger over a new selection/build.
    this.clearInspectHighlight();
    this.onToolChange?.(toolName);
  }

  // Route the committed selection through the highlighter, then sync the panel.
  _applyBoxSelection(keys: any): void {
    this.highlighter.show({ keys }, { channel: 'selection' });
    this.multiSelectHandler?.update(this.boxSelectState.selectedKeys);
  }

  // Inspect "peek" highlight — outline a single entity's tiles on the canvas
  // without touching the drag-select state or opening the multi-select panel.
  // Entity editors use it to point at the switch a cross-link row refers to
  // ("show me where this is") while the user stays on the current editor. It is
  // cleared on any panel swap (showSecondarySidebar / hideSecondarySidebar) or
  // tool change, so a peek never outlives the moment.
  highlightInspectTiles(keys: Set<string>): void {
    this.highlighter.show({ keys }, { channel: 'peek' });
  }

  clearInspectHighlight(): void {
    this.highlighter.clear('peek');
  }

  // Group the selected tiles by the entity each belongs to, so the renderer can
  // trace one coloured perimeter per entity (segment / platform / switch /
  // signal) instead of a field of tiles. A tile can land in several groups (a
  // switch tile is also in a segment); each group carries only its own tiles, so
  // its outline is exactly that entity's footprint. Colours match the
  // selection-panel chips. The segment outline is thin, light, and drawn last
  // (on top) so it stays a delicate border; platform / switch / signal are a
  // touch wider and drawn first, so they still peek around the segment when they
  // share its tiles. The outlined geometry unit is the SEGMENT — a block groups
  // segments and has no footprint of its own to trace.
  _buildSelectionOutlineGroups(keys: Set<string>): any[] {
    if (!keys || keys.size === 0) return [];
    const tracks = this.cachedNetwork?.tracks || [];
    const byKey = new Map<string, any>();
    for (const t of tracks) byKey.set(`${t.x},${t.y}`, t);

    const segments = new Map<string, Set<string>>();
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
      if (t.segment_id) add(segments, t.segment_id, key);
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
    // Mix a hex colour toward white so the segment border reads light, not bold.
    const lighten = (hex: string, amt: number): string => {
      const m = hex.trim().replace('#', '');
      const full = m.length === 3 ? m.split('').map((c) => c + c).join('') : m;
      if (full.length !== 6) return hex;
      const ch = (i: number): number => parseInt(full.slice(i, i + 2), 16);
      const mix = (c: number): number => Math.round(c + (255 - c) * amt);
      return `rgb(${mix(ch(0))}, ${mix(ch(2))}, ${mix(ch(4))})`;
    };
    const segmentColor = lighten(hue('--accent-hover', '#9466ff'), 0.45);
    const platformColor = hue('--info-color', '#00d4ff');
    const switchColor = hue('--warning-color', '#ffcd1e');
    const signalColor = hue('--success-color', '#04d361');

    // Draw platform / switch / signal first (a touch wider) and the segment last
    // (thin + light, on top) so the segment stays a delicate border that never
    // hides the entities sharing its tiles.
    const groups: any[] = [];
    for (const s of platforms.values()) groups.push({ keys: s, color: platformColor, width: 3.5, dash: [7, 5] });
    for (const s of switches.values()) groups.push({ keys: s, color: switchColor, width: 3.5, dash: [2, 4] });
    for (const s of signals.values()) groups.push({ keys: s, color: signalColor, width: 3.5, dash: [7, 4, 2, 4] });
    for (const s of segments.values()) groups.push({ keys: s, color: segmentColor, width: 2, dash: [] });
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

  setupSegmentManager(): void {
    this.segmentHandler.setup();
  }

  async refreshSegmentsList(): Promise<any> {
    return this.segmentHandler.refreshList();
  }

  async deleteSegment(segmentId: any): Promise<any> {
    return this.segmentHandler.delete(segmentId);
  }

  getSegmentColor(segmentId: any): any {
    return this.segmentHandler.getColor(segmentId);
  }

  showSegmentModal(options: any): any {
    return this.segmentHandler.showModal(options);
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

  updatePreviewPath(): void {
    this.dragBuildHandler.updatePreviewPath();
  }

  async commitDragBuild(): Promise<void> {
    return this.dragBuildHandler.commitDragBuild();
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
      } else if (this.selectedTool === 'remove-segment-tile') {
        if (!existingTrack.segment_id) {
          showToast('No segment at this position', 'error', 3000);
          return;
        }

        try {
          const result = await this.api.removeSegmentTiles(existingTrack.segment_id, [
            [gridPos.x, gridPos.y],
          ]);

          if (result.removed > 0) {
            showToast('Segment tile removed', 'success');
          } else if (result.errors && result.errors.length > 0) {
            showToast(`Cannot remove: ${result.errors[0]}`, 'error', 5000);
          }
        } catch (err: any) {
          showToast(`Failed to remove segment tile: ${err.message}`, 'error', 5000);
        }
      }

      await this.refreshAndSelect(null);
    } catch (err) {
      console.error('[Editor] Failed to handle removal click:', err);
    }
  }

  // Single click placement for signal/platform/tunnel tools
  async handleSingleClickPlace(gridPos: any, ev?: MouseEvent): Promise<void> {
    const existingTrack = this.cachedNetwork?.tracks?.find(
      (t: any) => t.x === gridPos.x && t.y === gridPos.y
    );

    if (!existingTrack) {
      showToast(`Cannot place ${this.selectedTool}: No track at this position`, 'error', 5000);
      return;
    }

    if (this.selectedTool === 'tunnel') {
      await this.handleTunnelClick(gridPos, existingTrack, ev);
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
      const tile =
        track && !track.error
          ? track
          : { x: gridPos.x, y: gridPos.y, track_bits: 0, directed_paths: [], edges: [], isVirtual: true };
      this.showTileContextMenu(gridPos, tile, clientX, clientY);
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
        onSelect: () => this.showTrainControlPanel(train.id),
      });
    }
    if (track.is_switch && track.switch_id) {
      const sw = this.cachedNetwork?.switches?.[track.switch_id];
      entityItems.push({
        ...entityIdentity(sw?.name, track.switch_id),
        detail: 'Switch',
        icon: 'ti ti-arrows-split-2',
        onSelect: () => this.switchHandler.showEditor(track.switch_id),
      });
    }
    // GET /api/track carries `signal_ids` only — the enriched `signals` array
    // is built on the network payload's tiles (RailwayNetwork.to_dict). Take
    // whichever the caller handed us, and resolve names through the network's
    // signal map so an id with no record still gets its row.
    const signalsById = this.cachedNetwork?.signals || {};
    const tileSignals: any[] = track.signals?.length
      ? track.signals
      : (track.signal_ids || []).map((sid: string) => signalsById[sid] || { signal_id: sid });
    for (const signal of tileSignals) {
      entityItems.push({
        ...entityIdentity(signal.name, signal.signal_id),
        detail: 'Signal',
        icon: 'ti ti-traffic-lights',
        onSelect: () => this.signalHandler.showSignalEditor(signal.signal_id),
      });
    }
    // A tile belongs to a segment (geometry) and to the block that owns it
    // (power / occupancy). Offer both, each opening its own editor. The two
    // carry the same name whenever the block is the segment's own — so each row
    // reads its own record's name and shows its own id underneath.
    const block = track.block_section_id
      ? this.cachedNetwork?.blocks?.[track.block_section_id]
      : null;
    if (track.segment_id) {
      const segment = (block?.segments || []).find((s: any) => s.segment_id === track.segment_id);
      entityItems.push({
        ...entityIdentity(segment?.name, track.segment_id),
        detail: 'Segment',
        icon: 'ti ti-square-dashed',
        onSelect: () => this.segmentHandler.showEditor(track.segment_id),
      });
    }
    if (track.block_section_id) {
      entityItems.push({
        ...entityIdentity(block?.name, track.block_section_id),
        detail: 'Block',
        icon: 'ti ti-bolt',
        onSelect: () => this.blockGroupingHandler.showDetail(track.block_section_id),
      });
    }
    if (track.station_id) {
      const platform = (this.cachedNetwork?.platforms || []).find(
        (p: any) => p.platform_id === track.station_id,
      );
      entityItems.push({
        ...entityIdentity(platform?.name, track.station_id),
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
      // Asked for by name, so the Tile Editor opens whatever is showing.
      onSelect: () => this.selectElement(gridPos, { force: true }),
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

  /**
   * Select a tile and show it in the panel.
   *
   * Incidental selection — clicking around the canvas, resyncing after an edit —
   * defers to whatever the panel is already showing: another entity's editor
   * stays in place (see updateSecondaryEditorForTile). `force` is for the
   * opposite case: the user asked for this tile by name through the
   * right-click "Inspect tile" action, so the Tile Editor opens whatever else
   * is on screen.
   */
  async selectElement(gridPos: any, options: { force?: boolean } = {}): Promise<void> {
    // Selecting a tile entity drops any tunnel highlight from a prior click.
    this.tunnelEditorHandler?.clearSelection();

    const virtualTile = {
      x: gridPos.x,
      y: gridPos.y,
      track_bits: 0,
      directed_paths: [],
      edges: [],
      isVirtual: true,
    };

    let tile: any;
    try {
      const track = await this.api.getTrack(gridPos.x, gridPos.y);
      if (track && !track.error) {
        tile = track;
        if (track.signals && track.signals.length > 0) {
          await this.refreshSegmentsList();
        }
      } else {
        tile = virtualTile;
      }
    } catch (err) {
      tile = virtualTile;
    }

    this.selectedElement = tile;

    if (!options.force && this.nextPanelHost?.selectTile(tile)) return;
    if (options.force) {
      this.showTileEditor(tile);
    } else {
      this.updateSecondaryEditorForTile(tile);
    }
  }

  /** Open the Tile Editor for a tile in the secondary inspector: the visual bit
   *  compass + the segment checkbox grid + a live bits readout and shape line.
   *  This is the one place track bits are edited; both the compass and the grid
   *  route every change through the validated commit path (_commitTrackBits). */
  showTileEditor(track: any): void {
    const trackBits: number = track.track_bits ?? 0;
    const isForbidden = FORBIDDEN_TRACK_BITS.has(trackBits) || isSquareCornerSwitch(trackBits);
    const editorId = `track-editor-${Date.now()}`;

    const SEGMENT_NAMES: Record<number, string> = {
      1: 'NS', 2: 'EW', 4: 'NE', 8: 'NW', 16: 'SE', 32: 'SW',
    };
    const activeSegments: string[] = [];
    for (const [bit, name] of Object.entries(SEGMENT_NAMES)) {
      if (trackBits & parseInt(bit)) activeSegments.push(name);
    }
    const numSegments = activeSegments.length;
    let shapeDesc = 'Empty';
    if (numSegments === 1) {
      shapeDesc = trackBits === 1 || trackBits === 2 ? 'Straight' : 'Diagonal';
    } else if (numSegments === 2) {
      if (trackBits === 3) shapeDesc = 'Crossing (NS+EW)';
      else if (trackBits === 36) shapeDesc = 'Diamond (NE+SW)';
      else if (trackBits === 24) shapeDesc = 'Diamond (NW+SE)';
      else shapeDesc = 'Switch';
    } else if (numSegments >= 3) {
      shapeDesc = 'Complex Junction';
    }

    const edges = track.edges || [];
    const binary = trackBits.toString(2).padStart(6, '0');

    const content = `
      <div class="tile-editor">
        <div class="compact-header info-display-medium">
          <span>(${track.x}, ${track.y}) — ${track.isVirtual ? 'Empty' : track.type}</span>
          ${track.isVirtual ? '<span class="badge badge-new">NEW</span>' : ''}
        </div>
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
        <div class="tile-bits-shape info-meta text-muted text-base">
          ${shapeDesc} • ${numSegments} segment${numSegments !== 1 ? 's' : ''} • ${activeSegments.length > 0 ? activeSegments.join(', ') : 'No segments'}${edges.length > 0 ? ' • Edges: ' + edges.join(', ') : ''}
        </div>

        <h3 class="tile-properties-header"><i class="ti ti-square panel-title-icon" aria-hidden="true"></i>Properties</h3>
        ${this._tilePropertiesHTML(track)}
      </div>
    `;

    this._currentEditorType = 'tile';
    this._currentEditorId = `${track.x},${track.y}`;
    this.showSecondarySidebar('Tile Editor', content);

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

  /** The entity cards for a tile — train, switch, signals, block, platform,
   *  station, plus occupancy / dead-end notes and the View Full JSON button.
   *  Rendered inside the Tile Editor; the per-card Edit buttons are wired by the
   *  init*Editor handlers that showTileEditor schedules. */
  _tilePropertiesHTML(track: any): string {
    return `
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
                    const status = trainDisplayStatus(
                      controller,
                      this.stateManager?.getOperatorStatus(train.id),
                    );
                    const dotColor =
                      status === 'eb'
                        ? 'var(--danger-color)'
                        : status === 'running'
                          ? 'var(--success-color)'
                          : status === 'dwelling'
                            ? 'var(--info-color)'
                            : status === 'manual'
                              ? 'var(--warning-color)'
                              : 'var(--text-secondary)';
                    return `
                <div class="property-item-row">
                    <div class="entity-card" style="--card-accent: var(--accent-color)">
                        <div class="entity-card-dot" style="background-color: ${dotColor}"></div>
                        <div class="entity-card-body">
                            <span class="entity-card-id">${train.id}</span>
                            <span class="entity-card-state">${status}</span>
                        </div>
                        ${copyIdButton(train.id, 'Train ID')}
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
                            ? `${copyIdButton(track.switch_id, 'Switch ID')}<button class="edit-btn edit-switch-btn" data-switch-id="${track.switch_id}">Edit</button>`
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
                    ${copyIdButton(signal.signal_id, 'Signal ID')}
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
              track.segment_id
                ? (() => {
                    const segmentColor = this.getSegmentColor(track.segment_id);
                    return `
                <div class="property-group-header">Segment</div>
                <div class="property-item-row">
                    <div class="entity-card" style="--card-accent: ${segmentColor}">
                        <div class="entity-card-body">
                            <span class="entity-card-id">${track.segment_id}</span>
                            <span class="entity-card-meta">geometry</span>
                        </div>
                        ${copyIdButton(track.segment_id, 'Segment ID')}
                        <button class="edit-btn edit-segment-btn" data-segment-id="${track.segment_id}">Edit</button>
                    </div>
                </div>
                `;
                  })()
                : ''
            }

            ${
              track.block_section_id
                ? (() => {
                    const block = this.cachedNetwork?.blocks?.[track.block_section_id];
                    const blockColor = blockColorCss(track.block_section_id);
                    const stateColor = block ? getBlockStateColor(block.state) : 'var(--text-secondary)';
                    return `
                <div class="property-group-header">Block</div>
                <div class="property-item-row">
                    <div class="entity-card" style="--card-accent: ${blockColor}">
                        <div class="entity-card-body">
                            <span class="entity-card-id">${track.block_section_id}</span>
                            <span class="entity-card-state" style="color: ${stateColor}">${block ? block.state : 'UNKNOWN'}</span>
                        </div>
                        ${copyIdButton(track.block_section_id, 'Block ID')}
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
                        ${copyIdButton(track.station_id, 'Platform ID')}
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
                        ${copyIdButton(group.station_id, 'Station ID')}
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

  // Initialize the segment (geometry) and block (grouping) summary edit buttons.
  // A tile belongs to both: its segment carries geometry, its block carries
  // power/occupancy, so each card opens its own editor.
  initBlockEditor(track: any): void {
    if (track.segment_id) {
      const segmentBtn = this.$q(
        `.edit-segment-btn[data-segment-id="${track.segment_id}"]`
      ) as HTMLElement | null;
      if (segmentBtn) {
        segmentBtn.onclick = () => {
          this.showSegmentEditor(track.segment_id);
        };
      }
    }

    if (track.block_section_id) {
      const blockBtn = this.$q(
        `.edit-block-btn[data-block-id="${track.block_section_id}"]`
      ) as HTMLElement | null;
      if (blockBtn) {
        blockBtn.onclick = () => {
          this.blockGroupingHandler.showDetail(track.block_section_id);
        };
      }
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

  setCachedNetwork(network: any): void {
    this.cachedNetwork = network;

    if (this.hoverPosition && ['signal', 'platform'].includes(this.selectedTool)) {
      this.updateHoverPreview(this.hoverPosition);
    }
  }

  /** Close the panel for a now-missing entity (matched by type + id), so a
   *  panel-open request that finds its entity gone clears the panel instead
   *  of surfacing an error. */
  closeStaleEditorTab(editorType: string, id: string): Promise<void> {
    this.nextPanelHost?.closePanel(editorType, id);
    return this.uiHandler.closeStaleSecondaryTab(editorType, id);
  }

  /** Close the panel with this editor type + id if it is showing. `id` is
   *  null for panels with no entity, such as the Create Path setup panel. */
  closeEditorTab(editorType: string, id: string | null): Promise<void> {
    this.nextPanelHost?.closePanel(editorType, id);
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

  async handleTunnelClick(gridPos: any, existingTrack: any, ev?: MouseEvent): Promise<void> {
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

    // A single dead-end is unambiguous — take it. An isolated segment has a
    // dead-end at each end, so two (or more) ports means the tool can't know
    // which end the user wants: pop up a menu and let them choose by where the
    // end sits on screen.
    if (deadEndPorts.length === 1) {
      await this._commitTunnelEndpoint({ x: gridPos.x, y: gridPos.y, edge: deadEndPorts[0] });
      return;
    }

    // Screen-space description of each tile edge in the fixed isometric
    // projection, mirroring getEdgePoints() in renderer-tracks.ts: N/W sit above
    // the tile centre and E/S below it; N/E to the right, S/W to the left.
    const EDGE_CHOICE: Record<string, { label: string; icon: string; up: boolean }> = {
      N: { label: 'Top-right end', icon: 'ti ti-arrow-up-right', up: true },
      W: { label: 'Top-left end', icon: 'ti ti-arrow-up-left', up: true },
      E: { label: 'Bottom-right end', icon: 'ti ti-arrow-down-right', up: false },
      S: { label: 'Bottom-left end', icon: 'ti ti-arrow-down-left', up: false },
    };

    const items: ContextMenuItem[] = deadEndPorts
      .slice()
      // Topmost end first, matching how the two ends read on screen.
      .sort((a: string, b: string) => Number(EDGE_CHOICE[b]?.up ?? false) - Number(EDGE_CHOICE[a]?.up ?? false))
      .map((edge: string) => ({
        label: EDGE_CHOICE[edge]?.label ?? `${edge} end`,
        detail: edge,
        icon: EDGE_CHOICE[edge]?.icon ?? 'ti ti-arrow-ramp-right',
        onSelect: () => this._commitTunnelEndpoint({ x: gridPos.x, y: gridPos.y, edge }),
      }));

    const rect = this.renderer.canvas.getBoundingClientRect();
    const clientX = ev?.clientX ?? rect.left + this.renderer.gridToScreenX(gridPos.x, gridPos.y);
    const clientY = ev?.clientY ?? rect.top + this.renderer.gridToScreenY(gridPos.x, gridPos.y);
    showContextMenu(clientX, clientY, [{ heading: 'Tunnel — which end?', items }]);
  }

  /**
   * Commit one chosen tunnel endpoint: arm it as the pending first endpoint, or,
   * when one is already pending, pair the two and create the tunnel.
   */
  async _commitTunnelEndpoint(endpoint: { x: number; y: number; edge: string }): Promise<void> {
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

  /**
   * Reveal where a tunnel mouth connects: flash the partner end so the eye
   * catches it, and pan to it first when it sits off-screen. The selected pair's
   * connecting line is drawn by the renderer once the tunnel is selected.
   */
  _revealTunnelPartner(partner: { x: number; y: number; edge: string }): void {
    // Pan to the partner only when it sits off-screen, then flash it — the same
    // "find on map" locate the editor lists use.
    this.highlighter.show(
      { tiles: [[partner.x, partner.y]] },
      { channel: 'locate', reveal: 'ifOffscreen' },
    );
  }
}
