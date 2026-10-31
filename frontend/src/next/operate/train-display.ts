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

/** The one-line hold reason, only while the train stands. */
export function holdLine(view: TrainView): string | null {
  if (!(view.controller?.is_stopped ?? true)) return null;
  return holdReasonText(view.operatorStatus?.hold_reason);
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
