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

/** Split a block's tile lines into continuous runs (a tunnel mouth breaks
 *  one), each as a list of points along the line of travel. */
function runs(lines: Line[]): Point[][] {
  const out: Point[][] = [];
  for (const [a, b] of lines) {
    const run = out[out.length - 1];
    const last = run?.[run.length - 1];
    if (last && Math.hypot(last.x - a.x, last.y - a.y) < 0.5) run.push(b);
    else out.push([a, b]);
  }
  return out;
}

/** The run with `inset` taken off the chosen ends, or null when it is too
 *  short. */
function setBack(points: Point[], inset: number, start = true, end = true): Point[] | null {
  const trim = (pts: Point[]): Point[] | null => {
    let left = inset;
    for (let i = 0; i + 1 < pts.length; i++) {
      const [a, b] = [pts[i], pts[i + 1]];
      const length = Math.hypot(b.x - a.x, b.y - a.y);
      if (length > left) {
        const t = left / length;
        return [{ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t }, ...pts.slice(i + 1)];
      }
      left -= length;
    }
    return null;
  };
  const front = start ? trim(points) : points;
  const both = front && (end ? trim(front.slice().reverse()) : front.slice().reverse());
  return both ? both.reverse() : null;
}

/** The points of `points` up to `length` along it. */
function cutAt(points: Point[], length: number): Point[] {
  const out = [points[0]];
  let left = length;
  for (let i = 0; i + 1 < points.length; i++) {
    const [a, b] = [points[i], points[i + 1]];
    const step = Math.hypot(b.x - a.x, b.y - a.y);
    if (step >= left) {
      const t = step ? left / step : 0;
      out.push({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });
      return out;
    }
    out.push(b);
    left -= step;
  }
  return out;
}

function polylineLength(points: Point[]): number {
  let total = 0;
  for (let i = 0; i + 1 < points.length; i++) {
    total += Math.hypot(points[i + 1].x - points[i].x, points[i + 1].y - points[i].y);
  }
  return total;
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
    // A quarter of the block's colour band, so the band still shows what the
    // block reads. Not drawn once the layout is zoomed out so far that it
    // would fill the track.
    const lineWidth = TRACK_WIDTH * 0.3 * renderer.scale;
    const showLine = lineWidth >= 1.2;
    // The widest band (an axial tile's), for the highlight drawn outside it.
    const bandWidth = TRACK_WIDTH * Math.SQRT2 * renderer.scale;
    ctx.save();
    ctx.font = '600 12px monospace';
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'center';
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
      // One line from the rear of the train to its front: each block's
      // tiles, joined across the switch between two blocks. It breaks only
      // where the track does (a tunnel mouth, or no single connection).
      const lines: Line[] = [];
      perBlock.forEach((blockLinesOf, i) => {
        if (i > 0) lines.push(...switchConnection(blocks[i - 1], blocks[i], renderer, project));
        lines.push(...blockLinesOf);
      });
      const pieces = runs(lines);
      // The line ahead into a claimed block: across the switch, then to the
      // middle of the claimed block.
      let claim: Point[] | null = null;
      const head = pieces[pieces.length - 1]?.[pieces[pieces.length - 1].length - 1];
      const claimTiles = !train.lost && train.claimed_block_id
        ? (network.blocks?.[train.claimed_block_id] as { ordered_tiles?: Tile[] } | undefined)?.ordered_tiles
        : undefined;
      if (head && claimTiles?.length) {
        const across = switchConnection(blocks[blocks.length - 1], claimTiles, renderer, project);
        const from = across.length ? across[across.length - 1][1] : head;
        let inside = blockLines(claimTiles, renderer, project);
        const near = (point: Point) => Math.hypot(point.x - from.x, point.y - from.y);
        if (inside.length && near(inside[inside.length - 1][1]) < near(inside[0][0])) {
          inside = inside.map(([a, b]): Line => [b, a]).reverse();
        }
        const ahead = runs([...across, ...inside]);
        if (ahead.length === 1 && near(inside[0]?.[0] ?? from) < 0.5 && Math.hypot(ahead[0][0].x - head.x, ahead[0][0].y - head.y) < 0.5) {
          const insideLength = polylineLength(runs(inside)[0] ?? []);
          claim = cutAt(ahead[0], polylineLength(ahead[0]) - insideLength / 2);
        }
      }
      // The tag sits on the middle tile of the front block.
      const frontLines = perBlock[perBlock.length - 1] ?? [];
      const middleLine = frontLines[Math.floor(frontLines.length / 2)];
      if (!middleLine) return [];
      const [start, end] = middleLine;
      const centre = { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 };
      const angle = Math.atan2(end.y - start.y, end.x - start.x);
      const text = train.lost ? `${train.train_id} ?` : train.train_id;
      const w = Math.ceil(ctx.measureText(text).width) + 14;
      const h = 18;
      const label = { x: centre.x - w / 2, y: centre.y - h / 2, w, h, tier: 'train' };
      renderer.labelBounds.push(label);
      this.targets.push({ id: train.train_id, lines, label });
      const pointed = !train.lost && (end.x !== start.x || end.y !== start.y);
      return [{ train, lines, pieces, claim, centre, angle, pointed, text, label, color: trainLineColor(train, palette) }];
    });

    const strokeLines = (lines: Line[]) => {
      ctx.beginPath();
      for (const [a, b] of lines) {
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
      }
      ctx.stroke();
    };

    // The highlight: two thin lines outside the edges of the train's blocks.
    // Cut out of the live layer, so the block's band underneath shows through.
    for (const { train, lines } of drawings) {
      if (train.train_id !== emphasized) continue;
      const inner = bandWidth + 4;
      ctx.strokeStyle = palette.trainHighlight;
      ctx.lineWidth = inner + 4;
      strokeLines(lines);
      ctx.save();
      ctx.globalCompositeOperation = 'destination-out';
      ctx.lineWidth = inner;
      strokeLines(lines);
      ctx.restore();
    }

    // The line stops short of the train's rear and finishes there with a bar
    // across the track. At the front of its front block it ends in an open
    // arrowhead. A claimed block ahead carries the line on to the middle of
    // that block, which ends in a second open arrowhead.
    if (showLine) {
      const inset = TRACK_WIDTH * 0.7 * renderer.scale;
      // The rear bar and both arrowheads reach past the edges of the block's
      // colour band (at most TRACK_WIDTH * SQRT2 wide), so they read as the
      // train's ends rather than as part of the band.
      const bar = TRACK_WIDTH * 1.25 * renderer.scale;
      const spread = 0.75;
      const arm = (TRACK_WIDTH * 1.25 * renderer.scale) / Math.sin(spread);
      const stroke = (points: Point[]) => {
        ctx.beginPath();
        ctx.moveTo(points[0].x, points[0].y);
        for (const point of points.slice(1)) ctx.lineTo(point.x, point.y);
        ctx.stroke();
      };
      const arrow = (tip: Point, before: Point) => {
        const along = Math.atan2(tip.y - before.y, tip.x - before.x);
        ctx.beginPath();
        ctx.moveTo(tip.x - Math.cos(along - spread) * arm, tip.y - Math.sin(along - spread) * arm);
        ctx.lineTo(tip.x, tip.y);
        ctx.lineTo(tip.x - Math.cos(along + spread) * arm, tip.y - Math.sin(along + spread) * arm);
        ctx.stroke();
      };
      ctx.save();
      ctx.lineCap = 'butt';
      ctx.lineJoin = 'miter';
      for (const { train, pieces, claim, color } of drawings) {
        ctx.lineWidth = lineWidth;
        ctx.strokeStyle = color;
        let rear: [Point, Point] | null = null;
        let front: [Point, Point] | null = null;
        for (let i = 0; i < pieces.length; i++) {
          const first = i === 0;
          const last = i === pieces.length - 1;
          // The front is set back only when no claimed line carries on.
          const points = setBack(pieces[i], inset, first, last && !claim);
          if (!points) continue;
          ctx.setLineDash(train.lost ? [lineWidth * 3, lineWidth * 3] : []);
          stroke(points);
          if (first) rear = [points[0], points[1]];
          if (last) front = [points[points.length - 1], points[points.length - 2]];
        }
        ctx.setLineDash([]);
        if (rear) {
          const [end, next] = rear;
          const along = Math.atan2(next.y - end.y, next.x - end.x);
          const dx = -Math.sin(along) * bar;
          const dy = Math.cos(along) * bar;
          ctx.beginPath();
          ctx.moveTo(end.x - dx, end.y - dy);
          ctx.lineTo(end.x + dx, end.y + dy);
          ctx.stroke();
        }
        if (front && !train.lost) arrow(front[0], front[1]);
        if (claim && claim.length > 1) {
          stroke(claim);
          arrow(claim[claim.length - 1], claim[claim.length - 2]);
        }
      }
      ctx.restore();
    }

    for (const { train, centre, angle, pointed, text, label, color } of drawings) {
      const active = train.train_id === emphasized;
      const tagPath = () => {
        // One outline: the tag's box, with its end (or corner, on a diagonal)
        // in the direction of travel drawn out to a point. The text stays
        // level.
        const x0 = label.x;
        const y0 = label.y;
        const x1 = label.x + label.w;
        const y1 = label.y + label.h;
        const cx = centre.x;
        const cy = centre.y;
        const point = 9;
        const cut = label.h / 2;
        const lean = point * 0.75;
        type Vertex = { x: number; y: number; r: number };
        const corner = (x: number, y: number): Vertex => ({ x, y, r: 4 });
        const shoulder = (x: number, y: number): Vertex => ({ x, y, r: 1 });
        const apex = (x: number, y: number): Vertex => ({ x, y, r: 0 });
        // Clockwise from the top left (screen y grows downward).
        let vertices: Vertex[] = [corner(x0, y0), corner(x1, y0), corner(x1, y1), corner(x0, y1)];
        if (pointed) {
          // Eight ways: 0 east, 1 south-east, 2 south ... 7 north-east.
          const way = ((Math.round(angle / (Math.PI / 4)) % 8) + 8) % 8;
          vertices = {
            0: [corner(x0, y0), shoulder(x1, y0), apex(x1 + point, cy), shoulder(x1, y1), corner(x0, y1)],
            1: [corner(x0, y0), corner(x1, y0), shoulder(x1, y1 - cut), apex(x1 + lean, y1 + lean), shoulder(x1 - cut, y1), corner(x0, y1)],
            2: [corner(x0, y0), corner(x1, y0), shoulder(x1, y1), apex(cx, y1 + point), shoulder(x0, y1)],
            3: [corner(x0, y0), corner(x1, y0), corner(x1, y1), shoulder(x0 + cut, y1), apex(x0 - lean, y1 + lean), shoulder(x0, y1 - cut)],
            4: [shoulder(x0, y0), corner(x1, y0), corner(x1, y1), shoulder(x0, y1), apex(x0 - point, cy)],
            5: [apex(x0 - lean, y0 - lean), shoulder(x0 + cut, y0), corner(x1, y0), corner(x1, y1), corner(x0, y1), shoulder(x0, y0 + cut)],
            6: [shoulder(x0, y0), apex(cx, y0 - point), shoulder(x1, y0), corner(x1, y1), corner(x0, y1)],
            7: [corner(x0, y0), shoulder(x1 - cut, y0), apex(x1 + lean, y0 - lean), shoulder(x1, y0 + cut), corner(x1, y1), corner(x0, y1)],
          }[way] as Vertex[];
        }
        ctx.beginPath();
        const last = vertices[vertices.length - 1];
        ctx.moveTo((last.x + vertices[0].x) / 2, (last.y + vertices[0].y) / 2);
        vertices.forEach((v, k) => {
          const next = vertices[(k + 1) % vertices.length];
          ctx.arcTo(v.x, v.y, next.x, next.y, v.r);
        });
        ctx.closePath();
      };
      // Stroke at double width, then fill: the fill hides the inner half of
      // the stroke, so the edge sits outside the fill.
      const edge = active ? 2 : 1.5;
      tagPath();
      ctx.setLineDash(train.lost ? [3, 2] : []);
      ctx.lineWidth = edge * 2;
      ctx.strokeStyle = active ? palette.trainHighlight : palette.trainTagInk;
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.fillStyle = palette.trainTagInk;
      ctx.fillText(text, centre.x, centre.y + 0.5);
    }
    ctx.restore();
  }
}
