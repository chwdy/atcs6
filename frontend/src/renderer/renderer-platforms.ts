// renderer-platforms.ts — resolves each platform's oriented body geometry,
// draws the body parallelograms, and stamps the platform name directly on
// each body bar (one per side, so a both-sides platform reads on both bars).
//
// Migrated from static/js/renderer-platforms.js (longterm_todo.md item #7,
// renderer cluster). Covered by renderer-platforms.test.ts.
//
// Imports TRACK_BITS from ../lib/constants.
// Part of the Vite bundle; the Renderer constructs it directly
// (`new RendererPlatformHandler(this)`).

import { TRACK_BITS } from '../lib/constants';
import { getCanvasPalette } from '../lib/canvas-palette';

interface Point {
  x: number;
  y: number;
}

interface BodySegment {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface Platform {
  positions?: Array<[number, number]>;
  orientation?: string;
  initial_orientation?: string;
  platform_sides?: string[];
  platform_side?: string;
  name?: string;
  platform_id?: string;
  station_id?: string;
}

interface PlatformTrack {
  track_bits?: number;
}

export interface PlatformBound {
  x: number;
  y: number;
  r: number;
  name: string;
  abbrev: string;
}

export interface PlatformLabelBound {
  x: number;
  y: number;
  w: number;
  h: number;
  tier: string;
}

/** The slice of the Renderer the platform handler reads through its back-reference. */
export interface PlatformHandlerRenderer {
  ctx: CanvasRenderingContext2D;
  scale: number;
  gridSize: number;
  currentPlatforms?: Platform[] | null;
  trackMap: Record<string, PlatformTrack | undefined>;
  mouseX: number;
  mouseY: number;
  gridToScreenX(x: number, y: number): number;
  gridToScreenY(x: number, y: number): number;
  platformBounds: PlatformBound[];
  labelBounds: PlatformLabelBound[];
  getNearbyTrackSegments(cx: number, cy: number, radius: number): unknown[];
  distRectToSegments(bx: number, by: number, boxW: number, boxH: number, segments: unknown[]): number;
}

interface PlatformMeta {
  id: string;
  name: string;
  orientation: string | undefined;
  platformSides: string[];
  point1: Point;
  point2: Point;
}

export class RendererPlatformHandler {
  renderer: PlatformHandlerRenderer;
  _platformBodySegments?: Array<{ id: string; segments: BodySegment[] }>;

  constructor(renderer: PlatformHandlerRenderer) {
    this.renderer = renderer;
  }

  get ctx(): CanvasRenderingContext2D {
    return this.renderer.ctx;
  }
  get scale(): number {
    return this.renderer.scale;
  }
  get gridSize(): number {
    return this.renderer.gridSize;
  }
  get currentPlatforms(): Platform[] | null | undefined {
    return this.renderer.currentPlatforms;
  }
  get trackMap(): Record<string, PlatformTrack | undefined> {
    return this.renderer.trackMap;
  }
  get mouseX(): number {
    return this.renderer.mouseX;
  }
  get mouseY(): number {
    return this.renderer.mouseY;
  }

  gridToScreenX(x: number, y: number): number {
    return this.renderer.gridToScreenX(x, y);
  }
  gridToScreenY(x: number, y: number): number {
    return this.renderer.gridToScreenY(x, y);
  }

  drawPlatforms(): void {
    if (!this.currentPlatforms || this.currentPlatforms.length === 0) return;

    const tileSize = this.gridSize * this.scale;
    const half = tileSize / 2;
    const width = 10 * this.scale;
    this.renderer.platformBounds = [];

    // Pass 1: resolve geometry for every platform.
    const meta: PlatformMeta[] = [];
    for (const platform of this.currentPlatforms) {
      const m = this._resolvePlatformMeta(platform, half);
      if (m) meta.push(m);
    }

    // Cache body perimeters (used by label picker here and by train handler).
    this._platformBodySegments = meta.map((m) => ({
      id: m.id,
      segments: this._computeBodySegments(m, half, width),
    }));

    // Pass 2: draw each platform body and stamp its name on the body. The
    // name is drawn once per side, so a both-sides platform reads on both bars.
    for (const m of meta) {
      this.ctx.save();
      if (m.orientation && m.platformSides.length > 0) {
        this.drawOrientedPlatform(
          m.name, m.point1, m.point2, m.orientation, m.platformSides, width, half,
        );
      }
      this.ctx.restore();
    }
  }

  _resolvePlatformMeta(platform: Platform, half: number): PlatformMeta | null {
    if (!platform.positions || platform.positions.length === 0) return null;

    const positions = platform.positions;
    let orientation = platform.orientation || platform.initial_orientation;
    let platformSides = platform.platform_sides || [];
    const platformName = platform.name || platform.platform_id || platform.station_id || 'Platform';
    const platformId = platform.platform_id || platform.station_id || platformName;

    if (!orientation) {
      const firstPos = positions[0];
      const firstTrack = this.trackMap[`${firstPos[0]},${firstPos[1]}`];
      if (firstTrack) {
        const trackBits = firstTrack.track_bits || 0;
        if (trackBits & TRACK_BITS.NS) orientation = 'NS';
        else if (trackBits & TRACK_BITS.EW) orientation = 'EW';
        else if (trackBits & TRACK_BITS.NE || trackBits & TRACK_BITS.SW) orientation = 'NE_SW';
        else if (trackBits & TRACK_BITS.NW || trackBits & TRACK_BITS.SE) orientation = 'NW_SE';
      }
    }

    if (platformSides.length === 0 && orientation) {
      const sideMapping: Record<string, Record<string, string[]>> = {
        NS: { both: ['west', 'east'], left: ['west'], right: ['east'] },
        EW: { both: ['north', 'south'], left: ['north'], right: ['south'] },
        NE_SW: { both: ['northwest', 'southeast'], left: ['northwest'], right: ['southeast'] },
        NW_SE: { both: ['southwest', 'northeast'], left: ['southwest'], right: ['northeast'] },
      };
      const side = (platform.platform_side || 'both').toLowerCase();
      platformSides = sideMapping[orientation]?.[side] || ['west', 'east'];
    }

    const endpointTiles = this.findEndpointTiles(positions);
    if (!endpointTiles) return null;
    const [tile1, tile2] = endpointTiles;
    const track1 = this.trackMap[`${tile1[0]},${tile1[1]}`];
    const track2 = this.trackMap[`${tile2[0]},${tile2[1]}`];
    if (!track1 || !track2) return null;

    const endpoints1 = this.getPathEndpoints(tile1, track1.track_bits || 0, half);
    const endpoints2 = this.getPathEndpoints(tile2, track2.track_bits || 0, half);
    const longestPair = this.findLongestPair([...endpoints1, ...endpoints2]);
    if (!longestPair) return null;

    return {
      id: platformId,
      name: platformName,
      orientation,
      platformSides,
      point1: longestPair[0],
      point2: longestPair[1],
    };
  }

  _computeBodySegments(meta: PlatformMeta, half: number, width: number): BodySegment[] {
    const { point1, point2, orientation, platformSides } = meta;
    let p1 = point1, p2 = point2;
    if (this.shouldSwapEndpoints(p1, p2, orientation)) [p1, p2] = [p2, p1];

    const dx = p2.x - p1.x, dy = p2.y - p1.y;
    const length = Math.hypot(dx, dy);
    if (length === 0) return [];

    const perpX = -dy / length, perpY = dx / length;
    const perpOffset = half * 0.3;
    const segs: BodySegment[] = [];

    for (const side of platformSides) {
      const sm = this.getSideMultiplier(side, orientation);
      const ox = perpX * perpOffset * sm, oy = perpY * perpOffset * sm;
      const innerS = { x: p1.x + ox, y: p1.y + oy };
      const innerE = { x: p2.x + ox, y: p2.y + oy };
      const outerS = { x: innerS.x + perpX * width * sm, y: innerS.y + perpY * width * sm };
      const outerE = { x: innerE.x + perpX * width * sm, y: innerE.y + perpY * width * sm };
      // 4 perimeter segments of the parallelogram body.
      segs.push(
        { x1: innerS.x, y1: innerS.y, x2: outerS.x, y2: outerS.y },
        { x1: outerS.x, y1: outerS.y, x2: outerE.x, y2: outerE.y },
        { x1: outerE.x, y1: outerE.y, x2: innerE.x, y2: innerE.y },
        { x1: innerE.x, y1: innerE.y, x2: innerS.x, y2: innerS.y },
      );
    }
    return segs;
  }

  getPlatformBodySegments(excludeId?: string): BodySegment[] {
    if (!this._platformBodySegments) return [];
    const out: BodySegment[] = [];
    for (const body of this._platformBodySegments) {
      if (body.id === excludeId) continue;
      for (const s of body.segments) out.push(s);
    }
    return out;
  }

  abbreviatePlatformName(name: string | null | undefined): string {
    if (!name || name.length <= 4) return String(name || '').toUpperCase();

    const words = String(name).split(/[\s\-_]+/).filter((w) => w.length > 0);
    if (words.length >= 2) {
      return words.slice(0, 4).map((w) => w[0].toUpperCase()).join('');
    }

    const consonants = String(name).replace(/[aeiou]/gi, '').toUpperCase();
    if (consonants.length >= 3) {
      return consonants.slice(0, 4);
    }
    return String(name).slice(0, 4).toUpperCase();
  }

  findEndpointTiles(positions: Array<[number, number]>): Array<[number, number]> | null {
    if (positions.length === 0) return null;
    if (positions.length === 1) return [positions[0], positions[0]];

    let maxDist = -1;
    let endpointPair: Array<[number, number]> | null = null;

    for (let i = 0; i < positions.length; i++) {
      for (let j = i + 1; j < positions.length; j++) {
        const dx = positions[j][0] - positions[i][0];
        const dy = positions[j][1] - positions[i][1];
        const dist = dx * dx + dy * dy;
        if (dist > maxDist) {
          maxDist = dist;
          endpointPair = [positions[i], positions[j]];
        }
      }
    }

    return endpointPair;
  }

  getPathEndpoints(tilePos: [number, number], trackBits: number, half: number): Point[] {
    const centerX = this.gridToScreenX(tilePos[0], tilePos[1]);
    const centerY = this.gridToScreenY(tilePos[0], tilePos[1]);

    const edgePositions = {
      N: { x: centerX + half / 2, y: centerY - half / 2 },
      S: { x: centerX - half / 2, y: centerY + half / 2 },
      E: { x: centerX + half / 2, y: centerY + half / 2 },
      W: { x: centerX - half / 2, y: centerY - half / 2 },
    };

    const endpoints: Point[] = [];

    if (trackBits & TRACK_BITS.NS) {
      endpoints.push(edgePositions.N, edgePositions.S);
    }
    if (trackBits & TRACK_BITS.EW) {
      endpoints.push(edgePositions.E, edgePositions.W);
    }
    if (trackBits & TRACK_BITS.NE) {
      endpoints.push(edgePositions.N, edgePositions.E);
    }
    if (trackBits & TRACK_BITS.NW) {
      endpoints.push(edgePositions.N, edgePositions.W);
    }
    if (trackBits & TRACK_BITS.SE) {
      endpoints.push(edgePositions.S, edgePositions.E);
    }
    if (trackBits & TRACK_BITS.SW) {
      endpoints.push(edgePositions.S, edgePositions.W);
    }

    return endpoints.slice(0, 2);
  }

  findLongestPair(points: Point[]): Point[] | null {
    if (points.length < 2) return null;

    let maxDist = -1;
    let longestPair: Point[] | null = null;

    for (let i = 0; i < points.length; i++) {
      for (let j = i + 1; j < points.length; j++) {
        const dx = points[j].x - points[i].x;
        const dy = points[j].y - points[i].y;
        const dist = dx * dx + dy * dy;
        if (dist > maxDist) {
          maxDist = dist;
          longestPair = [points[i], points[j]];
        }
      }
    }

    return longestPair;
  }

  drawOrientedPlatform(
    name: string,
    point1: Point,
    point2: Point,
    orientation: string | undefined,
    platformSides: string[],
    width: number,
    half: number,
  ): void {
    const p = getCanvasPalette();
    // Body fill only — no outline. The name sits on the body, so a border
    // would just box in text that already reads against the fill.
    this.ctx.fillStyle = p.overlayBorder;

    let p1 = point1;
    let p2 = point2;

    const needsSwap = this.shouldSwapEndpoints(p1, p2, orientation);
    if (needsSwap) {
      [p1, p2] = [p2, p1];
    }

    const dx = p2.x - p1.x;
    const dy = p2.y - p1.y;
    const length = Math.sqrt(dx * dx + dy * dy);
    if (length === 0) return;

    const dirX = dx / length;
    const dirY = dy / length;
    const perpX = -dirY;
    const perpY = dirX;

    const perpOffset = half * 0.3;

    for (const side of platformSides) {
      const sideMultiplier = this.getSideMultiplier(side, orientation);

      const offsetX = perpX * perpOffset * sideMultiplier;
      const offsetY = perpY * perpOffset * sideMultiplier;

      const innerStartX = p1.x + offsetX;
      const innerStartY = p1.y + offsetY;
      const innerEndX = p2.x + offsetX;
      const innerEndY = p2.y + offsetY;

      const outerStartX = innerStartX + perpX * width * sideMultiplier;
      const outerStartY = innerStartY + perpY * width * sideMultiplier;
      const outerEndX = innerEndX + perpX * width * sideMultiplier;
      const outerEndY = innerEndY + perpY * width * sideMultiplier;

      this.drawPlatformQuad(
        innerStartX, innerStartY,
        outerStartX, outerStartY,
        outerEndX, outerEndY,
        innerEndX, innerEndY,
      );

      // Stamp the name on this side's bar, centered on the parallelogram.
      const cx = (innerStartX + innerEndX + outerStartX + outerEndX) / 4;
      const cy = (innerStartY + innerEndY + outerStartY + outerEndY) / 4;
      this.drawPlatformNameOnBody(name, cx, cy, dirX, dirY, width, length);
    }
  }

  getSideMultiplier(side: string, orientation: string | undefined): number {
    const sideMap: Record<string, Record<string, number>> = {
      NS: { east: 1, west: -1 },
      EW: { south: 1, north: -1 },
      NE_SW: { southeast: 1, northwest: -1 },
      NW_SE: { northeast: 1, southwest: -1 },
    };

    return sideMap[orientation ?? '']?.[side] || 1;
  }

  shouldSwapEndpoints(p1: Point, p2: Point, orientation: string | undefined): boolean {
    const dx = p2.x - p1.x;
    const dy = p2.y - p1.y;

    switch (orientation) {
      case 'NS':
        return dy < 0;
      case 'EW':
        return dx > 0;
      case 'NE_SW':
        return (dx + dy) > 0;
      case 'NW_SE':
        return dx < 0;
      default:
        return false;
    }
  }

  drawPlatformQuad(
    x1: number, y1: number, x2: number, y2: number,
    x3: number, y3: number, x4: number, y4: number,
  ): void {
    this.ctx.beginPath();
    this.ctx.moveTo(x1, y1);
    this.ctx.lineTo(x2, y2);
    this.ctx.lineTo(x3, y3);
    this.ctx.lineTo(x4, y4);
    this.ctx.closePath();
    this.ctx.fill();
  }

  // Stamp the platform name onto one body bar, rotated to run along the
  // platform and sized to the bar thickness (≈ the §16 7px-at-scale-1 size
  // family). Full name when it fits the bar's length, else the abbreviation
  // shrunk to fit. Registers the text's screen rect so train labels — which
  // share labelBounds — steer around it.
  drawPlatformNameOnBody(
    name: string,
    cx: number,
    cy: number,
    dirX: number,
    dirY: number,
    bandThickness: number,
    bandLength: number,
  ): void {
    if (!name) return;

    // Text runs along the platform; flip past vertical so it's never upside down.
    let angle = Math.atan2(dirY, dirX);
    if (angle > Math.PI / 2) angle -= Math.PI;
    else if (angle < -Math.PI / 2) angle += Math.PI;

    let fontSize = bandThickness * 0.7;
    const maxTextW = bandLength * 0.9;

    this.ctx.save();
    this.ctx.font = `bold ${fontSize}px sans-serif`;
    let text = name;
    let textW = this.ctx.measureText(text).width;
    if (textW > maxTextW) {
      text = this.abbreviatePlatformName(name);
      textW = this.ctx.measureText(text).width;
      if (textW > maxTextW && textW > 0) {
        fontSize *= maxTextW / textW;
        this.ctx.font = `bold ${fontSize}px sans-serif`;
        textW = this.ctx.measureText(text).width;
      }
    }

    const p = getCanvasPalette();
    this.ctx.translate(cx, cy);
    this.ctx.rotate(angle);
    this.ctx.fillStyle = p.text;
    this.ctx.textAlign = 'center';
    this.ctx.textBaseline = 'middle';
    this.ctx.fillText(text, 0, 0);
    this.ctx.restore();

    // Reserve the text's axis-aligned screen box for train-label avoidance.
    const cos = Math.abs(Math.cos(angle));
    const sin = Math.abs(Math.sin(angle));
    const hx = (textW / 2) * cos + (fontSize / 2) * sin;
    const hy = (textW / 2) * sin + (fontSize / 2) * cos;
    this.renderer.labelBounds.push({
      x: cx - hx,
      y: cy - hy,
      w: hx * 2,
      h: hy * 2,
      tier: 'platform',
    });
  }
}
