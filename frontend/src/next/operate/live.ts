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
  'runningTrains',
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

/** One v2 train entry from the snapshot (design_v2.md "Live fields", A train
 *  entry). */
export interface RunningTrain {
  train_id: string;
  name: string;
  direction: 'forward' | 'reverse';
  knob_volts: number;
  lost: boolean;
  blocks: { block_id: string; reads_occupied: boolean }[];
  state: 'stopped' | 'moving' | 'waiting' | 'lost';
  waiting_reason: string | null;
  next_block_id: string | null;
  claimed_block_id: string | null;
}

/** Whether this session runs v2 trains: a Virtual Hardware or Real Hardware
 *  environment. The v1 Simulation environment keeps the v1 train cards. */
export function usesRunningTrains(sm: StateManager = stateManager): boolean {
  return sm.operationMode !== null && sm.operationMode !== 'simulation';
}

/** Every v2 train, oldest first, so the cards keep their places. */
export function runningTrains(sm: StateManager = stateManager): RunningTrain[] {
  const number = (id: string) => Number(id.replace(/\D/g, '')) || 0;
  return (sm.getAll('runningTrains') as unknown as RunningTrain[])
    .slice()
    .sort((a, b) => number(a.train_id) - number(b.train_id));
}

/** A block's name for a card line, or its id when it has none. */
export function blockName(blockId: string | null, sm: StateManager = stateManager): string {
  if (!blockId) return '';
  return (sm.get('blocks', blockId) as any)?.name || blockId;
}
