/**
 * Live trains, blocks and switches for the Operate screen.
 *
 * Today's StateManager already holds every entity from the network poll and
 * tells its subscribers when one really changed. `liveVersion` goes up once
 * for each poll that changed something, so a component that reads it
 * re-draws then and only then, and reads the values themselves from
 * StateManager. No Operate component runs its own timer for this
 * (docs/ui-layout-rewrite-plan.md §10).
 */
import { signal } from '@preact/signals';

import { stateManager, type StateManager } from '../../lib/state-manager';

/** The StateManager maps the Operate screen shows. */
const WATCHED = [
  'trains',
  'controllers',
  'operatorStatus',
  'assignedRoutes',
  'routeRuns',
  'dispatcherProgress',
  'blocks',
  'switches',
  'stations',
];

/** Goes up after each poll that changed a watched entity. */
export const liveVersion = signal(0);

/** The simulation clock in seconds, for the cab display. */
export const simClock = signal(0);

/** Start following StateManager. Returns a function that stops. */
export function watchLiveState(sm: StateManager = stateManager): () => void {
  // One poll changes many entities, and each change calls back on its own;
  // bump once per batch so the screen re-draws once.
  let queued = false;
  const bump = () => {
    if (queued) return;
    queued = true;
    queueMicrotask(() => {
      queued = false;
      liveVersion.value++;
    });
  };
  const stops = WATCHED.map((type) => sm.subscribe(type, '*', bump, { immediate: false }));
  stops.push(sm.subscribeSimClock((t) => (simClock.value = t)));
  simClock.value = sm.simulationTime;
  bump();
  return () => stops.forEach((stop) => stop());
}

/** Everything the train card and the train panel read about one train.
 *  Train runtime state comes in sibling maps owned by different parts of
 *  the server (design_new.md "Data Flow To UI"), gathered here. */
export interface TrainView {
  id: string;
  /** The persistent train record. */
  train: any;
  /** The TrainController payload; undefined before the first snapshot. */
  controller: any;
  /** `{ato_status, pending_actions, hold_reason}`. */
  operatorStatus: any;
  assignedRouteId: string | null;
  /** The live route run `{route_id, run_number, …}`, or null. */
  routeRun: any;
  dispatcher: any;
}

export function trainView(id: string, sm: StateManager = stateManager): TrainView | null {
  const train = sm.get('trains', id);
  if (!train) return null;
  return {
    id,
    train,
    controller: sm.getController(id),
    operatorStatus: sm.getOperatorStatus(id),
    assignedRouteId: (sm.getAssignedRouteId(id) as string | null | undefined) ?? null,
    routeRun: sm.getRouteRun(id) ?? null,
    dispatcher: sm.getDispatcherProgress(id) ?? null,
  };
}

/** Every train, ordered by id so the cards keep their places. */
export function trainViews(sm: StateManager = stateManager): TrainView[] {
  return sm
    .getAll('trains')
    .map((t: any) => trainView(String(t.id), sm))
    .filter((v): v is TrainView => v !== null)
    .sort((a, b) => a.id.localeCompare(b.id));
}

/** The trains whose span covers tile (x, y): what a click on that tile hits. */
export function trainsOnTile(x: number, y: number, sm: StateManager = stateManager): string[] {
  return sm
    .getAll('trains')
    .map((t: any) => String(t.id))
    .filter((id) => {
      const tiles = sm.getController(id)?.span?.occupied_tiles;
      return Array.isArray(tiles) && tiles.some((tile: any) => tile[0] === x && tile[1] === y);
    });
}
