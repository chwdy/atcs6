// renderer-stations.ts — draws a single label per station group at the
// centroid of its platforms. Reuses the shared track-/platform-avoiding
// label picker. Hovering a station label expands it (full name) and
// highlights the member platform bodies.
//
// Migrated from static/js/renderer-stations.js (longterm_todo.md item #7,
// renderer cluster). Behavior is unchanged; covered by renderer-stations.test.ts.
//
// Part of the Vite bundle; the Renderer constructs it directly
// (`new RendererStationHandler(this)`).

import { getCanvasPalette, withAlpha } from '../lib/canvas-palette';

export interface StationPlatform {
  platform_id?: string;
  positions?: Array<[number, number]>;
}

export interface Station {
  station_id?: string;
  name?: string;
  center_position?: number[];
  platforms?: StationPlatform[];
  platform_ids?: string[];
}

interface PlatformBodySegment {
  id: string;
  segments: Array<{ x1: number; y1: number; x2: number; y2: number }>;
}

export interface StationLabelBound {
  x: number;
  y: number;
  w: number;
  h: number;
  tier: string;
}

/** The slice of the Renderer the station handler reads through its back-reference. */
export interface StationHandlerRenderer {
  ctx: CanvasRenderingContext2D;
  scale: number;
  gridSize: number;
  mouseX: number;
  mouseY: number;
  gridToScreenX(x: number, y: number): number;
  gridToScreenY(x: number, y: number): number;
  currentNetwork?: { stations?: Station[] } | null;
  labelBounds: StationLabelBound[];
  platformHandler?: {
    getPlatformBodySegments?(): unknown[];
    _platformBodySegments?: PlatformBodySegment[];
  };
  distRectToSegments(bx: number, by: number, boxW: number, boxH: number, segments: unknown[]): number;
}

interface StationPlacement {
  stationId: string | undefined;
  fullName: string;
  abbrev: string;
  labelX: number;
  labelY: number;
  boxW: number;
  boxH: number;
  platformIds: Array<string | undefined>;
}

export class RendererStationHandler {
  renderer: StationHandlerRenderer;
  // Cached per-frame placements.
  _placements: StationPlacement[];
  // Station id under the mouse this frame, or null. Other modules
  // (e.g. editor selection) can read this on click.
  hoveredStationId: string | undefined | null;

  constructor(renderer: StationHandlerRenderer) {
    this.renderer = renderer;
    this._placements = [];
    this.hoveredStationId = null;
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

  // Identity tier: place labels, run hover hit-test, draw the non-hovered
  // labels. Called early in the render loop so anything live (trains, route
  // overlays) renders on top — a static station label must not occlude a
  // moving train. The hovered label is deferred to drawHoverOverlay().
  drawStations(): void {
    const stations = this.renderer.currentNetwork?.stations;
    if (!stations || !stations.length) {
      this._placements = [];
      this.hoveredStationId = null;
      return;
    }

    this._placements = stations
      .map((s) => this._placeLabel(s))
      .filter((p): p is StationPlacement => p !== null);

    this.hoveredStationId = null;
    for (const p of this._placements) {
      if (this._mouseInRect(p.labelX, p.labelY, p.boxW, p.boxH)) {
        this.hoveredStationId = p.stationId;
        break;
      }
    }

    for (const p of this._placements) {
      if (p.stationId === this.hoveredStationId) continue;
      this._drawLabel(p, false);
      // Register the station rect into the shared label registry so
      // train labels can avoid overlap. The hovered station is excluded
      // because its expanded overlay paints AFTER trains (drawHoverOverlay)
      // — its bounds shouldn't constrain train placement.
      this.renderer.labelBounds.push({
        x: p.labelX - p.boxW / 2,
        y: p.labelY - p.boxH / 2,
        w: p.boxW,
        h: p.boxH,
        tier: 'station',
      });
    }
  }

  // Hover tier: the hovered label + its platform highlights, drawn at the
  // top of the canvas stack so user attention always wins over liveness.
  // Called at the end of the render loop.
  drawHoverOverlay(): void {
    if (!this.hoveredStationId) return;
    const hovered = this._placements.find((p) => p.stationId === this.hoveredStationId);
    if (!hovered) return;
    this._drawPlatformHighlights(hovered.platformIds);
    this._drawLabel(hovered, true);
  }

  abbreviate(name: string | null | undefined): string {
    if (!name) return '';
    const words = String(name).split(/[\s\-_]+/).filter((w) => w.length > 0);
    if (words.length >= 2) {
      // "Station 1" -> "S1", "Central Junction" -> "CJ"
      return words.slice(0, 4).map((w) => w[0].toUpperCase()).join('');
    }
    const consonants = String(name).replace(/[aeiou]/gi, '').toUpperCase();
    if (consonants.length >= 2) return consonants.slice(0, 3);
    return String(name).slice(0, 3).toUpperCase();
  }

  _placeLabel(station: Station | null | undefined): StationPlacement | null {
    if (!station) return null;
    // Prefer the backend-computed centroid; fall back to averaging the
    // platforms' positions if it's missing.
    let gridX: number, gridY: number;
    if (Array.isArray(station.center_position) && station.center_position.length >= 2) {
      [gridX, gridY] = station.center_position;
    } else if (Array.isArray(station.platforms)) {
      let sx = 0, sy = 0, n = 0;
      for (const pf of station.platforms) {
        for (const pos of (pf.positions || [])) {
          sx += pos[0]; sy += pos[1]; n++;
        }
      }
      if (n === 0) return null;
      gridX = sx / n; gridY = sy / n;
    } else {
      return null;
    }

    const cx = this.gridToScreenX(gridX, gridY);
    const cy = this.gridToScreenY(gridX, gridY);

    const name = station.name || station.station_id || 'Station';
    const abbrev = this.abbreviate(name);

    // Measure the collapsed label so placement is stable across hover.
    this.ctx.save();
    const s = this.scale;
    const fontSize = 9 * s;
    this.ctx.font = `bold ${fontSize}px sans-serif`;
    const textWidth = this.ctx.measureText(abbrev).width;
    const padding = 4 * s;
    const boxW = textWidth + padding * 2;
    const boxH = 12 * s;
    this.ctx.restore();

    // The station chip anchors to the platform group's middle, but the
    // platform names now sit ON the bars — so the chip must not land on a
    // bar and cover that text. Keep the centroid when it is genuinely clear
    // (platforms far apart); otherwise move the chip just outside the
    // group's bounding box, to the clear side nearest the centroid.
    const platformIds: Array<string | undefined> = Array.isArray(station.platform_ids)
      ? station.platform_ids
      : (station.platforms || []).map((pf) => pf.platform_id);

    // Only platform BODIES count as obstacles; tracks are thin and a chip
    // sitting on a track is acceptable. The bars carry the platform names,
    // so clearing the bars also clears that text.
    const allBodies = this.renderer.platformHandler?.getPlatformBodySegments?.() || [];

    // Bounding box of THIS station's bars (screen coords) — the group we
    // place the chip beside.
    const bodySegs = this.renderer.platformHandler?._platformBodySegments || [];
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const body of bodySegs) {
      if (!platformIds.includes(body.id)) continue;
      for (const seg of body.segments) {
        minX = Math.min(minX, seg.x1, seg.x2);
        minY = Math.min(minY, seg.y1, seg.y2);
        maxX = Math.max(maxX, seg.x1, seg.x2);
        maxY = Math.max(maxY, seg.y1, seg.y2);
      }
    }
    const haveBox = minX !== Infinity;
    const gap = 6 * s;

    // Centroid first (kept when clear), then just-outside-the-group slots.
    const candidates = [{ x: cx, y: cy }];
    if (haveBox) {
      candidates.push(
        { x: cx, y: minY - gap - boxH / 2 },  // above the group
        { x: cx, y: maxY + gap + boxH / 2 },  // below the group
        { x: minX - gap - boxW / 2, y: cy },  // left of the group
        { x: maxX + gap + boxW / 2, y: cy },  // right of the group
      );
    }

    const clearWanted = 2 * s;
    let bestX = cx, bestY = cy, bestScore = -Infinity;
    for (let i = 0; i < candidates.length; i++) {
      const { x: lx, y: ly } = candidates[i];
      const dist = this.renderer.distRectToSegments(
        lx - boxW / 2, ly - boxH / 2, boxW, boxH, allBodies,
      );
      const clear = dist >= clearWanted;
      // Centroid wins outright when clear. Otherwise the clear outside slot
      // closest to the centroid wins; proximity breaks ties between slots.
      const proximity = -Math.hypot(lx - cx, ly - cy) * 0.02;
      const score = i === 0
        ? (clear ? 1000 : dist)
        : (clear ? 100 : dist) + proximity;
      if (score > bestScore) {
        bestScore = score;
        bestX = lx;
        bestY = ly;
      }
    }

    return {
      stationId: station.station_id,
      fullName: name,
      abbrev,
      labelX: bestX,
      labelY: bestY,
      boxW,
      boxH,
      platformIds,
    };
  }

  _drawLabel(placement: StationPlacement, isHovered: boolean): void {
    const s = this.scale;
    const { labelX, labelY, abbrev, fullName } = placement;
    const displayText = isHovered ? fullName : abbrev;
    // Stations sit one tier above platform labels (platforms are 7px / 9px
    // hover). Size carries the hierarchy; chrome stays neutral so identity
    // doesn't compete with state.
    const fontSize = isHovered ? 11 * s : 9 * s;
    const padding = 4 * s;

    this.ctx.save();
    this.ctx.font = `bold ${fontSize}px sans-serif`;
    const textWidth = this.ctx.measureText(displayText).width;
    const boxH = (isHovered ? 14 : 12) * s;
    const boxW = textWidth + padding * 2;
    const bx = labelX - boxW / 2;
    const by = labelY - boxH / 2;
    const radius = 4 * s;

    // Fills are opaque (≥85%) so an overlapping label doesn't show
    // through and read as visual mush. Categorical color lives in the
    // border + a small mix into the dark base, not in a saturated fill.
    // Default: near-opaque neutral dark. Hover: dark base mixed with
    // ~15% amber so the warning hue is perceptible without becoming an
    // alert pill.
    const p = getCanvasPalette();
    const fillColor = isHovered ? withAlpha(p.overlayBg, 0.92) : withAlpha(p.overlayBg, 0.92);
    const borderColor = isHovered ? withAlpha(p.caution, 0.9) : withAlpha(p.text, 0.45);
    // Borders are screen-pixel capped so they read as hairlines at any
    // zoom — see the same pattern in _drawPlatformHighlights and the
    // train label.
    const borderWidth = isHovered
      ? Math.min(Math.max(1.2, s * 0.4), 2.5)
      : Math.min(Math.max(0.8, s * 0.25), 1.5);

    this.ctx.fillStyle = fillColor;
    this._roundRect(bx, by, boxW, boxH, radius);
    this.ctx.fill();

    this.ctx.strokeStyle = borderColor;
    this.ctx.lineWidth = borderWidth;
    this._roundRect(bx, by, boxW, boxH, radius);
    this.ctx.stroke();

    this.ctx.fillStyle = p.text;
    this.ctx.textAlign = 'center';
    this.ctx.textBaseline = 'middle';
    this.ctx.fillText(displayText, labelX, labelY);

    this.ctx.restore();
  }

  _drawPlatformHighlights(platformIds: Array<string | undefined>): void {
    const ph = this.renderer.platformHandler;
    if (!ph || !ph._platformBodySegments) return;
    const s = this.scale;
    // Highlights are context for the hovered label, not the focus. Cap the
    // stroke at ~2.5px screen width so it doesn't grow into a wall of amber
    // at high zoom, and match the label's border color exactly so the eye
    // reads them as one connected gesture.
    const strokeW = Math.min(Math.max(1.5, s * 0.5), 2.5);
    const p = getCanvasPalette();

    this.ctx.save();
    this.ctx.strokeStyle = withAlpha(p.caution, 0.7);
    this.ctx.lineWidth = strokeW;
    this.ctx.lineJoin = 'round';
    this.ctx.lineCap = 'round';

    for (const body of ph._platformBodySegments) {
      if (!platformIds.includes(body.id)) continue;
      // The body's segments come in groups of 4 per platform side
      // (parallelogram perimeter: innerS->outerS->outerE->innerE->innerS).
      // Draw each group as one closed path so corners join cleanly.
      for (let i = 0; i + 3 < body.segments.length; i += 4) {
        const seg0 = body.segments[i];
        const seg1 = body.segments[i + 1];
        const seg2 = body.segments[i + 2];
        this.ctx.beginPath();
        this.ctx.moveTo(seg0.x1, seg0.y1);
        this.ctx.lineTo(seg0.x2, seg0.y2);
        this.ctx.lineTo(seg1.x2, seg1.y2);
        this.ctx.lineTo(seg2.x2, seg2.y2);
        this.ctx.closePath();
        this.ctx.stroke();
      }
    }
    this.ctx.restore();
  }

  _mouseInRect(cx: number, cy: number, w: number, h: number): boolean {
    const mx = this.mouseX, my = this.mouseY;
    if (mx < 0 || my < 0) return false;
    const bx = cx - w / 2, by = cy - h / 2;
    return mx >= bx && mx <= bx + w && my >= by && my <= by + h;
  }

  /**
   * Resolve the station id at a given canvas-relative point, or null.
   * Useful for click-to-select wiring.
   */
  stationAt(x: number, y: number): string | undefined | null {
    for (const p of this._placements) {
      const bx = p.labelX - p.boxW / 2;
      const by = p.labelY - p.boxH / 2;
      if (x >= bx && x <= bx + p.boxW && y >= by && y <= by + p.boxH) {
        return p.stationId;
      }
    }
    return null;
  }

  _roundRect(x: number, y: number, w: number, h: number, r: number): void {
    this.ctx.beginPath();
    this.ctx.moveTo(x + r, y);
    this.ctx.lineTo(x + w - r, y);
    this.ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    this.ctx.lineTo(x + w, y + h - r);
    this.ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    this.ctx.lineTo(x + r, y + h);
    this.ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    this.ctx.lineTo(x, y + r);
    this.ctx.quadraticCurveTo(x, y, x + r, y);
    this.ctx.closePath();
  }
}
