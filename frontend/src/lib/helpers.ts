// helpers.ts — Reusable UI components and helper functions.
//
// Migrated from static/js/helpers.js (longterm_todo.md item #7, Wave 1 —
// foundation cluster). Standalone helpers for generating consistent HTML across
// the editor panels (Segment, Block, Switch editors) plus a few small
// geometry/utility functions. Behavior is unchanged from the classic file.
//
// All functions follow the project's CSS utility class system (see
// STYLE_GUIDE.md / docs/DESIGN_LANGUAGE.md). Part of the Vite bundle; the editor
// panels, app, and renderer modules import these helpers directly.

import {
  TRACK_NS,
  TRACK_EW,
  TRACK_NE,
  TRACK_NW,
  TRACK_SE,
  TRACK_SW,
} from './constants';
import { showToast } from './toast';
import { getCanvasPalette, withAlpha } from './canvas-palette';

// ---- Light shapes the helpers read off otherwise-dynamic objects ------------

interface TrackLike {
  x: number;
  y: number;
  track_bits: number;
}

interface AvailabilityEntity {
  disabled?: boolean;
  manual?: boolean;
}

interface DirectionEntity {
  allowed_travel_directions?: string[];
}

type Coord = [number, number];

// ============================================================================
// BLOCK-RELATIVE DIRECTION FORMATTING
// ============================================================================

// ============================================================================
// TRACK & ORIENTATION LOGIC
// ============================================================================

export function getTrackExits(trackBits: number): string[] {
  const bitMap: Array<[number, string[]]> = [
    [TRACK_NS, ['N', 'S']],
    [TRACK_EW, ['E', 'W']],
    [TRACK_NE, ['N', 'E']],
    [TRACK_NW, ['N', 'W']],
    [TRACK_SE, ['S', 'E']],
    [TRACK_SW, ['S', 'W']],
  ];

  const exits = bitMap.flatMap(([bit, dirs]) => (trackBits & bit ? dirs : []));
  return [...new Set(exits)];
}

export function getNeighborPosition(x: number, y: number, direction: string): Coord {
  const offsets: Record<string, Coord> = {
    N: [0, 1],
    E: [1, 0],
    S: [0, -1],
    W: [-1, 0],
  };
  const [dx, dy] = offsets[direction] || [0, 0];
  return [x + dx, y + dy];
}

export function getTrackBitsForOrientation(
  orientation: string,
  nextX: number,
  nextY: number,
  edgeX: number,
  edgeY: number
): number {
  if (orientation === 'NS') return TRACK_NS;
  if (orientation === 'EW') return TRACK_EW;

  const dx = nextX - edgeX;
  const dy = nextY - edgeY;

  if (orientation === 'NE_SW') {
    if (dx === 1 && dy === 0) return TRACK_SW;
    if (dx === 0 && dy === 1) return TRACK_NE;
    if (dx === -1 && dy === 0) return TRACK_NE;
    if (dx === 0 && dy === -1) return TRACK_SW;
  }

  if (orientation === 'NW_SE') {
    if (dx === 1 && dy === 0) return TRACK_SE;
    if (dx === 0 && dy === 1) return TRACK_SE;
    if (dx === -1 && dy === 0) return TRACK_NW;
    if (dx === 0 && dy === -1) return TRACK_NW;
  }

  throw new Error(`Cannot determine track bits for orientation ${orientation}`);
}

export function detectOrientation(sortedPositions: Coord[], tracks: TrackLike[]): string {
  if (!sortedPositions?.length)
    throw new Error('Need at least 1 position to detect orientation');
  if (!Array.isArray(tracks)) throw new Error('Track data is required to detect orientation');

  if (sortedPositions.length === 1) {
    const [x, y] = sortedPositions[0];
    const track = tracks.find((t) => t.x === x && t.y === y);
    if (!track) throw new Error(`No track found at position (${x}, ${y})`);

    if (track.track_bits & TRACK_NS) return 'NS';
    if (track.track_bits & TRACK_EW) return 'EW';
    if (track.track_bits & (TRACK_NE | TRACK_SW)) return 'NE_SW';
    if (track.track_bits & (TRACK_NW | TRACK_SE)) return 'NW_SE';

    throw new Error('Cannot determine orientation from track bits');
  }

  const allSameX = sortedPositions.every(([x]) => x === sortedPositions[0][0]);
  if (allSameX) return 'NS';

  const allSameY = sortedPositions.every(([, y]) => y === sortedPositions[0][1]);
  if (allSameY) return 'EW';

  const sums = sortedPositions.map(([x, y]) => x + y);
  const diffs = sortedPositions.map(([x, y]) => x - y);

  // Check strict monotonicity
  const isMonotonic = (arr: number[]) => arr.slice(1).every((val, i) => val > arr[i]);

  if (isMonotonic(diffs)) return 'NE_SW';
  if (isMonotonic(sums)) return 'NW_SE';

  const sumVariance = Math.max(...sums) - Math.min(...sums);
  const diffVariance = Math.max(...diffs) - Math.min(...diffs);

  return diffVariance > sumVariance ? 'NE_SW' : 'NW_SE';
}

export function calculateNextTile(
  sortedPositions: Coord[],
  orientation: string,
  direction: string,
  tracks: TrackLike[]
): { x: number; y: number; trackBits: number } {
  if (!sortedPositions?.length) throw new Error('No positions to extend from');
  if (!Array.isArray(tracks)) throw new Error('Track data is required to extend');

  const edgeTile =
    direction === 'start' ? sortedPositions[0] : sortedPositions[sortedPositions.length - 1];
  const [edgeX, edgeY] = edgeTile;

  const edgeTrack = tracks.find((t) => t.x === edgeX && t.y === edgeY);
  if (!edgeTrack) throw new Error(`No track found at edge position (${edgeX}, ${edgeY})`);

  const exits = getTrackExits(edgeTrack.track_bits);
  if (exits.length !== 2) throw new Error(`Edge tile must have exactly 2 exits, found ${exits.length}`);

  console.debug('[Editor] Edge tile:', { x: edgeX, y: edgeY, trackBits: edgeTrack.track_bits, exits });

  const neighbors = exits.map((exit) => getNeighborPosition(edgeX, edgeY, exit));
  const positionSet = new Set(sortedPositions.map(([x, y]) => `${x},${y}`));
  const availableNeighbors = neighbors.filter(([nx, ny]) => !positionSet.has(`${nx},${ny}`));

  if (availableNeighbors.length === 0)
    throw new Error('Both neighbors are already in the segment - cannot extend');

  let nextX!: number;
  let nextY!: number;
  if (availableNeighbors.length > 1) {
    const [n1x, n1y] = availableNeighbors[0];
    const [n2x, n2y] = availableNeighbors[1];

    // Logic to pick correct neighbor based on orientation flow
    if (orientation === 'NS') {
      nextX = edgeX;
      nextY = direction === 'start' ? Math.max(n1y, n2y) : Math.min(n1y, n2y);
    } else if (orientation === 'EW') {
      nextX = direction === 'start' ? Math.min(n1x, n2x) : Math.max(n1x, n2x);
      nextY = edgeY;
    } else if (orientation === 'NE_SW') {
      const diff1 = n1x - n1y;
      const diff2 = n2x - n2y;
      [nextX, nextY] =
        direction === 'start'
          ? diff1 > diff2
            ? [n1x, n1y]
            : [n2x, n2y]
          : diff1 < diff2
            ? [n1x, n1y]
            : [n2x, n2y];
    } else if (orientation === 'NW_SE') {
      const sum1 = n1x + n1y;
      const sum2 = n2x + n2y;
      [nextX, nextY] =
        direction === 'start'
          ? sum1 > sum2
            ? [n1x, n1y]
            : [n2x, n2y]
          : sum1 < sum2
            ? [n1x, n1y]
            : [n2x, n2y];
    }
    console.debug('[Editor] Single-tile segment, chose direction:', direction, 'position:', { x: nextX, y: nextY });
  } else {
    [nextX, nextY] = availableNeighbors[0];
  }

  const trackBits = getTrackBitsForOrientation(orientation, nextX, nextY, edgeX, edgeY);
  console.debug('[Editor] Calculated next tile:', { x: nextX, y: nextY, trackBits });

  return { x: nextX, y: nextY, trackBits };
}

// ============================================================================
// PROPERTY RENDERING FUNCTIONS
// ============================================================================

interface ConfirmModalOptions {
  title?: string;
  message?: string;
  detail?: string;
  confirmText?: string;
  cancelText?: string;
  confirmVariant?: string;
}

/**
 * Reusable confirmation modal for destructive or important actions.
 * Returns a promise that resolves true on confirm, false on cancel.
 */
export function showConfirmModal(options: ConfirmModalOptions = {}): Promise<boolean> {
  const {
    title = 'Confirm Action',
    message = 'Are you sure?',
    detail = '',
    confirmText = 'Confirm',
    cancelText = 'Cancel',
    confirmVariant = 'danger',
  } = options;

  const variantClassMap: Record<string, string> = {
    danger: 'btn-danger',
    success: 'btn-success',
    primary: 'btn-primary',
    secondary: 'btn-secondary',
  };
  const confirmClass = variantClassMap[confirmVariant] || 'btn-danger';

  return new Promise((resolve) => {
    const existing = document.querySelector('.app-modal-overlay');
    if (existing) {
      existing.remove();
    }

    const overlay = document.createElement('div');
    overlay.className = 'app-modal-overlay';

    const modal = document.createElement('div');
    modal.className = 'app-modal';

    const header = document.createElement('div');
    header.className = 'app-modal-header';
    header.textContent = title;

    const body = document.createElement('div');
    body.className = 'app-modal-body';

    const messageEl = document.createElement('div');
    messageEl.className = 'app-modal-message';
    messageEl.textContent = message;
    body.appendChild(messageEl);

    if (detail) {
      const detailEl = document.createElement('div');
      detailEl.className = 'app-modal-detail';
      detailEl.textContent = detail;
      body.appendChild(detailEl);
    }

    const footer = document.createElement('div');
    footer.className = 'app-modal-footer';

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn btn-secondary btn-sm';
    cancelBtn.textContent = cancelText;

    const confirmBtn = document.createElement('button');
    confirmBtn.className = `btn ${confirmClass} btn-sm`;
    confirmBtn.textContent = confirmText;

    footer.appendChild(cancelBtn);
    footer.appendChild(confirmBtn);

    modal.appendChild(header);
    modal.appendChild(body);
    modal.appendChild(footer);
    overlay.appendChild(modal);

    const cleanup = () => {
      document.removeEventListener('keydown', onKeyDown);
      overlay.remove();
    };

    const handleCancel = () => {
      cleanup();
      resolve(false);
    };

    const handleConfirm = () => {
      cleanup();
      resolve(true);
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        handleCancel();
        return;
      }

      if (event.key === 'Enter') {
        event.preventDefault();
        handleConfirm();
      }
    };

    overlay.addEventListener('click', (event) => {
      if (event.target === overlay) {
        handleCancel();
      }
    });
    cancelBtn.addEventListener('click', handleCancel);
    confirmBtn.addEventListener('click', handleConfirm);
    document.addEventListener('keydown', onKeyDown);

    document.body.appendChild(overlay);
    cancelBtn.focus();
  });
}

// ============================================================================
// BIT TOGGLE COMPONENT
// ============================================================================

export function renderBitToggle(bit: number, name: string, trackBits: number): string {
  const isActive = (trackBits & bit) !== 0;
  // Canonical checkbox chrome (DESIGN_LANGUAGE.md §4) — accent-purple custom
  // box, dark-theme safe. The label is just the segment name (NS, EW, …); the
  // bit weight is implicit in the readout's binary string, not the label.
  return `
        <label class="checkbox-wrapper">
            <input type="checkbox" class="checkbox-input" data-bit="${bit}" ${isActive ? 'checked' : ''} />
            <span class="checkbox-custom"></span>
            <span class="checkbox-label">${name}</span>
        </label>
    `;
}

// ============================================================================
// AVAILABILITY SEGMENT (Block / Switch)
// ----------------------------------------------------------------------------
// Three-state segmented control per docs/DESIGN_LANGUAGE.md §3:
//   Available  — disabled=false, manual=false  (default)
//   Manual     — disabled=false, manual=true   (auto blocks; operator can use)
//   Disabled   — disabled=true                 (off-limits to everything)
//
// Returns the markup for the segmented control. The caller wires three
// buttons by ID and applies the chosen state back via the entity PATCH
// endpoint (`{disabled: ..., manual: ...}`).
//
// `entityKind` is used only for ID prefixing so multiple availability
// segments can coexist on one screen (e.g. block-editor + adjacent switch).
// ============================================================================

export function deriveAvailabilityState(entity?: AvailabilityEntity | null): string {
  if (!entity) return 'available';
  if (entity.disabled) return 'disabled';
  if (entity.manual) return 'manual';
  return 'available';
}

export function availabilityStateToFlags(state: string): { disabled: boolean; manual: boolean } {
  switch (state) {
    case 'disabled':
      return { disabled: true, manual: false };
    case 'manual':
      return { disabled: false, manual: true };
    case 'available':
    default:
      return { disabled: false, manual: false };
  }
}

export function renderAvailabilitySegment(
  entityKind: string,
  entity: AvailabilityEntity | null,
  opts: {
    idBase?: string;
    label?: string;
    help?: string;
    // When provided, the three states are explained one per line below the
    // segment (block editor) instead of the single-line `help` string the
    // other panels keep.
    explanations?: { normal: string; manual: string; disabled: string };
  } = {}
): string {
  const state = deriveAvailabilityState(entity);
  const idBase = (opts.idBase || `${entityKind}-availability`).replace(/[^a-z0-9-]/gi, '-');
  const label = opts.label || 'Availability';
  const help =
    opts.help ||
    'Manual = hidden from automatic routing (operator can still override). Disabled = fully off-limits.';
  const dis = (s: string) => (state === s ? 'active' : '');
  const ex = opts.explanations;
  const explain = ex
    ? `<div class="seg-explain mt-4">
                <div><span class="seg-explain-term">Normal</span> — ${ex.normal}</div>
                <div><span class="seg-explain-term">Manual</span> — ${ex.manual}</div>
                <div><span class="seg-explain-term">Disabled</span> — ${ex.disabled}</div>
            </div>`
    : `<div class="text-xxs text-muted mt-4">${help}</div>`;
  return `
        <div class="input-large-container mb-8" data-availability-segment="${entityKind}" data-sim-keep>
            <label class="input-large-label">${label}</label>
            <div class="switch-group fluid" role="radiogroup" aria-label="${label}">
                <button type="button"
                    class="switch-btn ${dis('available')}"
                    id="${idBase}-available"
                    data-availability="available"
                    title="Normal — works under automatic routing and ATO">Normal</button>
                <button type="button"
                    class="switch-btn ${dis('manual')}"
                    id="${idBase}-manual"
                    data-availability="manual"
                    title="Manual — automatic routing skips it; an operator route with override can still use it">Manual</button>
                <button type="button"
                    class="switch-btn ${dis('disabled')}"
                    id="${idBase}-disabled"
                    data-availability="disabled"
                    title="Disabled — off-limits to all trains; treated as missing track">Disabled</button>
            </div>
            ${explain}
        </div>
    `;
}

// Generic whole-number validator for editor number fields (lengths, radii,
// speeds typed as integers). Mirrors validatePriorityInput's three-shape
// return so callers share one "empty / ok / error" decision:
//   { kind: 'empty' }            — blank; caller decides revert-vs-clear.
//   { kind: 'ok', value: int }   — a whole number within [min, max].
//   { kind: 'error', message }   — not an integer, or out of range.
export function validateIntegerInput(
  rawValue: unknown,
  opts: { min?: number; max?: number; label?: string } = {}
): { kind: 'empty' } | { kind: 'ok'; value: number } | { kind: 'error'; message: string } {
  const label = opts.label || 'Value';
  if (rawValue === undefined || rawValue === null) return { kind: 'empty' };
  const trimmed = String(rawValue).trim();
  if (trimmed === '') return { kind: 'empty' };
  const n = Number(trimmed);
  if (!Number.isInteger(n)) {
    return { kind: 'error', message: `${label} must be a whole number` };
  }
  if (opts.min !== undefined && n < opts.min) {
    return { kind: 'error', message: `${label} must be ${opts.min} or more` };
  }
  if (opts.max !== undefined && n > opts.max) {
    return { kind: 'error', message: `${label} must be ${opts.max} or less` };
  }
  return { kind: 'ok', value: n };
}

// ============================================================================
// SIMULATION STRUCTURAL LOCK
// ============================================================================
//
// In operation (Simulation) mode the physical layout is frozen. The entity
// inspector panels still open — Inspect is always live — but their structural
// controls (names, geometry, lengths, radii, and the create / extend / remove /
// delete buttons) must be read-only. The dispatch controls stay live: the
// availability and direction segments, the switch Toggle State, and navigation
// buttons are marked `data-sim-keep` (on the control or an ancestor) and are
// never frozen.

/** True when the app is in Operate mode (the operation session mode). */
export function isSimMode(): boolean {
  return (window as any).app?.editorMode === 'sim';
}

// `applyStructuralLock` is the one general mechanism every structural entity
// panel calls after it renders (and that `setEditorMode` re-runs when the mode
// flips with a panel open). It only manages the `disabled` state it sets itself
// — each control it disables is tagged `data-sim-locked`, and only those are
// re-enabled when the lock lifts — so a control already disabled for another
// reason (a locked switch's Toggle, an un-extendable block's Extend) is left
// exactly as the panel set it.
export function applyStructuralLock(root: HTMLElement | null): void {
  if (!root) return;
  const lock = isSimMode();
  root.classList.toggle('sim-locked', lock);

  root
    .querySelectorAll<HTMLInputElement | HTMLButtonElement | HTMLSelectElement | HTMLTextAreaElement>(
      'input, textarea, select, button'
    )
    .forEach((el) => {
      // Operational / navigation controls opt out of the freeze.
      if (el.hasAttribute('data-sim-keep') || el.closest('[data-sim-keep]')) return;
      if (lock) {
        if (!el.disabled) {
          el.disabled = true;
          el.setAttribute('data-sim-locked', '');
        }
      } else if (el.hasAttribute('data-sim-locked')) {
        el.disabled = false;
        el.removeAttribute('data-sim-locked');
      }
    });

  // One hint at the top of the panel explains why fields are read-only. Added
  // and removed idempotently so re-renders and mode flips never stack it.
  const existing = root.querySelector(':scope > .sim-lock-hint');
  if (lock && !existing) {
    const hint = document.createElement('div');
    hint.className = 'sim-lock-hint';
    hint.innerHTML =
      '<i class="ti ti-lock"></i> Layout locked while operating — structural edits are paused.';
    root.insertBefore(hint, root.firstChild);
  } else if (!lock && existing) {
    existing.remove();
  }
}

// Escape a string for safe interpolation into an HTML attribute value
// (double-quoted). Used where user-entered text — a block display
// name — is rendered back into a `value="…"` / `title="…"` attribute.
export function escapeAttr(value: string): string {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// ============================================================================
// PER-FIELD AUTO-SAVE (§5C / §19)
// ----------------------------------------------------------------------------
// The shared wiring behind the no-Save-button editing contract. Every entity
// editor (block / switch / segment) commits each field on blur (or
// `change` for a select), with a one-shot field-flash as the only success
// acknowledgement and a revert + danger-flash on a rejected value. There is no
// success toast — six fields each saving themselves would spam; only an
// unexpected server error earns a toast.
// ============================================================================

// A field parse outcome: a validated value to commit, or a rejection (the
// caller reverts to the last good value and flashes danger).
export type FieldParseResult = { ok: true; value: unknown } | { ok: false };

// Briefly flash a field's border: 'ok' (accent) acknowledges a save, 'err'
// (danger) marks a rejected/reverted value. Re-adding the class after a forced
// reflow restarts the animation when the same field is edited twice in a row.
// The global reduced-motion reset (base/motion.css) collapses it to its end
// state.
export function flashField(el: HTMLElement | null, kind: 'ok' | 'err'): void {
  if (!el) return;
  const cls = kind === 'ok' ? 'field-flash-ok' : 'field-flash-err';
  el.classList.remove('field-flash-ok', 'field-flash-err');
  void el.offsetWidth;
  el.classList.add(cls);
  window.setTimeout(() => el.classList.remove(cls), 700);
}

// Wire one input (or select) to the auto-save cycle. `parse` turns the raw
// string into a value or a rejection; `commit` POSTs just that field and
// returns the API result (so a server-side `{error}` reverts too); `onApplied`
// runs after a successful save for any in-place DOM sync (a header summary,
// refreshNetwork). A select commits on `change`; a text/number input on blur,
// and Enter blurs to commit.
export function bindFieldAutoSave(
  input: HTMLInputElement | HTMLSelectElement | null,
  opts: {
    parse: (raw: string) => FieldParseResult;
    commit: (value: unknown) => Promise<{ error?: string } | unknown>;
    onApplied?: () => void | Promise<void>;
    event?: 'blur' | 'change';
  }
): void {
  if (!input) return;
  input.dataset.lastValue = input.value;
  const run = async (): Promise<void> => {
    const raw = input.value;
    if (raw === (input.dataset.lastValue ?? '')) return; // unchanged — skip the round-trip
    const parsed = opts.parse(raw);
    if (!parsed.ok) {
      input.value = input.dataset.lastValue ?? '';
      flashField(input, 'err');
      return;
    }
    try {
      const result = (await opts.commit(parsed.value)) as { error?: string } | undefined;
      if (result && result.error) {
        showToast(`Couldn't save: ${result.error}`, 'error', 4000);
        input.value = input.dataset.lastValue ?? '';
        flashField(input, 'err');
        return;
      }
      input.dataset.lastValue = input.value;
      await opts.onApplied?.();
      flashField(input, 'ok');
    } catch (err: any) {
      console.error('[auto-save] field commit failed', err);
      showToast(`Couldn't save: ${err.message || err}`, 'error', 4000);
      input.value = input.dataset.lastValue ?? '';
      flashField(input, 'err');
    }
  };
  const evt = opts.event || 'blur';
  input.addEventListener(evt, () => void run());
  if (evt === 'blur') {
    input.addEventListener('keydown', (e) => {
      if ((e as KeyboardEvent).key === 'Enter') {
        e.preventDefault();
        (input as HTMLInputElement).blur();
      }
    });
  }
}

// Parse factories shared by the editors so the mm↔cm conversion and the
// "empty = no limit" rule live in one place.

// Optional free text (a name / notes); empty is allowed.
export const parseText = (raw: string): FieldParseResult => ({ ok: true, value: raw.trim() });

// Required whole millimetres ≥ 0, returned as cm (÷10). Blank or invalid → reject.
export function parseRequiredMmToCm(label: string): (raw: string) => FieldParseResult {
  return (raw) => {
    const v = validateIntegerInput(raw, { min: 0, label });
    return v.kind === 'ok' ? { ok: true, value: v.value / 10 } : { ok: false };
  };
}

// Optional whole millimetres ≥ 0, returned as cm (÷10). Empty → null ("no limit").
export function parseOptionalMmToCm(label: string): (raw: string) => FieldParseResult {
  return (raw) => {
    const v = validateIntegerInput(raw, { min: 0, label });
    if (v.kind === 'empty') return { ok: true, value: null };
    if (v.kind === 'ok') return { ok: true, value: v.value / 10 };
    return { ok: false };
  };
}

// Optional whole integer ≥ 0 (a km/h speed). Empty → null ("no limit").
export function parseOptionalInt(label: string): (raw: string) => FieldParseResult {
  return (raw) => {
    const v = validateIntegerInput(raw, { min: 0, label });
    if (v.kind === 'empty') return { ok: true, value: null };
    if (v.kind === 'ok') return { ok: true, value: v.value };
    return { ok: false };
  };
}

// receives `(state, flags)` where `flags = {disabled, manual}` ready to send
// to the entity PATCH endpoint.
export function bindAvailabilitySegment(
  idBase: string,
  onChange: (state: string, flags: { disabled: boolean; manual: boolean }) => void
): void {
  const states = ['available', 'manual', 'disabled'];
  states.forEach((state) => {
    const btn = document.getElementById(`${idBase}-${state}`);
    if (!btn) return;
    btn.onclick = () => {
      states.forEach((s) => {
        const b = document.getElementById(`${idBase}-${s}`);
        if (b) b.classList.toggle('active', s === state);
      });
      try {
        onChange(state, availabilityStateToFlags(state));
      } catch (err) {
        console.error('[availability-segment] onChange failed', err);
      }
    };
  });
}

// ============================================================================
// BLOCK ALLOWED-TRAVEL-DIRECTION SEGMENTED CONTROL
// ============================================================================
//
// `Block.allowed_travel_directions` is a non-empty subset of
// {"forward", "reverse"} (defaults to both). "forward" means the increasing
// direction of the block's tile order; "reverse" the decreasing direction.
// The three mutually-exclusive UI states map 1:1 onto the allowed subset:
//   both    -> ["forward", "reverse"]
//   forward -> ["forward"]
//   reverse -> ["reverse"]
//
// Mirrors the availability segmented control above (same `switch-group` /
// `switch-btn` design-language component, same render/bind split). The caller
// wires three buttons by ID and sends the chosen subset to the block PATCH
// endpoint as `{ allowed_travel_directions: [...] }`.
// ============================================================================

export function deriveDirectionState(entity?: DirectionEntity | null): string {
  const atd = (entity && entity.allowed_travel_directions) || ['forward', 'reverse'];
  const set = new Set(atd);
  const hasForward = set.has('forward');
  const hasReverse = set.has('reverse');
  if (hasForward && hasReverse) return 'both';
  if (hasForward) return 'forward';
  if (hasReverse) return 'reverse';
  // Empty / unknown — fall back to the block default (both directions).
  return 'both';
}

export function directionStateToPayload(state: string): { allowed_travel_directions: string[] } {
  switch (state) {
    case 'forward':
      return { allowed_travel_directions: ['forward'] };
    case 'reverse':
      return { allowed_travel_directions: ['reverse'] };
    case 'both':
    default:
      return { allowed_travel_directions: ['forward', 'reverse'] };
  }
}

export function renderDirectionSegment(
  entityKind: string,
  entity: DirectionEntity | null,
  opts: { idBase?: string; label?: string; help?: string } = {}
): string {
  const state = deriveDirectionState(entity);
  const idBase = (opts.idBase || `${entityKind}-direction`).replace(/[^a-z0-9-]/gi, '-');
  const label = opts.label || 'Allowed travel';
  const help =
    opts.help ||
    'Which way trains may travel through this block. Auto-routing obeys this; an operator can override it.';
  const dir = (s: string) => (state === s ? 'active' : '');
  return `
        <div class="input-large-container mb-8" data-direction-segment="${entityKind}" data-sim-keep>
            <label class="input-large-label">${label}</label>
            <div class="switch-group fluid" role="radiogroup" aria-label="${label}">
                <button type="button"
                    class="switch-btn ${dir('both')}"
                    id="${idBase}-both"
                    data-direction="both"
                    title="Both directions allowed">Both</button>
                <button type="button"
                    class="switch-btn ${dir('forward')}"
                    id="${idBase}-forward"
                    data-direction="forward"
                    title="Forward only — follow the direction arrows on the layout">Forward</button>
                <button type="button"
                    class="switch-btn ${dir('reverse')}"
                    id="${idBase}-reverse"
                    data-direction="reverse"
                    title="Reverse only — follow the direction arrows on the layout">Reverse</button>
            </div>
            <div class="text-xxs text-muted mt-4">${help}</div>
        </div>
    `;
}

// Wire the three buttons of a `renderDirectionSegment` block. `onChange`
// receives `(state, payload)` where `payload = {allowed_travel_directions}`
// ready to send to the block PATCH endpoint.
export function bindDirectionSegment(
  idBase: string,
  onChange: (state: string, payload: { allowed_travel_directions: string[] }) => void
): void {
  const states = ['both', 'forward', 'reverse'];
  states.forEach((state) => {
    const btn = document.getElementById(`${idBase}-${state}`);
    if (!btn) return;
    btn.onclick = () => {
      states.forEach((s) => {
        const b = document.getElementById(`${idBase}-${s}`);
        if (b) b.classList.toggle('active', s === state);
      });
      try {
        onChange(state, directionStateToPayload(state));
      } catch (err) {
        console.error('[direction-segment] onChange failed', err);
      }
    };
  });
}

// ============================================================================
// TRACK POLARITY SEGMENTED CONTROL
// ----------------------------------------------------------------------------
// Same `switch-group` / `switch-btn` component and render/bind split as the two
// controls above, with two cells instead of three. Distinct from the travel
// direction control it sits near: that one is a routing policy the pathfinder
// reads, this one is wiring — which physical rail a positive voltage drives.
// The two steps are separate on purpose: the block's tile order fixes which way
// a positive voltage MEANS (toward the F arrow the renderer draws at the
// higher-index end), and this flag fixes which rail carries it. Only the second
// can be checked by eye, which is what the track Test sweep is for.
// The flag lives on the block's `block_track` hardware binding and auto-saves
// with it (design_v2.md "Hardware setup", The rail binding bonds tile order to
// rail polarity), so
// the control is disabled until the rails are bound: with no binding there is
// no wiring to describe.
// ============================================================================

export function renderPolaritySegment(
  entityKind: string,
  entity: { polarity_reversed?: boolean } | null,
  opts: { idBase?: string; label?: string; help?: string; disabled?: boolean } = {}
): string {
  const reversed = !!(entity && entity.polarity_reversed);
  const idBase = (opts.idBase || `${entityKind}-polarity`).replace(/[^a-z0-9-]/gi, '-');
  const label = opts.label || 'Track direction';
  const disabledAttr = opts.disabled ? 'disabled' : '';
  const help =
    opts.help ||
    'Which of the block’s two rails a positive voltage drives. To check it, run the ' +
      'track Test below: its first leg should move the loco toward the green F arrow on ' +
      'the map. If it moves toward R instead, the rails are wired the other way round — ' +
      'switch to Reversed and sweep again. Applies to every segment in the block, and ' +
      'leaves the detection wiring where it is.';
  return `
        <div class="input-large-container mb-8" data-polarity-segment="${entityKind}" data-sim-keep>
            <label class="input-large-label">${label}</label>
            <div class="switch-group fluid" role="radiogroup" aria-label="${label}">
                <button type="button"
                    class="switch-btn ${reversed ? '' : 'active'}"
                    id="${idBase}-normal"
                    data-polarity="normal" ${disabledAttr}
                    title="A positive voltage drives the loco toward the F arrow">Normal</button>
                <button type="button"
                    class="switch-btn ${reversed ? 'active' : ''}"
                    id="${idBase}-reversed"
                    data-polarity="reversed" ${disabledAttr}
                    title="Rails swapped — the block’s other rail is the one a positive voltage drives">Reversed</button>
            </div>
            <div class="text-xxs text-muted mt-4">${help}</div>
        </div>
    `;
}

// Wire the two buttons of a `renderPolaritySegment` block. `onChange` receives
// `(reversed, payload)` where `payload = {polarity_reversed}`; the block panel
// routes it onto the block's `block_track` hardware binding (PUT
// /api/hardware/binding), which auto-persists like every other wiring edit.
export function bindPolaritySegment(
  idBase: string,
  onChange: (reversed: boolean, payload: { polarity_reversed: boolean }) => void
): void {
  const cells: Array<[string, boolean]> = [
    ['normal', false],
    ['reversed', true],
  ];
  cells.forEach(([key, reversed]) => {
    const btn = document.getElementById(`${idBase}-${key}`);
    if (!btn) return;
    btn.onclick = () => {
      cells.forEach(([k]) => {
        const b = document.getElementById(`${idBase}-${k}`);
        if (b) b.classList.toggle('active', k === key);
      });
      try {
        onChange(reversed, { polarity_reversed: reversed });
      } catch (err) {
        console.error('[polarity-segment] onChange failed', err);
      }
    };
  });
}

// ============================================================================
// TILE LIST RENDERING
// ============================================================================

function renderTileItem(x: number, y: number, index: number, total: number, removeBtnClass: string): string {
  const isEdge = index === 0 || index === total - 1;
  const removeButton = isEdge
    ? `<button class="${removeBtnClass} btn btn-xs btn-danger" data-x="${x}" data-y="${y}">Remove</button>`
    : `<span class="tile-list-middle-label" title="Interior tiles can't be removed — only the first and last tile.">middle</span>`;
  return `
        <div class="tile-list-item" data-x="${x}" data-y="${y}">
            <span class="tile-list-coords">(${x}, ${y})</span>
            ${removeButton}
        </div>
    `;
}

interface ExtendBtnOpts {
  label?: string; // button text (default "Extend Start" / "Extend End")
  disabled?: boolean; // greyed out when there's no clean next tile
  title?: string; // tooltip — the predicted coordinate, or the refusal reason
  // Leading icon HTML. Omitted = the default plus icon; '' (or null) drops the
  // icon entirely (the block editor's "Extend tiles (x, y)" buttons spell out
  // the action in words, so they pass '').
  icon?: string | null;
}

interface TileListOptions {
  extendStartBtnId: string;
  extendEndBtnId: string;
  removeBtnClass: string;
  // Override the list heading text (the default is "Block Tiles"). The "(N)"
  // count is always appended.
  headerLabel?: string | null;
  // Per-end extend-button overrides — used by the block editor to show the
  // predicted next-tile coordinate (and disable when extension is impossible).
  extendStart?: ExtendBtnOpts | null;
  extendEnd?: ExtendBtnOpts | null;
}

export function renderTileList(sortedPositions: Coord[], options: TileListOptions): string {
  const {
    extendStartBtnId,
    extendEndBtnId,
    removeBtnClass,
    headerLabel = null,
    extendStart = null,
    extendEnd = null,
  } = options;

  if (!sortedPositions?.length) return '';

  const tileItems = sortedPositions
    .map(([x, y], index) => renderTileItem(x, y, index, sortedPositions.length, removeBtnClass))
    .join('');

  const heading = headerLabel || 'Block Tiles';
  const extendBtn = (id: string, fallback: string, o: ExtendBtnOpts | null): string => {
    const text = o?.label || fallback;
    const disabled = o?.disabled ? ' disabled' : '';
    const title = o?.title ? ` title="${escapeAttr(o.title)}"` : '';
    // Default leading icon is the plus; a caller can pass icon: '' to drop it.
    const iconHtml = o?.icon !== undefined ? (o.icon ?? '') : '<i class="ti ti-plus"></i>';
    const iconPart = iconHtml ? `${iconHtml} ` : '';
    return `<button id="${id}" class="btn btn-secondary btn-sm tile-list-extend-btn"${disabled}${title}>${iconPart}${text}</button>`;
  };

  return `
        <div class="tile-list">
            <div class="tile-list-header">${heading} (${sortedPositions.length})</div>
            ${extendBtn(extendStartBtnId, 'Extend Start', extendStart)}
            <div class="tile-list-container">
                ${tileItems}
            </div>
            ${extendBtn(extendEndBtnId, 'Extend End', extendEnd)}
            <div class="tile-list-note text-xxs text-muted mt-4 mb-0">Only the first and last tile can be removed.</div>
        </div>
    `;
}

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

export function getBlockStateColor(state?: string, alpha = 1): string {
  const p = getCanvasPalette();
  const stateMap: Record<string, string> = {
    free: p.blockFree,
    reserved: p.blockReserved,
    occupied: p.blockOccupied,
    caution: p.blockCaution,
    disabled: p.blockDisabled,
    manual: p.blockManual,
  };
  const hex = stateMap[(state || '').toLowerCase()] || '#888';
  return withAlpha(hex, alpha);
}
