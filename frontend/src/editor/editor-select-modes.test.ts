import { beforeEach, describe, expect, it, vi } from 'vitest';

import { graphSearch } from '../lib/graph-search';
import { showToast } from '../lib/toast';
import { showResourcePicker } from './resource-picker';
import { SelectModesHandler } from './editor-select-modes';

// Behavior-pinning ("characterization") specs for the selection-mode builders
// freshly extracted from the Editor god-class. They lock in the ToolMode
// contract each builder returns (the tool registry depends on these toolName
// strings) and the click behavior of the entity / trace modes, so further
// Editor decomposition has a net underneath it. The handler takes its host via
// the constructor, so a hand-built fake EditorHost exercises it without a DOM
// (vitest runs in the node env). Library collaborators are stubbed.
vi.mock('../lib/toast', () => ({ showToast: vi.fn() }));
vi.mock('../lib/graph-search', () => ({ graphSearch: vi.fn(() => ({ path: null, visited: new Map() })) }));
vi.mock('../lib/pathfinders', () => ({ traceNodeKey: (n: any) => `${n.x},${n.y},${n.entry}` }));
vi.mock('./resource-picker', () => ({ showResourcePicker: vi.fn(), dismissResourcePicker: vi.fn() }));

function makeSelectEditor(overrides: any = {}): any {
  const { renderer: rendererOverrides, routeHandler: routeOverrides, ...rest } = overrides;
  return {
    boxSelectState: { selectedKeys: new Set(), dragScreenStart: null, dragScreenEnd: null, isDragging: false, shiftRemoving: false },
    lassoSelectState: { screenPoints: [], isDrawing: false, shiftRemoving: false },
    paintSelectState: { isPainting: false, shiftRemoving: false, lastKey: null },
    traceSelectState: { firstPoint: null },
    cachedNetwork: { tracks: [] },
    _applyBoxSelection: vi.fn(),
    _resetTraceAnchor: vi.fn(),
    _tracksByKey: vi.fn(() => new Map()),
    $id: vi.fn(() => ({ textContent: '' })),
    pathfinder: { followExistingNeighbors: vi.fn(() => () => []) },
    routeHandler: {
      _resourceOptionsAtTile: vi.fn(() => []),
      getBlockPositionSet: vi.fn(() => new Set()),
      getPlatformPositionSet: vi.fn(() => new Set()),
      getStationGroups: vi.fn(() => []),
      ...routeOverrides,
    },
    renderer: {
      setBoxSelectRect: vi.fn(),
      setLassoPath: vi.fn(),
      requestRedraw: vi.fn(),
      setReachableHighlights: vi.fn(),
      ...rendererOverrides,
    },
    ...rest,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('selection-mode builders — ToolMode contract', () => {
  it('each builder reports its stable toolName', () => {
    const h = new SelectModesHandler(makeSelectEditor());
    expect(h._buildBoxSelectMode().toolName).toBe('box-select');
    expect(h._buildLassoSelectMode().toolName).toBe('lasso-select');
    expect(h._buildPaintSelectMode().toolName).toBe('paint-select');
    expect(h._buildEntitySelectMode().toolName).toBe('entity-select');
    expect(h._buildTraceSelectMode().toolName).toBe('trace');
  });

  it('box-select onEnter clears the selection; onExit repaints', () => {
    const ed = makeSelectEditor();
    const mode = new SelectModesHandler(ed)._buildBoxSelectMode();

    mode.onEnter();
    expect(ed._applyBoxSelection).toHaveBeenCalledWith(new Set());
    expect(ed.renderer.setBoxSelectRect).toHaveBeenCalledWith(null);

    mode.onExit();
    expect(ed.renderer.requestRedraw).toHaveBeenCalled();
  });
});

describe('entity-select onClick', () => {
  it('does nothing selectable when the tile carries no entity', () => {
    const ed = makeSelectEditor({ routeHandler: { _resourceOptionsAtTile: vi.fn(() => []) } });
    const mode = new SelectModesHandler(ed)._buildEntitySelectMode();

    const handled = mode.onClick({ gridPos: { x: 1, y: 1 }, screenX: 10, screenY: 20 });

    expect(handled).toBe(true);
    expect(ed._applyBoxSelection).not.toHaveBeenCalled();
    expect(showResourcePicker).not.toHaveBeenCalled();
  });

  it('a single entity is selected directly (all its tiles), no picker', () => {
    const ed = makeSelectEditor({
      cachedNetwork: { tracks: [{ x: 1, y: 1 }] },
      routeHandler: {
        _resourceOptionsAtTile: vi.fn(() => [{ type: 'block', id: 'B1', label: 'Block B1' }]),
        getBlockPositionSet: vi.fn(() => new Set(['1,1', '1,2'])),
      },
    });
    const mode = new SelectModesHandler(ed)._buildEntitySelectMode();

    const handled = mode.onClick({ gridPos: { x: 1, y: 1 }, screenX: 0, screenY: 0 });

    expect(handled).toBe(true);
    expect(ed._applyBoxSelection).toHaveBeenCalledWith(new Set(['1,1', '1,2']));
    expect(showToast).toHaveBeenCalled();
    expect(showResourcePicker).not.toHaveBeenCalled();
  });

  it('several entities on one tile open the picker and select nothing yet', () => {
    const ed = makeSelectEditor({
      routeHandler: {
        _resourceOptionsAtTile: vi.fn(() => [
          { type: 'block', id: 'B1', label: 'Block B1' },
          { type: 'platform', id: 'P1', label: 'Platform P1' },
        ]),
      },
    });
    const mode = new SelectModesHandler(ed)._buildEntitySelectMode();

    const handled = mode.onClick({ gridPos: { x: 1, y: 1 }, screenX: 5, screenY: 6 });

    expect(handled).toBe(true);
    expect(showResourcePicker).toHaveBeenCalledTimes(1);
    expect(ed._applyBoxSelection).not.toHaveBeenCalled();
  });
});

describe('trace-select onClick', () => {
  it('first click on a track anchors the start and lights reachable tiles', () => {
    const ed = makeSelectEditor({
      cachedNetwork: { tracks: [{ x: 2, y: 3 }] },
      _tracksByKey: vi.fn(() => new Map([['2,3', { x: 2, y: 3 }]])),
    });
    const mode = new SelectModesHandler(ed)._buildTraceSelectMode();

    const handled = mode.onClick({ gridPos: { x: 2, y: 3 } });

    expect(handled).toBe(true);
    expect(ed.traceSelectState.firstPoint).toEqual({ x: 2, y: 3 });
    expect(ed.renderer.setReachableHighlights).toHaveBeenCalled();
  });

  it('a click off any track sets no anchor', () => {
    const ed = makeSelectEditor({ cachedNetwork: { tracks: [] } });
    const mode = new SelectModesHandler(ed)._buildTraceSelectMode();

    const handled = mode.onClick({ gridPos: { x: 9, y: 9 } });

    expect(handled).toBe(true);
    expect(ed.traceSelectState.firstPoint).toBeNull();
    expect(ed.renderer.setReachableHighlights).not.toHaveBeenCalled();
  });
});

// graphSearch is stubbed above; reference the import so its mock is registered
// even though the trace specs above drive it indirectly.
void graphSearch;
