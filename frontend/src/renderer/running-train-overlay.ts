import { getCanvasPalette } from '../lib/canvas-palette';
import { getActiveSegments, getOppositeEdge } from '../lib/constants';
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
    ctx.save();
    ctx.font = '600 12px monospace';
    ctx.textBaseline = 'middle';
    ctx.lineCap = 'round';
    const drawings = trains.flatMap((train) => {
      const blocks = train.blocks.map(({ block_id }) =>
        (network.blocks?.[block_id] as { ordered_tiles?: Tile[] } | undefined)?.ordered_tiles ?? []);
      const lines = blocks.flatMap((tiles) => blockLines(tiles, renderer, project));
      for (let i = 1; i < blocks.length; i++) {
        lines.push(...switchConnection(blocks[i - 1], blocks[i], renderer, project));
      }
      const front = blocks[blocks.length - 1] ?? [];
      const terminal = blockTerminalArrowSpec(front, renderer.trackMap, train.direction);
      if (!terminal) return [];
      const tip = project(terminal.x, terminal.y, terminal.exitEdge);
      const start = project(terminal.x, terminal.y, terminal.entryEdge);
      const text = train.lost ? `${train.train_id} ?` : train.train_id;
      const w = Math.ceil(ctx.measureText(text).width) + 12;
      const h = 20;
      const candidates = [
        { x: tip.x + 9, y: tip.y - h - 8 },
        { x: tip.x - w - 9, y: tip.y - h - 8 },
        { x: tip.x + 9, y: tip.y + 8 },
        { x: tip.x - w - 9, y: tip.y + 8 },
      ];
      const position = candidates.find(({ x, y }) =>
        !renderer.labelBounds.some((b) => x < b.x + b.w && x + w > b.x && y < b.y + b.h && y + h > b.y),
      ) ?? candidates[0];
      const label = { ...position, w, h, tier: 'train' };
      renderer.labelBounds.push(label);
      this.targets.push({ id: train.train_id, lines, label });
      return [{ train, lines, tip, start, text, label }];
    });

    for (const { train, lines, tip, start } of drawings) {
      const active = train.train_id === emphasized;
      const color = active ? palette.accentBright : palette.trainBody;
      ctx.setLineDash(train.lost ? [5, 5] : []);
      ctx.beginPath();
      for (const [a, b] of lines) {
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
      }
      const width = active ? 4 : 2.5;
      ctx.lineWidth = width + 2;
      ctx.strokeStyle = palette.bg;
      ctx.stroke();
      ctx.lineWidth = width;
      ctx.strokeStyle = color;
      ctx.stroke();
      ctx.setLineDash([]);

      const angle = Math.atan2(tip.y - start.y, tip.x - start.x);
      if (!train.lost && (tip.x !== start.x || tip.y !== start.y)) {
        ctx.save();
        ctx.translate(tip.x, tip.y);
        ctx.rotate(angle);
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.lineTo(-10, -5);
        ctx.lineTo(-10, 5);
        ctx.closePath();
        ctx.fillStyle = color;
        ctx.fill();
        ctx.lineWidth = 1;
        ctx.strokeStyle = palette.bg;
        ctx.stroke();
        ctx.restore();
      }
    }

    for (const { train, text, label: { x, y, w, h } } of drawings) {
      const active = train.train_id === emphasized;
      ctx.fillStyle = palette.overlayBg;
      ctx.fillRect(x, y, w, h);
      ctx.strokeStyle = active ? palette.accentBright : palette.overlayBorder;
      ctx.lineWidth = active ? 2 : 1;
      ctx.strokeRect(x, y, w, h);
      ctx.fillStyle = palette.text;
      ctx.fillText(text, x + 6, y + h / 2);
    }
    ctx.restore();
  }
}
