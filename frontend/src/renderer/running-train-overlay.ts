import { getCanvasPalette, type CanvasPalette } from '../lib/canvas-palette';
import { getActiveSegments, getOppositeEdge, TRACK_WIDTH } from '../lib/constants';
import type { RunningTrain } from '../next/operate/live';
import { blockTerminalArrowSpec, compassFromDelta, forwardArrowEdges, type Renderer } from './renderer';
import type { NetworkPayload } from './payload-types';
import type { OverlayPhase, RendererOverlay } from './renderer-overlay';

type Tile = [number, number, string];
type Point = { x: number; y: number };
type Line = [Point, Point];
type Label = { x: number; y: number; w: number; h: number; tier: string };

function tileDirection(a: Tile, b: Tile): string | null {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  return Math.abs(dx) + Math.abs(dy) === 1 ? compassFromDelta(dx, dy) : null;
}

function blockLines(tiles: Tile[], renderer: Renderer, project: (x: number, y: number, edge: string) => Point): Line[] {
  return tiles.flatMap(([x, y, entry], i) => {
    const track = renderer.trackMap[`${x},${y}`];
    if (!track || track.is_switch) return [];
    const edges = forwardArrowEdges(
      track.track_bits ?? 0, entry,
      i > 0 ? tileDirection(tiles[i - 1], tiles[i]) : null,
      i + 1 < tiles.length ? tileDirection(tiles[i], tiles[i + 1]) : null,
    );
    return edges ? [[project(x, y, edges[0]), project(x, y, edges[1])]] : [];
  });
}

const STEP: Record<string, [number, number]> = { N: [0, 1], E: [1, 0], S: [0, -1], W: [-1, 0] };

function switchConnection(
  from: Tile[], to: Tile[], renderer: Renderer,
  project: (x: number, y: number, edge: string) => Point,
): Line[] {
  const ends = (tiles: Tile[]) => (['forward', 'reverse'] as const)
    .map((direction) => blockTerminalArrowSpec(tiles, renderer.trackMap, direction));
  const destinations = ends(to);
  const connections: Line[][] = [];
  for (const end of ends(from)) {
    if (!end) continue;
    const lines: Line[] = [];
    const visited = new Set<string>();
    let { x, y, exitEdge } = end;
    for (;;) {
      const [dx, dy] = STEP[exitEdge];
      x += dx;
      y += dy;
      const entry = getOppositeEdge(exitEdge);
      if (!entry) break;
      if (destinations.some((target) => target?.x === x && target.y === y && target.exitEdge === entry)) {
        connections.push(lines);
        break;
      }
      const key = `${x},${y}`;
      const track = renderer.trackMap[key];
      if (!track?.is_switch || visited.has(key)) break;
      visited.add(key);
      const routes = getActiveSegments(track.track_bits ?? 0).filter(([a, b]) => {
        const points = track.switch_points ?? {};
        const fork = [a, b].find((edge) => (points[edge]?.length ?? 0) > 1);
        if (!fork) return false;
        return renderer.trackHandler.selectedExitEdge(
          fork, points[fork], track.switch_state ?? 0, !!track.is_diagonal_only_switch,
        ) === (fork === a ? b : a);
      });
      const exits = routes.flatMap(([a, b]) => a === entry ? [b] : b === entry ? [a] : []);
      if (exits.length !== 1) break;
      exitEdge = exits[0];
      lines.push([project(x, y, entry), project(x, y, exitEdge)]);
    }
  }
  return connections.length === 1 ? connections[0] : [];
}

function distanceToLine(x: number, y: number, [a, b]: Line): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const lengthSquared = dx * dx + dy * dy;
  const t = lengthSquared ? Math.max(0, Math.min(1, ((x - a.x) * dx + (y - a.y) * dy) / lengthSquared)) : 0;
  return Math.hypot(x - a.x - t * dx, y - a.y - t * dy);
}

/** The line colour for a train's state (design_v2.md "Seeing trains on the
 *  layout"). Each layout theme sets the four colours. */
export function trainLineColor(train: RunningTrain, palette: CanvasPalette): string {
  if (train.lost || train.state === 'lost') return palette.trainLineLost;
  if (train.state === 'moving') return palette.trainLineMoving;
  if (train.state === 'waiting') return palette.trainLineWaiting;
  return palette.trainLineStopped;
}

export class RunningTrainOverlay implements RendererOverlay {
  private targets: { id: string; lines: Line[]; label: Label }[] = [];

  constructor(private read: () => { trains: RunningTrain[]; emphasized: string | null }) {}

  trainAt(x: number, y: number, onSwitch: boolean): string | undefined {
    const label = this.targets.find(({ label: b }) => x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h);
    if (label || onSwitch) return label?.id;
    let bestDistance = 6;
    let bestId: string | undefined;
    for (const { id, lines } of this.targets) {
      for (const line of lines) {
        const distance = distanceToLine(x, y, line);
        if (distance < bestDistance) {
          bestDistance = distance;
          bestId = id;
        }
      }
    }
    return bestId;
  }

  draw(phase: OverlayPhase, network: NetworkPayload, renderer: Renderer): void {
    if (phase !== 'afterBlockDirection') return;
    this.targets = [];
    const { trains, emphasized } = this.read();
    if (!trains.length) return;

    const edges = renderer.trackHandler.getEdgePoints(renderer.gridSize * renderer.scale / 2);
    const project = (x: number, y: number, edge: string): Point => ({
      x: renderer.gridToScreenX(x, y) + edges[edge].x,
      y: renderer.gridToScreenY(x, y) + edges[edge].y,
    });
    const palette = getCanvasPalette();
    const { ctx } = renderer;
    // Narrower than the block's colour band (TRACK_WIDTH * 1.2 and up), so
    // the band still shows what the block reads.
    const lineWidth = Math.max(3, TRACK_WIDTH * 0.55 * renderer.scale);
    const arrowLength = Math.max(14, TRACK_WIDTH * 2.6 * renderer.scale);
    const markSize = lineWidth * 0.4;
    ctx.save();
    ctx.font = '600 12px monospace';
    ctx.textBaseline = 'middle';
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    const drawings = trains.flatMap((train) => {
      const blocks = train.blocks.map(({ block_id }) =>
        (network.blocks?.[block_id] as { ordered_tiles?: Tile[] } | undefined)?.ordered_tiles ?? []);
      // Each block's lines, turned to run along the train's line of travel.
      const perBlock = blocks.map((tiles, i) => {
        const front = i === blocks.length - 1;
        const direction = front ? train.direction : train.blocks[i].direction ?? 'forward';
        const forward = blockLines(tiles, renderer, project);
        return direction === 'reverse' ? forward.map(([a, b]): Line => [b, a]).reverse() : forward;
      });
      const travel = perBlock.flat();
      const lines = [...travel];
      for (let i = 1; i < blocks.length; i++) {
        lines.push(...switchConnection(blocks[i - 1], blocks[i], renderer, project));
      }
      // The arrow sits on the middle tile of the front block.
      const frontLines = perBlock[perBlock.length - 1] ?? [];
      const middleLine = frontLines[Math.floor(frontLines.length / 2)];
      if (!middleLine) return [];
      const [start, end] = middleLine;
      const middle = { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 };
      const angle = Math.atan2(end.y - start.y, end.x - start.x);
      const tip = {
        x: middle.x + Math.cos(angle) * arrowLength / 2,
        y: middle.y + Math.sin(angle) * arrowLength / 2,
      };
      const text = train.lost ? `${train.train_id} ?` : train.train_id;
      const w = Math.ceil(ctx.measureText(text).width) + 16;
      const h = 20;
      const gap = arrowLength / 2 + 6;
      const candidates = [
        { x: middle.x + gap, y: middle.y - h - gap },
        { x: middle.x - w - gap, y: middle.y - h - gap },
        { x: middle.x + gap, y: middle.y + gap },
        { x: middle.x - w - gap, y: middle.y + gap },
      ];
      const position = candidates.find(({ x, y }) =>
        !renderer.labelBounds.some((b) => x < b.x + b.w && x + w > b.x && y < b.y + b.h && y + h > b.y),
      ) ?? candidates[0];
      const label = { ...position, w, h, tier: 'train' };
      renderer.labelBounds.push(label);
      this.targets.push({ id: train.train_id, lines, label });
      const hasArrow = !train.lost && (end.x !== start.x || end.y !== start.y);
      return [{ train, lines, travel, tip, angle, hasArrow, text, label, color: trainLineColor(train, palette) }];
    });

    for (const { train, lines, travel, tip, angle, hasArrow, color } of drawings) {
      const active = train.train_id === emphasized;
      const outline = active ? palette.accentBright : palette.bg;
      const width = active ? lineWidth * 1.35 : lineWidth;
      ctx.setLineDash(train.lost ? [width * 1.5, width * 1.5] : []);
      ctx.beginPath();
      for (const [a, b] of lines) {
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
      }
      ctx.lineWidth = width + (active ? 4 : 2);
      ctx.strokeStyle = outline;
      ctx.stroke();
      ctx.lineWidth = width;
      ctx.strokeStyle = color;
      ctx.stroke();
      ctx.setLineDash([]);

      // A small mark on each tile points along the line of travel while the
      // train is moving.
      if (train.state === 'moving' && markSize >= 1.5) {
        ctx.beginPath();
        for (const [a, b] of travel) {
          const along = Math.atan2(b.y - a.y, b.x - a.x);
          const cx = (a.x + b.x) / 2;
          const cy = (a.y + b.y) / 2;
          if (hasArrow && Math.hypot(cx - tip.x, cy - tip.y) < arrowLength) continue;
          for (const side of [-1, 1]) {
            ctx.moveTo(cx + Math.cos(along) * markSize, cy + Math.sin(along) * markSize);
            ctx.lineTo(
              cx - Math.cos(along) * markSize + Math.cos(along + Math.PI / 2) * markSize * side,
              cy - Math.sin(along) * markSize + Math.sin(along + Math.PI / 2) * markSize * side,
            );
          }
        }
        ctx.lineWidth = Math.max(1, lineWidth * 0.18);
        ctx.strokeStyle = palette.bg;
        ctx.stroke();
      }

      if (hasArrow) {
        const half = arrowLength * 0.5;
        ctx.save();
        ctx.translate(tip.x, tip.y);
        ctx.rotate(angle);
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.lineTo(-arrowLength, -half);
        ctx.lineTo(-arrowLength, half);
        ctx.closePath();
        ctx.fillStyle = color;
        ctx.fill();
        ctx.lineWidth = active ? 2.5 : 1.5;
        ctx.strokeStyle = outline;
        ctx.stroke();
        ctx.restore();
      }
    }

    for (const { train, text, color, label: { x, y, w, h } } of drawings) {
      const active = train.train_id === emphasized;
      ctx.beginPath();
      ctx.roundRect(x, y, w, h, h / 2);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.strokeStyle = active ? palette.accentBright : palette.bg;
      ctx.lineWidth = active ? 2.5 : 1.5;
      ctx.stroke();
      ctx.fillStyle = palette.bg;
      ctx.fillText(text, x + 8, y + h / 2);
    }
    ctx.restore();
  }
}
