// renderer-signals.ts — draws signal heads (stick + lamp) and the dashed lines
// linking a signal to the block it monitors.
//
// Migrated from static/js/renderer-signals.js (longterm_todo.md item #7,
// renderer cluster). Behavior is unchanged; covered by renderer-signals.test.ts.
//
// A canvas-drawing handler owned by the Renderer: renderer.js calls
// drawSingleSignal() per signal and drawSignalBlockConnections() once per flush.
// It reaches back through its `renderer` reference for the 2D context, the
// projection, and the track handler's edge-point geometry, and reads the global
// trackBitsToEdges() (still-classic constants.js) until that module migrates.
//
// Part of the Vite bundle; the Renderer constructs it directly
// (`new RendererSignalHandler(this)`).

export interface EdgePoint {
  x: number;
  y: number;
}

export interface Signal {
  signal_id?: string;
  detection_direction?: string;
  approach_direction?: string;
  state?: string;
  name?: string;
  signal_type?: string;
  monitored_block_id?: string | null;
}

export interface SignalTrack {
  x: number;
  y: number;
  track_bits?: number;
  block_section_id?: string | null;
  signals?: Signal[];
}

/** The slice of the Renderer the signal handler reads through its back-reference. */
export interface SignalHandlerRenderer {
  ctx: CanvasRenderingContext2D;
  scale: number;
  gridSize: number;
  gridToScreenX(x: number, y: number): number;
  gridToScreenY(x: number, y: number): number;
  trackHandler: {
    getEdgePoints(half: number): Record<string, EdgePoint>;
  };
}

export class RendererSignalHandler {
  renderer: SignalHandlerRenderer;

  constructor(renderer: SignalHandlerRenderer) {
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

  drawSingleSignal(signal: Signal, trackBits: number | undefined, half: number): void {
    if (!signal.detection_direction) {
      console.error(`[Renderer] Signal ${signal.signal_id} missing detection_direction!`, signal);
      return;
    }
    if (!signal.approach_direction) {
      console.error(`[Renderer] Signal ${signal.signal_id} missing approach_direction!`, signal);
      return;
    }
    const approachDir = signal.approach_direction;
    const detectionDir = signal.detection_direction;
    const facingEdge = approachDir.charAt(0).toUpperCase();
    const edgePoints = this.renderer.trackHandler.getEdgePoints(half);
    const isLongPath = (trackBits === 1 || trackBits === 2);
    const isDiagonal = (trackBits === 4 || trackBits === 8 || trackBits === 16 || trackBits === 32);

    let sx, sy;
    if (isLongPath) {
      const travelEdge = detectionDir.charAt(0).toUpperCase();
      const rightSide = ({ N: 'E', S: 'W', E: 'S', W: 'N' } as Record<string, string>)[travelEdge] || 'E';
      const sidePoint = edgePoints[rightSide] || { x: 0, y: 0 };
      sx = sidePoint.x * 0.5;
      sy = sidePoint.y * 0.5;
    } else {
      sx = 0;
      sy = 0;
    }

    const state = (signal.state || 'green').toUpperCase();
    let signalColor;
    if (state === 'GREEN') {
      signalColor = '#04d361';
    } else if (state === 'YELLOW') {
      signalColor = '#ffd93d';
    } else {
      signalColor = '#e83f5b';
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

      const detectionEdge = detectionDir.charAt(0).toUpperCase();
      if (detectionEdge === edge1) {
        diagonalX = -diagonalX;
        diagonalY = -diagonalY;
      }

      const len = Math.sqrt(diagonalX * diagonalX + diagonalY * diagonalY);
      dirX = diagonalX / len;
      dirY = diagonalY / len;
    } else {
      const travelEdge = facingEdge || 'E';
      const dirPoint = edgePoints[travelEdge] || { x: 1, y: 0 };
      const len = Math.sqrt(dirPoint.x * dirPoint.x + dirPoint.y * dirPoint.y);
      dirX = dirPoint.x / len;
      dirY = dirPoint.y / len;
    }

    const stickLength = half * 0.4;
    const stickEndX = sx + dirX * stickLength;
    const stickEndY = sy + dirY * stickLength;

    this.ctx.strokeStyle = '#999';
    this.ctx.lineWidth = 1.5 * this.scale;
    this.ctx.beginPath();
    this.ctx.arc(sx, sy, 4 * this.scale, 0, Math.PI * 2);
    this.ctx.stroke();

    this.ctx.strokeStyle = '#999';
    this.ctx.lineWidth = 2 * this.scale;
    this.ctx.beginPath();
    this.ctx.moveTo(sx, sy);
    this.ctx.lineTo(stickEndX, stickEndY);
    this.ctx.stroke();

    const innerRadius = 3 * this.scale;
    this.ctx.fillStyle = signalColor;
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

    const showSignalDetailsElement = document.getElementById('debug-show-signals') as HTMLInputElement | null;
    const showSignalDetails = showSignalDetailsElement
      ? showSignalDetailsElement.checked
      : false;
    if (signal.name && showSignalDetails) {
      this.ctx.fillStyle = '#fff';
      this.ctx.font = `bold ${6 * this.scale}px sans-serif`;
      this.ctx.textAlign = 'center';
      this.ctx.textBaseline = 'top';
      const labelY = sy + 8 * this.scale;
      this.ctx.fillText(signal.name, sx, labelY);
    }

    const signalType = signal.signal_type || 'block';
    const hasBlockConfigured = signal.monitored_block_id;
    if (signalType === 'block' && !hasBlockConfigured) {
      const triSize = 5 * this.scale;
      const triX = sx + 6 * this.scale;
      const triY = sy - 6 * this.scale;

      this.ctx.fillStyle = '#ff9500';
      this.ctx.beginPath();
      this.ctx.moveTo(triX, triY - triSize);
      this.ctx.lineTo(triX - triSize * 0.866, triY + triSize * 0.5);
      this.ctx.lineTo(triX + triSize * 0.866, triY + triSize * 0.5);
      this.ctx.closePath();
      this.ctx.fill();

      this.ctx.fillStyle = '#000';
      this.ctx.font = `bold ${6 * this.scale}px sans-serif`;
      this.ctx.textAlign = 'center';
      this.ctx.textBaseline = 'middle';
      this.ctx.fillText('!', triX, triY);
    }
  }

  drawSignalBlockConnections(tracks: SignalTrack[]): void {
    const blockTracks: Record<
      string,
      Array<{ x: number; y: number; track_bits: number | undefined; screenX: number; screenY: number }>
    > = {};
    for (const track of tracks) {
      if (track.block_section_id) {
        if (!blockTracks[track.block_section_id]) {
          blockTracks[track.block_section_id] = [];
        }
        blockTracks[track.block_section_id].push({
          x: track.x,
          y: track.y,
          track_bits: track.track_bits,
          screenX: this.gridToScreenX(track.x, track.y),
          screenY: this.gridToScreenY(track.x, track.y),
        });
      }
    }

    const half = (this.gridSize * this.scale) / 2;

    for (const track of tracks) {
      if (!track.signals || track.signals.length === 0) continue;

      for (const signal of track.signals) {
        const monitoredBlockId = signal.monitored_block_id;
        if (!monitoredBlockId || !blockTracks[monitoredBlockId]) continue;

        const tileX = this.gridToScreenX(track.x, track.y);
        const tileY = this.gridToScreenY(track.x, track.y);
        if (!signal.detection_direction) {
          console.error(`[Renderer] Signal ${signal.signal_id} missing detection_direction in block line drawing!`, signal);
          continue;
        }
        const detectionDir = signal.detection_direction;
        const travelEdge = detectionDir.charAt(0).toUpperCase();
        const trackBits = track.track_bits || 0;
        const isLongPath = (trackBits === 1 || trackBits === 2);

        let signalOffsetX = 0, signalOffsetY = 0;
        if (isLongPath) {
          const edgePoints = this.renderer.trackHandler.getEdgePoints(half);
          const rightSide = ({ N: 'E', S: 'W', E: 'S', W: 'N' } as Record<string, string>)[travelEdge] || 'E';
          const sidePoint = edgePoints[rightSide] || { x: 0, y: 0 };
          signalOffsetX = sidePoint.x * 0.5;
          signalOffsetY = sidePoint.y * 0.5;
        }
        const signalX = tileX + signalOffsetX;
        const signalY = tileY + signalOffsetY;

        const blockTrackList = blockTracks[monitoredBlockId];
        let nearestDist = Infinity;
        let nearestEdgeX = null, nearestEdgeY = null;

        for (const bt of blockTrackList) {
          const edgePointsRelative = this.renderer.trackHandler.getEdgePoints(half);
          const usedEdges = trackBitsToEdges(bt.track_bits || 0);

          for (const edgeName of usedEdges) {
            const ep = edgePointsRelative[edgeName];
            if (!ep) continue;

            const absX = bt.screenX + ep.x;
            const absY = bt.screenY + ep.y;

            const dx = absX - signalX;
            const dy = absY - signalY;
            const dist = dx * dx + dy * dy;
            if (dist < nearestDist) {
              nearestDist = dist;
              nearestEdgeX = absX;
              nearestEdgeY = absY;
            }
          }
        }

        if (nearestEdgeX !== null && nearestEdgeY !== null) {
          this.ctx.save();
          this.ctx.strokeStyle = 'rgba(180, 100, 255, 0.6)';
          this.ctx.lineWidth = 2 * this.scale;
          this.ctx.setLineDash([4 * this.scale, 4 * this.scale]);
          this.ctx.beginPath();
          this.ctx.moveTo(signalX, signalY);
          this.ctx.lineTo(nearestEdgeX, nearestEdgeY);
          this.ctx.stroke();
          this.ctx.setLineDash([]);
          this.ctx.restore();
        }
      }
    }
  }
}
