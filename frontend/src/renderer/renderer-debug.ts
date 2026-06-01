// renderer-debug.ts — debug overlays: track-bit dots, coordinate / edge labels,
// switch-state badges, dead-end markers.
//
// Migrated from static/js/renderer-debug.js (longterm_todo.md item #7,
// renderer cluster). Behavior is unchanged; covered by renderer-debug.test.ts.
//
// A canvas-drawing handler owned by the Renderer: renderer.js does
// `new RendererDebugHandler(this)` and calls drawDebugOverlays() each world-layer
// flush; renderer-tracks.js also calls highlightDeadEnds() through the shared
// renderer back-reference. It reaches back only for the 2D context, the scale /
// grid size, and the grid<->screen projection — its track-bit geometry is self
// contained.
//
// Part of the Vite bundle; the Renderer constructs it directly
// (`new RendererDebugHandler(this)`).

/** The slice of the Renderer the debug handler reads through its back-reference. */
export interface DebugHandlerRenderer {
  ctx: CanvasRenderingContext2D;
  scale: number;
  gridSize: number;
  gridToScreenX(x: number, y: number): number;
  gridToScreenY(x: number, y: number): number;
}

/** The track fields the debug overlays read (all optional except the position). */
export interface DebugTrack {
  x: number;
  y: number;
  track_bits?: number;
  is_switch?: boolean;
  switch_state?: number;
  is_diagonal_only_switch?: boolean;
}

export class RendererDebugHandler {
  renderer: DebugHandlerRenderer;

  constructor(renderer: DebugHandlerRenderer) {
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

  gridToScreenX(x: number, y: number): number {
    return this.renderer.gridToScreenX(x, y);
  }
  gridToScreenY(x: number, y: number): number {
    return this.renderer.gridToScreenY(x, y);
  }

  /**
   * Get possible exit edges for a track given an entry edge.
   * Mirrors the backend TrackSegment.get_exits() logic.
   */
  getTrackExits(track: DebugTrack | null | undefined, entryEdge: string | null | undefined): string[] {
    if (!track || !entryEdge) return [];

    const bits = track.track_bits || 0;
    const TRACK_NS = 1, TRACK_EW = 2, TRACK_NE = 4, TRACK_NW = 8, TRACK_SE = 16, TRACK_SW = 32;

    const exits: string[] = [];

    if (bits & TRACK_NS) {
      if (entryEdge === 'N') exits.push('S');
      if (entryEdge === 'S') exits.push('N');
    }
    if (bits & TRACK_EW) {
      if (entryEdge === 'E') exits.push('W');
      if (entryEdge === 'W') exits.push('E');
    }
    if (bits & TRACK_NE) {
      if (entryEdge === 'N') exits.push('E');
      if (entryEdge === 'E') exits.push('N');
    }
    if (bits & TRACK_NW) {
      if (entryEdge === 'N') exits.push('W');
      if (entryEdge === 'W') exits.push('N');
    }
    if (bits & TRACK_SE) {
      if (entryEdge === 'S') exits.push('E');
      if (entryEdge === 'E') exits.push('S');
    }
    if (bits & TRACK_SW) {
      if (entryEdge === 'S') exits.push('W');
      if (entryEdge === 'W') exits.push('S');
    }

    if (track.is_switch && track.switch_state !== undefined && exits.length > 1) {
      return [exits[track.switch_state] || exits[0]];
    }

    return exits;
  }

  drawTrackBitsIndicator(x: number, y: number, trackBits: number): void {
    const screenX = this.gridToScreenX(x, y);
    const screenY = this.gridToScreenY(x, y);
    const cellSize = this.gridSize * this.scale;

    this.ctx.save();
    this.ctx.font = `${10 * this.scale}px monospace`;
    this.ctx.fillStyle = '#888';
    this.ctx.textAlign = 'right';
    this.ctx.textBaseline = 'bottom';

    this.ctx.fillText(
      trackBits.toString(),
      screenX + cellSize * 0.45,
      screenY + cellSize * 0.45,
    );

    this.ctx.restore();
  }

  drawBitVisualization(x: number, y: number, trackBits: number): void {
    const screenX = this.gridToScreenX(x, y);
    const screenY = this.gridToScreenY(x, y);
    const cellSize = this.gridSize * this.scale;

    const bitPositions = {
      NS: { x: 0.1, y: 0.1, bit: 1 },
      EW: { x: 0.3, y: 0.1, bit: 2 },
      NE: { x: 0.5, y: 0.1, bit: 4 },
      NW: { x: 0.7, y: 0.1, bit: 8 },
      SE: { x: 0.1, y: 0.3, bit: 16 },
      SW: { x: 0.3, y: 0.3, bit: 32 },
    };

    this.ctx.save();
    for (const pos of Object.values(bitPositions)) {
      const isActive = (trackBits & pos.bit) !== 0;

      this.ctx.fillStyle = isActive ? '#00ff88' : '#333';
      this.ctx.beginPath();
      this.ctx.arc(
        screenX - cellSize * 0.5 + cellSize * pos.x,
        screenY - cellSize * 0.5 + cellSize * pos.y,
        3 * this.scale,
        0, Math.PI * 2,
      );
      this.ctx.fill();
    }
    this.ctx.restore();
  }

  drawCoordinateLabel(x: number, y: number): void {
    const screenX = this.gridToScreenX(x, y);
    const screenY = this.gridToScreenY(x, y);
    const cellSize = this.gridSize * this.scale;

    this.ctx.save();
    this.ctx.font = `${8 * this.scale}px monospace`;
    this.ctx.fillStyle = '#666';
    this.ctx.textAlign = 'left';
    this.ctx.textBaseline = 'top';
    this.ctx.fillText(
      `(${x},${y})`,
      screenX - cellSize * 0.45,
      screenY - cellSize * 0.45,
    );
    this.ctx.restore();
  }

  highlightDeadEnds(x: number, y: number, deadEnds: string[], trackBits = 0): void {
    const screenX = this.gridToScreenX(x, y);
    const screenY = this.gridToScreenY(x, y);
    const tileSize = this.gridSize * this.scale;
    const quarterTile = tileSize / 4;
    const markerLen = 6 * this.scale;

    const TRACK_NS = 1, TRACK_EW = 2, TRACK_NE = 4, TRACK_NW = 8, TRACK_SE = 16, TRACK_SW = 32;

    const portToPaths: Record<string, number[]> = {
      N: [TRACK_NS, TRACK_NE, TRACK_NW],
      S: [TRACK_NS, TRACK_SE, TRACK_SW],
      E: [TRACK_EW, TRACK_NE, TRACK_SE],
      W: [TRACK_EW, TRACK_NW, TRACK_SW],
    };

    const pathPerpendicular: Record<number, { dx: number; dy: number }> = {
      [TRACK_NS]: { dx: 1, dy: 1 },
      [TRACK_EW]: { dx: 1, dy: -1 },
      [TRACK_NE]: { dx: 1, dy: 0 },
      [TRACK_NW]: { dx: 0, dy: 1 },
      [TRACK_SE]: { dx: 0, dy: 1 },
      [TRACK_SW]: { dx: 1, dy: 0 },
    };

    const edgePositions: Record<string, { x: number; y: number }> = {
      N: { x: quarterTile, y: -quarterTile },
      E: { x: quarterTile, y: quarterTile },
      S: { x: -quarterTile, y: quarterTile },
      W: { x: -quarterTile, y: -quarterTile },
    };

    this.ctx.save();
    this.ctx.strokeStyle = '#ff9900';
    this.ctx.lineWidth = 3 * this.scale;

    for (const edge of deadEnds) {
      const port = edge.length > 1 ? edge[0] : edge;
      const paths = portToPaths[port];
      if (!paths) continue;

      let activePath: number | null = null;
      for (const pathBit of paths) {
        if (trackBits & pathBit) {
          activePath = pathBit;
          if (pathBit === TRACK_NS || pathBit === TRACK_EW) break;
        }
      }

      let dx, dy;
      if (activePath && pathPerpendicular[activePath]) {
        dx = pathPerpendicular[activePath].dx;
        dy = pathPerpendicular[activePath].dy;
      } else {
        dx = (port === 'N' || port === 'S') ? 1 : -1;
        dy = 1;
      }

      const len = Math.sqrt(dx * dx + dy * dy);
      dx = (dx / len) * markerLen;
      dy = (dy / len) * markerLen;

      const pos = edgePositions[port];
      const cx = screenX + pos.x;
      const cy = screenY + pos.y;

      this.ctx.beginPath();
      this.ctx.moveTo(cx - dx, cy - dy);
      this.ctx.lineTo(cx + dx, cy + dy);
      this.ctx.stroke();
    }
    this.ctx.restore();
  }

  drawSwitchStateOverlay(track: DebugTrack): void {
    const screenX = this.gridToScreenX(track.x, track.y);
    const screenY = this.gridToScreenY(track.x, track.y);

    this.ctx.save();

    const stateText = track.switch_state === 0 ? 'MAIN' : 'DIV';
    const isDiagonalOnly = track.is_diagonal_only_switch;

    this.ctx.font = `bold ${11 * this.scale}px sans-serif`;
    this.ctx.textAlign = 'center';
    this.ctx.textBaseline = 'middle';

    const bgColor = track.switch_state === 0 ? '#1a5a2a' : '#5a4a1a';
    const textColor = track.switch_state === 0 ? '#04d361' : '#ffd93d';

    this.ctx.fillStyle = bgColor;
    this.ctx.globalAlpha = 0.9;
    this.ctx.beginPath();
    this.ctx.roundRect(screenX - 25 * this.scale, screenY - 18 * this.scale, 50 * this.scale, 16 * this.scale, 3);
    this.ctx.fill();
    this.ctx.globalAlpha = 1;

    this.ctx.fillStyle = textColor;
    this.ctx.fillText(stateText, screenX, screenY - 10 * this.scale);

    if (isDiagonalOnly) {
      this.ctx.font = `${8 * this.scale}px sans-serif`;
      this.ctx.fillStyle = '#ff9900';
      this.ctx.fillText('⟳clockwise', screenX, screenY + 6 * this.scale);
    }

    this.ctx.restore();
  }

  drawEdgeLabels(x: number, y: number): void {
    const screenX = this.gridToScreenX(x, y);
    const screenY = this.gridToScreenY(x, y);
    const tileSize = this.gridSize * this.scale;
    const quarterTile = tileSize / 4;

    this.ctx.save();
    this.ctx.font = `bold ${9 * this.scale}px sans-serif`;
    this.ctx.textAlign = 'center';
    this.ctx.textBaseline = 'middle';
    this.ctx.fillStyle = '#00ffff';

    this.ctx.fillText('N', screenX + quarterTile + 8, screenY - quarterTile - 8);
    this.ctx.fillText('E', screenX + quarterTile + 8, screenY + quarterTile + 8);
    this.ctx.fillText('S', screenX - quarterTile - 8, screenY + quarterTile + 8);
    this.ctx.fillText('W', screenX - quarterTile - 8, screenY - quarterTile - 8);

    this.ctx.restore();
  }

  drawDebugOverlays(tracks: DebugTrack[]): void {
    const showBitsElement = document.getElementById('debug-show-bits') as HTMLInputElement | null;
    const showCoordsElement = document.getElementById('debug-show-coords') as HTMLInputElement | null;
    const showSwitchesElement = document.getElementById('debug-show-switches') as HTMLInputElement | null;
    const showEdgesElement = document.getElementById('debug-show-edges') as HTMLInputElement | null;
    const showBits = showBitsElement ? showBitsElement.checked : false;
    const showCoords = showCoordsElement ? showCoordsElement.checked : false;
    const showSwitches = showSwitchesElement ? showSwitchesElement.checked : false;
    const showEdges = showEdgesElement ? showEdgesElement.checked : false;

    for (const track of tracks) {
      if (showBits && track.track_bits !== undefined) {
        this.drawBitVisualization(track.x, track.y, track.track_bits);
        this.drawTrackBitsIndicator(track.x, track.y, track.track_bits);
      }

      if (showCoords) {
        this.drawCoordinateLabel(track.x, track.y);
      }

      if (showSwitches && track.is_switch) {
        this.drawSwitchStateOverlay(track);
      }

      if (showEdges) {
        this.drawEdgeLabels(track.x, track.y);
      }
    }
  }
}
