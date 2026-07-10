/**
 * Hardware Setup modal (#hardware-modal).
 *
 * This screen commissions the *chips* on the control board and nothing else:
 * where each chip is (which bus + address / latch line), what it is, how many
 * ports it has, and whether those ports respond. It does not bind ports to the
 * layout — blocks and switches are wired to ports in their own editor panels
 * (see hardware-binding.ts), so the config the panel writes carries only the
 * controller and the chip list and leaves the binding maps untouched.
 *
 * The flow, top to bottom:
 *   1. Connect the bridge  — pick the controller, Detect it (scans the I2C bus).
 *   2. Your chips, by bus  — I2C / SPI / GPIO groups, each chip ordered by its
 *      address or latch line. Prove each chip: outputs Sweep, inputs Read live.
 *   3. Go live (footer)    — one readiness checklist gates the Enable switch;
 *      enabling applies on the next session build, so a save offers a restart.
 *
 * setupHardwarePanel() wires the delegated handlers once; refreshHardwarePanel()
 * loads the catalog + persisted config and renders — call it each time the modal
 * opens.
 */
import { api } from '../lib/api';
import { showToast } from '../lib/toast';
import { startRestartReconnectFlow } from './restart-flow';

const BUS_ORDER = ['i2c', 'spi', 'gpio'] as const;
const BUS_LABEL: Record<string, string> = { i2c: 'I²C', spi: 'SPI', gpio: 'GPIO' };
const BUS_ORDERED_BY: Record<string, string> = {
  i2c: 'ordered by address', spi: 'ordered by latch line', gpio: 'ordered by pin',
};

interface CatalogChip {
  kind: string; label: string; buses: string[]; role: string; direction: string;
  ports_per_unit: number; cascadable: boolean; port_kind: string;
  default_i2c_address: number | null; port_help: string;
}
interface Catalog {
  controllers: { kind: string; label: string; needs_device_port: boolean }[];
  chips: CatalogChip[];
  spi_select_lines: string[];
  gpio_lines: string[];
}
interface ChipCfg {
  chip_id: string; kind: string; bus: string;
  i2c_address: number | null; chain_length: number; spi_select: string | null; label: string;
}
interface HwState {
  enabled: boolean;
  controller: { kind: string; device_port: string | null };
  chips: ChipCfg[];
}
interface PortResult { port: number; available: boolean; }
type SelfTest = Record<string, { reachable: boolean; ports: PortResult[] }>;

const DEFAULT_STATE: HwState = {
  enabled: false,
  controller: { kind: 'ch347', device_port: null },
  chips: [],
};

let state: HwState = clone(DEFAULT_STATE);
let catalog: Catalog | null = null;
let selfTest: SelfTest = {};
let i2cScan: string[] = [];
let controllerReachable = false;
let savedSinceEdit = false;
let adding = false;

// Commissioning — a held device for the live input view and the output sweep.
// Only one activity runs at a time; it is stopped on modal close / refresh.
let commissionActive = false;
let activity: { chipId: string; kind: 'sweep' | 'live'; port: number; values: number[] } | null = null;
let activityTimer: any = null;
let sweepAbort = false;

function clone<T>(v: T): T { return JSON.parse(JSON.stringify(v)); }
function esc(s: any): string {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' } as any)[c]);
}
function el<T extends HTMLElement = HTMLElement>(id: string): T | null {
  return document.getElementById(id) as T | null;
}
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// --- catalog-derived chip facts --------------------------------------------
function catalogChip(kind: string): CatalogChip | undefined {
  return catalog?.chips.find((c) => c.kind === kind);
}
function chipPorts(chip: ChipCfg): number {
  const ck = catalogChip(chip.kind);
  if (!ck) return 0;
  return ck.ports_per_unit * (ck.cascadable ? Math.max(1, chip.chain_length) : 1);
}
function chipDirection(chip: ChipCfg): string { return catalogChip(chip.kind)?.direction || 'output'; }
function isCurrentSense(chip: ChipCfg): boolean { return catalogChip(chip.kind)?.port_kind === 'current_sense'; }
function chipLabel(chip: ChipCfg): string { return catalogChip(chip.kind)?.label || chip.kind; }
function latchLines(bus: string): string[] {
  if (bus === 'spi') return catalog?.spi_select_lines || [];
  if (bus === 'gpio') return catalog?.gpio_lines || [];
  return [];
}
function coordText(chip: ChipCfg): string {
  if (chip.bus === 'i2c') return chip.i2c_address != null ? '0x' + chip.i2c_address.toString(16) : '';
  return (chip.spi_select || '').toUpperCase();
}
function coordSort(chip: ChipCfg): number {
  if (chip.bus === 'i2c') return chip.i2c_address ?? 9999;
  return latchLines(chip.bus).indexOf(chip.spi_select || '');
}

// --- setup (once) ----------------------------------------------------------
export function setupHardwarePanel(): void {
  const modal = el('hardware-modal');
  const bodyEl = el('hw-body');
  const gateEl = el('hw-gate');
  if (!modal || !bodyEl || !gateEl) {
    console.warn('[App] Hardware panel elements not found');
    return;
  }

  const root = modal;
  root.addEventListener('click', (e) => void onClick(e));
  root.addEventListener('change', (e) => onChange(e));
  root.addEventListener('input', (e) => onInput(e));

  // Stop any live/sweep activity (and release the device) when the modal closes
  // via any path — close button, backdrop, or Escape all drop the `active` class.
  new MutationObserver(() => {
    if (!modal.classList.contains('active')) void stopCommissioning();
  }).observe(modal, { attributes: true, attributeFilter: ['class'] });
}

function onEdit(): void { savedSinceEdit = false; }

// --- refresh (each open) ---------------------------------------------------
export async function refreshHardwarePanel(): Promise<void> {
  await stopCommissioning();
  savedSinceEdit = false;
  adding = false;
  selfTest = {};
  i2cScan = [];
  controllerReachable = false;
  try {
    const [cat, cfg] = await Promise.all([api.getHardwareCatalog(), api.getHardwareConfig()]);
    catalog = cat as Catalog;
    state = normalizeConfig(cfg?.config || {});
  } catch (e) {
    console.error('[App] Failed to load hardware setup:', e);
    showToast('Failed to load hardware setup.', 'error');
    state = clone(DEFAULT_STATE);
  }
  render();
}

function normalizeConfig(cfg: any): HwState {
  return {
    enabled: !!cfg.enabled,
    controller: { kind: cfg.controller?.kind || 'ch347', device_port: cfg.controller?.device_port ?? null },
    chips: (cfg.chips || []).map((c: any): ChipCfg => ({
      chip_id: String(c.chip_id),
      kind: String(c.kind),
      bus: String(c.bus || catalogChip(String(c.kind))?.buses?.[0] || 'i2c'),
      i2c_address: c.i2c_address ?? null,
      chain_length: Number(c.chain_length) || 1,
      spi_select: c.spi_select ?? null,
      label: c.label || '',
    })),
  };
}

// --- payload: controller + chips only (binding maps stay untouched) --------
function payload(): any {
  return {
    enabled: state.enabled,
    controller: { kind: state.controller.kind, device_port: state.controller.device_port },
    chips: state.chips.map((c) => ({
      chip_id: c.chip_id, kind: c.kind, bus: c.bus, i2c_address: c.i2c_address,
      chain_length: c.chain_length, spi_select: c.spi_select, label: c.label,
    })),
  };
}

// --- readiness -------------------------------------------------------------
interface Check { key: string; ok: boolean; label: string; }
function readiness(): Check[] {
  const checks: Check[] = [];
  checks.push({ key: 'bridge', ok: controllerReachable, label: controllerReachable ? 'Bridge connected' : 'Connect the bridge' });

  const noCoord = state.chips.filter((c) => (c.bus === 'i2c' ? c.i2c_address == null : !c.spi_select));
  checks.push({
    key: 'coord', ok: state.chips.length > 0 && noCoord.length === 0,
    label: noCoord.length ? `Set ${noCoord.length} address/latch` : state.chips.length ? 'Addresses & latches set' : 'Add a chip',
  });

  const unproven = state.chips.filter((c) => !selfTest[c.chip_id]?.reachable);
  checks.push({
    key: 'test', ok: state.chips.length > 0 && unproven.length === 0,
    label: unproven.length ? `${unproven.length} chip${unproven.length > 1 ? 's' : ''} unproven` : 'Every port responds',
  });
  return checks;
}
const isReady = (): boolean => state.chips.length > 0 && readiness().every((c) => c.ok);

// --- render ----------------------------------------------------------------
function render(): void {
  const body = el('hw-body');
  const gate = el('hw-gate');
  if (!body || !gate || !catalog) return;
  const scan = `<datalist id="hw-i2c-scan">${i2cScan.map((a) => `<option value="${esc(a)}"></option>`).join('')}</datalist>`;
  body.innerHTML = controllerStageHtml() + chipsStageHtml() + scan;
  gate.innerHTML = gateHtml();
}

function controllerStageHtml(): string {
  const ctrl = catalog!.controllers.find((c) => c.kind === state.controller.kind) || catalog!.controllers[0];
  const portRow = ctrl?.needs_device_port
    ? `<div class="hw-row"><span class="hw-lbl">Port</span>
        <input type="text" class="setting-input-compact" data-hw="device-port"
          placeholder="auto (USB VID/PID)" value="${esc(state.controller.device_port ?? '')}" spellcheck="false" /></div>`
    : '';
  const conn = controllerReachable
    ? `<div class="hw-conn ok"><span class="hw-conn-dot"></span>
        <span><strong>Connected.</strong> ${i2cScan.length} device${i2cScan.length === 1 ? '' : 's'} on the I²C bus.</span>
        <span class="hw-scan-chips">${i2cScan.map((a) => `<span class="hw-addr-chip">${esc(a)}</span>`).join('')}</span></div>`
    : `<div class="hw-conn"><span class="hw-conn-dot"></span>
        <span>Not connected — running in <strong>simulation</strong>. Detect to talk to the board.</span></div>`;
  const done = controllerReachable ? ' done' : '';
  return `<section class="hw-stage${done}">
    <div class="hw-stage-head"><span class="hw-stage-num">${controllerReachable ? '✓' : '1'}</span>
      <span class="hw-stage-title">Connect the bridge</span></div>
    <p class="hw-stage-desc">The USB board every message travels through. Detect scans the I²C bus for chips.</p>
    <div class="hw-row hw-indent">
      <span class="hw-lbl">Bridge</span>
      <select class="setting-select" data-hw="controller-kind">
        ${catalog!.controllers.map((c) => `<option value="${esc(c.kind)}"${c.kind === state.controller.kind ? ' selected' : ''}>${esc(c.label)}</option>`).join('')}
      </select>
      <button type="button" class="btn btn-accent hw-btn-sm" data-hw="detect"><i class="ti ti-plug-connected" aria-hidden="true"></i> ${controllerReachable ? 'Re-detect' : 'Detect controller'}</button>
    </div>
    ${portRow ? `<div class="hw-indent">${portRow}</div>` : ''}
    <div class="hw-indent">${conn}</div>
  </section>`;
}

function chipsStageHtml(): string {
  const proven = state.chips.length > 0 && readiness()[1].ok && readiness()[2].ok;
  const groups = BUS_ORDER.map(busGroupHtml).join('');
  return `<section class="hw-stage${proven ? ' done' : ''}">
    <div class="hw-stage-head"><span class="hw-stage-num">${proven ? '✓' : '2'}</span>
      <span class="hw-stage-title"><i class="ti ti-cpu" aria-hidden="true"></i> Your chips, by bus</span></div>
    <p class="hw-stage-desc">Every chip on the board — grouped by the bus it lives on, ordered by its address or latch line. Prove each one's ports respond.</p>
    <div class="hw-indent">${groups}${addFormHtml()}</div>
  </section>`;
}

function busGroupHtml(bus: string): string {
  const chips = state.chips.filter((c) => c.bus === bus).sort((a, b) => coordSort(a) - coordSort(b));
  const cards = chips.length ? chips.map(chipCardHtml).join('') : `<div class="hw-bus-empty">No chips on this bus yet.</div>`;
  return `<div class="hw-bus" data-bus="${bus}">
    <div class="hw-bus-head"><span class="hw-bus-badge hw-bus-${bus}">${BUS_LABEL[bus]}</span>
      <span class="hw-bus-order">${BUS_ORDERED_BY[bus]}</span></div>
    ${cards}</div>`;
}

function chipCardHtml(chip: ChipCfg): string {
  const ck = catalogChip(chip.kind);
  const dir = chipDirection(chip);
  const st = selfTest[chip.chip_id];
  const stateCls = st ? (st.reachable ? 'alive' : 'noresp') : 'untested';
  const badge = st
    ? (st.reachable
      ? `<span class="hw-state-badge alive"><i class="ti ti-check" aria-hidden="true"></i> Responds</span>`
      : `<span class="hw-state-badge noresp"><i class="ti ti-alert-triangle" aria-hidden="true"></i> No response</span>`)
    : `<span class="hw-state-badge untested">Untested</span>`;
  const dirBadge = `<span class="hw-dir ${dir === 'output' ? 'out' : 'in'}">${dir === 'output' ? 'OUTPUT' : 'INPUT'}</span>`;
  const chained = ck?.cascadable ? ` (${Math.max(1, chip.chain_length)} chip${chip.chain_length > 1 ? 's' : ''} chained)` : '';
  const on = activity?.chipId === chip.chip_id;
  const testBtn = dir === 'output'
    ? `<button type="button" class="hw-chip-action${on ? ' on' : ''}" data-hw="sweep" data-id="${esc(chip.chip_id)}"><i class="ti ti-${on ? 'player-stop' : 'player-play'}" aria-hidden="true"></i> ${on ? 'Stop' : 'Sweep'}</button>`
    : `<button type="button" class="hw-chip-action${on ? ' on' : ''}" data-hw="live" data-id="${esc(chip.chip_id)}"><i class="ti ti-${on ? 'player-stop' : 'activity'}" aria-hidden="true"></i> ${on ? 'Stop' : 'Read live'}</button>`;
  return `<div class="hw-chip ${stateCls}" data-chip="${esc(chip.chip_id)}">
    <div class="hw-chip-top">
      ${coordCellHtml(chip)}
      <div class="hw-chip-id">
        <span class="hw-chip-name">${esc(chip.kind)} ${dirBadge}</span>
        <span class="hw-chip-meta">${esc(chipLabel(chip))} · ${chipPorts(chip)} ports${chained}</span>
      </div>
      <div class="hw-chip-actions">
        ${badge}
        <button type="button" class="hw-chip-action" data-hw="self-test" data-id="${esc(chip.chip_id)}"><i class="ti ti-test-pipe" aria-hidden="true"></i> Self-test</button>
        ${testBtn}
        <button type="button" class="hw-chip-remove" data-hw="remove" data-id="${esc(chip.chip_id)}" title="Remove chip" aria-label="Remove chip">&times;</button>
      </div>
    </div>
    ${chip.bus !== 'i2c' && ck?.cascadable ? portCountRowHtml(chip) : ''}
    ${portsHtml(chip)}
  </div>`;
}

function coordCellHtml(chip: ChipCfg): string {
  if (chip.bus === 'i2c') {
    const val = chip.i2c_address != null ? '0x' + chip.i2c_address.toString(16) : '';
    return `<span class="hw-coord">
      <input type="text" class="hw-coord-input" data-hw="addr" data-id="${esc(chip.chip_id)}"
        list="hw-i2c-scan" placeholder="0x40" value="${esc(val)}" spellcheck="false" title="I²C address" />
    </span>`;
  }
  const lines = latchLines(chip.bus);
  return `<span class="hw-coord"><select class="hw-coord-select" data-hw="latch" data-id="${esc(chip.chip_id)}" title="Latch line">
    ${lines.map((l) => `<option value="${esc(l)}"${l === chip.spi_select ? ' selected' : ''}>${esc(l.toUpperCase())}</option>`).join('')}
  </select></span>`;
}

function portCountRowHtml(chip: ChipCfg): string {
  const ck = catalogChip(chip.kind)!;
  return `<div class="hw-portcount">
    <span class="hw-lbl">Ports</span>
    <input type="number" class="hw-portcount-input" data-hw="ports" data-id="${esc(chip.chip_id)}"
      min="${ck.ports_per_unit}" step="${ck.ports_per_unit}" value="${chipPorts(chip)}" />
    <span class="hw-hint-inline">${ck.ports_per_unit} per chip</span>
  </div>`;
}

function portsHtml(chip: ChipCfg): string {
  const ck = catalogChip(chip.kind);
  const n = chipPorts(chip);
  const dir = chipDirection(chip);
  const current = isCurrentSense(chip);
  let legend: string;
  if (ck?.port_kind === 'bipolar_pair') legend = `${n} bipolar ports · ±12V`;
  else if (ck?.port_kind === 'digital_out') legend = `${n} output bits · 0/1`;
  else if (ck?.port_kind === 'digital_in') legend = `${n} input bits`;
  else legend = `${n} current channels · mA`;
  const hint = dir === 'output' ? 'click a port to pulse it' : 'Read live to watch these';

  const act = activity?.chipId === chip.chip_id ? activity : null;
  const pills: string[] = [];
  for (let p = 0; p < n; p++) {
    const name = current ? 'CH' + (p + 1) : 'P' + p;
    let cls = 'hw-port', txt = name, extra = '';
    if (act?.kind === 'sweep' && act.port === p) cls += ' sweep';
    if (act?.kind === 'live') {
      const v = act.values[p];
      if (current) { cls += ' wide'; txt = v != null ? Math.round(v) + 'mA' : name; if (v > 30) cls += ' live'; }
      else if (v) cls += ' live';
    }
    if (dir === 'output') {
      pills.push(`<button type="button" class="${cls}${extra}" data-hw="pulse" data-id="${esc(chip.chip_id)}" data-port="${p}" title="${name} — click to pulse">${txt}</button>`);
    } else {
      pills.push(`<span class="${cls}${extra}" title="${name}">${txt}</span>`);
    }
  }
  return `<div class="hw-ports">
    <div class="hw-ports-legend"><span>${legend}</span><span class="hw-test-hint">· ${hint}</span></div>
    <div class="hw-port-grid">${pills.join('')}</div>
  </div>`;
}

// --- add-chip form ---------------------------------------------------------
function addFormHtml(): string {
  if (!adding) {
    return `<button type="button" class="btn btn-secondary hw-add-open" data-hw="add-open"><i class="ti ti-plus" aria-hidden="true"></i> Add a chip</button>`;
  }
  const kinds = catalog!.chips;
  const typeOpts = kinds.map((k) => `<option value="${esc(k.kind)}">${esc(k.label)}</option>`).join('');
  return `<div class="hw-add-form" id="hw-add-form">
    <div class="hw-add-fld"><span class="hw-lbl">Chip</span>
      <select class="setting-select" id="hw-af-type">${typeOpts}</select></div>
    <div class="hw-add-fld"><span class="hw-lbl">Bus</span>
      <select class="setting-select" id="hw-af-bus"></select></div>
    <div class="hw-add-fld"><span class="hw-lbl" id="hw-af-coord-lbl">Address</span>
      <span id="hw-af-coord"></span></div>
    <div class="hw-add-fld"><span class="hw-lbl">Ports</span>
      <input type="number" class="hw-portcount-input" id="hw-af-ports" min="1" value="8" /></div>
    <div class="hw-add-fld hw-add-actions">
      <button type="button" class="btn btn-accent hw-btn-sm" data-hw="add-commit">Add chip</button>
      <button type="button" class="btn btn-secondary hw-btn-sm" data-hw="add-cancel">Cancel</button>
    </div>
  </div>`;
}

function syncAddForm(): void {
  const typeSel = el<HTMLSelectElement>('hw-af-type');
  const busSel = el<HTMLSelectElement>('hw-af-bus');
  const coordLbl = el('hw-af-coord-lbl');
  const coord = el('hw-af-coord');
  const ports = el<HTMLInputElement>('hw-af-ports');
  if (!typeSel || !busSel || !coordLbl || !coord || !ports) return;
  const ck = catalogChip(typeSel.value);
  if (!ck) return;
  busSel.innerHTML = ck.buses.map((b) => `<option value="${esc(b)}">${BUS_LABEL[b]}</option>`).join('');
  ports.value = String(ck.ports_per_unit);
  ports.step = String(ck.ports_per_unit);
  ports.disabled = !ck.cascadable;
  syncAddCoord();

  function syncAddCoord(): void {
    const bus = busSel!.value;
    if (bus === 'i2c') {
      coordLbl!.textContent = 'Address';
      const def = ck!.default_i2c_address != null ? '0x' + ck!.default_i2c_address.toString(16) : '';
      coord!.innerHTML = `<input type="text" class="hw-coord-input" id="hw-af-coord-val" list="hw-i2c-scan" placeholder="0x40" value="${esc(def)}" spellcheck="false" />`;
    } else {
      coordLbl!.textContent = 'Latch line';
      const lines = latchLines(bus);
      coord!.innerHTML = `<select class="setting-select" id="hw-af-coord-val">${lines.map((l) => `<option value="${esc(l)}">${esc(l.toUpperCase())}</option>`).join('')}</select>`;
    }
  }
  typeSel.onchange = syncAddForm;
  busSel.onchange = syncAddCoord;
}

function commitAdd(): void {
  const typeSel = el<HTMLSelectElement>('hw-af-type');
  const busSel = el<HTMLSelectElement>('hw-af-bus');
  const portsEl = el<HTMLInputElement>('hw-af-ports');
  const coordVal = el<HTMLInputElement>('hw-af-coord-val');
  if (!typeSel || !busSel || !portsEl) return;
  const ck = catalogChip(typeSel.value);
  if (!ck) return;
  const bus = busSel.value;
  const chip: ChipCfg = {
    chip_id: nextChipId(typeSel.value), kind: typeSel.value, bus,
    i2c_address: null, chain_length: 1, spi_select: null, label: '',
  };
  if (bus === 'i2c') {
    const raw = (coordVal?.value || '').trim();
    const addr = raw.toLowerCase().startsWith('0x') ? parseInt(raw, 16) : parseInt(raw, 10);
    if (!Number.isFinite(addr)) { showToast('Enter an I²C address (e.g. 0x40).', 'error'); return; }
    chip.i2c_address = addr;
  } else {
    chip.spi_select = coordVal?.value || latchLines(bus)[0];
  }
  if (ck.cascadable) {
    const ports = Math.max(ck.ports_per_unit, parseInt(portsEl.value, 10) || ck.ports_per_unit);
    chip.chain_length = Math.max(1, Math.round(ports / ck.ports_per_unit));
  }
  state.chips.push(chip);
  adding = false;
  onEdit();
  render();
  showToast('Chip added.', 'success', 1800);
}

function nextChipId(kind: string): string {
  const existing = new Set(state.chips.map((c) => c.chip_id));
  let n = 1;
  while (existing.has(`${kind}-${n}`)) n++;
  return `${kind}-${n}`;
}

// --- footer gate -----------------------------------------------------------
function gateHtml(): string {
  const checks = readiness();
  const ready = isReady();
  const pills = checks.map((c) =>
    `<span class="hw-check ${c.ok ? 'ok' : 'warn'}"${c.ok ? '' : ` data-hw="jump" data-key="${c.key}"`}>` +
    `<i class="ti ti-${c.ok ? 'check' : 'alert-triangle'}" aria-hidden="true"></i> ${esc(c.label)}</span>`).join('');
  const effEnabled = state.enabled && ready;
  const enableTitle = effEnabled ? 'Hardware enabled' : ready ? 'Enable hardware' : 'Simulation only';
  const enableSub = ready ? 'applies after restart' : 'clear the amber steps first';
  const restart = savedSinceEdit
    ? `<span class="hw-restart-note">Applies after a restart.</span>
       <button type="button" class="btn btn-primary" data-hw="restart">Restart now</button>`
    : `<button type="button" class="btn ${effEnabled ? 'btn-accent' : 'btn-secondary'}" data-hw="save">Save</button>`;
  return `<div class="hw-checklist">${pills}</div>
    <div class="hw-gate-actions">
      <label class="hw-enable ${effEnabled ? 'on' : ''} ${ready ? '' : 'locked'}" data-hw="enable">
        <span class="hw-enable-text"><span class="hw-enable-label">${enableTitle}</span>
          <span class="hw-enable-sub">${enableSub}</span></span>
        <span class="hw-toggle"></span>
      </label>
      ${restart}
    </div>`;
}

// --- event handlers --------------------------------------------------------
async function onClick(e: Event): Promise<void> {
  const target = (e.target as HTMLElement).closest<HTMLElement>('[data-hw]');
  if (!target) return;
  const act = target.dataset.hw;
  const id = target.dataset.id || '';
  switch (act) {
    case 'detect': return void detect();
    case 'self-test': return void selfTestChip(id);
    case 'remove': return removeChip(id);
    case 'sweep': return void toggleSweep(id);
    case 'live': return void toggleLive(id);
    case 'pulse': return void pulsePort(id, Number(target.dataset.port));
    case 'add-open': adding = true; render(); syncAddForm(); return;
    case 'add-cancel': adding = false; render(); return;
    case 'add-commit': return commitAdd();
    case 'enable': return toggleEnable();
    case 'save': return void save();
    case 'restart': return void restart();
    case 'jump': return jumpTo(target.dataset.key || '');
  }
}

function onChange(e: Event): void {
  const t = (e.target as HTMLElement).closest<HTMLElement>('[data-hw]');
  if (!t) return;
  const id = t.dataset.id || '';
  if (t.dataset.hw === 'controller-kind') { state.controller.kind = (t as HTMLSelectElement).value; onEdit(); render(); }
  else if (t.dataset.hw === 'latch') { setChip(id, (c) => { c.spi_select = (t as HTMLSelectElement).value; }); }
  else if (t.dataset.hw === 'addr') { setAddress(id, (t as HTMLInputElement).value); }
  else if (t.dataset.hw === 'ports') { setPorts(id, (t as HTMLInputElement).value); }
}

function onInput(e: Event): void {
  const t = (e.target as HTMLElement).closest<HTMLElement>('[data-hw="device-port"]');
  if (t) { state.controller.device_port = (t as HTMLInputElement).value.trim() || null; onEdit(); }
}

function setChip(id: string, mut: (c: ChipCfg) => void): void {
  const chip = state.chips.find((c) => c.chip_id === id);
  if (!chip) return;
  mut(chip);
  delete selfTest[id];
  onEdit();
  render();
}
function setAddress(id: string, raw: string): void {
  const v = raw.trim();
  const addr = v ? (v.toLowerCase().startsWith('0x') ? parseInt(v, 16) : parseInt(v, 10)) : NaN;
  setChip(id, (c) => { c.i2c_address = Number.isFinite(addr) ? addr : null; });
}
function setPorts(id: string, raw: string): void {
  const chip = state.chips.find((c) => c.chip_id === id);
  const ck = chip && catalogChip(chip.kind);
  if (!chip || !ck) return;
  const ports = Math.max(ck.ports_per_unit, parseInt(raw, 10) || ck.ports_per_unit);
  setChip(id, (c) => { c.chain_length = Math.max(1, Math.round(ports / ck.ports_per_unit)); });
}

function removeChip(id: string): void {
  if (activity?.chipId === id) void stopCommissioning();
  state.chips = state.chips.filter((c) => c.chip_id !== id);
  delete selfTest[id];
  onEdit();
  render();
}

function toggleEnable(): void {
  if (!isReady()) { showToast('Finish the amber steps to enable hardware.', 'warning', 3600); return; }
  state.enabled = !state.enabled;
  onEdit();
  render();
}

function jumpTo(key: string): void {
  const sel = key === 'bridge' ? '.hw-stage' : '.hw-bus';
  el('hw-body')?.querySelector(sel)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// --- detect / self-test ----------------------------------------------------
async function detect(): Promise<void> {
  const btn = document.querySelector<HTMLButtonElement>('[data-hw="detect"]');
  if (btn) btn.disabled = true;
  try {
    const res = await api.runHardwareSelfTest(payload());
    controllerReachable = !!res?.ok && !!res?.controller?.reachable;
    i2cScan = res?.controller?.i2c_scan || [];
    selfTest = {};
    for (const c of res?.chips || []) selfTest[c.chip_id] = { reachable: !!c.reachable, ports: c.ports || [] };
    render();
    if (!controllerReachable) showToast(res?.error ? `Controller not reachable: ${res.error}` : 'Controller not reachable.', 'error', 4000);
    else {
      const reachable = Object.values(selfTest).filter((r) => r.reachable).length;
      showToast(`Connected — ${reachable}/${state.chips.length} chips respond.`, 'success', 2500);
    }
  } catch (e: any) {
    showToast(e?.message || 'Detect failed.', 'error', 4000);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function selfTestChip(id: string): Promise<void> {
  const chip = state.chips.find((c) => c.chip_id === id);
  if (!chip) return;
  try {
    const res = await api.runHardwareSelfTest(payload(), id);
    if (!res?.ok) { showToast(res?.error ? `Controller not reachable: ${res.error}` : 'Controller not reachable.', 'error', 4000); return; }
    controllerReachable = !!res?.controller?.reachable;
    if (res.controller?.i2c_scan) i2cScan = res.controller.i2c_scan;
    const c = (res.chips || []).find((x: any) => x.chip_id === id);
    if (c) selfTest[id] = { reachable: !!c.reachable, ports: c.ports || [] };
    render();
    showToast(c?.reachable ? `${chip.kind} · all ${chipPorts(chip)} ports respond.` : `${chip.kind} did not respond.`,
      c?.reachable ? 'success' : 'error', c?.reachable ? 2200 : 3500);
  } catch (e: any) {
    showToast(e?.message || 'Self-test failed.', 'error', 4000);
  }
}

async function pulsePort(id: string, port: number): Promise<void> {
  if (activity) return;   // don't pulse while a sweep/live session holds the device
  try {
    const res = await api.runHardwarePortTest(payload(), id, port, 'pulse_output');
    if (!res?.ok) { showToast(res?.error || 'Port test failed.', 'error', 3500); return; }
    showToast(`Pulsed port ${port}.`, 'success', 1600);
  } catch (e: any) {
    showToast(e?.message || 'Port test failed.', 'error', 3500);
  }
}

// --- commissioning: sweep (output) + read-live (input) ---------------------
async function ensureCommission(): Promise<boolean> {
  if (commissionActive) return true;
  try {
    const res = await api.commissionOpen(payload());
    if (!res?.ok) { showToast(res?.error ? `Controller unavailable: ${res.error}` : 'Controller unavailable.', 'error', 4000); return false; }
  } catch (e: any) {
    showToast(e?.message || 'Controller unavailable.', 'error', 4000);
    return false;
  }
  commissionActive = true;
  return true;
}

async function stopCommissioning(): Promise<void> {
  sweepAbort = true;
  if (activityTimer) { clearInterval(activityTimer); activityTimer = null; }
  activity = null;
  if (commissionActive) {
    commissionActive = false;
    try { await api.commissionClose(); } catch { /* best effort */ }
  }
}

async function toggleSweep(id: string): Promise<void> {
  if (activity?.chipId === id) { await stopCommissioning(); render(); return; }
  await stopCommissioning();
  if (!(await ensureCommission())) return;
  activity = { chipId: id, kind: 'sweep', port: 0, values: [] };
  sweepAbort = false;
  render();
  void runSweep(id);
}

async function runSweep(id: string): Promise<void> {
  const chip = state.chips.find((c) => c.chip_id === id);
  if (!chip) return;
  const count = chipPorts(chip);
  const duties = chip.bus === 'i2c' ? [0.3, 0.6, 1.0] : [1.0];   // PWM steps vs on/off
  let pass = 0;
  while (!sweepAbort && activity?.chipId === id) {
    const duty = duties[pass % duties.length];
    pass++;
    for (let p = 0; p < count && !sweepAbort && activity?.chipId === id; p++) {
      try { await api.commissionOutput(id, p, duty); } catch { /* ignore */ }
      if (activity) { activity.port = p; render(); }
      await sleep(450);
      try { await api.commissionOutput(id, p, 0); } catch { /* ignore */ }
    }
  }
}

async function toggleLive(id: string): Promise<void> {
  if (activity?.chipId === id) { await stopCommissioning(); render(); return; }
  await stopCommissioning();
  if (!(await ensureCommission())) return;
  const chip = state.chips.find((c) => c.chip_id === id);
  const current = chip ? isCurrentSense(chip) : false;
  activity = { chipId: id, kind: 'live', port: -1, values: [] };
  render();
  const poll = async () => {
    if (activity?.chipId !== id) return;
    try {
      if (current) {
        const res = await api.commissionCurrents(id);
        if (res?.ok && activity) { activity.values = (res.currents || []).map((p: any) => Number(p.current_ma) || 0); render(); }
      } else {
        const res = await api.commissionInputs(id);
        if (res?.ok && activity) { activity.values = (res.ports || []).map((p: any) => (p.value ? 1 : 0)); render(); }
      }
    } catch { /* transient */ }
  };
  await poll();
  activityTimer = setInterval(poll, 500);
}

// --- save / restart --------------------------------------------------------
async function save(): Promise<void> {
  const btn = document.querySelector<HTMLButtonElement>('[data-hw="save"]');
  if (btn) btn.disabled = true;
  try {
    if (state.enabled && !isReady()) { state.enabled = false; render(); showToast('Hardware stayed off — finish the checklist.', 'warning', 4000); return; }
    await api.setHardwareConfig(payload());
    savedSinceEdit = true;
    render();
    showToast('Hardware setup saved — restart to apply.', 'success', 2500);
  } catch (e: any) {
    showToast(e?.message || 'Failed to save hardware setup.', 'error', 4000);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function restart(): Promise<void> {
  try { await api.restartServer(); } catch (e) {
    console.error('[App] Hardware restart request failed:', e);
    showToast('Could not reach server to restart.', 'error', 4000);
    return;
  }
  startRestartReconnectFlow();
}
