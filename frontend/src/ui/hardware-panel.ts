/**
 * Hardware Setup modal (#hardware-modal).
 *
 * Three modular sections (design_new.md "Power -> Hardware configuration"):
 *   - Controller: the transport bridge (CH347) + optional device port.
 *   - Output: the chips that drive track power (PCA9685) and switches (74HC595).
 *   - Input: the chips that read occupancy (74HC165).
 * Each chip is added from a dropdown with its parameters (I2C address, or SPI
 * select line + cascade length). A self-test probes every chip; the ports that
 * respond become the pool the Connections tables bind to blocks and switches.
 *
 * The selection is persisted server-side and applies on the next session build,
 * so a save is followed by an offer to restart (the shared restart/reconnect
 * flow). setupHardwarePanel() wires the fixed controls once; refreshHardwarePanel()
 * loads the catalog + persisted config + the layout's blocks/switches and
 * renders — call it each time the modal opens.
 */
import { api } from '../lib/api';
import { showToast } from '../lib/toast';
import { startRestartReconnectFlow } from './restart-flow';

const ROLE_TRACK = 'track_output';
const ROLE_SWITCH = 'switch_output';
const ROLE_OCCUPANCY = 'occupancy_input';
const OUTPUT_ROLES = [ROLE_TRACK, ROLE_SWITCH];

interface Binding { chip_id: string; port: number; }
interface ChipCfg {
  chip_id: string;
  kind: string;
  i2c_address: number | null;
  chain_length: number;
  spi_select: string | null;
  label: string;
}
interface HwState {
  enabled: boolean;
  controller: { kind: string; device_port: string | null };
  chips: ChipCfg[];
  block_track: Record<string, Binding>;
  block_occupancy: Record<string, Binding>;
  switch_output: Record<string, Binding>;
}
interface CatalogChip {
  kind: string; label: string; bus: string; role: string;
  ports_per_unit: number; cascadable: boolean;
  needs_i2c_address: boolean; needs_spi_select: boolean;
  port_kind: string; default_i2c_address: number | null;
  default_spi_select: string | null; port_help: string;
}
interface Catalog {
  controllers: { kind: string; label: string; needs_device_port: boolean }[];
  chips: CatalogChip[];
  spi_select_lines: string[];
}
interface PortResult { port: number; available: boolean; }
type SelfTest = Record<string, { reachable: boolean; ports: PortResult[] }>;

const DEFAULT_STATE: HwState = {
  enabled: false,
  controller: { kind: 'ch347', device_port: null },
  chips: [],
  block_track: {},
  block_occupancy: {},
  switch_output: {},
};

let state: HwState = clone(DEFAULT_STATE);
let catalog: Catalog | null = null;
let blocks: { id: string; name: string }[] = [];
let switches: { id: string; name: string }[] = [];
let selfTest: SelfTest = {};
let i2cScan: string[] = [];
let savedSinceEdit = false;

// Commissioning session — a held device for the live input view and the output
// marquee. Only one activity at a time; stopped on modal close / refresh.
let commissionActive = false;
let liveChipId: string | null = null;
let liveValues: boolean[] = [];
let liveTimer: any = null;
let sweepChipId: string | null = null;
let sweepActivePort = -1;
let sweepAbort = false;

function clone<T>(v: T): T { return JSON.parse(JSON.stringify(v)); }
function esc(s: any): string {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' } as any)[c]);
}
function catalogChip(kind: string): CatalogChip | undefined {
  return catalog?.chips.find((c) => c.kind === kind);
}
function chipRole(chip: ChipCfg): string | undefined { return catalogChip(chip.kind)?.role; }
function chipPortCount(chip: ChipCfg): number {
  const ck = catalogChip(chip.kind);
  if (!ck) return 0;
  return ck.ports_per_unit * (ck.cascadable ? Math.max(1, chip.chain_length) : 1);
}
function chipLabel(chip: ChipCfg): string {
  return chip.label || catalogChip(chip.kind)?.label || chip.kind;
}
function chipParamSummary(chip: ChipCfg): string {
  const ck = catalogChip(chip.kind);
  if (ck?.needs_i2c_address) {
    return `${ck.bus} @ 0x${(chip.i2c_address ?? 0).toString(16)} · ${chipPortCount(chip)} ports`;
  }
  if (ck?.needs_spi_select) {
    return `${ck.bus} ${chip.spi_select} ×${chip.chain_length} · ${chipPortCount(chip)} ports`;
  }
  return `${chipPortCount(chip)} ports`;
}

// --- element access --------------------------------------------------------
function el<T extends HTMLElement = HTMLElement>(id: string): T | null {
  return document.getElementById(id) as T | null;
}

// --- setup (once) ----------------------------------------------------------
export function setupHardwarePanel(): void {
  const enabled = el<HTMLInputElement>('hw-enabled');
  const ctrlKind = el<HTMLSelectElement>('hw-controller-kind');
  const ctrlPort = el<HTMLInputElement>('hw-controller-port');
  const selfTestBtn = el('hw-selftest-all');
  const saveBtn = el<HTMLButtonElement>('hw-save-btn');
  const restartBtn = el('hw-restart-btn');
  const outputChips = el('hw-output-chips');
  const inputChips = el('hw-input-chips');
  const bindings = el('hw-bindings');
  if (!enabled || !ctrlKind || !ctrlPort || !selfTestBtn || !saveBtn ||
      !restartBtn || !outputChips || !inputChips || !bindings) {
    console.warn('[App] Hardware panel elements not found');
    return;
  }

  enabled.addEventListener('change', () => { state.enabled = enabled.checked; onEdit(); render(); });
  ctrlKind.addEventListener('change', () => { state.controller.kind = ctrlKind.value; onEdit(); render(); });
  ctrlPort.addEventListener('input', () => {
    state.controller.device_port = ctrlPort.value.trim() || null; onEdit();
  });
  selfTestBtn.addEventListener('click', () => void runSelfTest());
  saveBtn.addEventListener('click', () => void save());
  restartBtn.addEventListener('click', async () => {
    try { await api.restartServer(); } catch (e) {
      console.error('[App] Hardware restart request failed:', e);
      showToast('Could not reach server to restart.', 'error', 4000);
      return;
    }
    startRestartReconnectFlow();
  });

  // Delegated: chip removal + port testing on the two chip lists.
  for (const list of [outputChips, inputChips]) {
    list.addEventListener('click', (e) => {
      const target = e.target as HTMLElement;
      const chipEl = target.closest<HTMLElement>('[data-chip-id]');
      if (!chipEl) return;
      const chipId = chipEl.dataset.chipId!;
      if (target.closest('[data-hw-action="remove-chip"]')) {
        removeChip(chipId);
      } else if (target.closest('[data-hw-action="live"]')) {
        void toggleLive(chipId);
      } else if (target.closest('[data-hw-action="sweep"]')) {
        void toggleSweep(chipId);
      } else {
        const pill = target.closest<HTMLElement>('[data-hw-action="test-port"]');
        if (pill) void testPort(chipId, Number(pill.dataset.port));
      }
    });
  }

  // Delegated: binding dropdown changes.
  bindings.addEventListener('change', (e) => {
    const sel = (e.target as HTMLElement).closest<HTMLSelectElement>('select[data-hw-bind]');
    if (!sel) return;
    applyBinding(sel.dataset.hwBind as keyof HwState, sel.dataset.hwId!, sel.value);
  });

  // Stop any live/sweep activity (and release the device) when the modal closes,
  // via any path — close button, backdrop, or Escape all drop the `active` class.
  const modal = document.getElementById('hardware-modal');
  if (modal) {
    new MutationObserver(() => {
      if (!modal.classList.contains('active')) void stopCommissioning();
    }).observe(modal, { attributes: true, attributeFilter: ['class'] });
  }
}

function onEdit(): void { savedSinceEdit = false; }

// --- refresh (each open) ---------------------------------------------------
export async function refreshHardwarePanel(): Promise<void> {
  await stopCommissioning();
  savedSinceEdit = false;
  selfTest = {};
  i2cScan = [];
  try {
    const [cat, cfg, blockList, switchList] = await Promise.all([
      api.getHardwareCatalog(),
      api.getHardwareConfig(),
      api.getBlocks().catch(() => []),
      api.getSwitches().catch(() => []),
    ]);
    catalog = cat as Catalog;
    state = normalizeConfig(cfg?.config || {});
    blocks = asIdList(blockList, 'block_id');
    switches = asIdList(switchList, 'switch_id');
  } catch (e) {
    console.error('[App] Failed to load hardware setup:', e);
    showToast('Failed to load hardware setup.', 'error');
    state = clone(DEFAULT_STATE);
  }
  buildControllerControls();
  buildAddForms();
  render();
}

function asIdList(raw: any, key: string): { id: string; name: string }[] {
  const arr = Array.isArray(raw) ? raw : raw?.blocks || raw?.switches || [];
  return arr
    .map((x: any) => ({ id: String(x?.[key] ?? ''), name: String(x?.name || x?.[key] || '') }))
    .filter((x: any) => x.id);
}

function normalizeConfig(cfg: any): HwState {
  return {
    enabled: !!cfg.enabled,
    controller: {
      kind: cfg.controller?.kind || 'ch347',
      device_port: cfg.controller?.device_port ?? null,
    },
    chips: (cfg.chips || []).map((c: any): ChipCfg => ({
      chip_id: String(c.chip_id),
      kind: String(c.kind),
      i2c_address: c.i2c_address ?? null,
      chain_length: Number(c.chain_length) || 1,
      spi_select: c.spi_select ?? null,
      label: c.label || '',
    })),
    block_track: cfg.block_track || {},
    block_occupancy: cfg.block_occupancy || {},
    switch_output: cfg.switch_output || {},
  };
}

// --- fixed controls --------------------------------------------------------
function buildControllerControls(): void {
  const sel = el<HTMLSelectElement>('hw-controller-kind');
  if (!sel || !catalog) return;
  sel.innerHTML = catalog.controllers
    .map((c) => `<option value="${esc(c.kind)}">${esc(c.label)}</option>`)
    .join('');
}

function buildAddForms(): void {
  const outputKinds = (catalog?.chips || []).filter((c) => OUTPUT_ROLES.includes(c.role));
  const inputKinds = (catalog?.chips || []).filter((c) => c.role === ROLE_OCCUPANCY);
  buildAddForm(el('hw-output-add'), outputKinds);
  buildAddForm(el('hw-input-add'), inputKinds);
}

function buildAddForm(container: HTMLElement | null, kinds: CatalogChip[]): void {
  if (!container) return;
  if (!kinds.length) { container.innerHTML = ''; return; }
  container.innerHTML = `
    <select class="setting-select hw-add-kind">
      ${kinds.map((k) => `<option value="${esc(k.kind)}">${esc(k.label)}</option>`).join('')}
    </select>
    <span class="hw-add-params"></span>
    <button type="button" class="btn btn-secondary hw-btn-sm hw-add-btn">Add</button>`;
  const kindSel = container.querySelector<HTMLSelectElement>('.hw-add-kind')!;
  const params = container.querySelector<HTMLElement>('.hw-add-params')!;
  const addBtn = container.querySelector<HTMLButtonElement>('.hw-add-btn')!;
  const renderParams = () => { params.innerHTML = paramFieldsHtml(catalogChip(kindSel.value)); };
  kindSel.addEventListener('change', renderParams);
  addBtn.addEventListener('click', () => addChipFromForm(kindSel.value, params));
  renderParams();
}

function paramFieldsHtml(ck: CatalogChip | undefined): string {
  if (!ck) return '';
  if (ck.needs_i2c_address) {
    const def = ck.default_i2c_address != null ? '0x' + ck.default_i2c_address.toString(16) : '';
    return `<input class="setting-input-compact hw-param" data-param="i2c_address"
              list="hw-i2c-scan" placeholder="0x40" value="${esc(def)}" spellcheck="false" />
            <datalist id="hw-i2c-scan"></datalist>
            <button type="button" class="hw-linkbtn hw-scan-btn" title="Scan the I2C bus">scan</button>`;
  }
  if (ck.needs_spi_select) {
    const opts = (catalog?.spi_select_lines || [])
      .map((l) => `<option value="${esc(l)}"${l === ck.default_spi_select ? ' selected' : ''}>${esc(l.toUpperCase())}</option>`)
      .join('');
    return `<select class="setting-select hw-param" data-param="spi_select" title="Latch/load line, by CH347 pad name">${opts}</select>
            <input class="setting-input-compact hw-param hw-chain" data-param="chain_length" type="number" min="1" value="1" title="Cascaded chips" />`;
  }
  return '';
}

function addChipFromForm(kind: string, params: HTMLElement): void {
  const ck = catalogChip(kind);
  if (!ck) return;
  const read = (name: string) =>
    params.querySelector<HTMLInputElement>(`[data-param="${name}"]`)?.value?.trim() ?? '';
  const chip: ChipCfg = {
    chip_id: nextChipId(kind),
    kind,
    i2c_address: null,
    chain_length: 1,
    spi_select: null,
    label: '',
  };
  if (ck.needs_i2c_address) {
    const raw = read('i2c_address');
    const addr = raw.toLowerCase().startsWith('0x') ? parseInt(raw, 16) : parseInt(raw, 10);
    if (!Number.isFinite(addr)) { showToast('Enter an I2C address (e.g. 0x40).', 'error'); return; }
    chip.i2c_address = addr;
  }
  if (ck.needs_spi_select) {
    chip.spi_select = read('spi_select') || ck.default_spi_select;
    chip.chain_length = Math.max(1, parseInt(read('chain_length'), 10) || 1);
  }
  state.chips.push(chip);
  delete selfTest[chip.chip_id];
  onEdit();
  render();
  // Reset the numeric/address inputs to defaults for the next add.
  params.innerHTML = paramFieldsHtml(ck);
}

function nextChipId(kind: string): string {
  const existing = new Set(state.chips.map((c) => c.chip_id));
  let n = 1;
  while (existing.has(`${kind}-${n}`)) n++;
  return `${kind}-${n}`;
}

function removeChip(chipId: string): void {
  if (chipId === liveChipId || chipId === sweepChipId) void stopCommissioning();
  state.chips = state.chips.filter((c) => c.chip_id !== chipId);
  delete selfTest[chipId];
  // Drop any bindings that referenced the removed chip.
  for (const map of [state.block_track, state.block_occupancy, state.switch_output]) {
    for (const id of Object.keys(map)) if (map[id].chip_id === chipId) delete map[id];
  }
  onEdit();
  render();
}

// --- render ----------------------------------------------------------------
function render(): void {
  const enabled = el<HTMLInputElement>('hw-enabled');
  const ctrlKind = el<HTMLSelectElement>('hw-controller-kind');
  const ctrlPort = el<HTMLInputElement>('hw-controller-port');
  if (enabled) enabled.checked = state.enabled;
  if (ctrlKind) ctrlKind.value = state.controller.kind;
  if (ctrlPort) ctrlPort.value = state.controller.device_port ?? '';

  renderOutputChips();
  renderInputChips();
  renderBindings();
  renderSelfTestSummary();
  renderStatus();
  renderFooter();
}

function renderChipList(containerId: string, pred: (c: ChipCfg) => boolean): void {
  const container = el(containerId);
  if (!container) return;
  const chips = state.chips.filter(pred);
  if (!chips.length) {
    container.innerHTML = `<p class="hw-empty">No chips added yet.</p>`;
    return;
  }
  container.innerHTML = chips.map((chip) => `
    <div class="hw-chip" data-chip-id="${esc(chip.chip_id)}">
      <div class="hw-chip-head">
        <span class="hw-chip-name">${esc(chipLabel(chip))}</span>
        <span class="hw-chip-meta">${esc(chipParamSummary(chip))}</span>
        ${chipActionHtml(chip)}
        <button type="button" class="hw-chip-remove" data-hw-action="remove-chip"
                title="Remove chip" aria-label="Remove chip">&times;</button>
      </div>
      <div class="hw-chip-ports">${portsHtml(chip)}</div>
    </div>`).join('');
}

function chipActionHtml(chip: ChipCfg): string {
  if (chipRole(chip) === ROLE_OCCUPANCY) {
    const on = liveChipId === chip.chip_id;
    return `<button type="button" class="hw-chip-action${on ? ' on' : ''}" data-hw-action="live">${on ? 'Stop' : 'Live view'}</button>`;
  }
  const on = sweepChipId === chip.chip_id;
  return `<button type="button" class="hw-chip-action${on ? ' on' : ''}" data-hw-action="sweep">${on ? 'Stop' : '走马灯 sweep'}</button>`;
}

function portsHtml(chip: ChipCfg): string {
  const count = chipPortCount(chip);
  // Live input view — every input bit lit by its current value.
  if (liveChipId === chip.chip_id) {
    const pills: string[] = [];
    for (let p = 0; p < count; p++) {
      pills.push(`<span class="hw-port ${liveValues[p] ? 'ok' : 'off'}" title="Port ${p}">${p}</span>`);
    }
    return `<span class="hw-chip-badge hw-live">Live</span><div class="hw-port-grid">${pills.join('')}</div>`;
  }
  // Output marquee — highlight the port currently driven.
  if (sweepChipId === chip.chip_id) {
    const pills: string[] = [];
    for (let p = 0; p < count; p++) {
      pills.push(`<span class="hw-port ${p === sweepActivePort ? 'sweep' : 'off'}" title="Port ${p}">${p}</span>`);
    }
    return `<span class="hw-chip-badge hw-live">Sweeping…</span><div class="hw-port-grid">${pills.join('')}</div>`;
  }
  const r = selfTest[chip.chip_id];
  if (!r) return `<span class="hw-chip-untested">Not tested yet — run self-test.</span>`;
  if (!r.reachable) return `<span class="hw-chip-badge hw-bad">No response</span>`;
  const avail = new Map(r.ports.map((p) => [p.port, p.available]));
  const pills: string[] = [];
  for (let p = 0; p < count; p++) {
    const ok = avail.get(p) ?? false;
    pills.push(
      `<button type="button" class="hw-port ${ok ? 'ok' : 'off'}" data-hw-action="test-port"
        data-port="${p}" title="Port ${p} — click to test">${p}</button>`);
  }
  return `<span class="hw-chip-badge hw-ok">Reachable</span><div class="hw-port-grid">${pills.join('')}</div>`;
}

function renderBindings(): void {
  const container = el('hw-bindings');
  if (!container) return;
  const hasTrack = state.chips.some((c) => chipRole(c) === ROLE_TRACK);
  const hasOcc = state.chips.some((c) => chipRole(c) === ROLE_OCCUPANCY);
  const hasSw = state.chips.some((c) => chipRole(c) === ROLE_SWITCH);

  let html = '';
  if (blocks.length && (hasTrack || hasOcc)) {
    html += `<div class="hw-bind-group"><h5 class="hw-bind-title">Blocks</h5>
      <table class="hw-bind-table"><thead><tr><th>Block</th>
      <th>Occupancy</th><th>Track power</th></tr></thead><tbody>`;
    html += blocks.map((b) => `<tr><td class="hw-bind-name">${esc(b.name)}</td>
      <td>${portSelect('block_occupancy', b.id, ROLE_OCCUPANCY)}</td>
      <td>${portSelect('block_track', b.id, ROLE_TRACK)}</td></tr>`).join('');
    html += `</tbody></table></div>`;
  }
  if (switches.length && hasSw) {
    html += `<div class="hw-bind-group"><h5 class="hw-bind-title">Switches</h5>
      <table class="hw-bind-table"><thead><tr><th>Switch</th><th>Throw output</th></tr></thead><tbody>`;
    html += switches.map((s) => `<tr><td class="hw-bind-name">${esc(s.name)}</td>
      <td>${portSelect('switch_output', s.id, ROLE_SWITCH)}</td></tr>`).join('');
    html += `</tbody></table></div>`;
  }
  if (!html) {
    html = `<p class="hw-hint">Add ${blocks.length ? '' : 'blocks and '}chips above to connect ports here.</p>`;
  }
  container.innerHTML = html;
}

function portSelect(mapName: keyof HwState, logicalId: string, role: string): string {
  const chips = state.chips.filter((c) => chipRole(c) === role);
  if (!chips.length) return `<select class="hw-bind-select" disabled><option>— no chip —</option></select>`;
  const current = (state[mapName] as Record<string, Binding>)[logicalId];
  const options = [`<option value="">— none —</option>`];
  for (const chip of chips) {
    const r = selfTest[chip.chip_id];
    for (let p = 0; p < chipPortCount(chip); p++) {
      const val = `${chip.chip_id}::${p}`;
      const sel = current && current.chip_id === chip.chip_id && current.port === p ? ' selected' : '';
      let mark = '';
      if (r) mark = r.reachable && (r.ports.find((x) => x.port === p)?.available) ? '' : ' ✕';
      options.push(`<option value="${esc(val)}"${sel}>${esc(chipLabel(chip))} · P${p}${mark}</option>`);
    }
  }
  return `<select class="hw-bind-select" data-hw-bind="${esc(mapName)}" data-hw-id="${esc(logicalId)}">${options.join('')}</select>`;
}

function applyBinding(mapName: keyof HwState, logicalId: string, value: string): void {
  const map = state[mapName] as Record<string, Binding>;
  if (!value) {
    delete map[logicalId];
  } else {
    const [chip_id, port] = value.split('::');
    map[logicalId] = { chip_id, port: Number(port) };
  }
  onEdit();
  renderStatus();
  renderFooter();
}

function renderSelfTestSummary(): void {
  const summary = el('hw-selftest-summary');
  if (!summary) return;
  const ids = Object.keys(selfTest);
  if (!ids.length) { summary.hidden = true; summary.innerHTML = ''; return; }
  const reachable = ids.filter((id) => selfTest[id].reachable).length;
  const scan = i2cScan.length ? ` · I2C bus: ${i2cScan.join(', ')}` : '';
  summary.hidden = false;
  summary.innerHTML = `<i class="ti ti-checks" aria-hidden="true"></i> ${reachable}/${ids.length} chips reachable${esc(scan)}`;
}

function renderStatus(): void {
  const status = el('hw-status');
  if (!status) return;
  const real = state.enabled && state.chips.length > 0;
  status.classList.toggle('real', real);
  let msg: string;
  if (!state.enabled) {
    msg = '<strong>Simulation</strong> — full simulation, no hardware attached.';
  } else if (!state.chips.length) {
    msg = '<strong>Real mode on</strong> — add at least one chip to drive hardware.';
  } else {
    const n = state.chips.length;
    msg = `<strong>Real mode</strong> — ${n} chip${n === 1 ? '' : 's'} on ${esc(controllerLabel())}. Applies on restart.`;
  }
  status.innerHTML =
    `<i class="ti ${real ? 'ti-plug-connected' : 'ti-device-desktop'} hw-status-icon" aria-hidden="true"></i><span>${msg}</span>`;
}

function controllerLabel(): string {
  return catalog?.controllers.find((c) => c.kind === state.controller.kind)?.label || state.controller.kind;
}

function renderFooter(): void {
  const note = el('hw-restart-note');
  const saveBtn = el('hw-save-btn');
  const restartBtn = el('hw-restart-btn');
  if (note) note.hidden = !savedSinceEdit;
  if (saveBtn) saveBtn.hidden = savedSinceEdit;
  if (restartBtn) restartBtn.hidden = !savedSinceEdit;
}

// --- payload / actions -----------------------------------------------------
function payload(): any {
  return {
    enabled: state.enabled,
    controller: { kind: state.controller.kind, device_port: state.controller.device_port },
    chips: state.chips.map((c) => ({
      chip_id: c.chip_id, kind: c.kind, i2c_address: c.i2c_address,
      chain_length: c.chain_length, spi_select: c.spi_select, label: c.label,
    })),
    block_track: state.block_track,
    block_occupancy: state.block_occupancy,
    switch_output: state.switch_output,
  };
}

async function save(): Promise<void> {
  const saveBtn = el<HTMLButtonElement>('hw-save-btn');
  if (saveBtn) saveBtn.disabled = true;
  try {
    await api.setHardwareConfig(payload());
    savedSinceEdit = true;
    renderFooter();
    showToast('Hardware setup saved — restart to apply.', 'success', 2500);
  } catch (e: any) {
    console.error('[App] Failed to save hardware config:', e);
    showToast(e?.message || 'Failed to save hardware setup.', 'error', 4000);
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

async function runSelfTest(): Promise<void> {
  const btn = el<HTMLButtonElement>('hw-selftest-all');
  if (btn) { btn.disabled = true; btn.classList.add('is-busy'); }
  try {
    const res = await api.runHardwareSelfTest(payload());
    selfTest = {};
    for (const c of res?.chips || []) {
      selfTest[c.chip_id] = { reachable: !!c.reachable, ports: c.ports || [] };
    }
    i2cScan = res?.controller?.i2c_scan || [];
    fillScanDatalist();
    if (!res?.ok) {
      showToast(res?.error ? `Controller not reachable: ${res.error}` : 'Controller not reachable.', 'error', 4000);
    } else {
      const reachable = Object.values(selfTest).filter((r) => r.reachable).length;
      showToast(`Self-test done — ${reachable}/${Object.keys(selfTest).length} chips reachable.`, 'success', 2500);
    }
    render();
  } catch (e: any) {
    console.error('[App] Self-test failed:', e);
    showToast(e?.message || 'Self-test failed — check chip settings.', 'error', 4000);
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('is-busy'); }
  }
}

function fillScanDatalist(): void {
  const dl = document.getElementById('hw-i2c-scan');
  if (dl) dl.innerHTML = i2cScan.map((a) => `<option value="${esc(a)}"></option>`).join('');
}

async function testPort(chipId: string, port: number): Promise<void> {
  const chip = state.chips.find((c) => c.chip_id === chipId);
  if (!chip) return;
  const role = chipRole(chip);
  const action = role === ROLE_OCCUPANCY ? 'read_input' : 'pulse_output';
  try {
    const res = await api.runHardwarePortTest(payload(), chipId, port, action);
    if (!res?.ok) { showToast(res?.error || 'Port test failed.', 'error', 3500); return; }
    if (action === 'read_input') {
      showToast(`${chipLabel(chip)} port ${port}: ${res.value ? 'current present' : 'no current'}`, 'info', 2500);
    } else {
      showToast(`Pulsed ${chipLabel(chip)} port ${port}.`, 'success', 2000);
    }
  } catch (e: any) {
    showToast(e?.message || 'Port test failed.', 'error', 3500);
  }
}

// --- commissioning: live input view + output marquee (走马灯) ---------------
function renderOutputChips(): void {
  renderChipList('hw-output-chips', (c) => OUTPUT_ROLES.includes(chipRole(c) || ''));
}
function renderInputChips(): void {
  renderChipList('hw-input-chips', (c) => chipRole(c) === ROLE_OCCUPANCY);
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function ensureCommission(): Promise<boolean> {
  if (commissionActive) return true;
  try {
    const res = await api.commissionOpen(payload());
    if (!res?.ok) {
      showToast(res?.error ? `Controller unavailable: ${res.error}` : 'Controller unavailable.', 'error', 4000);
      return false;
    }
  } catch (e: any) {
    showToast(e?.message || 'Controller unavailable.', 'error', 4000);
    return false;
  }
  commissionActive = true;
  return true;
}

async function stopCommissioning(): Promise<void> {
  sweepAbort = true;
  sweepChipId = null;
  sweepActivePort = -1;
  if (liveTimer) {
    clearInterval(liveTimer);
    liveTimer = null;
  }
  liveChipId = null;
  liveValues = [];
  if (commissionActive) {
    commissionActive = false;
    try {
      await api.commissionClose();
    } catch {
      /* best effort */
    }
  }
}

async function toggleLive(chipId: string): Promise<void> {
  if (liveChipId === chipId) {
    await stopCommissioning();
    render();
    return;
  }
  await stopCommissioning();
  if (!(await ensureCommission())) return;
  liveChipId = chipId;
  render();
  const poll = async () => {
    if (liveChipId !== chipId) return;
    try {
      const res = await api.commissionInputs(chipId);
      if (res?.ok) {
        liveValues = (res.ports || []).map((p: any) => !!p.value);
        renderInputChips();
      }
    } catch {
      /* transient */
    }
  };
  await poll();
  liveTimer = setInterval(poll, 400);
}

async function toggleSweep(chipId: string): Promise<void> {
  if (sweepChipId === chipId) {
    await stopCommissioning();
    render();
    return;
  }
  await stopCommissioning();
  if (!(await ensureCommission())) return;
  sweepChipId = chipId;
  sweepAbort = false;
  render();
  void runSweep(chipId);
}

async function runSweep(chipId: string): Promise<void> {
  const chip = state.chips.find((c) => c.chip_id === chipId);
  if (!chip) return;
  const count = chipPortCount(chip);
  // PWM (track) chips step through voltage %; digital (switch) chips are on/off.
  const duties = chipRole(chip) === ROLE_TRACK ? [0.3, 0.6, 1.0] : [1.0];
  let pass = 0;
  while (!sweepAbort && sweepChipId === chipId) {
    const duty = duties[pass % duties.length];
    pass++;
    for (let p = 0; p < count && !sweepAbort && sweepChipId === chipId; p++) {
      try {
        await api.commissionOutput(chipId, p, duty);
      } catch {
        /* ignore */
      }
      sweepActivePort = p;
      renderOutputChips();
      await sleep(500);
      try {
        await api.commissionOutput(chipId, p, 0);
      } catch {
        /* ignore */
      }
    }
  }
}
