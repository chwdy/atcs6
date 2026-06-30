/**
 * Train cab/list display formatters — extracted from editor-trains.ts.
 *
 * Pure string formatters for the speed readout, ATO ETA, durations, the sim
 * clock, and middle-ellipsis truncation. formatATOEta reads the global sim time
 * from stateManager; the rest are pure.
 */
import { stateManager } from '../lib/state-manager';

/** Render the cab speed readout: "<big current>/<small max> km/h". */
export function formatSpeedReadout(currentSpeed: any, maxSpeed: any): string {
  const v = Number(currentSpeed) || 0;
  const formatted = v < 10 ? v.toFixed(1) : Math.round(v).toString();
  return `<span class="tc-speed-current">${formatted}</span><span class="tc-speed-max">/${maxSpeed}</span><span class="tc-speed-unit">km/h</span>`;
}

/** Format ATO ETA. Prefers controller.time_to_target_sec; falls back to a
 *  coarse distance/speed estimate when the curve isn't published. */
export function formatATOEta(controller: any, clearanceCm: any, currentSpeed: any): string {
  const ato = controller?.time_to_target_sec;
  let etaSec = null;
  if (ato != null && Number.isFinite(ato) && ato > 0) {
    etaSec = ato;
  } else {
    const convFactor = controller?.speed_conversion_factor;
    const lookahead = controller?.lookahead ?? [];
    const pathEnd =
      lookahead.findLast?.((lp: any) => lp.is_path_end) ??
      [...lookahead].reverse().find((lp: any) => lp.is_path_end);
    const distCm = pathEnd?.distance_cm ?? clearanceCm;
    const atoSpeed = controller?.target_speed;
    const speed = atoSpeed != null && atoSpeed > 0.5 ? atoSpeed : currentSpeed;
    if (distCm != null && Number.isFinite(distCm) && speed > 0.5 && convFactor > 0) {
      etaSec = distCm / (speed * convFactor);
    }
  }
  if (etaSec == null) return '—';

  const duration = formatDuration(etaSec);
  const simTime = stateManager?.simulationTime;
  if (typeof simTime === 'number' && Number.isFinite(simTime)) {
    const arrival = formatSimClock(simTime + etaSec);
    return `${arrival} (${duration})`;
  }
  return duration;
}

// ETA duration ("<1s" / "Ns" / "Nm Ns") — distinct from helpers.formatDuration
// (runtime "Xh Ym"); module-internal, used only by formatATOEta.
function formatDuration(etaSec: number): string {
  if (etaSec < 1) return '<1s';
  if (etaSec < 60) return `${Math.round(etaSec)}s`;
  const mins = Math.floor(etaSec / 60);
  const secs = Math.round(etaSec % 60);
  return `${mins}m ${secs}s`;
}

export function formatSimClock(totalSec: number, { subsecond = false }: { subsecond?: boolean } = {}): string {
  const t = Math.max(0, totalSec);
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = Math.floor(t % 60);
  const hh = String(h).padStart(2, '0');
  const mm = String(m).padStart(2, '0');
  const ss = String(s).padStart(2, '0');
  if (subsecond) {
    const ds = Math.floor((t % 1) * 10);
    return `${hh}:${mm}:${ss}.${ds}`;
  }
  return `${hh}:${mm}:${ss}`;
}

/** Middle-ellipsis truncation. */
export function middleTruncate(str: any, max: number): string {
  if (!str) return '';
  const s = String(str);
  if (s.length <= max) return s;
  const keep = max - 1; // reserve 1 char for the ellipsis
  const head = Math.ceil(keep / 2);
  const tail = Math.floor(keep / 2);
  return `${s.slice(0, head)}…${s.slice(-tail)}`;
}
