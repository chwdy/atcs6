/**
 * The starter loop behind the first-run card's "Start from a loop"
 * (docs/ui-layout-rewrite-plan.md §9).
 *
 * It builds an oval with the same calls the editor and the Operate screen
 * make, so it needs no server support of its own: lay the track, make one
 * segment per side (the server gives each segment its own block), give each
 * segment a length, place one train, and give that train a circular route
 * of waypoints around the four blocks. The user then presses Start route and
 * Play, and the train runs round the loop.
 *
 * Saved layouts carry no routes, which is why the loop is built here rather
 * than shipped as a layout file.
 */

/** One track tile, as `POST /api/tracks/bulk` takes it. */
export interface LoopTile {
  x: number;
  y: number;
  track_bits: number;
}

/** One side of the loop: its tiles in travel order, and its length. */
export interface LoopSide {
  name: string;
  tiles: LoopTile[];
  length_cm: number;
}

// Track bits name the two tile edges a piece joins (see the Tile Editor).
const NS = 1;
const EW = 2;
const NE = 4;
const NW = 8;
const SE = 16;
const SW = 32;

/** The loop is 14 tiles wide and 8 tall. The sides' lengths are what a small
 *  tabletop oval measures; a model layout's lengths are always entered by
 *  hand, so the loop has to set them. */
export const LOOP_WIDTH = 14;
export const LOOP_HEIGHT = 8;
const SHORT_SIDE_CM = 100;
const LONG_SIDE_CM = 200;

/**
 * The four sides in clockwise travel order, starting with the left side
 * going up. North is y + 1 on this grid, so row 0 is the bottom. Each side
 * starts with the corner tile that turns into it, so every tile belongs to
 * exactly one side.
 */
export function starterLoopSides(): LoopSide[] {
  const w = LOOP_WIDTH;
  const h = LOOP_HEIGHT;
  const left: LoopTile[] = [{ x: 0, y: 0, track_bits: NE }];
  for (let y = 1; y < h - 1; y++) left.push({ x: 0, y, track_bits: NS });
  const top: LoopTile[] = [{ x: 0, y: h - 1, track_bits: SE }];
  for (let x = 1; x < w - 1; x++) top.push({ x, y: h - 1, track_bits: EW });
  const right: LoopTile[] = [{ x: w - 1, y: h - 1, track_bits: SW }];
  for (let y = h - 2; y > 0; y--) right.push({ x: w - 1, y, track_bits: NS });
  const bottom: LoopTile[] = [{ x: w - 1, y: 0, track_bits: NW }];
  for (let x = w - 2; x > 0; x--) bottom.push({ x, y: 0, track_bits: EW });
  return [
    { name: 'Loop left', tiles: left, length_cm: SHORT_SIDE_CM },
    { name: 'Loop top', tiles: top, length_cm: LONG_SIDE_CM },
    { name: 'Loop right', tiles: right, length_cm: SHORT_SIDE_CM },
    { name: 'Loop bottom', tiles: bottom, length_cm: LONG_SIDE_CM },
  ];
}

/** The train starts in the middle of the top side, facing east, so it runs
 *  clockwise: top, right, bottom, left. */
export const TRAIN_START = { x: Math.floor(LOOP_WIDTH / 2), y: LOOP_HEIGHT - 1, direction: 'EAST' } as const;

/** The route's waypoints, in the order the train meets them from its start. */
export const ROUTE_SIDE_ORDER = ['Loop right', 'Loop bottom', 'Loop left', 'Loop top'];

/** The calls the build makes, so a test can stand in for the server. */
export interface StarterLoopApi {
  addTracksBulk(tracks: LoopTile[]): Promise<any>;
  createSegment(segmentId: string | null, positions: Array<[number, number]>, name?: string): Promise<any>;
  updateSegment(segmentId: string, data: { length_cm: number }): Promise<any>;
  createTrain(train: unknown): Promise<any>;
  createRoute(route: unknown): Promise<any>;
  assignRoute(trainId: string, routeId: string): Promise<any>;
}

export interface StarterLoopResult {
  trainId: string;
  routeId: string;
}

/** A train id not already taken: LOOP_1, then LOOP_2, and so on. */
export function freeTrainId(taken: Iterable<string>): string {
  const used = new Set(taken);
  let n = 1;
  while (used.has(`LOOP_${n}`)) n++;
  return `LOOP_${n}`;
}

/**
 * Build the loop on the current (empty) layout. Each step names itself in
 * the error it throws, so a failure says how far the build got.
 */
export async function buildStarterLoop(
  api: StarterLoopApi,
  existingTrainIds: Iterable<string> = []
): Promise<StarterLoopResult> {
  const sides = starterLoopSides();
  const step = async <T>(what: string, run: () => Promise<T>): Promise<T> => {
    try {
      return await run();
    } catch (e: any) {
      throw new Error(`Could not ${what}: ${e?.message ?? e}`);
    }
  };

  await step('lay the track', () => api.addTracksBulk(sides.flatMap((s) => s.tiles)));

  const blockBySide = new Map<string, string>();
  for (const side of sides) {
    const created = await step(`make the segment "${side.name}"`, () =>
      api.createSegment(null, side.tiles.map((t) => [t.x, t.y] as [number, number]), side.name)
    );
    const segmentId: string | undefined = created?.segment_id;
    const blockId: string | undefined = created?.block_id;
    if (!segmentId || !blockId) throw new Error(`Could not make the segment "${side.name}": the server sent no segment or block id`);
    await step(`set the length of "${side.name}"`, () => api.updateSegment(segmentId, { length_cm: side.length_cm }));
    blockBySide.set(side.name, blockId);
  }

  const trainId = freeTrainId(existingTrainIds);
  await step('place the train', () =>
    api.createTrain({
      id: trainId,
      position: { x: TRAIN_START.x, y: TRAIN_START.y },
      direction: TRAIN_START.direction,
      max_speed: 60,
      length_cm: 30,
    })
  );

  const route = await step('make the route', () =>
    api.createRoute({
      name: 'Around the loop',
      mode: 'circular',
      tasks: ROUTE_SIDE_ORDER.map((name) => ({
        task_kind: 'waypoint',
        task_location: { block_id: blockBySide.get(name) },
      })),
    })
  );
  const routeId: string | undefined = route?.route_id ?? route?.route?.route_id;
  if (!routeId) throw new Error('Could not make the route: the server sent no route id');

  await step('assign the route', () => api.assignRoute(trainId, routeId));
  return { trainId, routeId };
}
