/**
 * The row of train cards along the bottom of the Operate screen
 * (docs/ui-layout-rewrite-plan.md §5).
 *
 * One card per train: its name, its speed against the ATC limit,
 * `train_status` and `ato_status`, and the one-line hold reason while it
 * stands. Each card carries the train's main control (see `cardControl`).
 * Clicking a card selects the train, the same as picking it on the map, and
 * the selected train's card is marked. The row ends with + Place train and
 * Hold all.
 */
import { useState } from 'preact/hooks';

import type { JSX } from 'preact';

import { liveVersion, trainViews, type TrainView } from './live';
import { selection, type OperateHost } from './operate-state';
import { HANDLE_ORDER, handleClass, trainCommands } from './train-commands';
import {
  atoStatus,
  atoStatusChip,
  cardControl,
  holdLine,
  speedText,
  trainCardTone,
  trainStatus,
  trainStatusChip,
} from './train-display';

export function TrainCardsRow({ host }: { host: OperateHost }) {
  liveVersion.value;
  const trains = trainViews();
  const sel = selection.value;
  return (
    <div class="op-cards" role="region" aria-label="Trains">
      {trains.length === 0 && <div class="empty-state op-cards-empty">No trains yet. Place one on the map.</div>}
      {trains.map((view) => (
        <TrainCard
          key={view.id}
          view={view}
          selected={sel?.kind === 'train' && sel.id === view.id}
          onSelect={() => host.selectTrain(view.id)}
        />
      ))}
      <div class="op-cards-actions">
        <button
          type="button"
          class="btn"
          title="Place a new train on the map"
          onClick={() => host.editor.showAddTrainDialog()}
        >
          <i class="ti ti-plus" aria-hidden="true" /> Place train
        </button>
        <button
          type="button"
          class="btn"
          title="ATO stop on every ATO train; each one resumes with Go"
          onClick={() => void host.holdAll()}
        >
          <i class="ti ti-player-stop" aria-hidden="true" /> Hold all
        </button>
      </div>
    </div>
  );
}

export function TrainCard({
  view,
  selected,
  onSelect,
}: {
  view: TrainView;
  selected: boolean;
  onSelect: () => void;
}) {
  const c = view.controller;
  const status = trainStatus(view);
  const ato = atoStatus(view);
  const hold = holdLine(view);
  const onKeyDown = (e: KeyboardEvent) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onSelect();
    }
  };
  return (
    <div
      class={selected ? 'op-card row-selected' : 'op-card'}
      data-tone={trainCardTone(view)}
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      aria-label={`Train ${view.id}`}
      onClick={onSelect}
      onKeyDown={onKeyDown}
    >
      <div class="op-card-head">
        <span class="op-card-name" title={view.id}>
          {view.train?.name || view.id}
        </span>
        <span class={`chip chip-state ${trainStatusChip(status)}`} title="train_status">
          {status.toUpperCase()}
        </span>
        {ato && (
          <span class={`chip chip-state ${atoStatusChip(ato)}`} title="ato_status">
            {ato}
          </span>
        )}
      </div>
      <div class="op-card-speed">
        <span class="op-card-speed-value">{speedText(c?.current_speed ?? 0)}</span>
        <span class="op-card-speed-unit">km/h</span>
        <span class="op-card-speed-limit" title="ATC limit">
          ATC {speedText(c?.atc_speed_limit)}
        </span>
      </div>
      {hold && (
        <div class="op-card-hold" title={hold}>
          {hold}
        </div>
      )}
      {/* The control acts on the train; it must not also select the card. */}
      <div class="op-card-control" onClick={(e) => e.stopPropagation()}>
        <CardControlView view={view} />
      </div>
    </div>
  );
}

function CardControlView({ view }: { view: TrainView }) {
  const control = cardControl(view);
  const id = view.id;
  switch (control.kind) {
    case 'go':
      return (
        <button
          type="button"
          class="tc-btn tc-btn-success op-card-btn"
          disabled={control.disabledReason !== null}
          title={control.disabledReason ?? 'Start ATO movement'}
          onClick={() => void trainCommands.atoGo(id)}
        >
          ▶ Go
        </button>
      );
    case 'stop':
      return (
        <button type="button" class="tc-btn tc-btn-danger op-card-btn" onClick={() => void trainCommands.atoStop(id)}>
          ■ Stop
        </button>
      );
    case 'start-route':
      return (
        <button
          type="button"
          class="tc-btn tc-btn-success op-card-btn"
          title="Power up if off, select ATO, and start the assigned route"
          onClick={() => void trainCommands.startRoute(id)}
        >
          ▶ Start route
        </button>
      );
    case 'power-on':
      return (
        <button type="button" class="tc-btn op-card-btn" onClick={() => void trainCommands.powerOn(id)}>
          Power on
        </button>
      );
    case 'release':
      return (
        <button
          type="button"
          class="tc-btn op-card-btn"
          title="Release the emergency brake or cut-out once the train has stopped"
          onClick={() => void trainCommands.release(id)}
        >
          Release
        </button>
      );
    case 'handle':
      return <CompactHandle view={view} />;
    case 'power-percent':
      return <CompactPowerPercent view={view} />;
    default:
      return <span class="op-card-control-note">{control.text}</span>;
  }
}

/** The manual handle, small: a slider over the notches that sends the notch
 *  when released, as today's panel does. While dragging it shows the notch
 *  under the thumb instead of the train's current one. */
export function CompactHandle({ view }: { view: TrainView }) {
  const c = view.controller;
  const current = c?.handle_position || 'N';
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const index = dragIndex ?? Math.max(0, HANDLE_ORDER.indexOf(current));
  const shown = HANDLE_ORDER[index] ?? 'N';
  const disabled = !!c?.eb_active || c?.train_status === 'calibrating';
  const onInput = (e: JSX.TargetedEvent<HTMLInputElement>) => setDragIndex(Number(e.currentTarget.value));
  const onChange = async (e: JSX.TargetedEvent<HTMLInputElement>) => {
    const position = HANDLE_ORDER[Number(e.currentTarget.value)];
    try {
      await trainCommands.setHandle(view.id, position);
    } finally {
      setDragIndex(null);
    }
  };
  return (
    <div class="op-card-handle">
      <span class="op-card-handle-power" aria-hidden="true">P</span>
      <input
        type="range"
        class="tc-handle-slider op-card-handle-slider"
        min={0}
        max={HANDLE_ORDER.length - 1}
        step={1}
        value={index}
        disabled={disabled}
        aria-label="Handle"
        aria-valuetext={shown}
        onInput={onInput}
        onChange={onChange}
      />
      <span class="op-card-handle-power" aria-hidden="true">B</span>
      <span class={`op-card-handle-pos handle-position-display ${handleClass(shown)}`}>{shown}</span>
    </div>
  );
}

/** The power-percentage input, small: sent when the slider is released. */
function CompactPowerPercent({ view }: { view: TrainView }) {
  const c = view.controller;
  const [drag, setDrag] = useState<number | null>(null);
  const value = drag ?? (c?.manual_power_percent ?? 0);
  const disabled = !!c?.eb_active || c?.train_status === 'calibrating';
  return (
    <div class="op-card-handle">
      <input
        type="range"
        class="tc-power-slider op-card-handle-slider"
        min={0}
        max={100}
        step={1}
        value={value}
        disabled={disabled}
        aria-label="Power %"
        onInput={(e) => setDrag(Number(e.currentTarget.value))}
        onChange={async (e) => {
          try {
            await trainCommands.setPowerPercent(view.id, Number(e.currentTarget.value));
          } finally {
            setDrag(null);
          }
        }}
      />
      <span class="op-card-handle-pos">{value}%</span>
    </div>
  );
}
