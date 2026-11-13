// editor-select-modes.ts — Multi-tile selection-mode builders.
//
// Lifted out of the Editor god-class (behavior unchanged). Builds the
// ToolMode objects for the box / lasso / paint / entity / trace selection
// tools (onEnter/onMove/onExit/preview wiring). The selection state
// (boxSelectState, lassoSelectState, paintSelectState, traceSelectState) and
// the apply/reset helpers stay on the Editor; this handler reaches them via
// `this.editor`.
//
// Owned by the Editor (`new SelectModesHandler(this)`).
import type { SelectModesHost } from './types';
import { showToast } from '../lib/toast';
import { graphSearch } from '../lib/graph-search';
import { traceNodeKey, type TraceNode } from '../lib/pathfinders';

export class SelectModesHandler {
  editor: SelectModesHost;
  constructor(editor: SelectModesHost) {
    this.editor = editor;
  }

  _buildBoxSelectMode(): any {
    const resetState = () => {
      this.editor.boxSelectState.selectedKeys = new Set();
      this.editor.boxSelectState.dragScreenStart = null;
      this.editor.boxSelectState.dragScreenEnd = null;
      this.editor.boxSelectState.isDragging = false;
      this.editor.boxSelectState.shiftRemoving = false;
      this.editor._applyBoxSelection(new Set());
      this.editor.renderer.setBoxSelectRect(null);
    };
    return {
      name: 'Box select',
      toolName: 'box-select',
      cancelMessage: 'Box select cancelled',
      onEnter: () => {
        resetState();
        const status = this.editor.$id('status-state');
        if (status)
          status.textContent =
            'Box: drag to add, Shift+drag to remove, Space/right-click clears, Delete removes tracks';
      },
      onClick: () => true,
      onExit: () => {
        resetState();
        const status = this.editor.$id('status-state');
        if (status) status.textContent = 'Ready';
        this.editor.renderer.requestRedraw();
      },
    };
  }

  _buildLassoSelectMode(): any {
    const resetState = () => {
      this.editor.boxSelectState.selectedKeys = new Set();
      this.editor.lassoSelectState.screenPoints = [];
      this.editor.lassoSelectState.isDrawing = false;
      this.editor.lassoSelectState.shiftRemoving = false;
      this.editor._applyBoxSelection(new Set());
      this.editor.renderer.setLassoPath(null);
    };
    return {
      name: 'Lasso select',
      toolName: 'lasso-select',
      cancelMessage: 'Lasso select cancelled',
      onEnter: () => {
        resetState();
        const status = this.editor.$id('status-state');
        if (status)
          status.textContent =
            'Lasso: draw a shape to add, Shift to remove, Space/right-click clears, Delete removes tracks';
      },
      onClick: () => true,
      onExit: () => {
        resetState();
        const status = this.editor.$id('status-state');
        if (status) status.textContent = 'Ready';
        this.editor.renderer.requestRedraw();
      },
    };
  }

  _buildPaintSelectMode(): any {
    const resetState = () => {
      this.editor.boxSelectState.selectedKeys = new Set();
      this.editor.paintSelectState.isPainting = false;
      this.editor.paintSelectState.shiftRemoving = false;
      this.editor.paintSelectState.lastKey = null;
      this.editor._applyBoxSelection(new Set());
    };
    return {
      name: 'Brush select',
      toolName: 'paint-select',
      cancelMessage: 'Brush select cancelled',
      onEnter: () => {
        resetState();
        const status = this.editor.$id('status-state');
        if (status)
          status.textContent =
            'Brush: click or drag to add, Shift to remove, Space/right-click clears, Delete removes tracks';
      },
      onClick: () => true,
      onExit: () => {
        resetState();
        const status = this.editor.$id('status-state');
        if (status) status.textContent = 'Ready';
        this.editor.renderer.requestRedraw();
      },
    };
  }

  // Entity select: click a tile to add ALL of its block's tiles to the shared
  // selection set. Delete / Space / ESC act on the selection exactly as they do
  // for box / lasso / brush / trace.
  _buildEntitySelectMode(): any {
    const resetState = () => {
      this.editor.boxSelectState.selectedKeys = new Set();
      this.editor._applyBoxSelection(new Set());
    };
    const setStatus = (text: string) => {
      const status = this.editor.$id('status-state');
      if (status) status.textContent = text;
    };
    return {
      name: 'Entity select',
      toolName: 'entity-select',
      cancelMessage: 'Entity select cancelled',
      onEnter: () => {
        resetState();
        setStatus('Entity: click a tile to select its whole block. Space/right-click clears, Delete removes tracks');
      },
      onClick: ({ gridPos }: any) => {
        const track = this.editor.cachedNetwork?.tracks?.find(
          (t: any) => t.x === gridPos.x && t.y === gridPos.y
        );
        const blockId = track?.block_section_id;
        if (!blockId) {
          setStatus('Entity: nothing here — click a tile that belongs to a block');
          return true;
        }
        const tiles = this._blockTileSet(blockId);
        tiles.forEach((k) => this.editor.boxSelectState.selectedKeys.add(k));
        this.editor._applyBoxSelection(this.editor.boxSelectState.selectedKeys);
        this.editor.renderer.requestRedraw();
        showToast(`Selected block ${blockId} — ${tiles.size} tile${tiles.size === 1 ? '' : 's'}`, 'success', 1800);
        return true;
      },
      onExit: () => {
        resetState();
        setStatus('Ready');
        this.editor.renderer.requestRedraw();
      },
    };
  }

  /** Every "x,y" tile of one block. */
  _blockTileSet(blockId: string): Set<string> {
    const positions = new Set<string>();
    for (const t of this.editor.cachedNetwork?.tracks || []) {
      if (t.block_section_id === blockId) positions.add(`${t.x},${t.y}`);
    }
    return positions;
  }

  // Trace select: click a first track, then a second; selects every existing
  // track on the path between them (found via the construction pathfinder) and
  // adds them to the shared box-select selection set. Delete / Space / ESC act
  // on that set exactly as they do for box / lasso / brush.
  _buildTraceSelectMode(): any {
    const resetState = () => {
      this.editor.boxSelectState.selectedKeys = new Set();
      this.editor._resetTraceAnchor();
      this.editor._applyBoxSelection(new Set());
    };
    const setStatus = (text: string) => {
      const status = this.editor.$id('status-state');
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
        if (this.editor.boxSelectState.selectedKeys.size === 0) {
          this.editor._resetTraceAnchor();
        }
        const tracks = this.editor.cachedNetwork?.tracks || [];
        const trackKeys = new Set(tracks.map((t: any) => `${t.x},${t.y}`));
        const here = `${gridPos.x},${gridPos.y}`;

        // Both endpoints must sit on an existing track.
        if (!trackKeys.has(here)) {
          setStatus(
            this.editor.traceSelectState.firstPoint
              ? 'Trace: click the second track (must be on a track)'
              : 'Trace: click a track to start'
          );
          return true;
        }

        if (!this.editor.traceSelectState.firstPoint) {
          // First endpoint: anchor it (yellow box-select) and light up every
          // track tile reachable from it (orange) so the operator can see which
          // tiles are valid second endpoints — the same "reachable" cue the
          // Create / Extend Path tools paint via the highlighter's reachable channel.
          this.editor.traceSelectState.firstPoint = { x: gridPos.x, y: gridPos.y };
          this.editor.boxSelectState.selectedKeys.add(here);
          this.editor._applyBoxSelection(this.editor.boxSelectState.selectedKeys);
          const reachable = this._traceReachableTiles(this.editor.traceSelectState.firstPoint);
          this.editor.highlighter.show({ keys: reachable }, { channel: 'reachable' });
          this.editor.renderer.requestRedraw();
          setStatus(
            reachable.size === 0
              ? 'Trace: this track has no connected neighbours — Space to pick another'
              : `Trace: click a highlighted track to select the path (${reachable.size} reachable)`
          );
          return true;
        }

        // Second endpoint: trace the path and add every existing track on it.
        const a = this.editor.traceSelectState.firstPoint;
        const path = this._traceTrackPath(a, { x: gridPos.x, y: gridPos.y });
        this.editor.traceSelectState.firstPoint = null;
        // The pick is resolved — drop the orange reachable overlay either way.
        this.editor.highlighter.clear('reachable');
        if (path && path.length) {
          for (const p of path) {
            // Every tile on the path is an existing track by construction.
            this.editor.boxSelectState.selectedKeys.add(`${p.x},${p.y}`);
          }
          this.editor._applyBoxSelection(this.editor.boxSelectState.selectedKeys);
          this.editor.renderer.requestRedraw();
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
        this.editor.renderer.requestRedraw();
      },
    };
  }

  // Walk the existing-track network from `start` to `goal`, following real
  // segment connectivity (switch-aware), and return the ordered list of track
  // tiles on the shortest connecting path — or null if they are not connected.
  // Directed BFS keyed on (tile, entry edge): a tile is left only through the
  // exits its track bits actually bind for the edge it was entered by, so the
  // path cannot cheat across the disconnected segments of a switch.
  _traceTrackPath(start: any, goal: any): Array<{ x: number; y: number }> | null {
    const byKey = this.editor._tracksByKey();
    if (!byKey.has(`${start.x},${start.y}`) || !byKey.has(`${goal.x},${goal.y}`)) return null;

    const { path } = graphSearch<TraceNode>({
      seeds: [{ x: start.x, y: start.y, entry: '' }],
      key: traceNodeKey,
      neighbors: this.editor.pathfinder.followExistingNeighbors(byKey),
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
    const byKey = this.editor._tracksByKey();
    const startKey = `${start.x},${start.y}`;
    const reachable = new Set<string>();
    if (!byKey.has(startKey)) return reachable;

    // No goal → graphSearch floods, settling every reachable (tile, entry)
    // node. Collapse those to tile keys; the start tile is excluded since it
    // already carries the yellow anchor highlight.
    const { visited } = graphSearch<TraceNode>({
      seeds: [{ x: start.x, y: start.y, entry: '' }],
      key: traceNodeKey,
      neighbors: this.editor.pathfinder.followExistingNeighbors(byKey),
    });
    for (const node of visited.values()) {
      const k = `${node.x},${node.y}`;
      if (k !== startKey) reachable.add(k);
    }
    return reachable;
  }
}
