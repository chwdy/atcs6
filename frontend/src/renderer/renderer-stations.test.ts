import { describe, expect, it, vi } from 'vitest';

import { RendererStationHandler, type Station, type StationHandlerRenderer } from './renderer-stations';

// renderer-stations.js had no test before the migration; this is fresh coverage.

function makeCtx() {
  return {
    save: vi.fn(),
    restore: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    quadraticCurveTo: vi.fn(),
    fill: vi.fn(),
    stroke: vi.fn(),
    closePath: vi.fn(),
    fillText: vi.fn(),
    measureText: vi.fn(() => ({ width: 20 })),
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 0,
    font: '',
    textAlign: '',
    textBaseline: '',
    lineJoin: '',
    lineCap: '',
  };
}

interface RendererOpts {
  stations?: Station[];
  mouseX?: number;
  mouseY?: number;
}

function makeRenderer(ctx: ReturnType<typeof makeCtx>, opts: RendererOpts = {}): StationHandlerRenderer {
  return {
    ctx: ctx as unknown as CanvasRenderingContext2D,
    scale: 1,
    gridSize: 40,
    mouseX: opts.mouseX ?? -1,
    mouseY: opts.mouseY ?? -1,
    gridToScreenX: (x) => x * 10,
    gridToScreenY: (_x, y) => y * 10,
    currentNetwork: { stations: opts.stations ?? [] },
    labelBounds: [],
    platformHandler: { getPlatformBodySegments: () => [], _platformBodySegments: [] },
    distRectToSegments: () => 100, // far from any body → centroid always wins
  };
}

const labels = (ctx: ReturnType<typeof makeCtx>) => ctx.fillText.mock.calls.map((c) => c[0]);

const STATION: Station = {
  station_id: 'ST1',
  name: 'Station 1',
  center_position: [5, 5],
  platform_ids: ['P1'],
};

describe('RendererStationHandler.abbreviate', () => {
  const h = () => new RendererStationHandler(makeRenderer(makeCtx()));

  it('uses initials for multi-word names', () => {
    expect(h().abbreviate('Station 1')).toBe('S1');
    expect(h().abbreviate('Central Junction')).toBe('CJ');
  });

  it('uses consonants for single words, and empty for blank', () => {
    expect(h().abbreviate('Park')).toBe('PRK');
    expect(h().abbreviate('')).toBe('');
  });
});

describe('RendererStationHandler.drawStations', () => {
  it('places and draws one collapsed label and registers its bounds', () => {
    const ctx = makeCtx();
    const r = makeRenderer(ctx, { stations: [STATION] });
    const h = new RendererStationHandler(r);
    h.drawStations();
    expect(h.hoveredStationId).toBeNull();
    expect(labels(ctx)).toEqual(['S1']); // collapsed abbreviation
    expect(r.labelBounds).toHaveLength(1);
    expect(r.labelBounds[0].tier).toBe('station');
  });

  it('defers the hovered label: skipped here, drawn full in drawHoverOverlay', () => {
    const ctx = makeCtx();
    // Centroid screen position is (50, 50); put the mouse there.
    const r = makeRenderer(ctx, { stations: [STATION], mouseX: 50, mouseY: 50 });
    const h = new RendererStationHandler(r);
    h.drawStations();
    expect(h.hoveredStationId).toBe('ST1');
    expect(labels(ctx)).toEqual([]); // hovered label not drawn in the identity tier
    expect(r.labelBounds).toHaveLength(0); // and excluded from the shared registry
    h.drawHoverOverlay();
    expect(labels(ctx)).toEqual(['Station 1']); // full name on hover
  });

  it('clears placements and hover state when there are no stations', () => {
    const ctx = makeCtx();
    const h = new RendererStationHandler(makeRenderer(ctx, { stations: [] }));
    h.drawStations();
    expect(h._placements).toEqual([]);
    expect(h.hoveredStationId).toBeNull();
    expect(labels(ctx)).toEqual([]);
  });

  it('stationAt resolves the station id at a point, else null', () => {
    const ctx = makeCtx();
    const h = new RendererStationHandler(makeRenderer(ctx, { stations: [STATION] }));
    h.drawStations();
    expect(h.stationAt(50, 50)).toBe('ST1');
    expect(h.stationAt(0, 0)).toBeNull();
  });
});
