import { beforeEach, describe, expect, it, vi } from 'vitest';

import { LayoutHighlighter } from './layout-highlighter';

// Specs for the LayoutHighlighter — the single front door each panel will use to
// highlight a tile/entity on the layout. They pin the channel→renderer mapping,
// the clear behaviour, the reveal (camera) policy, and that the locate flash
// colour comes from the theme palette (in the node test env the canvas palette
// resolves to its FALLBACK, so highlightLocate is #ffd93d → "255, 217, 61").
// A hand-built fake renderer records the calls; no DOM is needed.

function makeRenderer(overrides: any = {}) {
  return {
    setBoxSelection: vi.fn(),
    setSelectionGroups: vi.fn(),
    setInspectHighlight: vi.fn(),
    setReachableHighlights: vi.fn(),
    highlightPath: vi.fn(),
    clearPathHighlight: vi.fn(),
    setSelectedTunnel: vi.fn(),
    flashTiles: vi.fn(),
    requestRedraw: vi.fn(),
    inspectHighlightGroups: [] as unknown[],
    camera: { panToWorld: vi.fn() },
    canvas: { getBoundingClientRect: () => ({ width: 800, height: 600 }) },
    gridToScreenX: vi.fn(() => 400),
    gridToScreenY: vi.fn(() => 300),
    ...overrides,
  };
}

function makeHighlighter(renderer: any, resolveEntityKeys?: any) {
  const host = {
    renderer,
    buildOutlineGroups: vi.fn((keys: Set<string>) => [{ keys, color: '#fff' }]),
    ...(resolveEntityKeys ? { resolveEntityKeys } : {}),
  };
  return { hi: new LayoutHighlighter(host), host };
}

describe('LayoutHighlighter — channel → renderer mapping', () => {
  let r: any;
  let hi: LayoutHighlighter;
  let host: any;

  beforeEach(() => {
    r = makeRenderer();
    ({ hi, host } = makeHighlighter(r));
  });

  it('selection: sets box selection + outline groups', () => {
    const keys = new Set(['1,1', '2,2']);
    hi.show({ keys }, { channel: 'selection' });
    expect(r.setBoxSelection).toHaveBeenCalledWith(keys);
    expect(host.buildOutlineGroups).toHaveBeenCalledWith(keys);
    expect(r.setSelectionGroups).toHaveBeenCalledWith([{ keys, color: '#fff' }]);
  });

  it('peek: outlines the entity via setInspectHighlight, no selection touched', () => {
    const keys = new Set(['3,4']);
    hi.show({ keys }, { channel: 'peek' });
    expect(r.setInspectHighlight).toHaveBeenCalledWith([{ keys, color: '#fff' }]);
    expect(r.setBoxSelection).not.toHaveBeenCalled();
  });

  it('reachable: forwards the key set straight through', () => {
    const keys = new Set(['5,5']);
    hi.show({ keys }, { channel: 'reachable' });
    expect(r.setReachableHighlights).toHaveBeenCalledWith(keys);
  });

  it('path: forwards the path object', () => {
    const path = { route_id: 'r1' };
    hi.show({ path }, { channel: 'path' });
    expect(r.highlightPath).toHaveBeenCalledWith(path);
  });

  it('pair: forwards the tunnel pair', () => {
    const pair = { a: { x: 1, y: 1 }, b: { x: 9, y: 9 } };
    hi.show({ pair }, { channel: 'pair' });
    expect(r.setSelectedTunnel).toHaveBeenCalledWith(pair);
  });
});

describe('LayoutHighlighter — locate flash', () => {
  let r: any;
  let hi: LayoutHighlighter;

  beforeEach(() => {
    r = makeRenderer();
    ({ hi } = makeHighlighter(r));
  });

  it('uses the theme palette colour and the preserved defaults', () => {
    hi.show({ tiles: [[7, 8]] }, { channel: 'locate' });
    expect(r.flashTiles).toHaveBeenCalledWith(
      [[7, 8]],
      expect.objectContaining({
        rgb: '255, 217, 61',
        fillAlpha: 0.4,
        durationMs: 900,
        highlightTrack: true,
      }),
    );
  });

  it('honours a caller-supplied duration (e.g. a longer route-task locate)', () => {
    hi.show({ tiles: [[7, 8]] }, { channel: 'locate', durationMs: 5000 });
    expect(r.flashTiles).toHaveBeenCalledWith(
      [[7, 8]],
      expect.objectContaining({ durationMs: 5000 }),
    );
  });

  it('honours caller fillAlpha / highlightTrack overrides', () => {
    hi.show({ tiles: [[0, 0]] }, { channel: 'locate', fillAlpha: 0.7, highlightTrack: false });
    expect(r.flashTiles).toHaveBeenCalledWith(
      [[0, 0]],
      expect.objectContaining({ fillAlpha: 0.7, highlightTrack: false }),
    );
  });

  it('derives tiles from a key-set target', () => {
    hi.show({ keys: new Set(['3,5']) }, { channel: 'locate' });
    expect(r.flashTiles).toHaveBeenCalledWith([[3, 5]], expect.any(Object));
  });

  it('does nothing when there are no tiles to flash', () => {
    hi.show({ keys: new Set() }, { channel: 'locate' });
    expect(r.flashTiles).not.toHaveBeenCalled();
  });
});

describe('LayoutHighlighter — clear', () => {
  let r: any;
  let hi: LayoutHighlighter;

  beforeEach(() => {
    r = makeRenderer();
    ({ hi } = makeHighlighter(r));
  });

  it('clear("selection") empties both selection fields', () => {
    hi.clear('selection');
    expect(r.setBoxSelection).toHaveBeenCalledWith(new Set());
    expect(r.setSelectionGroups).toHaveBeenCalledWith([]);
  });

  it('clear("peek") only redraws when something is peeked', () => {
    hi.clear('peek');
    expect(r.setInspectHighlight).not.toHaveBeenCalled();
    r.inspectHighlightGroups = [{ keys: new Set(['1,1']) }];
    hi.clear('peek');
    expect(r.setInspectHighlight).toHaveBeenCalledWith([]);
  });

  it('clear("reachable" | "path" | "pair") routes to the right clear', () => {
    hi.clear('reachable');
    expect(r.setReachableHighlights).toHaveBeenCalledWith(new Set());
    hi.clear('path');
    expect(r.clearPathHighlight).toHaveBeenCalled();
    hi.clear('pair');
    expect(r.setSelectedTunnel).toHaveBeenCalledWith(null);
  });

  it('clear() with no channel clears every persistent channel', () => {
    r.inspectHighlightGroups = [{ keys: new Set(['1,1']) }];
    hi.clear();
    expect(r.setBoxSelection).toHaveBeenCalledWith(new Set());
    expect(r.setSelectionGroups).toHaveBeenCalledWith([]);
    expect(r.setInspectHighlight).toHaveBeenCalledWith([]);
    expect(r.setReachableHighlights).toHaveBeenCalledWith(new Set());
    expect(r.clearPathHighlight).toHaveBeenCalled();
    expect(r.setSelectedTunnel).toHaveBeenCalledWith(null);
  });

  it('clear([...]) clears exactly the listed channels (panel swap clears peek+pair)', () => {
    r.inspectHighlightGroups = [{ keys: new Set(['1,1']) }];
    hi.clear(['peek', 'pair']);
    expect(r.setInspectHighlight).toHaveBeenCalledWith([]);
    expect(r.setSelectedTunnel).toHaveBeenCalledWith(null);
    expect(r.setReachableHighlights).not.toHaveBeenCalled();
    expect(r.setBoxSelection).not.toHaveBeenCalled();
    expect(r.clearPathHighlight).not.toHaveBeenCalled();
  });
});

describe('LayoutHighlighter — reveal (camera)', () => {
  let r: any;
  let hi: LayoutHighlighter;

  beforeEach(() => {
    r = makeRenderer();
    ({ hi } = makeHighlighter(r));
  });

  it('default / "none" never pans', () => {
    hi.show({ tiles: [[3, 3]] }, { channel: 'locate' });
    hi.show({ tiles: [[3, 3]] }, { channel: 'locate', reveal: 'none' });
    expect(r.camera.panToWorld).not.toHaveBeenCalled();
  });

  it('"center" pans to the tile-list bbox centre and repaints', () => {
    hi.show({ tiles: [[2, 2], [4, 4]] }, { channel: 'locate', reveal: 'center' });
    expect(r.camera.panToWorld).toHaveBeenCalledWith(3, 3);
    expect(r.requestRedraw).toHaveBeenCalled();
  });

  it('"ifOffscreen" pans only when the centre is outside the safe margin', () => {
    // On screen (gridToScreen* default to 400,300 inside an 800×600 canvas).
    hi.show({ tiles: [[1, 1]] }, { channel: 'locate', reveal: 'ifOffscreen' });
    expect(r.camera.panToWorld).not.toHaveBeenCalled();
    // Off screen.
    r.gridToScreenX = vi.fn(() => 5);
    hi.show({ tiles: [[1, 1]] }, { channel: 'locate', reveal: 'ifOffscreen' });
    expect(r.camera.panToWorld).toHaveBeenCalledWith(1, 1);
  });
});

describe('LayoutHighlighter — entity targets', () => {
  it('throws a clear error when no resolver is wired', () => {
    const r = makeRenderer();
    const { hi } = makeHighlighter(r);
    expect(() => hi.show({ entity: { kind: 'block', id: 'B1' } }, { channel: 'peek' })).toThrow(
      /resolveEntityKeys/,
    );
  });

  it('resolves an entity to its footprint keys', () => {
    const r = makeRenderer();
    const resolve = vi.fn(() => new Set(['8,8', '9,8']));
    const { hi, host } = makeHighlighter(r, resolve);
    hi.show({ entity: { kind: 'block', id: 'B1' } }, { channel: 'peek' });
    expect(resolve).toHaveBeenCalledWith('block', 'B1');
    expect(host.buildOutlineGroups).toHaveBeenCalledWith(new Set(['8,8', '9,8']));
  });
});
