/**
 * How the train card and the train panel put a train's state into words and
 * colours. Pure functions over a TrainView, so they are tested without a
 * page. The class names are today's train panel classes
 * (static/css/features/train-control.css), so both read the same.
 */
import { holdReasonText } from '../../lib/hold-reason';
import { atoGoUnavailableReason, startRouteUnavailableReason } from '../../lib/train-status';
import type { TrainView } from './live';

/** The train-state class for a display status token (`trainDisplayStatus`). */
export function trainStateClass(status: string): string {
  const map: Record<string, string> = {
    off: 'train-state-off',
    manual: 'train-state-manual',
    calibrating: 'train-state-calibrating',
    eb: 'train-state-off',
    ready: 'train-state-idle',
    running: 'train-state-running',
    held: 'train-state-waiting',
    dwelling: 'train-state-station',
  };
  return map[status] || 'train-state-off';
}

/** The panel's power accent: off, on, or on with the emergency brake. */
export function obcStateClass(powerOn: boolean, emergencyActive: boolean): string {
  if (!powerOn) return 'obc-power-off';
  return emergencyActive ? 'obc-power-emergency' : 'obc-power-on';
}

/** A whole number for a speed readout, or a dash when there is none. */
export function speedText(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) ? String(Math.round(value)) : '—';
}

/** `train_status` (design_new.md "Controller state machine"), `off` before
 *  the first snapshot. */
export function trainStatus(view: TrainView): string {
  return view.controller?.train_status || 'off';
}

/** `ato_status` (design_new.md "ATO status"), shown only for an ATO train. */
export function atoStatus(view: TrainView): string | null {
  if (trainStatus(view) !== 'ato') return null;
  return String(view.operatorStatus?.ato_status || 'READY').toUpperCase();
}

/** The status-chip colour (DESIGN_LANGUAGE.md §7) for a `train_status`.
 *  Danger is kept for the emergency brake. */
export function trainStatusChip(status: string): string {
  const map: Record<string, string> = {
    manual: 'chip-warn',
    ato: 'chip-accent',
    eb: 'chip-danger',
    calibrating: 'chip-info',
  };
  return map[status] || 'chip-muted';
}

/** The status-chip colour for an `ato_status`: green while ATO drives. */
export function atoStatusChip(status: string): string {
  const map: Record<string, string> = { RUNNING: 'chip-success', HELD: 'chip-info', DWELLING: 'chip-info' };
  return map[status] || 'chip-muted';
}

/** The colour of a train card's left edge (DESIGN_LANGUAGE.md §6), so the
 *  train that needs the operator stands out in the row. */
export type CardTone = 'danger' | 'warn' | 'info' | 'ok' | 'none';

/** `no_reservation` / `reservation_end` details that are a failure, not a
 *  wait (design_new.md "Error events" origins). `meet_hold`, a block id,
 *  or no detail is the railway making the train wait. */
const FAILED_PLANNING = new Set(['planning_failed', 'task_skipped', 'run_stalled', 'wedge']);

/**
 * The card's edge, read from `hold_reason` (design_new.md "Hold reason"):
 * - danger: the emergency brake or cut-out, or the detectors lost the train;
 * - warn: the train waits on the operator or on a failure — unidentified,
 *   ATO not started, no route, or planning failed;
 * - info: the railway makes it wait — a calibration run, a pending action,
 *   a dwell, the end of its reservation, the ATC limit;
 * - ok: it is moving;
 * - none: it stands by choice (powered off, the driver's handle at rest),
 *   or the whole session is stopped, which the top bar already says.
 */
export function trainCardTone(view: TrainView): CardTone {
  const hold = view.operatorStatus?.hold_reason as { code?: string; detail?: string | null } | null | undefined;
  if (!hold?.code) return view.controller?.is_stopped === false ? 'ok' : 'none';
  switch (hold.code) {
    case 'emergency_cut_out':
    case 'emergency_brake':
    case 'detection_lost':
      return 'danger';
    case 'unidentified':
    case 'ato_not_engaged':
    case 'no_route':
      return 'warn';
    case 'no_reservation':
    case 'reservation_end':
      return FAILED_PLANNING.has(hold.detail ?? '') ? 'warn' : 'info';
    case 'calibrating':
    case 'pending_action':
    case 'dwelling':
    case 'atc_limit':
      return 'info';
    default:
      return 'none';
  }
}

/** The one-line hold reason, only while the train stands. */
export function holdLine(view: TrainView): string | null {
  if (!(view.controller?.is_stopped ?? true)) return null;
  return holdReasonText(view.operatorStatus?.hold_reason);
}

/** The card's hold line. A question-1 reason is the session state: it is the
 *  same for every train and the top bar already shows it, so the card leaves
 *  it out. The train panel still shows it in full (`holdLine`). */
export function cardHoldLine(view: TrainView): string | null {
  const hold = view.operatorStatus?.hold_reason as { question?: number } | null | undefined;
  if (hold?.question === 1) return null;
  return holdLine(view);
}

/**
 * The card's main control. It follows `train_status`: Go or Stop for an ATO
 * train and the handle for a manual one (plan §5); a powered-off train
 * offers Start route when its route can start that way, otherwise Power on;
 * a train under the emergency brake offers Release; a calibrating train is
 * driven by its calibration run and offers nothing.
 */
export type CardControl =
  | { kind: 'go'; disabledReason: string | null }
  | { kind: 'stop' }
  | { kind: 'handle' }
  | { kind: 'power-percent' }
  | { kind: 'start-route' }
  | { kind: 'power-on' }
  | { kind: 'release' }
  | { kind: 'none'; text: string };

export function cardControl(view: TrainView): CardControl {
  const c = view.controller;
  if (view.train?.is_unidentified) return { kind: 'none', text: 'Unidentified' };
  switch (trainStatus(view)) {
    case 'ato':
      if (c?.engaged) return { kind: 'stop' };
      return { kind: 'go', disabledReason: atoGoUnavailableReason(c, view.assignedRouteId) };
    case 'manual':
      return c?.manual_mode_type === 'power_percent' ? { kind: 'power-percent' } : { kind: 'handle' };
    case 'eb':
      return { kind: 'release' };
    case 'calibrating':
      return { kind: 'none', text: 'Calibrating' };
    default:
      return startRouteUnavailableReason(c, view.assignedRouteId, view.operatorStatus) === null
        ? { kind: 'start-route' }
        : { kind: 'power-on' };
  }
}
