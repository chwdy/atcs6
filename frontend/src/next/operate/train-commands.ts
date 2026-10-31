/**
 * The driving commands the train cards and the train panel send.
 *
 * Ported from TrainEditorHandler.initTrainControlHandlers
 * (editor/editor-trains.ts): the same endpoints, the same toasts, and the
 * same handling of the reply, so a train driven from a card behaves exactly
 * like one driven from today's panel.
 */
import { api } from '../../lib/api';
import { stateManager } from '../../lib/state-manager';
import { showToast } from '../../lib/toast';

/** The manual handle's notches, from full power to emergency brake. */
export const HANDLE_ORDER = ['P5', 'P4', 'P3', 'P2', 'P1', 'N', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'EB'];

/** What a handle notch does, in words. */
export function handleDescription(position: string): string {
  if (position === 'N') return 'Coast (no power, no braking)';
  if (position.startsWith('P')) return `Power ${position.slice(1)} — Accelerating`;
  if (position === 'EB') return 'Emergency Brake';
  return `Brake ${position.slice(1)} — Decelerating`;
}

/** The CSS class today's panel colours a notch with. */
export function handleClass(position: string): string {
  if (position.startsWith('P')) return 'handle-power';
  if (position === 'EB') return 'handle-eb';
  if (position.startsWith('B')) return 'handle-brake';
  return 'handle-neutral';
}

/** Copy a command reply's train status into StateManager, so the card and
 *  the panel show it on the same frame. Train command replies carry the
 *  train, controller, dispatcher progress and operator status on acceptance
 *  and refusal alike (design_new.md "Operator-command processing slot"). */
export function applyReply(trainId: string, reply: any): boolean {
  if (!reply || !(reply.controller || reply.train)) return false;
  if (reply.train) stateManager.updateEntity('trains', trainId, reply.train);
  if (reply.controller) stateManager.updateEntity('controllers', trainId, reply.controller);
  if (reply.dispatcher_progress !== undefined) {
    stateManager.updateEntity('dispatcherProgress', trainId, reply.dispatcher_progress);
  }
  if (reply.operator_status) stateManager.updateEntity('operatorStatus', trainId, reply.operator_status);
  return true;
}

/** Send one command and report how it went. A refused train command replies
 *  400 with `{success: false, reason}` plus the train's status, so the reason
 *  and the hold reason show at once instead of on the next poll. */
export async function runTrainCommand(
  trainId: string,
  label: string,
  send: () => Promise<any>,
  { refresh = true }: { refresh?: boolean } = {}
): Promise<void> {
  try {
    const result = await send();
    if (result && result.status === 'success') showToast(`${label}: Success`, 'success', 2000);
    if (!refresh) return;
    if (applyReply(trainId, result)) return;
    // Endpoints that do not echo the train's new status.
    await window.app?.refreshNetwork();
  } catch (err: any) {
    const reply = err?.data;
    if (reply && reply.success === false) {
      applyReply(trainId, reply);
      showToast(`${label}: ${reply.reason || err.message}`, 'warning', 5000);
      return;
    }
    console.error(`[Operate] ${label} failed:`, err);
    showToast(`${label}: ${err?.message ?? err}`, 'error', 5000);
  }
}

/** Poll until the train reads stopped, or give up after `timeoutMs`. */
async function waitForTrainStopped(trainId: string, timeoutMs = 8000, intervalMs = 250): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const resp = await api.getTrain(trainId);
      if (resp?.controller?.is_stopped) return true;
    } catch (err) {
      console.warn('[Operate] Failed to poll controller state:', err);
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  return false;
}

/** One function per command, named for what the operator presses. */
export const trainCommands = {
  powerOn: (id: string) => runTrainCommand(id, 'Power On', () => api.trainPowerOn(id)),
  powerOff: (id: string) => runTrainCommand(id, 'Power Off', () => api.trainPowerOff(id)),
  setMode: (id: string, mode: 'ato' | 'manual') =>
    runTrainCommand(id, mode === 'ato' ? 'Switch to ATO' : 'Switch to Manual', () => api.setControlMode(id, mode)),
  /** Power up if OFF, select ATO, and engage (design_new.md "Start route"). */
  startRoute: (id: string) => runTrainCommand(id, 'Start route', () => api.startRoute(id)),
  atoGo: (id: string) => runTrainCommand(id, 'ATO Go', () => api.atoGo(id)),
  atoStop: (id: string) =>
    runTrainCommand(id, 'ATO Stop', async () => {
      const result = await api.atoStop(id);
      if (result && result.status === 'success') await waitForTrainStopped(id);
      return result;
    }),
  setHandle: (id: string, position: string) =>
    runTrainCommand(id, `Set Handle ${position}`, () => api.setHandle(id, position)),
  setInput: (id: string, type: 'simulation' | 'power_percent') =>
    runTrainCommand(id, type === 'simulation' ? 'Set Input: Handle' : 'Set Input: Power %', () =>
      api.setManualModeType(id, type)
    ),
  setPowerPercent: (id: string, percent: number) =>
    runTrainCommand(id, `Set Power ${percent}%`, () => api.setManualPowerPercent(id, percent), { refresh: false }),
  emergencyBrake: (id: string) => runTrainCommand(id, 'Emergency brake', () => api.emergencyBrake(id)),
  cutOut: (id: string) => runTrainCommand(id, 'Cut out', () => api.activateCutOut(id)),
  release: (id: string) => runTrainCommand(id, 'Release', () => api.releaseEmergency(id)),
};
