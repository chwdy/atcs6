/**
 * The row of v2 train cards along the bottom of the Operate screen
 * (design_v2.md "Running a train").
 *
 * One card per train the runtime holds: its name, whether it is stopped,
 * moving, waiting or lost, the blocks it is on, a speed knob from 0 to 12 V,
 * Reverse and Stop. A lost or stopped train can be removed. The row ends with
 * Stop all. A train appears here by being put on the track; nothing is
 * created by hand.
 */
import { useState } from 'preact/hooks';

import { api } from '../../lib/api';
import { showToast } from '../../lib/toast';
import { blockName, liveVersion, runningTrains, type RunningTrain } from './live';

export const MAX_KNOB_VOLTS = 12;

/** What a refusal reason means, in the operator's words. */
const REASONS: Record<string, string> = {
  not_live: 'The session is not Live.',
  train_lost: 'This train is lost. Remove it, or put it back on its block.',
  out_of_range: 'The knob goes from 0 to 12 V.',
  not_stopped: 'Stop the train first.',
  still_claiming: 'The train may still be rolling. Try again in a few seconds.',
  unknown_train: 'This train is no longer on the track.',
};

const WAITING: Record<string, string> = {
  block_taken: 'the next block is taken',
  switch_against: 'a switch is set against it',
  switch_held: 'a switch on its way is held',
  own_block: 'the next block is its own',
};

async function send(label: string, request: Promise<{ success: boolean; reason: string | null }>): Promise<void> {
  try {
    const reply = await request;
    if (!reply.success) {
      showToast(`${label}: ${REASONS[reply.reason ?? ''] ?? reply.reason ?? 'refused'}`, 'warning', 5000);
    }
  } catch (err: any) {
    showToast(`${label}: ${err?.message ?? err}`, 'error', 5000);
  }
  await window.app?.refreshNetwork();
}

export function stateChip(state: RunningTrain['state']): string {
  return { moving: 'chip-success', waiting: 'chip-info', lost: 'chip-danger', stopped: 'chip-muted' }[state];
}

export function cardTone(state: RunningTrain['state']): string {
  return { moving: 'ok', waiting: 'info', lost: 'danger', stopped: '' }[state];
}

/** The line under the name: where the train is and what it is doing. */
export function cardLine(train: RunningTrain): string {
  const where = train.blocks.map((b) => blockName(b.block_id)).join(' → ');
  if (train.state === 'lost') return `Lost on ${where}`;
  if (train.state === 'waiting') {
    return `${where} · waiting: ${WAITING[train.waiting_reason ?? ''] ?? train.waiting_reason ?? ''}`;
  }
  const ahead = train.claimed_block_id ?? train.next_block_id;
  return ahead ? `${where} → ${blockName(ahead)}` : `${where} · track ends`;
}

export function RunningTrainsRow() {
  liveVersion.value;
  const trains = runningTrains();
  return (
    <div class="op-cards" role="region" aria-label="Trains">
      {trains.length === 0 && (
        <div class="empty-state op-cards-empty">No trains yet. Put one on the track and it appears here.</div>
      )}
      {trains.map((train) => (
        <RunningTrainCard key={train.train_id} train={train} />
      ))}
      <div class="op-cards-actions">
        <button
          type="button"
          class="tc-btn tc-btn-danger"
          title="Set every train's knob to 0 V and take the voltage off every block"
          onClick={() => void send('Stop all', api.stopAllTrains())}
        >
          ■ Stop all
        </button>
      </div>
    </div>
  );
}

export function RunningTrainCard({ train }: { train: RunningTrain }) {
  const id = train.train_id;
  const [drag, setDrag] = useState<number | null>(null);
  const volts = drag ?? train.knob_volts;
  const lost = train.state === 'lost';
  const line = cardLine(train);
  return (
    <div class="op-card rt-card" data-tone={cardTone(train.state)} aria-label={`Train ${train.name}`}>
      <div class="op-card-head">
        <span class="op-card-name" title={id}>
          {train.name}
        </span>
        <span class={`chip chip-state ${stateChip(train.state)}`}>{train.state.toUpperCase()}</span>
      </div>
      <div class="rt-card-line" title={line}>
        {line}
      </div>
      <div class="op-card-handle">
        <input
          type="range"
          class="tc-power-slider op-card-handle-slider"
          min={0}
          max={MAX_KNOB_VOLTS}
          step={0.5}
          value={volts}
          disabled={lost}
          aria-label="Speed knob"
          aria-valuetext={`${volts} volts`}
          onInput={(e) => {
            const value = Number(e.currentTarget.value);
            setDrag(value);
            // The knob acts while it is turned, as a power brick does.
            void api.setTrainKnob(id, value);
          }}
          onChange={async (e) => {
            try {
              await send('Knob', api.setTrainKnob(id, Number(e.currentTarget.value)));
            } finally {
              setDrag(null);
            }
          }}
        />
        <span class="op-card-handle-pos rt-card-volts">{volts.toFixed(1)} V</span>
      </div>
      <div class="rt-card-buttons">
        <button
          type="button"
          class="tc-btn op-card-btn"
          disabled={lost || train.state !== 'stopped'}
          title="Turn the train round. It must be stopped."
          onClick={() => void send('Reverse', api.reverseRunningTrain(id))}
        >
          ⇄ Reverse
        </button>
        <button
          type="button"
          class="tc-btn tc-btn-danger op-card-btn"
          disabled={train.state === 'stopped' || lost}
          onClick={() => void send('Stop', api.stopRunningTrain(id))}
        >
          ■ Stop
        </button>
        {(lost || train.state === 'stopped') && (
          <button
            type="button"
            class="tc-btn op-card-btn rt-card-remove"
            title="Remove this train from the page. If it is still on the track it appears again as a new train."
            aria-label={`Remove ${train.name}`}
            onClick={() => void send('Remove', api.removeRunningTrain(id))}
          >
            ✕
          </button>
        )}
      </div>
    </div>
  );
}
