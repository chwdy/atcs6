// renderer-tracks.ts — draws track tiles (straight/diagonal segments, switches
// with their state badge + lock rings, dead-ends, crossings) and the editor
// preview overlays. Owns the shared getEdgePoints() / drawGridSegment() geometry
// that the other renderer handlers reach for through the renderer.
//
// Migrated from static/js/renderer-tracks.js (longterm_todo.md item #7,
// renderer cluster). Behavior is unchanged; covered by renderer-tracks.test.ts.
//
// Consumes the getActiveSegments, isCrossing, TRACK_WIDTH globals
// (frontend/src/legacy-globals.d.ts). Part of the Vite bundle; the Renderer
// constructs it directly (`new RendererTrackHandler(this)`).

import type { Signal } from './renderer-signals';

export interface EdgePoint {
  x: number;
  y: number;
}

interface SwitchEntity {
  occupied_by?: unknown;
  train_protection_claims?: unknown[];
  locks?: unknown[];
}

export interface Track {
  x: number;
  y: number;
  track_bits?: number;
  block_section_id?: string | null;
  occupied_by?: unknown;
  reserved_by?: unknown;
  is_switch?: boolean;
  switch_state?: number;
  is_diagonal_only_switch?: boolean;
  switch_points?: Record<string, string[]>;
  switch_id?: string;
  signals?: Signal[];
  dead_end_ports?: string[];
}

/** The slice of the Renderer the track handler reads through its back-reference. */
export interface TrackHandlerRenderer {
  ctx: CanvasRenderingContext2D;
  scale: number;
  gridSize: number;
  currentBlocks?: Record<string, { state?: string } | undefined> | null;
  currentNetwork?: { switches?: Record<string, SwitchEntity | undefined> } | null;
  gridToScreenX(x: number, y: number): number;
  gridToScreenY(x: number, y: number): number;
  signalHandler: { drawSingleSignal(signal: unknown, trackBits: number | undefined, half: number): void };
  debugHandler: { highlightDeadEnds(x: number, y: number, deadEnds: string[], trackBits?: number): void };
}

export class RendererTrackHandler {
  renderer: TrackHandlerRenderer;

  constructor(renderer: TrackHandlerRenderer) {
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
  get currentBlocks(): Record<string, { state?: string } | undefined> | null | undefined {
    return this.renderer.currentBlocks;
  }
  get currentNetwork(): { switches?: Record<string, SwitchEntity | undefined> } | null | undefined {
    return this.renderer.currentNetwork;
  }

  gridToScreenX(x: number, y: number): number {
    return this.renderer.gridToScreenX(x, y);
  }
  gridToScreenY(x: number, y: number): number {
    return this.renderer.gridToScreenY(x, y);
  }

  drawTrack(track: Track, drawSignals = true): void {
    const cellSize = this.gridSize * this.scale;
    const cx = this.gridToScreenX(track.x, track.y);
    const cy = this.gridToScreenY(track.x, track.y);
    const half = cellSize / 2;

    const trackBits = track.track_bits ?? 0;

    this.ctx.save();
    this.ctx.translate(cx, cy);

    let trackColor = '#666';
    if (track.block_section_id && this.currentBlocks) {
      const block = this.currentBlocks[track.block_section_id];
      if (block) {
        const blockState = (block.state || 'free').toLowerCase();
        if (blockState === 'occupied') {
          trackColor = '#e83f5b';
        } else if (blockState === 'reserved') {
          trackColor = '#04d361';
        } else if (blockState === 'free') {
          trackColor = '#4285f4';
        } else if (blockState === 'caution') {
          trackColor = '#ffcd1e';
        }
      }
    } else if (track.occupied_by) {
      trackColor = '#e83f5b';
    } else if (track.reserved_by) {
      trackColor = '#ffcd1e';
    }

    const isSwitch = track.is_switch || false;
    const switchState = track.switch_state || 0;
    const isDiagonalOnly = track.is_diagonal_only_switch || false;
    const switchPoints = track.switch_points || {};

    if (isSwitch) {
      this.drawTrackWithSwitches(trackBits, half, trackColor, switchState, isDiagonalOnly, switchPoints);

      // Two distinct lock states per design/120-switch.md:32 — path-owned
      // future authority (Switch.locks) vs train-owned physical hold
      // (Switch.occupied_by / train_protection_claims) after head-entry
      // consumes the primary lock. Train-owned wins when both present.
      const switchEntity = (track.switch_id && this.currentNetwork && this.currentNetwork.switches)
        ? this.currentNetwork.switches[track.switch_id]
        : null;
      const trainOwned = !!(switchEntity && (
        switchEntity.occupied_by ||
        (Array.isArray(switchEntity.train_protection_claims) && switchEntity.train_protection_claims.length > 0)
      ));

      if (trainOwned) {
        this.ctx.strokeStyle = '#8B4513'; // Brown — train-owned hold
        this.ctx.lineWidth = 2 * this.scale;
        this.ctx.beginPath();
        this.ctx.arc(0, 0, 6 * this.scale, 0, Math.PI * 2);
        this.ctx.stroke();
      } else if (switchEntity && Array.isArray(switchEntity.locks) && switchEntity.locks.length > 0) {
        this.ctx.strokeStyle = '#ff8c00'; // Orange — path-owned future lock
        this.ctx.lineWidth = 2 * this.scale;
        this.ctx.beginPath();
        this.ctx.arc(0, 0, 6 * this.scale, 0, Math.PI * 2);
        this.ctx.stroke();
      }
    } else {
      this.drawTrackFromBits(trackBits, half, trackColor, track.dead_end_ports || []);
    }

    if (drawSignals && track.signals && track.signals.length > 0) {
      for (const signal of track.signals) {
        this.renderer.signalHandler.drawSingleSignal(signal, track.track_bits, half);
      }
    }

    this.ctx.restore();

    if (track.dead_end_ports && track.dead_end_ports.length > 0) {
      this.renderer.debugHandler.highlightDeadEnds(track.x, track.y, track.dead_end_ports, track.track_bits || 0);
    }
  }

  getEdgePoints(half: number): Record<string, EdgePoint> {
    const h = half;
    return {
      N: { x: h / 2, y: -h / 2 },
      E: { x: h / 2, y: h / 2 },
      S: { x: -h / 2, y: h / 2 },
      W: { x: -h / 2, y: -h / 2 },
    };
  }

  drawGridSegment(start: EdgePoint, end: EdgePoint, width: number, e1: string, e2: string, fillColor: string): void {
    const halfWidth = width / 2;

    const getEdgeOffset = (edge: string): EdgePoint => {
      switch (edge) {
        case 'N': return { x: -halfWidth, y: -halfWidth };
        case 'E': return { x: halfWidth, y: -halfWidth };
        case 'S': return { x: halfWidth, y: halfWidth };
        case 'W': return { x: -halfWidth, y: halfWidth };
        default: return { x: 0, y: 0 };
      }
    };

    const offset1 = getEdgeOffset(e1);
    const offset2 = getEdgeOffset(e2);

    const p1 = { x: start.x + offset1.x, y: start.y + offset1.y };
    const p2 = { x: start.x - offset1.x, y: start.y - offset1.y };
    const p3 = { x: end.x + offset2.x, y: end.y + offset2.y };
    const p4 = { x: end.x - offset2.x, y: end.y - offset2.y };

    this.ctx.fillStyle = fillColor;
    this.ctx.beginPath();
    this.ctx.moveTo(p1.x, p1.y);
    this.ctx.lineTo(p2.x, p2.y);
    this.ctx.lineTo(p3.x, p3.y);
    this.ctx.lineTo(p4.x, p4.y);
    this.ctx.closePath();
    this.ctx.fill();
  }

  drawTrackFromBits(trackBits: number, half: number, color: string, deadEndPorts: string[] = []): void {
    const edgePoints = this.getEdgePoints(half);

    const segments = getActiveSegments(trackBits);
    for (const [e1, e2] of segments) {
      const start = edgePoints[e1];
      const end = edgePoints[e2];
      if (start && end) {
        this.drawGridSegment(start, end, TRACK_WIDTH * this.scale, e1, e2, color);
      }
    }

    for (const port of deadEndPorts) {
      const deadEndEdge = port.charAt(0).toUpperCase();

      for (const [e1, e2] of segments) {
        if (e1 === deadEndEdge || e2 === deadEndEdge) {
          const otherEdge = (e1 === deadEndEdge) ? e2 : e1;
          const deadEndPoint = edgePoints[deadEndEdge];
          const otherPoint = edgePoints[otherEdge];

          const isAxial = (deadEndEdge === 'N' && otherEdge === 'S') ||
            (deadEndEdge === 'S' && otherEdge === 'N') ||
            (deadEndEdge === 'E' && otherEdge === 'W') ||
            (deadEndEdge === 'W' && otherEdge === 'E');

          if (isAxial) {
            this.drawGridSegment({ x: 0, y: 0 }, otherPoint, TRACK_WIDTH * this.scale, deadEndEdge, otherEdge, color);
          } else {
            this.drawGridSegment(deadEndPoint, otherPoint, TRACK_WIDTH * this.scale, e1, e2, color);
          }
          break;
        }
      }
    }

    if (isCrossing(trackBits)) {
      this.ctx.fillStyle = '#8257e5';
      this.ctx.beginPath();
      this.ctx.arc(0, 0, 4 * this.scale, 0, Math.PI * 2);
      this.ctx.fill();
    }
  }

  drawTrackWithSwitches(
    trackBits: number,
    half: number,
    color: string,
    switchState: number,
    isDiagonalOnly: boolean,
    switchPoints: Record<string, string[]>,
  ): void {
    const edgePoints = this.getEdgePoints(half);

    const SEGMENTS = [
      { bit: 0, edges: ['N', 'S'], name: 'NS' },
      { bit: 1, edges: ['E', 'W'], name: 'EW' },
      { bit: 2, edges: ['N', 'E'], name: 'NE' },
      { bit: 3, edges: ['N', 'W'], name: 'NW' },
      { bit: 4, edges: ['S', 'E'], name: 'SE' },
      { bit: 5, edges: ['S', 'W'], name: 'SW' },
    ];

    const activeSegmentBits = new Set<number>();

    if (isDiagonalOnly && switchPoints) {
      const CLOCKWISE = ['N', 'E', 'S', 'W'];

      for (const [entryEdge, exits] of Object.entries(switchPoints)) {
        if (!exits || exits.length < 2) continue;

        const sortedExits = [...exits].sort((a, b) =>
          CLOCKWISE.indexOf(a) - CLOCKWISE.indexOf(b),
        );

        const activeExit = switchState === 0 ? sortedExits[0] : sortedExits[1];

        for (const seg of SEGMENTS) {
          if (!(trackBits & (1 << seg.bit))) continue;
          const [e1, e2] = seg.edges;
          if ((e1 === entryEdge && e2 === activeExit) ||
            (e2 === entryEdge && e1 === activeExit)) {
            activeSegmentBits.add(seg.bit);
          }
        }
      }
    }

    for (const seg of SEGMENTS) {
      if (!(trackBits & (1 << seg.bit))) continue;

      const [e1, e2] = seg.edges;
      const start = edgePoints[e1];
      const end = edgePoints[e2];
      if (!start || !end) continue;

      let isActive;
      if (isDiagonalOnly) {
        isActive = activeSegmentBits.has(seg.bit);
      } else {
        const isLongSegment = seg.bit <= 1;
        isActive = (isLongSegment && switchState === 0) ||
          (!isLongSegment && switchState === 1);
      }

      if (!isActive) {
        const segmentColor = '#444';
        const segmentWidth = 2 * this.scale;
        this.drawGridSegment(start, end, segmentWidth, e1, e2, segmentColor);
      }
    }

    for (const seg of SEGMENTS) {
      if (!(trackBits & (1 << seg.bit))) continue;

      const [e1, e2] = seg.edges;
      const start = edgePoints[e1];
      const end = edgePoints[e2];
      if (!start || !end) continue;

      let isActive;
      if (isDiagonalOnly) {
        isActive = activeSegmentBits.has(seg.bit);
      } else {
        const isLongSegment = seg.bit <= 1;
        isActive = (isLongSegment && switchState === 0) ||
          (!isLongSegment && switchState === 1);
      }

      if (isActive) {
        const segmentColor = color;
        const segmentWidth = TRACK_WIDTH * this.scale;
        this.drawGridSegment(start, end, segmentWidth, e1, e2, segmentColor);
      }
    }

    const isMain = switchState === 0;
    this.ctx.fillStyle = isMain ? '#04d361' : '#ffd93d';
    this.ctx.beginPath();
    this.ctx.arc(0, 0, 5 * this.scale, 0, Math.PI * 2);
    this.ctx.fill();

    this.ctx.fillStyle = '#000';
    this.ctx.font = `bold ${8 * this.scale}px sans-serif`;
    this.ctx.textAlign = 'center';
    this.ctx.textBaseline = 'middle';
    this.ctx.fillText(isMain ? 'M' : 'D', 0, 0);

    if (isDiagonalOnly) {
      this.ctx.font = `${7 * this.scale}px sans-serif`;
      this.ctx.fillStyle = '#ff9900';
      this.ctx.fillText('⟳', 9 * this.scale, -7 * this.scale);
    }
  }

  drawPreviewPath(path: Array<{ x: number; y: number }> | null | undefined): void {
    if (!path || path.length === 0) return;

    this.ctx.save();

    this.ctx.strokeStyle = 'rgba(130, 87, 229, 0.6)';
    this.ctx.fillStyle = 'rgba(130, 87, 229, 0.3)';
    this.ctx.lineWidth = 3 * this.scale;
    this.ctx.setLineDash([5, 5]);

    const cellSize = this.gridSize * this.scale;

    for (let i = 0; i < path.length; i++) {
      const pos = path[i];
      const cx = this.gridToScreenX(pos.x, pos.y);
      const cy = this.gridToScreenY(pos.x, pos.y);

      this.ctx.beginPath();
      this.ctx.arc(cx, cy, cellSize / 3, 0, Math.PI * 2);
      this.ctx.fill();

      if (i < path.length - 1) {
        const next = path[i + 1];
        const nx = this.gridToScreenX(next.x, next.y);
        const ny = this.gridToScreenY(next.x, next.y);

        this.ctx.beginPath();
        this.ctx.moveTo(cx, cy);
        this.ctx.lineTo(nx, ny);
        this.ctx.stroke();
      }
    }

    this.ctx.restore();
  }

  drawPreviewTracks(tracks: Track[] | null | undefined): void {
    if (!tracks || tracks.length === 0) {
      return;
    }

    const cellSize = this.gridSize * this.scale;
    const half = cellSize / 2;
    const previewColor = 'rgba(255, 200, 50, 0.8)';

    for (const track of tracks) {
      const trackBits = track.track_bits ?? 0;
      if (trackBits === 0) continue;

      const cx = this.gridToScreenX(track.x, track.y);
      const cy = this.gridToScreenY(track.x, track.y);

      this.ctx.save();
      this.ctx.translate(cx, cy);

      const edgePoints = this.getEdgePoints(half);
      this.ctx.strokeStyle = previewColor;
      this.ctx.lineWidth = 5 * this.scale;
      this.ctx.lineCap = 'square';

      const segments = getActiveSegments(trackBits);
      for (const [e1, e2] of segments) {
        const start = edgePoints[e1];
        const end = edgePoints[e2];
        if (start && end) {
          this.ctx.beginPath();
          this.ctx.moveTo(start.x, start.y);
          this.ctx.lineTo(end.x, end.y);
          this.ctx.stroke();
        }
      }

      if (isCrossing(trackBits)) {
        this.ctx.fillStyle = 'rgba(255, 200, 50, 0.9)';
        this.ctx.beginPath();
        this.ctx.arc(0, 0, 4 * this.scale, 0, Math.PI * 2);
        this.ctx.fill();
      }

      this.ctx.restore();
    }
  }

  drawHoverPreview(
    gridPos: { x: number; y: number },
    toolType: string,
    isValid: boolean,
    signalDirection: string | null = null,
    trackBits: number | null = null,
  ): void {
    const cx = this.gridToScreenX(gridPos.x, gridPos.y);
    const cy = this.gridToScreenY(gridPos.x, gridPos.y);
    const half = (this.gridSize * this.scale) / 2;

    this.ctx.save();

    this.ctx.strokeStyle = isValid ? 'rgba(4, 211, 97, 0.8)' : 'rgba(232, 63, 91, 0.5)';
    this.ctx.fillStyle = isValid ? 'rgba(4, 211, 97, 0.2)' : 'rgba(232, 63, 91, 0.1)';
    this.ctx.lineWidth = 2 * this.scale;

    this.ctx.beginPath();
    this.ctx.moveTo(cx, cy - half);
    this.ctx.lineTo(cx + half, cy);
    this.ctx.lineTo(cx, cy + half);
    this.ctx.lineTo(cx - half, cy);
    this.ctx.closePath();
    this.ctx.fill();
    this.ctx.stroke();

    if (isValid) {
      if (toolType === 'signal' && signalDirection) {
        this.ctx.translate(cx, cy);

        const detectionDir = signalDirection.charAt(0).toUpperCase();
        const travelEdge = detectionDir;
        const edgePoints = this.getEdgePoints(half);
        const isLongPath = (trackBits === 1 || trackBits === 2);
        const isDiagonal = (trackBits === 4 || trackBits === 8 || trackBits === 16 || trackBits === 32);

        let sx, sy;
        if (isLongPath) {
          const rightSide = ({
            N: 'E', S: 'W', E: 'S', W: 'N',
          } as Record<string, string>)[travelEdge] || 'E';
          const sidePoint = edgePoints[rightSide] || { x: 0, y: 0 };
          sx = sidePoint.x * 0.5;
          sy = sidePoint.y * 0.5;
        } else {
          sx = 0;
          sy = 0;
        }

        let dirX, dirY;
        if (isDiagonal) {
          let diagonalX, diagonalY;
          switch (trackBits) {
            case 4: diagonalX = 0; diagonalY = -1; break;
            case 8: diagonalX = 1; diagonalY = 0; break;
            case 16: diagonalX = -1; diagonalY = 0; break;
            case 32: diagonalX = 0; diagonalY = 1; break;
            default: diagonalX = 1; diagonalY = 0; break;
          }

          let edge1, edge2;
          switch (trackBits) {
            case 4: edge1 = 'N'; edge2 = 'E'; break;
            case 8: edge1 = 'N'; edge2 = 'W'; break;
            case 16: edge1 = 'S'; edge2 = 'E'; break;
            case 32: edge1 = 'S'; edge2 = 'W'; break;
            default: edge1 = 'N'; edge2 = 'E'; break;
          }

          if (detectionDir === edge1) {
            diagonalX = -diagonalX;
            diagonalY = -diagonalY;
          }

          const len = Math.sqrt(diagonalX * diagonalX + diagonalY * diagonalY);
          dirX = diagonalX / len;
          dirY = diagonalY / len;
        } else {
          const approachEdge = ({ N: 'S', S: 'N', E: 'W', W: 'E' } as Record<string, string>)[detectionDir] || 'W';
          const dirPoint = edgePoints[approachEdge] || { x: 1, y: 0 };
          const len = Math.sqrt(dirPoint.x * dirPoint.x + dirPoint.y * dirPoint.y);
          dirX = dirPoint.x / len;
          dirY = dirPoint.y / len;
        }

        this.ctx.strokeStyle = 'rgba(153, 153, 153, 0.8)';
        this.ctx.lineWidth = 1.5 * this.scale;
        this.ctx.beginPath();
        this.ctx.arc(sx, sy, 4 * this.scale, 0, Math.PI * 2);
        this.ctx.stroke();

        const stickLength = half * 0.4;
        const stickEndX = sx + dirX * stickLength;
        const stickEndY = sy + dirY * stickLength;

        this.ctx.strokeStyle = 'rgba(153, 153, 153, 0.8)';
        this.ctx.lineWidth = 2 * this.scale;
        this.ctx.beginPath();
        this.ctx.moveTo(sx, sy);
        this.ctx.lineTo(stickEndX, stickEndY);
        this.ctx.stroke();

        const innerRadius = 3 * this.scale;
        this.ctx.fillStyle = 'rgba(4, 211, 97, 0.7)';
        this.ctx.beginPath();
        this.ctx.arc(sx, sy, innerRadius, 0, Math.PI * 2);
        this.ctx.fill();

        const orthoX = -dirY;
        const orthoY = dirX;
        const orthoLen = half * 0.15;

        this.ctx.beginPath();
        this.ctx.moveTo(stickEndX - orthoX * orthoLen, stickEndY - orthoY * orthoLen);
        this.ctx.lineTo(stickEndX + orthoX * orthoLen, stickEndY + orthoY * orthoLen);
        this.ctx.stroke();

        this.ctx.fillStyle = 'rgba(255, 255, 255, 0.9)';
        this.ctx.font = `bold ${8 * this.scale}px sans-serif`;
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';
        const labelX = sx - dirX * half * 0.3;
        const labelY = sy - dirY * half * 0.3;
        this.ctx.fillText(signalDirection.charAt(0), labelX, labelY);
      } else {
        this.ctx.font = `${16 * this.scale}px sans-serif`;
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';
        this.ctx.fillStyle = 'rgba(255, 255, 255, 0.8)';
        const icon = toolType === 'signal' ? '🚦' : '🚉';
        this.ctx.fillText(icon, cx, cy);
      }
    }

    this.ctx.restore();
  }

  drawEraserPreviewPath(path: Array<{ x: number; y: number }> | null | undefined): void {
    if (!path || path.length === 0) return;

    this.ctx.save();
    this.ctx.strokeStyle = 'rgba(232, 63, 91, 0.8)';
    this.ctx.fillStyle = 'rgba(232, 63, 91, 0.3)';
    this.ctx.lineWidth = 4 * this.scale;
    this.ctx.setLineDash([6, 4]);

    const cellSize = this.gridSize * this.scale;
    const halfTile = cellSize / 2;

    for (const pos of path) {
      const cx = this.gridToScreenX(pos.x, pos.y);
      const cy = this.gridToScreenY(pos.x, pos.y);

      this.ctx.beginPath();
      this.ctx.moveTo(cx, cy - halfTile);
      this.ctx.lineTo(cx + halfTile, cy);
      this.ctx.lineTo(cx, cy + halfTile);
      this.ctx.lineTo(cx - halfTile, cy);
      this.ctx.closePath();
      this.ctx.fill();

      const xSize = halfTile * 0.4;
      this.ctx.beginPath();
      this.ctx.moveTo(cx - xSize, cy - xSize);
      this.ctx.lineTo(cx + xSize, cy + xSize);
      this.ctx.moveTo(cx + xSize, cy - xSize);
      this.ctx.lineTo(cx - xSize, cy + xSize);
      this.ctx.stroke();
    }

    this.ctx.setLineDash([]);
    this.ctx.strokeStyle = 'rgba(232, 63, 91, 0.6)';
    this.ctx.lineWidth = 2 * this.scale;
    this.ctx.beginPath();
    for (let i = 0; i < path.length; i++) {
      const cx = this.gridToScreenX(path[i].x, path[i].y);
      const cy = this.gridToScreenY(path[i].x, path[i].y);
      if (i === 0) this.ctx.moveTo(cx, cy);
      else this.ctx.lineTo(cx, cy);
    }
    this.ctx.stroke();

    this.ctx.restore();
  }
}
