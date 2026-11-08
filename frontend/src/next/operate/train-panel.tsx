/**
 * The selected train, in the Operate screen's right panel
 * (docs/ui-layout-rewrite-plan.md §5).
 *
 * Ported from today's train panel (TrainEditorHandler.showTrainControlPanel
 * in editor/editor-trains.ts), in the order the quick fixes left it: the
 * status line, the cab display, and the driving controls. Then folded
 * sections: Route (assign a route, the route run, and the path tools that
 * used to sit on the tool rail), Look-ahead (today's Train Monitor graph and
 * Upcoming list), Details, and Calibrate, which opens the Locomotives screen.
 *
 * It keeps today's markup classes (static/css/features/train-control.css) so
 * it reads the same, and sends the same commands (train-commands.ts). Its
 * values come from StateManager on each poll that changed something; it runs
 * no timer except the one-second dwell read while the session is live.
 */
import { signal } from '@preact/signals';
import { useEffect, useState } from 'preact/hooks';

import type { ComponentChildren, JSX } from 'preact';

import { api } from '../../lib/api';
import { pickableProfileChoices } from '../../lib/profile-choices';
import { installCopyIdHandler } from '../../lib/copy-id';
import { formatDistanceCm, formatDuration, showConfirmModal } from '../../lib/helpers';
import { showToast } from '../../lib/toast';
import {
  atoGoUnavailableReason,
  atoIsDriving,
  nextStepToRun,
  routeAssignedMessage,
  routeUnassignedMessage,
  startRouteUnavailableReason,
  trainDisplayStatus,
} from '../../lib/train-status';
import { stateManager } from '../../lib/state-manager';
import { formatATOEta, formatSimClock, middleTruncate } from '../../editor/train-format';
import { openLocomotiveForTrain } from '../locomotives';
import { usePoll } from '../poll';
import { sessionState } from '../state';
import { liveVersion, simClock, trainView, type TrainView } from './live';
import { LookAhead } from './look-ahead';
import type { OperateHost } from './operate-state';
import { setSelection } from './panel-host';
import { HANDLE_ORDER, handleClass, handleDescription, trainCommands } from './train-commands';
import { holdLine, obcStateClass, speedText, trainStateClass } from './train-display';

/** Which folded sections are open. Kept across trains and redraws, so an
 *  operator who reads one keeps seeing it. */
const openFolds = signal<Record<string, boolean>>({});

export function Fold({
  id,
  title,
  summary,
  children,
}: {
  id: string;
  title: string;
  summary?: string;
  children: ComponentChildren;
}) {
  const open = !!openFolds.value[id];
  const onToggle = (e: JSX.TargetedEvent<HTMLDetailsElement>) => {
    const next = e.currentTarget.open;
    if (next !== open) openFolds.value = { ...openFolds.value, [id]: next };
  };
  return (
    <details class="tc-disclosure op-fold" open={open} onToggle={onToggle}>
      <summary>
        {title}
        {summary && <span class="op-fold-summary">{summary}</span>}
      </summary>
      {/* Closed sections draw nothing, so their polls stop too. */}
      {open && <div class="tc-disclosure-body">{children}</div>}
    </details>
  );
}

/** The copy-to-clipboard button today's panels put beside an id. */
export function CopyId({ id, label }: { id: string; label: string }) {
  installCopyIdHandler();
  return (
    <button
      type="button"
      class="copy-id-btn"
      data-copy-id={id}
      title={`Copy ${label}`}
      aria-label={`Copy ${label}: ${id}`}
    >
      <i class="ti ti-copy" aria-hidden="true" />
    </button>
  );
}

export function TrainPanel({ host, trainId }: { host: OperateHost; trainId: string }) {
  liveVersion.value;
  const view = trainView(trainId);
  const gone = !view;
  // A train removed from the layout (or a new layout loaded) closes its panel.
  useEffect(() => {
    if (gone) setSelection(host, null);
  }, [gone]);
  if (!view) {
    return <div class="empty-state empty-state-warn">Train {trainId} is no longer on the layout.</div>;
  }
  return (
    <div class="tc-panel train-control-panel op-train-panel" data-train-id={trainId}>
      <StatusZone view={view} />
      <CabDisplay view={view} />
      <DrivingControls host={host} view={view} />
      <section class="tc-section tc-section-debug op-train-folds">
        <Fold id="route" title="Route" summary={view.assignedRouteId ?? 'none'}>
          <RouteSection host={host} view={view} />
        </Fold>
        <Fold id="look-ahead" title="Look-ahead">
          <LookAhead host={host} trainId={trainId} />
        </Fold>
        <Fold id="details" title="Details">
          <DetailsSection host={host} view={view} />
        </Fold>
        <button
          type="button"
          class="tc-btn tc-btn-ghost op-calibrate-btn"
          title="Open the Locomotives screen to calibrate this train's speed profile"
          onClick={() => openLocomotiveForTrain(trainId)}
        >
          <i class="ti ti-chart-line" aria-hidden="true" /> Calibrate
        </button>
      </section>
    </div>
  );
}

/** The status line: id, display status, ATO and EB badges, why it stands,
 *  what it is waiting to finish, and the presence-unknown warning. */
function StatusZone({ view }: { view: TrainView }) {
  const c = view.controller;
  const status = trainDisplayStatus(c, view.operatorStatus);
  const eb = c?.eb_active ?? false;
  const hold = holdLine(view);
  const pending: Array<{ label?: string }> = view.operatorStatus?.pending_actions || [];
  const removeTrain = async () => {
    const confirmed = await showConfirmModal({
      title: 'Remove Train',
      message: `Remove train "${view.id}"?`,
      detail: 'No occupancy backs this train — its span represents nothing detected on the track.',
      confirmText: 'Remove',
      confirmVariant: 'danger',
    });
    if (!confirmed) return;
    try {
      await api.deleteTrain(view.id);
      showToast(`Train "${view.id}" removed`, 'success', 3000);
      await window.app?.refreshNetwork();
    } catch (err: any) {
      showToast('Failed to remove train: ' + err.message, 'error', 5000);
    }
  };
  return (
    <section class={`tc-section tc-section-task ${obcStateClass(c?.power_on ?? false, eb)}`}>
      <header class="tc-status-headline">
        <span class="id-copy-group">
          <span class="tc-train-id">{view.id}</span>
          <CopyId id={view.id} label="Train ID" />
        </span>
        <span class={`tc-state-badge train-state-display ${trainStateClass(status)}`}>
          {status.toUpperCase().replace('_', ' ')}
        </span>
        {atoIsDriving(c, view.operatorStatus) && <span class="tc-running-badge">▶ ATO</span>}
        {eb && <span class="tc-eb-badge tc-status-emergency">EB ACTIVE</span>}
      </header>
      {hold && (
        <div class="banner banner-warn tc-banner">
          <strong>Not moving:</strong> {hold}
        </div>
      )}
      {pending.some((p) => p?.label) && (
        <ul class="tc-pending-actions">
          {pending.filter((p) => p?.label).map((p, i) => (
            <li key={i}>{p.label}</li>
          ))}
        </ul>
      )}
      {c?.presence_unknown === true && (
        <div class="banner banner-warn tc-banner">
          <div>
            ? No occupancy detected under this train — nothing on the track backs its position. It may have been
            picked up or run somewhere undetected.
          </div>
          <button type="button" class="btn btn-sm btn-danger-ghost mt-4" onClick={() => void removeTrain()}>
            <i class="ti ti-trash" aria-hidden="true" /> Remove train
          </button>
        </div>
      )}
    </section>
  );
}

/** Dwell time left, read from the look-ahead payload once a second while the
 *  session is live, as today's panel does. */
function useDwell(trainId: string): { remaining: number | null; finish: number | null } {
  const [dwell, setDwell] = useState<{ remaining: number | null; finish: number | null }>({
    remaining: null,
    finish: null,
  });
  const live = sessionState.value?.value === 'live';
  usePoll(async () => {
    if (!live) return;
    try {
      const payload = await api.getTrainLookahead(trainId);
      const isDwell = payload?.current_task_kind === 'dwell';
      setDwell({
        remaining: isDwell ? (payload?.dwell_remaining_sec ?? null) : null,
        finish: isDwell ? (payload?.dwell_finish_sim_sec ?? null) : null,
      });
    } catch {
      /* the next read tries again */
    }
  }, 1000);
  return live ? dwell : { remaining: null, finish: null };
}

/** The cab display: clock, speed, ATC limit, ATO target, handle, run number,
 *  next stop, distance and ETA. */
function CabDisplay({ view }: { view: TrainView }) {
  const c = view.controller;
  const t = simClock.value;
  const dwell = useDwell(view.id);
  const handle = c?.handle_position || 'N';
  const clearanceCm = c?.clearance_ahead_cm ?? null;
  const eta = formatATOEta(c, clearanceCm, c?.current_speed ?? 0);
  const destination =
    c?.ato_info?.destination_station_name || c?.ato_info?.destination_block_id || c?.ato_destination_block_id || null;
  const run = view.routeRun?.route_id ? `#${view.routeRun.run_number}` : null;
  const cars = view.train?.car_number;
  return (
    <section class="tc-section tc-section-status">
      <div class="tc-hmi">
        <div class="tc-hmi-header">
          <div class="rows-1 tc-hmi-time">{Number.isFinite(t) ? formatSimClock(t, { subsecond: true }) : '—'}</div>
          <div class="rows-1 tc-hmi-meta">{cars ? `${view.id} · ${cars} cars` : view.id}</div>
        </div>
        {c?.eb_active && <div class="rows-1 tc-hmi-alert tc-alert-eb">EB ACTIVE</div>}
        <div class="tc-hmi-grid">
          <div class="col-full rows-3 tc-hmi-speed train-speed-display">{speedText(c?.current_speed ?? 0)}</div>
          <div class="col-r rows-1 tc-hmi-atc">ATC {speedText(c?.atc_speed_limit)}</div>
          {c?.control_mode === 'ato' && <div class="col-r rows-1 tc-hmi-ato">ATO {speedText(c?.target_speed)}</div>}
          {dwell.remaining !== null && <div class="tc-hmi-dwell-rem">{Math.ceil(dwell.remaining)}s left</div>}
          {dwell.remaining !== null && dwell.finish !== null && (
            <div class="tc-hmi-dwell-until">{formatSimClock(dwell.finish)} ←</div>
          )}
          <div class={`col-l rows-2 tc-hmi-handle ${handleClass(handle)}`}>{handle}</div>
          <div class="col-r rows-1 tc-hmi-cab-run">{run ? `RUN ${run}` : ''}</div>
        </div>
        <div class="tc-hmi-footer">
          <span class="hmi-footer-l" title={destination ?? ''}>
            {destination ? `Next: ${destination}` : ''}
          </span>
          <span class="hmi-footer-c">
            {clearanceCm != null && Number.isFinite(clearanceCm) ? `${Math.round(clearanceCm / 100)} m` : '—'}
          </span>
          <span class="hmi-footer-r">{eta && eta !== '—' ? `ETA ${eta}` : ''}</span>
        </div>
      </div>
    </section>
  );
}

/** Whether the active environment is Virtual or Real Hardware, the only
 *  place an operator may cut one train out (design_new.md "Emergency
 *  cut-out"). */
function inHardwareEnvironment(): boolean {
  const env = window.app?.operationEnvironment;
  return env === 'virtual_hardware' || env === 'real_hardware';
}

function DrivingControls({ host, view }: { host: OperateHost; view: TrainView }) {
  const c = view.controller;
  const id = view.id;
  const isATO = (c?.control_mode || 'manual') === 'ato';
  const isManual = !isATO;
  const isStopped = c?.is_stopped ?? true;
  const isCalibrating = c?.train_status === 'calibrating';
  const canSwitchModes = isStopped && !isCalibrating;
  const powerOn = c?.power_on ?? false;
  const eb = c?.eb_active ?? false;
  const isHandleMode = (c?.manual_mode_type || 'simulation') === 'simulation';
  const startReason = startRouteUnavailableReason(c, view.assignedRouteId, view.operatorStatus);
  const goReason = atoGoUnavailableReason(c, view.assignedRouteId);
  return (
    <section class="tc-section tc-section-controls">
      <div class="tc-section-title">Controls</div>
      <div class="tc-actions tc-start-route-row">
        <button
          type="button"
          class="tc-btn tc-btn-success tc-btn-hero"
          title={startReason ?? 'Power up if off, select ATO, and start the assigned route'}
          disabled={startReason !== null}
          onClick={() => void trainCommands.startRoute(id)}
        >
          <span class="tc-btn-glyph">▶</span>
          <span class="tc-btn-text">Start route</span>
        </button>
        <button
          type="button"
          class="tc-btn tc-btn-ghost tc-send-to-btn"
          title={
            isCalibrating
              ? 'The train is calibrating'
              : 'Quick Route: the blocks this train can reach light up; click one to send it there'
          }
          disabled={isCalibrating}
          onClick={() => {
            const mode = host.editor.pathHandler?.buildQuickRouteMode(id);
            if (mode) void host.editor.enterMode(mode);
          }}
        >
          <i class="ti ti-map-pin" aria-hidden="true" /> Send to…
        </button>
      </div>
      <div class="tc-controls-pair">
        <div class="tc-row">
          <span class="tc-row-label">Power</span>
          <div class="tc-segment" role="group" aria-label="Power">
            <button
              type="button"
              class={`tc-seg-btn switch-btn ${powerOn ? 'active' : ''}`}
              disabled={powerOn}
              onClick={() => void trainCommands.powerOn(id)}
            >
              On
            </button>
            <button
              type="button"
              class={`tc-seg-btn switch-btn ${!powerOn ? 'active' : ''}`}
              disabled={!powerOn || !isStopped}
              onClick={() => void trainCommands.powerOff(id)}
            >
              Off
            </button>
          </div>
        </div>
        <div class="tc-row">
          <span class="tc-row-label">Mode</span>
          <div class="tc-segment" role="group" aria-label="Mode">
            <button
              type="button"
              class={`tc-seg-btn switch-btn ${isATO ? 'active' : ''}`}
              disabled={!canSwitchModes}
              onClick={() => void trainCommands.setMode(id, 'ato')}
            >
              ATO
            </button>
            <button
              type="button"
              class={`tc-seg-btn switch-btn ${isManual ? 'active' : ''}`}
              disabled={!canSwitchModes}
              onClick={() => void trainCommands.setMode(id, 'manual')}
            >
              Manual
            </button>
          </div>
        </div>
      </div>

      {isATO && (
        <div class="tc-branch tc-ato-display">
          <div class="tc-actions tc-ato-actions">
            <button
              type="button"
              class="tc-btn tc-btn-success tc-btn-hero"
              disabled={goReason !== null}
              title={goReason ?? undefined}
              onClick={() => void trainCommands.atoGo(id)}
            >
              <span class="tc-btn-glyph">▶</span>
              <span class="tc-btn-text">Go</span>
            </button>
            <button
              type="button"
              class="tc-btn tc-btn-danger tc-btn-hero"
              disabled={!(c?.engaged ?? false)}
              onClick={() => void trainCommands.atoStop(id)}
            >
              <span class="tc-btn-glyph">■</span>
              <span class="tc-btn-text">Stop</span>
            </button>
          </div>
        </div>
      )}

      {isManual && (
        <div class="tc-branch tc-manual-display">
          <div class="tc-row">
            <span class="tc-row-label">Input</span>
            <div class="tc-segment" role="group" aria-label="Manual input mode">
              <button
                type="button"
                class={`tc-seg-btn ${isHandleMode ? 'active' : ''}`}
                disabled={!isStopped || isCalibrating}
                onClick={() => void trainCommands.setInput(id, 'simulation')}
              >
                Handle
              </button>
              <button
                type="button"
                class={`tc-seg-btn ${!isHandleMode ? 'active' : ''}`}
                disabled={!isStopped || isCalibrating}
                onClick={() => void trainCommands.setInput(id, 'power_percent')}
              >
                Power %
              </button>
            </div>
          </div>
          {isHandleMode ? (
            <HandleBlock view={view} disabled={eb || isCalibrating} />
          ) : (
            <PowerPercentBlock view={view} disabled={eb || isCalibrating} />
          )}
        </div>
      )}

      <div class="tc-emergency-row">
        <button
          type="button"
          class="tc-eb-btn"
          title="Apply this train's emergency brake"
          onClick={() => void trainCommands.emergencyBrake(id)}
        >
          EMERGENCY BRAKE
        </button>
        {inHardwareEnvironment() && (
          <button
            type="button"
            class="tc-eb-btn tc-cut-out-btn"
            title="Cut this train out: its blocks go to 0 V and it stops at once"
            onClick={() => void trainCommands.cutOut(id)}
          >
            CUT OUT
          </button>
        )}
        {eb && (
          <button
            type="button"
            class="tc-eb-reset-btn"
            title="Release the emergency brake or cut-out once the train has stopped"
            onClick={() => void trainCommands.release(id)}
          >
            Release
          </button>
        )}
      </div>
    </section>
  );
}

/** The full handle: notch labels over a slider that sends on release. */
function HandleBlock({ view, disabled }: { view: TrainView; disabled: boolean }) {
  const current = view.controller?.handle_position || 'N';
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const index = dragIndex ?? Math.max(0, HANDLE_ORDER.indexOf(current));
  const shown = HANDLE_ORDER[index] ?? 'N';
  return (
    <div class="tc-handle-block">
      <div class="tc-handle-zones" aria-hidden="true">
        <span class="tc-handle-zone tc-zone-power">POWER</span>
        <span class="tc-handle-zone tc-zone-neutral">N</span>
        <span class="tc-handle-zone tc-zone-brake">BRAKE</span>
        <span class="tc-handle-zone tc-zone-eb">EB</span>
      </div>
      <div class="tc-handle-ticks" aria-hidden="true">
        {HANDLE_ORDER.map((p, i) => (
          <span key={p} class={`tc-handle-tick ${i === index ? 'active' : ''}`}>
            {p}
          </span>
        ))}
      </div>
      <input
        type="range"
        class="tc-handle-slider"
        min={0}
        max={HANDLE_ORDER.length - 1}
        step={1}
        value={index}
        disabled={disabled}
        aria-label="Handle"
        aria-valuetext={shown}
        onInput={(e) => setDragIndex(Number(e.currentTarget.value))}
        onChange={async (e) => {
          try {
            await trainCommands.setHandle(view.id, HANDLE_ORDER[Number(e.currentTarget.value)]);
          } finally {
            setDragIndex(null);
          }
        }}
      />
      <div class="tc-handle-readout">
        <span class={`tc-handle-pos handle-position-display ${handleClass(shown)}`}>{shown}</span>
        <span class="tc-handle-desc handle-description">{handleDescription(shown)}</span>
      </div>
    </div>
  );
}

function PowerPercentBlock({ view, disabled }: { view: TrainView; disabled: boolean }) {
  const [drag, setDrag] = useState<number | null>(null);
  const value = drag ?? (view.controller?.manual_power_percent ?? 0);
  return (
    <div class="tc-power-block">
      <div class="tc-power-readout">
        <span class="tc-power-value">{value}</span>
        <span class="tc-power-unit">%</span>
      </div>
      <input
        type="range"
        class="tc-power-slider"
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
      <div class="tc-power-bar">
        <div class="tc-power-fill" style={{ width: `${value}%` }} />
      </div>
      <div class="tc-power-hint">Direct throttle command (0–100). Set a travel direction first.</div>
    </div>
  );
}

/** The route picker's first line: what the list holds, or why it is empty. */
export function routePickerPrompt(
  routes: ReadonlyArray<unknown> | null,
  readError: string | null,
): string {
  if (routes === null) return readError === null ? 'Reading the routes…' : 'Could not read the routes';
  return routes.length ? 'Choose a route…' : 'No routes yet';
}

/** The route this train runs: assign or unassign one, open it or its run,
 *  and the path tools, which act on the map. */
function RouteSection({ host, view }: { host: OperateHost; view: TrainView }) {
  // Null until the first read answers, so "No routes yet" is never said
  // before the routes were read, or after a read failed.
  const [routes, setRoutes] = useState<Array<{ route_id: string; name?: string }> | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const [picked, setPicked] = useState('');
  usePoll(async () => {
    try {
      const resp = await api.getRoutes();
      setRoutes(Array.isArray(resp?.routes) ? resp.routes : []);
      setReadError(null);
    } catch (err: any) {
      setReadError(String(err?.message || err || 'no answer'));
    }
  }, 5000);

  const assigned = view.assignedRouteId;
  const run = view.routeRun;
  const assign = async () => {
    if (!picked) return;
    try {
      const replacing = !!assigned && assigned !== picked;
      await api.assignRoute(view.id, picked);
      await window.app?.refreshNetwork();
      showToast(routeAssignedMessage(view.id, stateManager.getController(view.id), replacing), 'success', 6000);
    } catch (err: any) {
      showToast('Failed to assign train: ' + err.message, 'error', 5000);
    }
  };
  const unassign = async () => {
    try {
      const hadRun = !!stateManager.getRouteRun(view.id);
      await api.assignRoute(view.id, null);
      await window.app?.refreshNetwork();
      showToast(routeUnassignedMessage(view.id, hadRun), 'success', 5000);
    } catch (err: any) {
      showToast('Failed to unassign train: ' + err.message, 'error', 5000);
    }
  };
  const pathTool = (build: () => any) => {
    const mode = build();
    if (mode) void host.editor.enterMode(mode);
  };
  const paths = host.editor.pathHandler;

  return (
    <div class="op-route">
      <dl class="tc-status-fields">
        <div class="tc-field">
          <dt class="tc-field-label">Route</dt>
          <dd class="tc-field-value tc-val-info">{assigned ?? '—'}</dd>
        </div>
        <div class="tc-field">
          <dt class="tc-field-label">Run</dt>
          <dd class="tc-field-value">
            {run?.route_id ? `#${run.run_number} ${run.display_state ?? run.state ?? ''}`.trim() : '—'}
          </dd>
        </div>
      </dl>
      {assigned && <div class="op-hint">{nextStepToRun(view.controller)}</div>}
      <div class="op-row">
        <select
          class="property-input op-grow"
          aria-label="Route to assign"
          value={picked}
          onChange={(e) => setPicked(e.currentTarget.value)}
        >
          <option value="">{routePickerPrompt(routes, readError)}</option>
          {(routes ?? []).map((r) => (
            <option key={r.route_id} value={r.route_id}>
              {r.name || r.route_id}
            </option>
          ))}
        </select>
        <button type="button" class="btn btn-sm" disabled={!picked} onClick={() => void assign()}>
          Assign
        </button>
      </div>
      {readError !== null && (
        <div class="banner banner-warn op-route-stale" role="status">
          Could not read the routes ({readError}). Trying again.
        </div>
      )}
      <div class="op-row">
        {assigned && (
          <button type="button" class="rr-flat-btn" onClick={() => void unassign()}>
            Unassign
          </button>
        )}
        {assigned && (
          <button type="button" class="rr-flat-btn" onClick={() => void host.editor.routeHandler?.showRouteEditor(assigned)}>
            Open route →
          </button>
        )}
        {run?.run_number != null && (
          <button
            type="button"
            class="rr-flat-btn"
            onClick={() => void host.editor.routeRunHandler?.showRouteRunEditor(Number(run.run_number))}
          >
            Route run →
          </button>
        )}
      </div>
      <div class="tc-section-subtitle">Path</div>
      <div class="op-row">
        <button type="button" class="btn btn-sm" title="Create a path between blocks: click the train's block, then the end" onClick={() => pathTool(() => paths?.buildRouteToolMode())}>
          <i class="ti ti-plus" aria-hidden="true" /> New
        </button>
        <button type="button" class="btn btn-sm" title="Extend an existing path" onClick={() => pathTool(() => paths?.buildExtendRouteMode())}>
          <i class="ti ti-arrows-horizontal" aria-hidden="true" /> Extend
        </button>
        <button type="button" class="btn btn-sm" title="Cancel a path" onClick={() => pathTool(() => paths?.buildCancelRouteMode())}>
          <i class="ti ti-route-x" aria-hidden="true" /> Cancel
        </button>
      </div>
    </div>
  );
}

/** Everything a driver does not need while driving: route and path ids,
 *  geometry, properties, maintenance, and the property editor. */
function DetailsSection({ host, view }: { host: OperateHost; view: TrainView }) {
  const t = view.train;
  const c = view.controller;
  const span = c?.span || null;
  const leadingA = span?.leading_end === 'a';
  const end = (e: any) => ({
    tile: e?.tile ? `(${e.tile[0]}, ${e.tile[1]})` : '—',
    dist: e && Number.isFinite(e.distance_cm) ? `${e.distance_cm.toFixed(1)} cm` : '—',
    edge: e?.entry_edge || '—',
  });
  const a = end(span?.end_a);
  const b = end(span?.end_b);
  const d = view.dispatcher;
  const pathIndex =
    d?.current_resource_index != null && Array.isArray(d?.resource_sequence) && d.resource_sequence.length > 0
      ? `${d.current_resource_index + 1} / ${d.resource_sequence.length}`
      : '—';
  const pathId = c?.ato_info?.path_id || c?.assigned_path_id || null;
  const field = (label: string, value: ComponentChildren, wide = false) => (
    <div class={wide ? 'tc-field tc-field-wide' : 'tc-field'}>
      <dt class="tc-field-label">{label}</dt>
      <dd class="tc-field-value">{value}</dd>
    </div>
  );
  return (
    <div class="op-details">
      <div class="tc-section-subtitle">Route</div>
      <dl class="tc-status-fields">
        {field('Plan ID', view.routeRun?.route_id || view.assignedRouteId || '—')}
        {field('Run ID', view.routeRun?.route_id ? `#${view.routeRun.run_number}` : '—')}
        {field('Path', pathId ? middleTruncate(pathId, 22) : '—')}
        {field('Path Index', pathIndex)}
      </dl>
      <div class="tc-section-subtitle">Train geometry</div>
      <dl class="tc-status-fields tc-span-grid">
        {field(`End A ${leadingA ? '(HEAD)' : '(TAIL)'}`, a.tile)}
        {field(`End B ${leadingA ? '(TAIL)' : '(HEAD)'}`, b.tile)}
        {field('End A dist', a.dist)}
        {field('End B dist', b.dist)}
        {field('End A edge', a.edge)}
        {field('End B edge', b.edge)}
        {field('Facing', c?.facing_direction || '—')}
      </dl>
      <div class="tc-section-subtitle">Maintenance</div>
      <dl class="tc-status-fields">
        {field('Distance', formatDistanceCm(t?.total_distance_cm || 0))}
        {field('Run time', formatDuration(t?.total_run_time_seconds || 0))}
      </dl>
      <EditProperties host={host} view={view} />
    </div>
  );
}

/** Car number, length, maximum speed and the linked speed profile. Saved
 *  with one button, as in today's panel. */
function EditProperties({ host, view }: { host: OperateHost; view: TrainView }) {
  const t = view.train;
  const initialProfile = String(t?.speed_profile_id || 'default');
  const [car, setCar] = useState(String(t?.car_number || ''));
  const [length, setLength] = useState(String(t?.length_cm || 0));
  const [maxSpeed, setMaxSpeed] = useState(String(t?.max_speed || 100));
  const [profile, setProfile] = useState(initialProfile);
  const [profiles, setProfiles] = useState<Array<{ id: string; label: string }>>([]);

  // A different train: start again from its own values.
  useEffect(() => {
    setCar(String(t?.car_number || ''));
    setLength(String(t?.length_cm || 0));
    setMaxSpeed(String(t?.max_speed || 100));
    setProfile(initialProfile);
  }, [view.id]);

  // Offer only what the server accepts: instance profiles no other train names,
  // plus this train's own (design_new.md "SpeedProfile", An operator points a
  // train only at an instance profile no other train names).
  useEffect(() => {
    let stale = false;
    Promise.all([api.getSpeedProfiles(), api.getTrains()])
      .then(([list, trains]: [any[], any[]]) => {
        if (stale) return;
        setProfiles(pickableProfileChoices(list, trains, initialProfile));
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [view.id, initialProfile]);

  const options = profiles.some((o) => o.id === profile) ? profiles : [{ id: profile, label: profile }, ...profiles];
  const save = async () => {
    const updates: Record<string, unknown> = { car_number: car };
    const len = parseFloat(length);
    if (!isNaN(len) && len >= 0) updates.length_cm = len;
    const max = parseFloat(maxSpeed);
    if (!isNaN(max) && max > 0) updates.max_speed = max;
    try {
      await api.updateTrain(view.id, updates);
      // The profile link lives behind its own validated endpoint.
      if (profile && profile !== initialProfile) await api.assignTrainSpeedProfile(view.id, profile);
      await host.refreshNetwork();
      showToast('Train properties saved', 'success', 5000);
    } catch (err: any) {
      showToast('Failed to save train properties: ' + err.message, 'error', 5000);
    }
  };
  return (
    <div class="op-edit-props">
      <div class="tc-section-subtitle">Edit properties</div>
      <div class="tc-row">
        <label class="tc-row-label" for="op-train-car">Car #</label>
        <input id="op-train-car" type="text" value={car} placeholder="Car number" onInput={(e) => setCar(e.currentTarget.value)} />
      </div>
      <div class="tc-row">
        <label class="tc-row-label" for="op-train-length">Length</label>
        <input id="op-train-length" type="number" min={0} step={10} value={length} onInput={(e) => setLength(e.currentTarget.value)} />
      </div>
      <div class="tc-row">
        <label class="tc-row-label" for="op-train-max">Max Speed</label>
        <input id="op-train-max" type="number" min={1} max={500} value={maxSpeed} onInput={(e) => setMaxSpeed(e.currentTarget.value)} />
      </div>
      <div class="tc-row">
        <label class="tc-row-label" for="op-train-profile">Speed Profile</label>
        <select id="op-train-profile" value={profile} onChange={(e) => setProfile(e.currentTarget.value)}>
          {options.map((o) => (
            <option key={o.id} value={o.id}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      <div class="tc-footer">
        <button type="button" class="tc-btn tc-btn-primary" onClick={() => void save()}>
          Save
        </button>
      </div>
    </div>
  );
}
