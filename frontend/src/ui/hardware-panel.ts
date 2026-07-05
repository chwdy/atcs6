/**
 * Hardware Setup modal wiring (#hardware-modal).
 *
 * Two independent backend choices (design_new.md "Power -> Hardware
 * configuration"): the output backend (train control) is virtual|gpio and the
 * occupancy backend is virtual|i2c. A real backend runs against one device
 * (ch347|raspberry_pi); ch347 also carries a serial/HID port. The selection is
 * persisted server-side and takes effect on the next session build, so a save is
 * followed by an offer to restart (reusing the shared restart/reconnect flow).
 *
 * setupHardwarePanel() wires the controls once; refreshHardwarePanel() loads the
 * persisted config and renders — call it each time the modal opens so it always
 * reflects what is on disk.
 */
import { api } from '../lib/api';
import { showToast } from '../lib/toast';
import { startRestartReconnectFlow } from './restart-flow';

type OutputBackend = 'virtual' | 'gpio';
type OccupancyBackend = 'virtual' | 'i2c';
type DeviceTarget = 'ch347' | 'raspberry_pi' | null;

interface HardwareState {
  output_backend: OutputBackend;
  occupancy_backend: OccupancyBackend;
  device_target: DeviceTarget;
  device_port: string | null;
}

const DEFAULT_STATE: HardwareState = {
  output_backend: 'virtual',
  occupancy_backend: 'virtual',
  device_target: null,
  device_port: null,
};

// Live edit buffer, the config as last loaded, and whether the buffer has been
// saved since the last edit (drives the post-save "Restart now" affordance).
let state: HardwareState = { ...DEFAULT_STATE };
let loaded: HardwareState = { ...DEFAULT_STATE };
let savedSinceEdit = false;

const isReal = (s: HardwareState): boolean =>
  s.output_backend !== 'virtual' || s.occupancy_backend !== 'virtual';

interface Els {
  output: HTMLElement;
  occupancy: HTMLElement;
  deviceSection: HTMLElement;
  deviceTarget: HTMLElement;
  devicePortRow: HTMLElement;
  devicePort: HTMLInputElement;
  status: HTMLElement;
  restartNote: HTMLElement;
  saveBtn: HTMLButtonElement;
  restartBtn: HTMLButtonElement;
}

function getEls(): Els | null {
  const output = document.getElementById('hw-output-backend');
  const occupancy = document.getElementById('hw-occupancy-backend');
  const deviceSection = document.getElementById('hw-device-section');
  const deviceTarget = document.getElementById('hw-device-target');
  const devicePortRow = document.getElementById('hw-device-port-row');
  const devicePort = document.getElementById('hw-device-port') as HTMLInputElement | null;
  const status = document.getElementById('hw-status');
  const restartNote = document.getElementById('hw-restart-note');
  const saveBtn = document.getElementById('hw-save-btn') as HTMLButtonElement | null;
  const restartBtn = document.getElementById('hw-restart-btn') as HTMLButtonElement | null;
  if (
    !output || !occupancy || !deviceSection || !deviceTarget || !devicePortRow ||
    !devicePort || !status || !restartNote || !saveBtn || !restartBtn
  ) {
    return null;
  }
  return {
    output, occupancy, deviceSection, deviceTarget, devicePortRow,
    devicePort, status, restartNote, saveBtn, restartBtn,
  };
}

/** Mark the button whose data-value matches `value` active; clear the rest. */
function syncGroup(group: HTMLElement, value: string | null): void {
  for (const btn of Array.from(group.querySelectorAll<HTMLButtonElement>('.switch-btn'))) {
    btn.classList.toggle('active', btn.dataset.value === value);
  }
}

/** Human summary of what the current selection actually does. */
function statusHtml(s: HardwareState): string {
  if (!isReal(s)) {
    return '<strong>Sim mode</strong> — full simulation, no hardware attached.';
  }
  const parts: string[] = [];
  if (s.output_backend === 'gpio') parts.push('GPIO train control');
  if (s.occupancy_backend === 'i2c') parts.push('I²C occupancy');
  const what = parts.join(' + ');
  const device =
    s.device_target === 'ch347' ? 'CH347' :
    s.device_target === 'raspberry_pi' ? 'Raspberry Pi' : null;
  if (!device) {
    return `<strong>Real mode</strong> — ${what}. Choose a device to continue.`;
  }
  return `<strong>Real mode</strong> — ${what} on ${device}.`;
}

function render(els: Els): void {
  const real = isReal(state);
  syncGroup(els.output, state.output_backend);
  syncGroup(els.occupancy, state.occupancy_backend);
  syncGroup(els.deviceTarget, state.device_target);

  els.deviceSection.hidden = !real;
  els.devicePortRow.hidden = state.device_target !== 'ch347';
  els.devicePort.value = state.device_port ?? '';

  els.status.classList.toggle('real', real);
  els.status.innerHTML =
    `<i class="ti ${real ? 'ti-plug-connected' : 'ti-device-desktop'} hw-status-icon" aria-hidden="true"></i>` +
    `<span>${statusHtml(state)}</span>`;

  // Post-save state offers a restart; any edit reverts to the Save button.
  els.restartNote.hidden = !savedSinceEdit;
  els.saveBtn.hidden = savedSinceEdit;
  els.restartBtn.hidden = !savedSinceEdit;
}

/** The payload to persist: device fields are nulled when the session is sim. */
function payload(): HardwareState {
  if (!isReal(state)) {
    return {
      output_backend: state.output_backend,
      occupancy_backend: state.occupancy_backend,
      device_target: null,
      device_port: null,
    };
  }
  return {
    output_backend: state.output_backend,
    occupancy_backend: state.occupancy_backend,
    device_target: state.device_target,
    device_port: state.device_target === 'ch347' ? (state.device_port || null) : null,
  };
}

export function setupHardwarePanel(): void {
  const els = getEls();
  if (!els) {
    console.warn('[App] Hardware panel elements not found');
    return;
  }

  // Any edit invalidates a prior save (re-shows the Save button).
  const onEdit = () => {
    savedSinceEdit = false;
  };

  els.output.addEventListener('click', (e) => {
    const btn = (e.target as HTMLElement).closest<HTMLButtonElement>('.switch-btn');
    if (!btn) return;
    state.output_backend = btn.dataset.value as OutputBackend;
    onEdit();
    render(els);
  });

  els.occupancy.addEventListener('click', (e) => {
    const btn = (e.target as HTMLElement).closest<HTMLButtonElement>('.switch-btn');
    if (!btn) return;
    state.occupancy_backend = btn.dataset.value as OccupancyBackend;
    onEdit();
    render(els);
  });

  els.deviceTarget.addEventListener('click', (e) => {
    const btn = (e.target as HTMLElement).closest<HTMLButtonElement>('.switch-btn');
    if (!btn) return;
    state.device_target = btn.dataset.value as DeviceTarget;
    onEdit();
    render(els);
  });

  els.devicePort.addEventListener('input', () => {
    state.device_port = els.devicePort.value.trim() || null;
    onEdit();
    // No full render — the input is the source of truth for its own value.
    els.restartNote.hidden = true;
    els.saveBtn.hidden = false;
    els.restartBtn.hidden = true;
  });

  els.saveBtn.addEventListener('click', () => saveConfig(els));

  els.restartBtn.addEventListener('click', async () => {
    try {
      await api.restartServer();
    } catch (e) {
      console.error('[App] Hardware restart request failed:', e);
      showToast('Could not reach server to restart.', 'error', 4000);
      return;
    }
    startRestartReconnectFlow();
  });
}

async function saveConfig(els: Els): Promise<void> {
  // Client-side guard mirroring the server contract: a real backend needs a
  // device. Catch it here for instant feedback instead of a round-trip 400.
  if (isReal(state) && !state.device_target) {
    showToast('Choose a device target for real mode.', 'error');
    return;
  }
  els.saveBtn.disabled = true;
  try {
    await api.setHardwareConfig(payload());
    loaded = { ...state };
    savedSinceEdit = true;
    render(els);
    showToast('Hardware setup saved — restart to apply.', 'success', 2500);
  } catch (e: any) {
    console.error('[App] Failed to save hardware config:', e);
    showToast(e?.message || 'Failed to save hardware setup.', 'error', 4000);
  } finally {
    els.saveBtn.disabled = false;
  }
}

export async function refreshHardwarePanel(): Promise<void> {
  const els = getEls();
  if (!els) return;
  savedSinceEdit = false;
  try {
    const res = await api.getHardwareConfig();
    const cfg = (res && res.config) || {};
    state = {
      output_backend: cfg.output_backend === 'gpio' ? 'gpio' : 'virtual',
      occupancy_backend: cfg.occupancy_backend === 'i2c' ? 'i2c' : 'virtual',
      device_target:
        cfg.device_target === 'ch347' || cfg.device_target === 'raspberry_pi'
          ? cfg.device_target
          : null,
      device_port: typeof cfg.device_port === 'string' && cfg.device_port ? cfg.device_port : null,
    };
    loaded = { ...state };
  } catch (e) {
    console.error('[App] Failed to load hardware config:', e);
    state = { ...DEFAULT_STATE };
    loaded = { ...DEFAULT_STATE };
    showToast('Failed to load hardware setup.', 'error');
  }
  render(els);
}
