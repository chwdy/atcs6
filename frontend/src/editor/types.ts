import type { LayoutHighlighter } from './layout-highlighter';

// Host interfaces for the editor handler cluster.
//
// Each editor-*.ts handler takes a back-reference to the Editor instance that
// owns it (`new DragBuildHandler(this)` in editor.ts). A handler should only see
// the slice of the Editor it actually uses, so each migrated handler declares a
// narrow host interface here listing exactly the members it touches. That turns
// the back-reference from "reach into anything" into an enforced contract: the
// compiler now rejects a host missing a method the handler depends on, and the
// interface doubles as documentation of the handler's real coupling surface.
//
// The collaborator surface (the `api` / `renderer` / `pathfinder` methods the
// handler calls) is typed precisely, since that is where coupling bites. Leaf
// data shapes (tiles, preview paths, network rows) are intentionally left loose
// (`any`) — modelling the full network types is a separate cleanup and not what
// this contract is protecting.
//
// `EditorHost` remains the permissive placeholder for handlers not yet migrated
// to a narrow host; they are converted one at a time.
export type EditorHost = any;

/** A grid tile position. */
export interface TilePos {
  x: number;
  y: number;
}

/** Host surface required by DragBuildHandler (editor-drag-build.ts). */
export interface DragBuildHost {
  // Drag + preview state (data shapes loose on purpose; see file header).
  dragStart: any;
  dragEnd: any;
  selectedTool: string;
  avoidConnectMode: boolean;
  forcedStartSegment: any;
  forcedEndSegment: any;
  previewPath: any;
  previewTracks: any;
  previewInvalid: boolean;
  cachedNetwork: { tracks: any[]; platforms?: any[] } | null;

  // Collaborators — the enforced coupling surface.
  pathfinder: {
    findPath(x1: number, y1: number, x2: number, y2: number, tracks: any[], opts: any): any[] | null;
    followExistingNeighbors(byKey: Map<string, any>, opts?: any): any;
  };
  renderer: {
    setBuildPreview(preview: unknown): void;
    requestRedraw(): void;
  };
  api: {
    addTracksBulk(tracks: any[]): Promise<any>;
    createBlock(id: string, name: string, options: Record<string, unknown>): Promise<any>;
    addTracksToBlock(blockId: string, positions: number[][]): Promise<any>;
    extendBlock(blockId: string, positions: number[][], hasSignals: boolean, hasOtherBlocks: boolean): Promise<any>;
    createPlatform(payload: any): Promise<any>;
    extendPlatform(platformId: any, positions: number[][]): Promise<any>;
    removePlatformTile(stationId: any, x: number, y: number): Promise<any>;
    removeBlockTiles(blockId: string, positions: number[][]): Promise<{ removed?: number; errors?: string[] }>;
  };

  _tracksByKey(): Map<string, any>;
  showBlockModal(opts: any): Promise<any>;
  showPlatformModal(opts: any): Promise<any>;
  refreshBlocksList(): Promise<void>;
  refreshAndSelect(target: any): Promise<void>;
}

/** Host surface required by SelectModesHandler (editor-select-modes.ts). */
export interface SelectModesHost {
  // Per-tool selection state (data shapes loose on purpose; see file header).
  boxSelectState: {
    selectedKeys: Set<string>;
    dragScreenStart: any;
    dragScreenEnd: any;
    isDragging: boolean;
    shiftRemoving: boolean;
  };
  lassoSelectState: { screenPoints: any[]; isDrawing: boolean; shiftRemoving: boolean };
  paintSelectState: { isPainting: boolean; shiftRemoving: boolean; lastKey: any };
  traceSelectState: { firstPoint: any };
  cachedNetwork: { tracks: any[] } | null;

  // Collaborators — the enforced coupling surface.
  pathfinder: { followExistingNeighbors(byKey: Map<string, any>, opts?: any): any };
  renderer: {
    setBoxSelectRect(rect: any): void;
    setLassoPath(path: any): void;
    requestRedraw(): void;
  };
  // The unified highlighter — the trace tool's reachable cue and its committed
  // selection outline both go through it.
  highlighter: LayoutHighlighter;
  routeHandler?: {
    _resourceOptionsAtTile(gridPos: any, track: any): any[];
    getBlockPositionSet(id: any): Set<string>;
    getPlatformPositionSet(ids: any[]): Set<string>;
    getStationGroups(): any[];
  };

  _applyBoxSelection(keys: Set<string>): void;
  // Outline a single entity's tiles on the canvas as a "show me where this is"
  // peek, without disturbing the drag-select state. Cleared on panel/tool swap.
  highlightInspectTiles(keys: Set<string>): void;
  clearInspectHighlight(): void;
  _resetTraceAnchor(): void;
  _tracksByKey(): Map<string, any>;
  $id(id: string): any;
}
