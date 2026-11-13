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
const WATCHED = ['runningTrains', 'blocks', 'switches'];

/** Goes up after each poll that changed a watched entity. */
export const liveVersion = signal(0);

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
  bump();
  return () => stops.forEach((stop) => stop());
}

/** One v2 train entry from the snapshot (design_v2.md "Live fields", A train
 *  entry). */
export interface RunningTrain {
  train_id: string;
  name: string;
  direction: 'forward' | 'reverse';
  knob_volts: number;
  lost: boolean;
  blocks: { block_id: string; reads_occupied: boolean; direction?: 'forward' | 'reverse' }[];
  state: 'stopped' | 'moving' | 'waiting' | 'lost';
  waiting_reason: string | null;
  next_block_id: string | null;
  claimed_block_id: string | null;
}

/** Whether this session runs v2 trains: true once the first snapshot names
 *  its environment. */
export function usesRunningTrains(sm: StateManager = stateManager): boolean {
  return sm.operationMode !== null;
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
