// editor-trains.ts — Train Editor Handler (trains list, the train
// control panel + its live HMI updates).
//
// Migrated from static/js/editor-trains.js (longterm_todo.md item #7, Wave 2).
// Behavior is unchanged. Owned by the Editor (`new TrainEditorHandler(this)`).
//
// The source-contract test (tests/test_javascript_syntax.py) pins the presence
// of `waitForTrainStopped`, `updateControlPanelValuesFromData`, and
// `updateCabButtons`; keep those names.

import { stateManager } from '../lib/state-manager';
import { showToast } from '../lib/toast';
import type { EditorHost } from './types';
import { showConfirmModal, getTrackExits, getNeighborPosition, formatDuration, formatDistanceCm } from '../lib/helpers';

/** Read the value of an input by id (empty string if missing). */
function inputValue(id: string): string {
  return (document.getElementById(id) as HTMLInputElement | null)?.value ?? '';
}

export class TrainEditorHandler {
  editor: EditorHost;
  _trainId: string | null;
  _unsubscribe: (() => void) | null;
  _dwellLookahead: any;
  _trainsListInterval: ReturnType<typeof setInterval> | null;
  _trainsListIntervalMs: number;
  _trainsListCache: any[];
  // Add-train placement state. Spatial inputs (where + which way) live on the
  // map, not as typed coordinates — see showAddTrainDialog.
  _placePos: { x: number; y: number } | null;
  _placeFacing: string | null;
  _placeExits: string[];
  _placeLengthCm: number;

  constructor(editor: EditorHost) {
    this.editor = editor;
    this._trainId = null; // Currently displayed train ID
    this._unsubscribe = null; // Unsubscribe function for StateManager
    this._dwellLookahead = null;
    this._trainsListInterval = null;
    this._trainsListIntervalMs = 1500;
    this._trainsListCache = [];
    this._placePos = null;
    this._placeFacing = null;
    this._placeExits = [];
    this._placeLengthCm = 100;
  }

  get api(): any {
    return this.editor.api;
  }

  showAddTrainDialog(): void {
    // Location and facing are spatial facts, so they're set on the map, not
    // typed as coordinates (DESIGN_LANGUAGE.md Principle 9). "Pick on map" drops
    // into a placement mode; clicking a track sets the location, the track's own
    // exit directions become the Cab/facing choice, and a translucent ghost on
    // the canvas shows how long the train will be along the track.
    this._resetPlacementState();

    const content = `
      <div class="at-form">
        <div class="input-large-container">
          <label class="input-large-label" for="train-id-input">Train ID</label>
          <input type="text" id="train-id-input" class="input-large-field" placeholder="e.g., TRAIN_01">
        </div>

        <div class="input-large-container">
          <label class="input-large-label">Location</label>
          <div class="at-place-row">
            <span class="at-place-value" id="at-location-value">Not placed</span>
            <button type="button" id="at-pick-btn" class="btn btn-outline btn-sm at-pick-btn">
              <i class="ti ti-map-pin"></i> Pick on map
            </button>
          </div>
        </div>

        <div class="input-large-container">
          <label class="input-large-label">Facing</label>
          <div id="at-facing-slot">${this._renderFacingOptions()}</div>
        </div>

        <div class="input-small-row">
          <label class="input-small-label input-small-label-inline" for="train-max-speed">Max speed</label>
          <input type="number" id="train-max-speed" class="input-small-field" value="100" min="1" max="500">
          <span class="at-unit">km/h</span>
        </div>

        <div class="input-small-row">
          <label class="input-small-label input-small-label-inline" for="train-speed-profile">Speed profile</label>
          <select id="train-speed-profile" class="input-small-field">
            <option value="">Default</option>
          </select>
        </div>

        <div class="input-small-row">
          <label class="input-small-label input-small-label-inline" for="train-min-radius">Min radius</label>
          <input type="number" id="train-min-radius" class="input-small-field" value="30" min="0" step="1">
          <span class="at-unit">cm</span>
        </div>

        <div class="input-small-row">
          <label class="input-small-label input-small-label-inline" for="train-max-slope">Max slope</label>
          <input type="number" id="train-max-slope" class="input-small-field" value="0" min="0" step="0.1">
          <span class="at-unit">%</span>
        </div>

        <div class="input-small-row">
          <label class="input-small-label input-small-label-inline" for="train-length">Length</label>
          <input type="number" id="train-length" class="input-small-field" value="100" min="10" step="10">
          <span class="at-unit">cm</span>
        </div>
        <div class="at-note" id="at-length-note"></div>

        <button id="btn-confirm-add-train" class="btn btn-accent btn-full mt-16" disabled>Add Train</button>
      </div>
    `;

    this.editor.showSecondarySidebar('Add Train', content);

    setTimeout(() => this._initAddTrainHandlers(), 0);
  }

  /** Wire the add-train panel after it mounts: default id, speed-profile list,
   *  length/pick/facing handlers, and the submit button. */
  async _initAddTrainHandlers(): Promise<void> {
    const idInput = document.getElementById('train-id-input') as HTMLInputElement | null;
    if (idInput && !idInput.value) {
      idInput.value = `TRAIN_${Date.now().toString().slice(-4)}`;
    }

    // Populate the speed-profile select from the network's profiles (data-driven
    // set → a select, per DESIGN_LANGUAGE.md §4 "Choosing a selection control").
    try {
      const profiles = await this.api.getSpeedProfiles();
      const sel = document.getElementById('train-speed-profile') as HTMLSelectElement | null;
      if (sel && Array.isArray(profiles)) {
        for (const p of profiles) {
          const id = p?.profile_id ?? p?.id;
          if (!id) continue;
          const opt = document.createElement('option');
          opt.value = id;
          opt.textContent = p?.name || id;
          sel.appendChild(opt);
        }
      }
    } catch (err) {
      console.debug('[TrainEditorHandler] speed profiles load failed:', err);
    }

    const lengthInput = document.getElementById('train-length') as HTMLInputElement | null;
    if (lengthInput) {
      this._placeLengthCm = Math.max(0, parseFloat(lengthInput.value) || 100);
      lengthInput.addEventListener('input', () => {
        this._placeLengthCm = Math.max(0, parseFloat(lengthInput.value) || 0);
        this._refreshPlacementUI();
      });
    }

    this._bindPickButton();
    this._bindFacingButtons();

    const addBtn = document.getElementById('btn-confirm-add-train') as HTMLButtonElement | null;
    if (addBtn) addBtn.onclick = () => this._submitAddTrain();

    this._refreshPlacementUI();
  }

  /** Clear placement state and any canvas ghost. */
  _resetPlacementState(): void {
    this._placePos = null;
    this._placeFacing = null;
    this._placeExits = [];
    this._placeLengthCm = 100;
    this.editor.renderer?.setTrainGhostPreview?.(null);
    this.editor.renderer?.requestRedraw?.();
  }

  /** Build the Facing segment from the picked track's exit directions. Two
   *  exits read as Cab A / Cab B (the track's two ends); any other count
   *  degrades to plain direction buttons. */
  _renderFacingOptions(): string {
    const DIR_WORD: Record<string, string> = { N: 'North', E: 'East', S: 'South', W: 'West' };
    if (!this._placePos || this._placeExits.length === 0) {
      return `<div class="switch-group fluid"><button type="button" class="switch-btn" disabled>Place on a track first</button></div>`;
    }
    const twoWay = this._placeExits.length === 2;
    const btns = this._placeExits
      .map((edge, i) => {
        const active = edge === this._placeFacing ? 'active' : '';
        const cab = twoWay ? `Cab ${i === 0 ? 'A' : 'B'} · ` : '';
        return `<button type="button" class="switch-btn ${active}" data-facing="${edge}">${cab}→ ${DIR_WORD[edge] ?? edge}</button>`;
      })
      .join('');
    return `<div class="switch-group fluid">${btns}</div>`;
  }

  _bindFacingButtons(): void {
    document.querySelectorAll<HTMLElement>('#at-facing-slot .switch-btn[data-facing]').forEach((btn) => {
      btn.onclick = () => {
        this._placeFacing = btn.dataset.facing || null;
        this._refreshPlacementUI();
      };
    });
  }

  _bindPickButton(): void {
    const btn = document.getElementById('at-pick-btn') as HTMLButtonElement | null;
    if (!btn) return;
    btn.onclick = () => {
      const active = this.editor.activeMode?.toolName === 'place-train';
      if (active) {
        this.editor.exitMode();
        this.editor._setSelectedTool?.('inspect');
      } else {
        this.editor.enterMode(this._buildTrainPlacementMode());
      }
      this._refreshPickButton();
    };
  }

  _refreshPickButton(): void {
    const btn = document.getElementById('at-pick-btn') as HTMLButtonElement | null;
    if (!btn) return;
    const active = this.editor.activeMode?.toolName === 'place-train';
    btn.classList.toggle('active', active);
    btn.innerHTML = active
      ? `<i class="ti ti-map-pin"></i> Click a track… (Esc)`
      : `<i class="ti ti-map-pin"></i> ${this._placePos ? 'Re-pick' : 'Pick on map'}`;
  }

  /** EditorMode for placing a new train by clicking a track tile. */
  _buildTrainPlacementMode(): any {
    const setStatus = (txt: string) => {
      const el = document.getElementById('status-state');
      if (el) el.textContent = txt;
    };
    return {
      name: 'Place train',
      toolName: 'place-train',
      cancelMessage: 'Train placement cancelled',
      onEnter: () => {
        setStatus('Click a track tile to place the train (Esc to cancel)');
        this._refreshPickButton();
      },
      onHover: (gridPos: any) => {
        const track = this._trackAt(gridPos.x, gridPos.y);
        if (!track || !track.block_section_id) {
          // Off a block: keep a committed ghost (re-pick), else show nothing.
          if (!this._placePos) {
            this.editor.renderer?.setTrainGhostPreview?.(null);
            this.editor.renderer?.requestRedraw?.();
          }
          return;
        }
        const exits = getTrackExits(track.track_bits || 0);
        this._setGhost(gridPos, exits[0] || 'E');
      },
      onClick: async ({ gridPos }: any) => {
        const track = this._trackAt(gridPos.x, gridPos.y);
        if (!track || !track.block_section_id) {
          showToast('No block here — place the train on a track that belongs to a block', 'error', 4000);
          return true;
        }
        const exits = getTrackExits(track.track_bits || 0);
        if (exits.length === 0) {
          showToast('This tile has no track direction to face along', 'error', 4000);
          return true;
        }
        this._placePos = { x: gridPos.x, y: gridPos.y };
        this._placeExits = exits;
        this._placeFacing = exits[0];
        await this.editor.exitMode();
        this.editor._setSelectedTool?.('inspect');
        this._refreshPlacementUI();
        return true;
      },
      onExit: () => {
        setStatus('Ready');
        // Drop the transient hover ghost; a committed placement re-asserts its
        // locked ghost in _refreshPlacementUI immediately after exitMode.
        if (!this._placePos) {
          this.editor.renderer?.setTrainGhostPreview?.(null);
          this.editor.renderer?.requestRedraw?.();
        }
        this._refreshPickButton();
      },
    };
  }

  /** Compute and push the placement ghost for a head tile + facing edge. */
  _setGhost(headTile: { x: number; y: number }, facing: string): void {
    const { tiles, fits } = this._computeGhostTiles(headTile, facing, this._placeLengthCm);
    this.editor.renderer?.setTrainGhostPreview?.({ tiles, headTile, facing, fits });
    this.editor.renderer?.requestRedraw?.();
  }

  /** Refresh the location text, facing segment, length note, ghost, and the
   *  Add button's enabled state from the current placement state. */
  _refreshPlacementUI(): void {
    const locEl = document.getElementById('at-location-value');
    if (locEl) locEl.textContent = this._placePos ? `(${this._placePos.x}, ${this._placePos.y})` : 'Not placed';

    const facingSlot = document.getElementById('at-facing-slot');
    if (facingSlot) {
      facingSlot.innerHTML = this._renderFacingOptions();
      this._bindFacingButtons();
    }

    const addBtn = document.getElementById('btn-confirm-add-train') as HTMLButtonElement | null;
    const note = document.getElementById('at-length-note');
    if (this._placePos && this._placeFacing) {
      const { tiles, fits } = this._computeGhostTiles(this._placePos, this._placeFacing, this._placeLengthCm);
      this.editor.renderer?.setTrainGhostPreview?.({ tiles, headTile: this._placePos, facing: this._placeFacing, fits });
      this.editor.renderer?.requestRedraw?.();
      if (note) {
        const n = tiles.length;
        note.textContent = fits
          ? `Spans ${n} tile${n === 1 ? '' : 's'} along the track`
          : `⚠ Track too short — ${this._placeLengthCm} cm needs more than the ${n} tile${n === 1 ? '' : 's'} ahead`;
        note.classList.toggle('at-note-warn', !fits);
      }
      if (addBtn) addBtn.disabled = false;
    } else {
      if (note) {
        note.textContent = '';
        note.classList.remove('at-note-warn');
      }
      if (addBtn) addBtn.disabled = true;
    }
    this._refreshPickButton();
  }

  /** Walk the track backward from the head tile (opposite the facing edge),
   *  accumulating each tile's length until the train's length is covered. The
   *  returned tiles are head-first; `fits` is false when the track runs out (a
   *  dead end / stub) before the length is reached. */
  _computeGhostTiles(
    head: { x: number; y: number },
    facing: string,
    lengthCm: number
  ): { tiles: Array<{ x: number; y: number }>; fits: boolean } {
    const OPP: Record<string, string> = { N: 'S', S: 'N', E: 'W', W: 'E' };
    const tiles: Array<{ x: number; y: number }> = [{ x: head.x, y: head.y }];
    const counts = this._blockTileCounts();
    let remaining = lengthCm - this._tileLengthCm(this._trackAt(head.x, head.y), counts);
    let curr = head;
    let back = OPP[facing] || 'W';
    const MAX = 60;
    while (remaining > 0 && tiles.length < MAX) {
      const [nx, ny] = getNeighborPosition(curr.x, curr.y, back);
      const t = this._trackAt(nx, ny);
      if (!t) break;
      const exits = getTrackExits(t.track_bits || 0);
      const entering = OPP[back]; // the neighbor's edge that touches curr
      if (!exits.includes(entering)) break;
      tiles.push({ x: nx, y: ny });
      remaining -= this._tileLengthCm(t, counts);
      const others = exits.filter((e) => e !== entering);
      if (others.length === 0) break; // stub
      const through = OPP[entering];
      back = others.includes(through) ? through : others[0];
      curr = { x: nx, y: ny };
    }
    return { tiles, fits: remaining <= 0 };
  }

  /** Per-tile physical length in cm, mirroring the renderer's body
   *  interpolation (block.length_cm / tiles-in-block; switch state length;
   *  50 cm fallback for 0-length tiles). */
  _tileLengthCm(track: any, counts: Map<string, number>): number {
    const DEFAULT = 50;
    if (!track) return DEFAULT;
    const blocks = this.editor.cachedNetwork?.blocks || {};
    const switches = this.editor.cachedNetwork?.switches || {};
    if (track.is_switch) {
      const sw = track.switch_id ? switches[track.switch_id] : undefined;
      if (!sw) return 0;
      return (track.switch_state === 1 ? sw.diverge_length_cm : sw.main_length_cm) || 0;
    }
    if (!track.block_section_id) return DEFAULT;
    const len = blocks[track.block_section_id]?.length_cm;
    if (!len || len <= 0) return DEFAULT;
    return len / (counts.get(track.block_section_id) || 1);
  }

  _blockTileCounts(): Map<string, number> {
    const m = new Map<string, number>();
    for (const t of this.editor.cachedNetwork?.tracks || []) {
      if (!t.block_section_id) continue;
      m.set(t.block_section_id, (m.get(t.block_section_id) || 0) + 1);
    }
    return m;
  }

  _trackAt(x: number, y: number): any {
    return (this.editor.cachedNetwork?.tracks || []).find((t: any) => t.x === x && t.y === y);
  }

  /** Exit placement mode (if active) and clear the canvas ghost. */
  _clearPlacement(): void {
    if (this.editor.activeMode?.toolName === 'place-train') {
      this.editor.exitMode();
      this.editor._setSelectedTool?.('inspect');
    }
    this.editor.renderer?.setTrainGhostPreview?.(null);
    this.editor.renderer?.requestRedraw?.();
  }

  async _submitAddTrain(): Promise<void> {
    const trainId = inputValue('train-id-input').trim();
    if (!trainId) {
      showToast('Please enter a train ID', 'error', 3000);
      return;
    }
    if (!this._placePos || !this._placeFacing) {
      showToast('Pick a location on the map first', 'error', 3000);
      return;
    }

    const DIR: Record<string, string> = { N: 'NORTH', E: 'EAST', S: 'SOUTH', W: 'WEST' };
    const maxSpeed = parseFloat(inputValue('train-max-speed')) || 100;
    const lengthCm = Math.max(0, parseFloat(inputValue('train-length')) || 100);
    const minRadius = parseFloat(inputValue('train-min-radius'));
    const maxSlope = parseFloat(inputValue('train-max-slope'));
    const profile = inputValue('train-speed-profile');

    const payload: any = {
      id: trainId,
      position: { x: this._placePos.x, y: this._placePos.y },
      direction: DIR[this._placeFacing] || 'EAST',
      max_speed: maxSpeed,
      length: lengthCm / 100, // API takes "track units" (× 100 = cm)
    };
    if (profile) payload.speed_profile_id = profile;
    if (Number.isFinite(minRadius)) payload.min_radius_cm = minRadius;
    if (Number.isFinite(maxSlope)) payload.max_slope = maxSlope;

    try {
      await this.api.createTrain(payload);
      showToast(`Train "${trainId}" created`, 'success', 3000);
      this._clearPlacement();
      this.editor.hideSecondarySidebar();
      window.app?.refreshNetwork();
    } catch (err: any) {
      console.error('[TrainEditorHandler] Failed to create train:', err);
      showToast('Failed to create train: ' + (err?.message ?? err), 'error', 5000);
    }
  }

  async showTrainsPanel(): Promise<void> {
    // Leaving any open train detail view — tear down its subscription before
    // the list poller starts (only one of the two is ever active).
    this._cleanup();

    this.editor._currentEditorType = 'trains-list';
    this.editor._currentEditorId = null;

    try {
      this._trainsListCache = await this.api.getTrains();
    } catch (err: any) {
      console.error('[TrainEditorHandler] Failed to load trains:', err);
      showToast('Failed to load trains: ' + err.message, 'error', 5000);
      this._trainsListCache = [];
    }

    this._renderTrainsListSidebar();
    this._startTrainsListPoller();
  }

  /** Re-render the trains list from `this._trainsListCache` without
   *  re-fetching. Shared by showTrainsPanel and the background poller so the
   *  list stays live (state, position, speed) without a manual refresh. */
  _renderTrainsListSidebar(): void {
    let content = `
            <div class="trains-panel">
                <!-- Reuses the routes list panel's .routes-toolbar treatment so the
                     "+ New Train" CTA reads as a flat-ghost accent button instead of a
                     solid fill (DESIGN_LANGUAGE.md §3 "Don't use solid-color filled
                     buttons in panels"), matching "+ New Route". -->
                <div class="routes-toolbar mb-16">
                    <button id="add-new-train-btn" class="btn btn-primary btn-sm flex-1">
                        + New Train
                    </button>
                </div>
        `;

    if (this._trainsListCache.length === 0) {
      content += '<div class="empty-state">No trains in network</div>';
    } else {
      content += '<div class="trains-list">';
      for (const train of this._trainsListCache) {
        content += this.renderTrainListItem(train);
      }
      content += '</div>';
    }

    content += '</div>';

    this.editor.showSecondarySidebar('Trains', content);

    setTimeout(() => this.initTrainsPanelHandlers(), 0);
  }

  /** Start the trains-list background poller. */
  _startTrainsListPoller(): void {
    this._stopTrainsListPoller();
    this._trainsListInterval = setInterval(
      () =>
        this._syncTrainsListFromServer().catch((err) => {
          console.debug('[TrainEditorHandler] trains-list sync failed:', err);
        }),
      this._trainsListIntervalMs
    );
  }

  _stopTrainsListPoller(): void {
    if (this._trainsListInterval) {
      clearInterval(this._trainsListInterval);
      this._trainsListInterval = null;
    }
  }

  /** Pull the latest trains and re-render only when a field the list shows
   *  changed (membership, state, position, speed). Skips the DOM swap while
   *  the user is hovering the list so a re-render can't steal an Edit/Delete
   *  click. */
  async _syncTrainsListFromServer(): Promise<void> {
    // Panel changed under us (opened a train / closed) — stop polling.
    // Checked before the tab-visibility gate so a stale poller is cleared
    // even while the tab is backgrounded.
    if (this.editor._currentEditorType !== 'trains-list') {
      this._stopTrainsListPoller();
      return;
    }
    // Tab hidden — pause fetching but keep the timer alive so it resumes
    // promptly when the tab is foregrounded again.
    if (typeof document !== 'undefined' && document.hidden) return;
    if (this._isTrainsListHovered()) return;

    const trains = await this.api.getTrains();
    const next = Array.isArray(trains) ? trains : [];
    if (this._trainsListSignature(next) === this._trainsListSignature(this._trainsListCache)) {
      return;
    }
    this._trainsListCache = next;
    this._renderTrainsListSidebar();
  }

  /** Signature of just the fields the trains list renders, so unrelated
   *  payload churn doesn't trigger a needless re-render. Controller state,
   *  head position, and speed come from the live StateManager cache. */
  _trainsListSignature(trains: any[]): string {
    return JSON.stringify(
      (trains || []).map((t) => {
        const c: any = stateManager?.getController(t.id);
        const span = c?.span;
        let pos = '0,0';
        if (span?.end_a) {
          const headEnd = span.leading_end === 'a' ? span.end_a : span.end_b;
          pos = `${headEnd.tile[0]},${headEnd.tile[1]}`;
        }
        return [
          t.id,
          c?.state || 'off',
          c?.facing_direction || '',
          pos,
          Math.round((c?.current_speed || 0) * 10),
        ];
      })
    );
  }

  /** True when the cursor is over the open trains list. */
  _isTrainsListHovered(): boolean {
    const el = document.querySelector('.trains-panel');
    if (!el || typeof el.matches !== 'function') return false;
    try {
      return el.matches(':hover');
    } catch (_) {
      return false;
    }
  }

  renderTrainListItem(train: any): string {
    const controller: any = stateManager?.getController(train.id);
    const state = controller?.state || 'off';
    const accent = this.getTrainAccent(state);
    const stateLabel = state.toUpperCase().replace('RUNNING_', '');

    // Head position from the controller span (leading end).
    let position = { x: 0, y: 0 };
    const span = controller?.span;
    if (span && span.end_a) {
      const headEnd = span.leading_end === 'a' ? span.end_a : span.end_b;
      position = { x: headEnd.tile[0], y: headEnd.tile[1] };
    }
    const heading = controller?.facing_direction ? ` → ${controller.facing_direction}` : '';
    const speed = (controller?.current_speed || 0).toFixed(1);

    // Reuses the canonical .rr-card list-card shell (DESIGN_LANGUAGE.md §11),
    // matching the routes/paths lists: a title row (train id + flat-ghost
    // Edit/Delete), a chip row (state · speed), and a foot caption with the
    // live position/heading. The §6 status accent is the 3px left border via
    // .accent-{success,info,muted}; the matching .chip-* colors the state chip.
    return `
            <div class="rr-card train-row accent-${accent} mb-8" data-train-id="${train.id}">
                <div class="rr-card-head">
                    <div class="rr-card-title-col">
                        <div class="rr-card-train font-mono">${train.id}</div>
                    </div>
                    <div class="route-row-actions">
                        <button class="rr-flat-btn" onclick="window.app.selectTrain('${train.id}')">Edit</button>
                        <button class="rr-flat-btn rr-flat-btn-danger delete-train-btn" data-train-id="${train.id}">Delete</button>
                    </div>
                </div>
                <div class="chip-row">
                    <span class="chip chip-state chip-${accent}">${stateLabel}</span>
                    <span class="chip">${speed} km/h</span>
                </div>
                <div class="rr-card-foot"><i class="ti ti-map-pin"></i> (${position.x}, ${position.y})${heading}</div>
            </div>
        `;
  }

  getTrainStateVisualClass(state: string): string {
    const map: Record<string, string> = {
      off: 'train-state-off',
      idle: 'train-state-idle',
      standby: 'train-state-standby',
      running_moving: 'train-state-running',
      running_at_station: 'train-state-station',
    };
    return map[state] || 'train-state-off';
  }

  /** Map a controller state to a §6 status-accent / §7 chip variant for the
   *  trains-list card (3px left border + state chip). Mirrors the token-based
   *  .tc-state-badge semantics: running = success, at-station = info, every
   *  not-running state (off / idle / standby) = muted. */
  getTrainAccent(state: string): string {
    const map: Record<string, string> = {
      off: 'muted',
      idle: 'muted',
      standby: 'muted',
      running_moving: 'success',
      running_at_station: 'info',
    };
    return map[state] || 'muted';
  }

  getHandleVisualClass(handlePosition: string): string {
    if (handlePosition?.startsWith('P')) return 'handle-power';
    if (handlePosition === 'EB') return 'handle-eb';
    if (handlePosition?.startsWith('B')) return 'handle-brake';
    return 'handle-neutral';
  }

  getObcStateClass(powerOn: boolean, emergencyActive: boolean): string {
    if (!powerOn) return 'obc-power-off';
    return emergencyActive ? 'obc-power-emergency' : 'obc-power-on';
  }

  _replaceVisualClass(element: Element | null, allowedClasses: string[], nextClass: string): void {
    if (!element) return;
    allowedClasses.forEach((cls) => element.classList.remove(cls));
    if (nextClass) {
      element.classList.add(nextClass);
    }
  }

  /** Render the cab speed readout: "<big current>/<small max> km/h". */
  _formatSpeedReadout(currentSpeed: any, maxSpeed: any): string {
    const v = Number(currentSpeed) || 0;
    const formatted = v < 10 ? v.toFixed(1) : Math.round(v).toString();
    return `<span class="tc-speed-current">${formatted}</span><span class="tc-speed-max">/${maxSpeed}</span><span class="tc-speed-unit">km/h</span>`;
  }

  /** Format ATO ETA. Prefers controller.time_to_target_sec; falls back to a
   *  coarse distance/speed estimate when the curve isn't published. */
  _formatATOEta(controller: any, clearanceCm: any, currentSpeed: any): string {
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

    const duration = this._formatDuration(etaSec);
    const simTime = stateManager?.simulationTime;
    if (typeof simTime === 'number' && Number.isFinite(simTime)) {
      const arrival = this._formatSimClock(simTime + etaSec);
      return `${arrival} (${duration})`;
    }
    return duration;
  }

  _formatDuration(etaSec: number): string {
    if (etaSec < 1) return '<1s';
    if (etaSec < 60) return `${Math.round(etaSec)}s`;
    const mins = Math.floor(etaSec / 60);
    const secs = Math.round(etaSec % 60);
    return `${mins}m ${secs}s`;
  }

  _formatSimClock(totalSec: number, { subsecond = false }: { subsecond?: boolean } = {}): string {
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
  _middleTruncate(str: any, max: number): string {
    if (!str) return '';
    const s = String(str);
    if (s.length <= max) return s;
    const keep = max - 1; // reserve 1 char for the ellipsis
    const head = Math.ceil(keep / 2);
    const tail = Math.floor(keep / 2);
    return `${s.slice(0, head)}…${s.slice(-tail)}`;
  }

  initTrainsPanelHandlers(): void {
    const addBtn = document.getElementById('add-new-train-btn') as HTMLButtonElement | null;
    if (addBtn) {
      addBtn.onclick = () => {
        this.showAddTrainDialog();
      };
    }

    const selectBtns = document.querySelectorAll<HTMLElement>('.select-train-btn');
    selectBtns.forEach((btn) => {
      btn.onclick = () => {
        const trainId = btn.dataset.trainId;
        window.app?.selectTrain(trainId);
        this.editor.hideSecondarySidebar();
      };
    });

    const deleteBtns = document.querySelectorAll<HTMLElement>('.delete-train-btn');
    deleteBtns.forEach((btn) => {
      btn.onclick = async () => {
        const trainId = btn.dataset.trainId;
        const confirmed = await showConfirmModal({
          title: 'Delete Train',
          message: `Delete train "${trainId}"?`,
          detail: 'This action cannot be undone.',
          confirmText: 'Delete',
          confirmVariant: 'danger',
        });
        if (!confirmed) return;

        try {
          const api: any = this.api;
          await api.deleteTrain(trainId);
          showToast(`Train "${trainId}" deleted`, 'success', 3000);
          await this.showTrainsPanel();
          window.app?.refreshNetwork();
        } catch (err: any) {
          console.error('[TrainEditorHandler] Failed to delete train:', err);
          showToast('Failed to delete train: ' + err.message, 'error', 5000);
        }
      };
    });
  }

  async showTrainControlPanel(trainId: string): Promise<void> {
    this.editor._currentEditorType = 'train-control';
    this.editor._currentEditorId = trainId;

    let train;
    let controller: any;
    let dispatcher: any;
    try {
      const trainResp = await this.api.getTrain(trainId);
      train = trainResp?.train;
      controller = trainResp?.controller;
      dispatcher = trainResp?.dispatcher_progress;
    } catch (err: any) {
      // A 404 means this train isn't in the current scene (typically a
      // persisted tab restored against a scene that lacks it). That's not an
      // error worth a toast — quietly close the stale tab.
      if (err?.status === 404) {
        await this.editor.closeStaleEditorTab('train-control', trainId);
        return;
      }
      console.error('[TrainEditorHandler] Failed to fetch train:', err);
      showToast(`Failed to load train: ${err.message}`, 'error', 5000);
      return;
    }

    if (!train) {
      // Endpoint returned a body without a train — treat as gone, like a 404.
      console.warn('[TrainEditorHandler] Train not found:', trainId);
      await this.editor.closeStaleEditorTab('train-control', trainId);
      return;
    }

    const trainState = controller?.state || 'off';
    const trainStateClass = this.getTrainStateVisualClass(trainState);

    // Get path and destination info for ATO section
    const onboardAto = controller?.ato_info;
    const assignedPathId = onboardAto?.path_id || controller?.assigned_path_id;
    let pathName = 'No path';
    let destinationName = 'No path';

    if (assignedPathId) {
      try {
        const path = await this.api.getPath(assignedPathId);
        if (path) {
          pathName = path.path_id || assignedPathId;
          // Get destination: prefer station name, fallback to block name
          const destinationBlockId =
            path.authority_end_block_id || path.requested_destination_block_id;
          if (path.end_platform_id || path.end_station_id) {
            const destinationPlatformId = path.end_platform_id || path.end_station_id;
            const platform = await this.api.getPlatform(destinationPlatformId);
            destinationName = platform?.name || destinationPlatformId;
          } else if (destinationBlockId) {
            const block = await this.api.getBlock(destinationBlockId);
            // Check if block belongs to a station, show station name if so
            if (block?.station_id) {
              const station = await this.api.getStation(block.station_id);
              destinationName = station?.name || block?.name || destinationBlockId;
            } else {
              destinationName = block?.name || destinationBlockId;
            }
          }
        }
      } catch (err) {
        console.warn('[TrainEditorHandler] Failed to fetch path info:', err);
        showToast('Could not load path details — showing cached data', 'warning', 5000);
        pathName = onboardAto?.path_id || assignedPathId;
        destinationName =
          onboardAto?.destination_station_name || onboardAto?.destination_block_id || 'No path';
      }
    } else {
      // No path assigned - check if controller has destination info anyway
      if (onboardAto?.destination_station_name) {
        destinationName = onboardAto.destination_station_name;
      } else if (onboardAto?.destination_block_id) {
        destinationName = onboardAto.destination_block_id;
      }
    }

    // Use Controller state for mode determination
    const controlMode = controller?.control_mode || 'ato';
    const isATO = controlMode === 'ato';
    const isManual = controlMode === 'manual';
    const isStopped = controller?.is_stopped ?? true;
    const canSwitchModes = isStopped;
    const handlePosition = controller?.handle_position || 'N';
    const powerOn = controller?.power_on ?? false;
    const emergencyActive = controller?.eb_active ?? false;
    const atoRunning = controller?.ato_running ?? false;
    const handleClass = this.getHandleVisualClass(handlePosition);
    const obcStateClass = this.getObcStateClass(powerOn, emergencyActive);
    const manualModeType = controller?.manual_mode_type || 'simulation';
    const isHandleMode = manualModeType === 'simulation';
    const isPowerPercent = manualModeType === 'power_percent';
    const manualPowerPercent = controller?.manual_power_percent ?? 0;

    // Handle position order for display
    const handleOrder = ['P5', 'P4', 'P3', 'P2', 'P1', 'N', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'EB'];
    const handleIndex = handleOrder.indexOf(handlePosition);

    const headPos =
      controller?.span && controller.span.end_a
        ? controller.span.leading_end === 'a'
          ? controller.span.end_a.tile
          : controller.span.end_b.tile
        : null;
    const posStr = headPos ? `(${headPos[0]}, ${headPos[1]})` : '—';

    // Span end-A / end-B details for the Status zone (formerly Debug).
    const span = controller?.span || null;
    const endA = span?.end_a || null;
    const endB = span?.end_b || null;
    const leadingA = span?.leading_end === 'a';
    const endATileStr = endA?.tile ? `(${endA.tile[0]}, ${endA.tile[1]})` : '—';
    const endBTileStr = endB?.tile ? `(${endB.tile[0]}, ${endB.tile[1]})` : '—';
    const endADistStr = endA && Number.isFinite(endA.distance_cm) ? `${endA.distance_cm.toFixed(1)} cm` : '—';
    const endBDistStr = endB && Number.isFinite(endB.distance_cm) ? `${endB.distance_cm.toFixed(1)} cm` : '—';
    const endAEdgeStr = endA?.entry_edge || '—';
    const endBEdgeStr = endB?.entry_edge || '—';

    // Path index (relocated from Debug to Task zone).
    const pathIndexStr =
      dispatcher?.current_resource_index != null &&
      Array.isArray(dispatcher?.resource_sequence) &&
      dispatcher.resource_sequence.length > 0
        ? `${dispatcher.current_resource_index + 1} / ${dispatcher.resource_sequence.length}`
        : '—';

    // Simulation clock — sourced from StateManager's network-wide scalar.
    const simTimeRaw = stateManager?.simulationTime;
    const simClockStr =
      typeof simTimeRaw === 'number' && Number.isFinite(simTimeRaw)
        ? this._formatSimClock(simTimeRaw, { subsecond: true })
        : '—';

    const handleDescription =
      handlePosition === 'N'
        ? 'Coast (no power, no braking)'
        : handlePosition.startsWith('P')
          ? `Power ${handlePosition.slice(1)} — Accelerating`
          : handlePosition === 'EB'
            ? 'Emergency Brake'
            : `Brake ${handlePosition.slice(1)} — Decelerating`;

    // ATO speed metrics
    const currentSpeed = controller?.current_speed ?? 0;
    const targetSpeed = controller?.target_speed ?? null;
    const atcLimit = controller?.atc_speed_limit ?? null;
    const blockLimit = controller?.current_block_speed_limit ?? null;
    const maxSpeed = train.max_speed || 100;

    // ATO destination metrics
    const clearanceCm = controller?.clearance_ahead_cm ?? null;
    const distanceM =
      clearanceCm != null && Number.isFinite(clearanceCm) ? (clearanceCm / 100).toFixed(1) : '—';
    const etaText = this._formatATOEta(controller, clearanceCm, currentSpeed);

    // Route metadata.
    const fullRunId = controller?.route_run_id || null;
    const planId = fullRunId ? fullRunId.split('#')[0] : train.assigned_route_id || null;
    const runId = fullRunId ? `#${fullRunId.split('#')[1] || ''}` : null;
    // Live path-acquisition error: present only while the train is route-
    // assigned but currently has no path (e.g. a Quick Route destination it
    // cannot actually reach). Cleared once it acquires authority.
    const pathError = controller?.path_error || null;
    const requiredCab = controller?.ato_info?.required_cab || '—';
    const pathHeading = controller?.ato_info?.path_heading || '—';
    const truncatedPath = pathName ? this._middleTruncate(pathName, 22) : '—';

    // Speed display — current value + max as a small "/N" suffix.
    const speedHtml = this._formatSpeedReadout(currentSpeed, maxSpeed);

    // Speed-limit reason banner content (also seen in train monitor footer)
    const limitReason = controller?.speed_limit_reason || '';

    const content = `
            <div class="tc-panel train-control-panel" data-train-id="${trainId}">

                <!-- ZONE 1: TASK -->
                <section class="tc-section tc-section-task ${obcStateClass}">
                    <header class="tc-status-headline">
                        <span class="tc-train-id">${trainId}</span>
                        <span class="tc-state-badge train-state-display ${trainStateClass}">${trainState.toUpperCase().replace('_', ' ')}</span>
                        <span id="obc-ato-running-badge" class="tc-running-badge ${atoRunning ? '' : 'hidden'}">▶ ATO</span>
                        <span id="train-eb-badge" class="tc-eb-badge tc-status-emergency ${emergencyActive ? '' : 'hidden'}">EB ACTIVE</span>
                    </header>
                    <dl class="tc-status-fields">
                        <div class="tc-field">
                            <dt class="tc-field-label">Plan ID</dt>
                            <dd class="tc-field-value tc-val-info" id="train-plan-id-val" title="${planId || ''}">${planId || '—'}</dd>
                        </div>
                        <div class="tc-field">
                            <dt class="tc-field-label">Run ID</dt>
                            <dd class="tc-field-value" id="train-run-id-val" title="${runId || ''}">${runId ? this._middleTruncate(runId, 22) : '—'}</dd>
                        </div>
                        <div class="tc-field">
                            <dt class="tc-field-label">Path</dt>
                            <dd class="tc-field-value train-ato-path-display" id="train-path-id-val" title="${pathName || ''}">${truncatedPath}</dd>
                        </div>
                        <div class="tc-field">
                            <dt class="tc-field-label">Path Index</dt>
                            <dd class="tc-field-value" id="train-path-index-val">${pathIndexStr}</dd>
                        </div>
                        <div class="tc-field tc-field-wide">
                            <dt class="tc-field-label">Next Destination</dt>
                            <dd class="tc-field-value train-ato-destination-display" title="${destinationName || ''}">${destinationName}</dd>
                        </div>
                    </dl>
                    <div id="train-path-error-banner" class="banner banner-danger tc-banner ${pathError && pathError.text ? '' : 'hidden'}">${pathError && pathError.text ? `⚠ No path: ${pathError.text}` : ''}</div>
                </section>

                <!-- ZONE 2: TRAIN STATUS — HMI (cab-style display) -->
                <section class="tc-section tc-section-status">
                    <div class="tc-hmi">
                        <div class="tc-hmi-header">
                            <div id="hmi-time" class="rows-1 tc-hmi-time">${simClockStr}</div>
                            <div id="hmi-train-id" class="rows-1 tc-hmi-meta">${trainId}${train.car_number ? ` · ${train.car_number} cars` : ''}</div>
                        </div>

                        <div id="hmi-eb-alert" class="rows-1 tc-hmi-alert tc-alert-eb ${emergencyActive ? '' : 'hidden'}">EB ACTIVE</div>

                        <div class="tc-hmi-grid">
                            <div id="hmi-speed" class="col-full rows-3 tc-hmi-speed train-speed-display">${Number.isFinite(currentSpeed) ? Math.round(currentSpeed) : '—'}</div>
                            <div id="hmi-atc" class="col-r rows-1 tc-hmi-atc">ATC ${atcLimit != null && Number.isFinite(atcLimit) ? Math.round(atcLimit) : '—'}</div>
                            <div id="hmi-ato" class="col-r rows-1 tc-hmi-ato ${isATO ? '' : 'hidden'}">ATO ${targetSpeed != null && Number.isFinite(targetSpeed) ? Math.round(targetSpeed) : '—'}</div>
                            <div id="hmi-dwell-remaining" class="tc-hmi-dwell-rem hidden"></div>
                            <div id="hmi-dwell-until" class="tc-hmi-dwell-until hidden"></div>
                            <div id="hmi-handle" class="col-l rows-2 tc-hmi-handle ${handleClass}">${handlePosition}</div>
                            <div id="hmi-cab-run" class="col-r rows-1 tc-hmi-cab-run">CAB ${controller?.cab || '—'}${runId ? ` · RUN ${runId}` : ''}</div>
                        </div>

                        <div class="tc-hmi-footer">
                            <span id="hmi-next-stop" class="hmi-footer-l" title="${destinationName || ''}">${destinationName && destinationName !== 'No path' ? `Next: ${destinationName}` : ''}</span>
                            <span id="hmi-distance" class="hmi-footer-c">${clearanceCm != null && Number.isFinite(clearanceCm) ? `${Math.round(clearanceCm / 100)} m` : '—'}</span>
                            <span id="hmi-eta" class="hmi-footer-r">${etaText && etaText !== '—' ? `ETA ${etaText}` : ''}</span>
                        </div>
                    </div>
                </section>

                <!-- ZONE 3: CONTROLS -->
                <section class="tc-section tc-section-controls">
                    <div class="tc-section-title">Controls</div>
                    <div class="tc-controls-pair">
                        <div class="tc-row">
                            <span class="tc-row-label">Power</span>
                            <div class="tc-segment" role="group">
                                <button type="button" id="train-power-on-btn" class="tc-seg-btn switch-btn ${powerOn ? 'active' : ''}" ${powerOn ? 'disabled' : ''}>On</button>
                                <button type="button" id="train-power-off-btn" class="tc-seg-btn switch-btn ${!powerOn ? 'active' : ''}" ${!powerOn || !isStopped ? 'disabled' : ''}>Off</button>
                            </div>
                        </div>
                        <div class="tc-row">
                            <span class="tc-row-label">Mode</span>
                            <div class="tc-segment" role="group">
                                <button type="button" id="train-mode-ato-btn" class="tc-seg-btn switch-btn ${isATO ? 'active' : ''}" ${!canSwitchModes ? 'disabled' : ''}>ATO</button>
                                <button type="button" id="train-mode-manual-btn" class="tc-seg-btn switch-btn ${isManual ? 'active' : ''}" ${!canSwitchModes ? 'disabled' : ''}>Manual</button>
                            </div>
                        </div>
                        <div class="tc-row tc-row-cab">
                            <span class="tc-row-label">Cab</span>
                            <div class="tc-segment" role="group">
                                <button type="button" id="train-cab-a-btn" class="tc-seg-btn switch-btn ${controller?.cab === 'A' ? 'active' : ''}" ${!powerOn || !isStopped ? 'disabled' : ''}>A</button>
                                <button type="button" id="train-cab-off-btn" class="tc-seg-btn switch-btn ${!controller?.cab ? 'active' : ''}" ${!powerOn || !isStopped || !controller?.cab ? 'disabled' : ''}>Off</button>
                                <button type="button" id="train-cab-b-btn" class="tc-seg-btn switch-btn ${controller?.cab === 'B' ? 'active' : ''}" ${!powerOn || !isStopped ? 'disabled' : ''}>B</button>
                            </div>
                        </div>
                    </div>
                    <div class="tc-nav-row">
                        <button id="train-monitor-btn" type="button" class="tc-btn tc-btn-ghost tc-nav-btn">Monitor →</button>
                        ${fullRunId ? `<button id="train-route-run-btn" type="button" class="tc-btn tc-btn-ghost tc-nav-btn" title="Open route run #${fullRunId.split('#')[1] || ''}">Route Run →</button>` : ''}
                    </div>

                    <div id="ato-controls" class="tc-branch tc-ato-display ${isATO ? '' : 'hidden'}">
                        <div class="tc-actions tc-ato-actions">
                            <button id="train-ato-go-btn" class="tc-btn tc-btn-success tc-btn-hero" ${!powerOn || atoRunning ? 'disabled' : ''}>
                                <span class="tc-btn-glyph">▶</span><span class="tc-btn-text">Go</span>
                            </button>
                            <button id="train-ato-stop-btn" class="tc-btn tc-btn-danger tc-btn-hero" ${!atoRunning ? 'disabled' : ''}>
                                <span class="tc-btn-glyph">■</span><span class="tc-btn-text">Stop</span>
                            </button>
                        </div>
                    </div>

                    <div id="manual-controls" class="tc-branch tc-manual-display ${isManual ? '' : 'hidden'}">
                        <div class="tc-row">
                            <span class="tc-row-label">Input</span>
                            <div class="tc-segment" role="group" aria-label="Manual input mode">
                                <button type="button" id="train-manual-mode-handle-btn" class="tc-seg-btn ${isHandleMode ? 'active' : ''}" ${!isManual || !isStopped ? 'disabled' : ''}>Handle</button>
                                <button type="button" id="train-manual-mode-power-btn" class="tc-seg-btn ${isPowerPercent ? 'active' : ''}" ${!isManual || !isStopped ? 'disabled' : ''}>Power %</button>
                            </div>
                        </div>

                        <div id="handle-mode-block" class="tc-handle-block ${isHandleMode ? '' : 'hidden'}">
                            <div class="tc-handle-zones" aria-hidden="true">
                                <span class="tc-handle-zone tc-zone-power">POWER</span>
                                <span class="tc-handle-zone tc-zone-neutral">N</span>
                                <span class="tc-handle-zone tc-zone-brake">BRAKE</span>
                                <span class="tc-handle-zone tc-zone-eb">EB</span>
                            </div>
                            <div class="tc-handle-ticks" aria-hidden="true">
                                ${handleOrder.map((p, i) => `<span class="tc-handle-tick ${i === handleIndex ? 'active' : ''}" data-handle="${p}">${p}</span>`).join('')}
                            </div>
                            <input type="range" id="train-handle-slider" class="tc-handle-slider"
                                min="0" max="14" step="1" value="${handleIndex >= 0 ? handleIndex : 5}"
                                ${!isManual || emergencyActive ? 'disabled' : ''}>
                            <div class="tc-handle-readout">
                                <span id="handle-position-label" class="tc-handle-pos handle-position-display ${handleClass}">${handlePosition}</span>
                                <span id="handle-description" class="tc-handle-desc handle-description">${handleDescription}</span>
                                <span id="handle-reason" class="tc-handle-reason train-handle-reason">${controller?.handle_reason || ''}</span>
                            </div>
                        </div>

                        <div id="power-percent-block" class="tc-power-block ${isPowerPercent ? '' : 'hidden'}">
                            <div class="tc-power-readout">
                                <span class="tc-power-value" id="train-power-percent-val">${manualPowerPercent}</span>
                                <span class="tc-power-unit">%</span>
                            </div>
                            <input type="range" id="train-power-percent-slider" class="tc-power-slider"
                                min="0" max="100" step="1" value="${manualPowerPercent}"
                                ${!isManual || !isPowerPercent || emergencyActive ? 'disabled' : ''}>
                            <div class="tc-power-bar">
                                <div class="tc-power-fill" id="train-power-percent-fill" style="width: ${manualPowerPercent}%"></div>
                            </div>
                            <div class="tc-power-hint">Direct throttle command (0–100). Set Cab + Travel direction first.</div>
                        </div>
                    </div>

                    <div class="tc-emergency-row">
                        <button id="train-emergency-btn" class="tc-eb-btn" type="button">EMERGENCY STOP</button>
                        <button id="train-emergency-reset-btn" class="tc-eb-reset-btn ${emergencyActive ? '' : 'hidden'}" type="button">Reset</button>
                    </div>
                </section>

                <!-- ZONE 4: DEBUG -->
                <section class="tc-section tc-section-debug">
                    <div class="tc-section-title">Debug</div>
                    <div id="train-control-debug-section" class="train-control-debug-section hidden"></div>

                    <div id="train-debug-geometry" class="train-control-debug-section hidden">
                        <div class="tc-section-subtitle">Train geometry</div>
                        <dl class="tc-status-fields tc-span-grid">
                            <div class="tc-field">
                                <dt class="tc-field-label" id="train-span-end-a-label">End A ${leadingA ? '(HEAD)' : '(TAIL)'}</dt>
                                <dd class="tc-field-value train-pos-display" id="train-span-end-a-tile">${endATileStr}</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label" id="train-span-end-b-label">End B ${leadingA ? '(TAIL)' : '(HEAD)'}</dt>
                                <dd class="tc-field-value" id="train-span-end-b-tile">${endBTileStr}</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">End A dist</dt>
                                <dd class="tc-field-value" id="train-span-end-a-dist">${endADistStr}</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">End B dist</dt>
                                <dd class="tc-field-value" id="train-span-end-b-dist">${endBDistStr}</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">End A edge</dt>
                                <dd class="tc-field-value" id="train-span-end-a-edge">${endAEdgeStr}</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">End B edge</dt>
                                <dd class="tc-field-value" id="train-span-end-b-edge">${endBEdgeStr}</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">Facing</dt>
                                <dd class="tc-field-value train-dir-display">${controller?.facing_direction || '—'}</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">Block</dt>
                                <dd class="tc-field-value">${train.block_section_id || '—'}</dd>
                            </div>
                        </dl>
                        <div class="tc-section-subtitle">Properties (read-only)</div>
                        <dl class="tc-status-fields">
                            <div class="tc-field">
                                <dt class="tc-field-label">Car #</dt>
                                <dd class="tc-field-value" id="train-status-car-val">${train.car_number || '—'}</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">Length</dt>
                                <dd class="tc-field-value" id="train-status-length-val">${train.length_cm || 0} cm</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">Max Speed</dt>
                                <dd class="tc-field-value" id="train-status-maxspeed-val">${train.max_speed || 100} km/h</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">Speed Profile</dt>
                                <dd class="tc-field-value flex gap-8">
                                    <span class="font-mono">${train.speed_profile_id || 'default'}</span>
                                    <span class="flex-1"></span>
                                    <button id="train-edit-profile-btn" class="rr-flat-btn" data-profile-id="${train.speed_profile_id || 'default'}">Edit profile →</button>
                                </dd>
                            </div>
                        </dl>
                        <div class="tc-section-subtitle">Maintenance</div>
                        <dl class="tc-status-fields">
                            <div class="tc-field">
                                <dt class="tc-field-label">Distance</dt>
                                <dd class="tc-field-value" id="train-status-distance-val" title="Cumulative distance travelled">${formatDistanceCm(train.total_distance_cm || 0)}</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">Run time</dt>
                                <dd class="tc-field-value" id="train-status-runtime-val" title="Cumulative powered-on time">${formatDuration(train.total_run_time_seconds || 0)}</dd>
                            </div>
                        </dl>
                    </div>

                    <details class="tc-disclosure">
                        <summary>Edit Properties</summary>
                        <div class="tc-disclosure-body">
                            <div class="tc-row">
                                <label class="tc-row-label" for="train-car-number-input">Car #</label>
                                <input type="text" id="train-car-number-input" value="${train.car_number || ''}" placeholder="Car number">
                            </div>
                            <div class="tc-row">
                                <label class="tc-row-label" for="train-length-input">Length</label>
                                <input type="number" id="train-length-input" value="${train.length_cm || 0}" min="0" step="10">
                            </div>
                            <div class="tc-row">
                                <label class="tc-row-label" for="train-max-speed-input">Max Speed</label>
                                <input type="number" id="train-max-speed-input" value="${train.max_speed || 100}" min="1" max="500">
                            </div>
                            <div class="tc-footer">
                                <button id="save-train-btn" class="tc-btn tc-btn-primary">Save</button>
                            </div>
                        </div>
                    </details>
                </section>
            </div>
        `;

    this.editor.showSecondarySidebar(`${trainId}`, content);

    // Check debug checkbox immediately and update visibility
    const debugCheckbox = document.getElementById('debug-show-train-position') as HTMLInputElement | null;
    const debugSection = document.getElementById('train-control-debug-section');
    const debugGeometry = document.getElementById('train-debug-geometry');
    if (debugCheckbox && debugCheckbox.checked) {
      if (debugSection) debugSection.classList.remove('hidden');
      if (debugGeometry) debugGeometry.classList.remove('hidden');
      // Trigger an immediate update to populate content
      if (window.app) {
        window.app.updateTrainDebugPanel(train);
      }
    }

    setTimeout(() => this.initTrainControlHandlers(trainId), 0);
  }

  async initTrainControlHandlers(trainId: string): Promise<void> {
    const api: any = this.api;

    /** Execute a button action with consistent error handling and feedback. */
    const executeAction = async (
      actionName: string,
      action: () => Promise<any>,
      refreshOnSuccess = true
    ) => {
      console.log(`[TrainControl] Executing: ${actionName}`);
      try {
        const result = await action();
        console.log(`[TrainControl] ${actionName} result:`, result);

        if (result && result.status === 'success') {
          showToast(`${actionName}: Success`, 'success', 2000);
        } else if (result && result.error) {
          showToast(`${actionName}: ${result.error}`, 'error', 5000);
          return;
        }

        if (!refreshOnSuccess) return;

        // Train mutation endpoints return the freshly-updated train, controller,
        // and dispatcher progress; push into StateManager directly so the
        // subscription patches the panel on the same frame.
        if (result && (result.controller || result.train)) {
          if (result.train) {
            stateManager?.updateEntity('trains', trainId, result.train);
          }
          if (result.controller) {
            stateManager?.updateEntity('controllers', trainId, result.controller);
          }
          if (result.dispatcher_progress !== undefined) {
            stateManager?.updateEntity('dispatcherProgress', trainId, result.dispatcher_progress);
          }
          return;
        }

        // Fallback for endpoints that do not echo the post-mutation state.
        await window.app?.refreshNetwork();
      } catch (err: any) {
        console.error(`[TrainControl] ${actionName} failed:`, err);
        showToast(`${actionName}: ${err.message}`, 'error', 5000);
      }
    };

    const waitForTrainStopped = async (timeoutMs = 8000, intervalMs = 250) => {
      const start = Date.now();
      while (Date.now() - start < timeoutMs) {
        try {
          const trainResp = await this.api.getTrain(trainId);
          if (trainResp?.controller?.is_stopped) {
            return true;
          }
        } catch (err) {
          console.warn('[TrainControl] Failed to poll controller state:', err);
        }

        await new Promise((resolve) => setTimeout(resolve, intervalMs));
      }

      return false;
    };

    // Handle position order for slider
    const handleOrder = ['P5', 'P4', 'P3', 'P2', 'P1', 'N', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'EB'];

    // ── POWER CONTROLS ──
    const powerOnBtn = document.getElementById('train-power-on-btn') as HTMLButtonElement | null;
    const powerOffBtn = document.getElementById('train-power-off-btn') as HTMLButtonElement | null;

    if (powerOnBtn) {
      powerOnBtn.onclick = () => executeAction('Power On', () => api.trainPowerOn(trainId));
    }

    if (powerOffBtn) {
      powerOffBtn.onclick = () => executeAction('Power Off', () => api.trainPowerOff(trainId));
    }

    // ── CAB CONTROLS ──
    const cabABtn = document.getElementById('train-cab-a-btn') as HTMLButtonElement | null;
    const cabBBtn = document.getElementById('train-cab-b-btn') as HTMLButtonElement | null;
    const cabOffBtn = document.getElementById('train-cab-off-btn') as HTMLButtonElement | null;

    if (cabABtn) {
      cabABtn.onclick = () => executeAction('Set Cab A', () => api.trainSetCab(trainId, 'A'));
    }

    if (cabBBtn) {
      cabBBtn.onclick = () => executeAction('Set Cab B', () => api.trainSetCab(trainId, 'B'));
    }

    if (cabOffBtn) {
      cabOffBtn.onclick = () => executeAction('Cab Off', () => api.trainSetCab(trainId, null));
    }

    // ── MODE CONTROLS (ATO / Manual) ──
    const modeATOBtn = document.getElementById('train-mode-ato-btn') as HTMLButtonElement | null;
    const modeManualBtn = document.getElementById('train-mode-manual-btn') as HTMLButtonElement | null;

    if (modeATOBtn) {
      modeATOBtn.onclick = () => executeAction('Switch to ATO', () => api.setControlMode(trainId, 'ato'));
    }

    if (modeManualBtn) {
      modeManualBtn.onclick = () =>
        executeAction('Switch to Manual', () => api.setControlMode(trainId, 'manual'));
    }

    // ── ATO CONTROLS (Go / Stop) ──
    const atoGoBtn = document.getElementById('train-ato-go-btn') as HTMLButtonElement | null;
    const atoStopBtn = document.getElementById('train-ato-stop-btn') as HTMLButtonElement | null;

    if (atoGoBtn) {
      atoGoBtn.onclick = () => executeAction('ATO Go', () => api.atoGo(trainId));
    }

    if (atoStopBtn) {
      atoStopBtn.onclick = () =>
        executeAction('ATO Stop', async () => {
          const result = await api.atoStop(trainId);
          if (result && result.status === 'success') {
            await waitForTrainStopped();
          }
          return result;
        });
    }

    // ── HANDLE POSITION SLIDER ──
    const handleSlider = document.getElementById('train-handle-slider') as HTMLInputElement | null;
    const handleLabel = document.getElementById('handle-position-label') as HTMLElement | null;
    const handleDesc = document.getElementById('handle-description') as HTMLElement | null;

    if (handleSlider) {
      // Live update during drag (visual feedback only)
      handleSlider.oninput = () => {
        const position = handleOrder[parseInt(handleSlider.value)];
        if (handleLabel) {
          handleLabel.textContent = position;
          this._replaceVisualClass(
            handleLabel,
            ['handle-power', 'handle-brake', 'handle-neutral', 'handle-eb'],
            this.getHandleVisualClass(position)
          );
        }
        if (handleDesc) {
          if (position === 'N') {
            handleDesc.textContent = 'Coast (no power, no braking)';
          } else if (position.startsWith('P')) {
            handleDesc.textContent = `Power ${position.slice(1)} — Accelerating`;
          } else if (position === 'EB') {
            handleDesc.textContent = 'Emergency Brake';
          } else {
            handleDesc.textContent = `Brake ${position.slice(1)} — Decelerating`;
          }
        }
        // Light up the matching tick label
        document.querySelectorAll<HTMLElement>('.tc-handle-tick').forEach((el, idx) => {
          el.classList.toggle('active', idx === parseInt(handleSlider.value));
        });
      };

      // Commit on release
      handleSlider.onchange = async () => {
        const position = handleOrder[parseInt(handleSlider.value)];
        await executeAction(`Set Handle ${position}`, () => api.setHandle(trainId, position), false);

        // Quick update of OBC display
        try {
          const trainResp = await api.getTrain(trainId);
          this._updateControllerDisplay(trainResp?.controller);
        } catch (e) {
          // Ignore errors here, next poll will update
        }
      };
    }

    // ── MANUAL MODE-TYPE TOGGLE (Handle / Power %) ──
    const manualModeHandleBtn = document.getElementById('train-manual-mode-handle-btn') as HTMLButtonElement | null;
    const manualModePowerBtn = document.getElementById('train-manual-mode-power-btn') as HTMLButtonElement | null;

    if (manualModeHandleBtn) {
      manualModeHandleBtn.onclick = () =>
        executeAction('Set Input: Handle', () => api.setManualModeType(trainId, 'simulation'));
    }
    if (manualModePowerBtn) {
      manualModePowerBtn.onclick = () =>
        executeAction('Set Input: Power %', () => api.setManualModeType(trainId, 'power_percent'));
    }

    // ── POWER-PERCENT SLIDER ──
    const powerSlider = document.getElementById('train-power-percent-slider') as HTMLInputElement | null;
    const powerVal = document.getElementById('train-power-percent-val') as HTMLElement | null;
    const powerFill = document.getElementById('train-power-percent-fill') as HTMLElement | null;

    if (powerSlider) {
      powerSlider.oninput = () => {
        const pct = parseInt(powerSlider.value, 10);
        if (powerVal) powerVal.textContent = String(pct);
        if (powerFill) powerFill.style.width = `${pct}%`;
      };
      powerSlider.onchange = async () => {
        const pct = parseInt(powerSlider.value, 10);
        await executeAction(`Set Power ${pct}%`, () => api.setManualPowerPercent(trainId, pct), false);
      };
    }

    // ── EMERGENCY STOP ──
    const emergencyBtn = document.getElementById('train-emergency-btn') as HTMLButtonElement | null;
    if (emergencyBtn) {
      emergencyBtn.onclick = async () => {
        try {
          await executeAction('Emergency Stop', () => api.setHandle(trainId, 'EB'));
        } catch (err: any) {
          console.error('[TrainControl] Emergency toggle failed:', err);
          showToast(`Emergency: ${err.message}`, 'error', 5000);
        }
      };
    }

    const emergencyResetBtn = document.getElementById('train-emergency-reset-btn') as HTMLButtonElement | null;
    if (emergencyResetBtn) {
      emergencyResetBtn.onclick = async () => {
        try {
          await executeAction('Release Emergency', () => api.releaseEmergency(trainId));
        } catch (err: any) {
          console.error('[TrainControl] Emergency reset failed:', err);
          showToast(`Emergency Reset: ${err.message}`, 'error', 5000);
        }
      };
    }

    const saveBtn = document.getElementById('save-train-btn') as HTMLButtonElement | null;
    const maxSpeedInput = document.getElementById('train-max-speed-input') as HTMLInputElement | null;
    const lengthInput = document.getElementById('train-length-input') as HTMLInputElement | null;
    const carNumberInput = document.getElementById('train-car-number-input') as HTMLInputElement | null;

    if (saveBtn) {
      saveBtn.onclick = async () => {
        const updates: Record<string, unknown> = {};

        if (maxSpeedInput) {
          const val = parseFloat(maxSpeedInput.value);
          if (!isNaN(val) && val > 0) {
            updates.max_speed = val;
          }
        }
        if (lengthInput) {
          const val = parseFloat(lengthInput.value);
          if (!isNaN(val) && val >= 0) {
            updates.length_cm = val;
          }
        }
        if (carNumberInput) {
          updates.car_number = carNumberInput.value;
        }

        try {
          await this.api.updateTrain(trainId, updates);
          await window.app?.refreshNetwork();
          this.showTrainControlPanel(trainId);
          showToast('Train properties saved', 'success', 5000);
        } catch (err: any) {
          console.error('Failed to save train properties:', err);
          showToast('Failed to save train properties: ' + err.message, 'error', 5000);
        }
      };
    }

    const editProfileBtn = document.getElementById('train-edit-profile-btn') as HTMLButtonElement | null;
    if (editProfileBtn) {
      editProfileBtn.onclick = () =>
        this.editor.showSpeedProfileEditor(editProfileBtn.dataset.profileId || 'default');
    }

    const monitorBtn = document.getElementById('train-monitor-btn') as HTMLButtonElement | null;
    if (monitorBtn) {
      monitorBtn.onclick = () => {
        window.app?.selectTrain(trainId);
      };
    }

    const routeRunBtn = document.getElementById('train-route-run-btn') as HTMLButtonElement | null;
    if (routeRunBtn) {
      routeRunBtn.onclick = () => {
        const ctrl: any = stateManager?.getController(trainId);
        const rrid = ctrl?.route_run_id;
        if (!rrid) {
          showToast('No active route run for this train', 'warning', 3000);
          return;
        }
        const runNumber = parseInt(rrid.split('#')[1], 10);
        if (!Number.isFinite(runNumber)) {
          showToast('Invalid route run id', 'error', 3000);
          return;
        }
        this.editor.routeRunHandler.showRouteRunEditor(runNumber);
      };
    }

    // ── SUBSCRIPTION SETUP (Phase 3) ──
    this._cleanup();
    this._trainId = trainId;

    const unsubTrain = stateManager?.subscribe(
      'trains',
      trainId,
      (newTrain: any, oldTrain: any) => this._onTrainUpdate(newTrain, oldTrain),
      { immediate: false }
    );

    const unsubController = stateManager?.subscribe(
      'controllers',
      trainId,
      () => {
        const train = stateManager?.get('trains', trainId);
        if (train) {
          this._onTrainUpdate(train, train);
        }
      },
      { immediate: false }
    );

    const unsubDispatcher = stateManager?.subscribe(
      'dispatcherProgress',
      trainId,
      () => {
        const train = stateManager?.get('trains', trainId);
        if (train) {
          this._onTrainUpdate(train, train);
        }
      },
      { immediate: false }
    );

    const unsubClock = stateManager?.subscribeSimClock((simTime: number) => {
      const panel = document.querySelector(`.tc-panel[data-train-id="${trainId}"]`);
      const simTimeEl = panel?.querySelector('#hmi-time');
      if (simTimeEl) {
        simTimeEl.textContent = this._formatSimClock(simTime, { subsecond: true });
      }
    });

    // Self-contained dwell poll.
    this._dwellLookahead = null;
    const fetchDwellOnce = async () => {
      if (this._trainId !== trainId) return;
      try {
        const payload = await this.api.getTrainLookahead(trainId);
        this._dwellLookahead = payload;
        const train = stateManager?.get('trains', trainId);
        if (train) this.updateControlPanelValuesFromData(train);
      } catch (e) {
        /* ignore transient errors */
      }
    };
    fetchDwellOnce();
    const dwellPoll = setInterval(() => {
      if (!window.app?.isSimulationRunning) return;
      fetchDwellOnce();
    }, 250);

    this._unsubscribe = () => {
      try {
        unsubTrain && unsubTrain();
      } catch (e) {
        /* ignore */
      }
      try {
        unsubController && unsubController();
      } catch (e) {
        /* ignore */
      }
      try {
        unsubDispatcher && unsubDispatcher();
      } catch (e) {
        /* ignore */
      }
      try {
        unsubClock && unsubClock();
      } catch (e) {
        /* ignore */
      }
      try {
        clearInterval(dwellPoll);
      } catch (e) {
        /* ignore */
      }
      this._dwellLookahead = null;
    };
  }

  /** Handle train updates from StateManager subscription. */
  _onTrainUpdate(newTrain: any, oldTrain: any): void {
    if (!newTrain) {
      // Train was deleted
      showToast('Train was deleted', 'warning');
      this.editor.hideSecondarySidebar();
      return;
    }

    // Always update display values - this is a live view (Control Panel pattern)
    this.updateControlPanelValuesFromData(newTrain);
  }

  /** Cleanup subscription when panel is hidden or train changes. */
  _cleanup(): void {
    this._stopTrainsListPoller();
    if (this._unsubscribe) {
      this._unsubscribe();
      this._unsubscribe = null;
    }
    this._trainId = null;
  }

  /** Update Controller display elements without full panel refresh. */
  _updateControllerDisplay(controller: any): void {
    if (!controller) return;

    const handlePosition = controller.handle_position || 'N';
    const powerOn = controller.power_on ?? false;
    const emergencyActive = controller.eb_active ?? false;
    const controlMode = controller.control_mode || 'ato';
    const atoRunning = controller.ato_running ?? false;

    // Update OBC status display
    const powerDisplay = document.querySelector('.obc-power-display');
    const modeDisplay = document.querySelector('.obc-mode-display');
    const handleDisplay = document.querySelector('.obc-handle-display');

    if (powerDisplay) {
      powerDisplay.textContent = powerOn ? 'ON' : 'OFF';
      this._replaceVisualClass(powerDisplay, ['power-on', 'power-off'], powerOn ? 'power-on' : 'power-off');
    }

    if (modeDisplay) {
      modeDisplay.textContent = controlMode.toUpperCase();
      this._replaceVisualClass(modeDisplay, ['mode-manual', 'mode-ato'], controlMode === 'manual' ? 'mode-manual' : 'mode-ato');
    }

    if (handleDisplay) {
      handleDisplay.textContent = handlePosition;
      this._replaceVisualClass(handleDisplay, ['handle-power', 'handle-brake', 'handle-neutral', 'handle-eb'], this.getHandleVisualClass(handlePosition));
    }

    const atoBadge = document.querySelector('#obc-ato-running-badge');
    if (atoBadge) {
      if (atoRunning) {
        atoBadge.textContent = '▶ ATO RUNNING';
        atoBadge.classList.remove('hidden');
        atoBadge.classList.add('badge', 'badge-success', 'text-xs');
      } else {
        atoBadge.textContent = '';
        atoBadge.classList.add('hidden');
        atoBadge.classList.remove('badge', 'badge-success', 'text-xs');
      }
    }

    // Update speed display
    const speedDisplay = document.querySelector('.train-speed-display');
    if (speedDisplay && controller.current_speed !== undefined) {
      const maxSpeed = 100; // Would need train data for actual max
      speedDisplay.innerHTML = `${controller.current_speed.toFixed(1)} <span class="text-muted">km/h</span>`;
    }
  }

  async updateTrainControlPanelValues(trainId: string): Promise<void> {
    const panel = document.querySelector(`.train-control-panel[data-train-id="${trainId}"]`);
    if (!panel) return;

    let train;
    try {
      const trainResp = await this.api.getTrain(trainId);
      train = trainResp?.train;
    } catch (err) {
      return;
    }
    if (!train) return;

    // Delegate to the data-based update method
    this.updateControlPanelValuesFromData(train);
  }

  /** Update train control panel values from train data object (no API call). */
  updateControlPanelValuesFromData(train: any): void {
    if (!train) return;

    const panel = document.querySelector(`.train-control-panel[data-train-id="${train.id}"]`) as HTMLElement | null;
    if (!panel) return;

    const controller: any = stateManager?.getController(train.id) || null;
    const trainState = controller?.state || 'off';

    const stateEl = panel.querySelector('.train-state-display');
    if (stateEl) {
      this._replaceVisualClass(
        stateEl,
        ['train-state-off', 'train-state-idle', 'train-state-standby', 'train-state-running', 'train-state-waiting', 'train-state-station', 'train-state-manual'],
        this.getTrainStateVisualClass(trainState)
      );
      stateEl.innerHTML = `
                ${trainState.toUpperCase().replace('_', ' ')}
            `;
    }

    // Update position - use controller span
    const positionEl = panel.querySelector('.train-pos-display');
    const span = controller?.span;
    if (positionEl && span && span.end_a) {
      const headEnd = span.leading_end === 'a' ? span.end_a : span.end_b;
      positionEl.textContent = `(${headEnd.tile[0]}, ${headEnd.tile[1]})`;
    }

    const directionEl = panel.querySelector('.train-dir-display');
    if (directionEl) {
      directionEl.textContent = controller?.facing_direction || 'Unknown';
    }

    const speedEl = panel.querySelector('#hmi-speed');
    if (speedEl) {
      const currentSpeed = controller?.current_speed ?? 0;
      speedEl.textContent = Number.isFinite(currentSpeed) ? String(Math.round(currentSpeed)) : '—';
    }

    if (controller) {
      const powerDisplay = panel.querySelector('.obc-power-display');
      const modeDisplay = panel.querySelector('.obc-mode-display');
      const handleDisplay = panel.querySelector('.obc-handle-display');

      const powerOn = controller.power_on ?? false;
      const controlMode = controller.control_mode || 'ato';
      const handlePosition = controller.handle_position || 'N';
      const atoRunning = controller.ato_running ?? false;

      if (powerDisplay) {
        powerDisplay.textContent = powerOn ? 'ON' : 'OFF';
        this._replaceVisualClass(powerDisplay, ['power-on', 'power-off'], powerOn ? 'power-on' : 'power-off');
      }

      if (modeDisplay) {
        modeDisplay.textContent = controlMode.toUpperCase();
        this._replaceVisualClass(modeDisplay, ['mode-manual', 'mode-ato'], controlMode === 'manual' ? 'mode-manual' : 'mode-ato');
      }

      if (handleDisplay) {
        handleDisplay.textContent = handlePosition;
        this._replaceVisualClass(handleDisplay, ['handle-power', 'handle-brake', 'handle-neutral', 'handle-eb'], this.getHandleVisualClass(handlePosition));
      }

      const atoBadge = panel.querySelector('#obc-ato-running-badge');
      if (atoBadge) {
        if (atoRunning) {
          atoBadge.textContent = '▶ ATO RUNNING';
          atoBadge.classList.remove('hidden');
          atoBadge.classList.add('badge', 'badge-success', 'text-xs');
        } else {
          atoBadge.textContent = '';
          atoBadge.classList.add('hidden');
          atoBadge.classList.remove('badge', 'badge-success', 'text-xs');
        }
      }

      // Apply OBC accent class to the Task section wrapper.
      const obcBox = panel.querySelector('.tc-section-task');
      if (obcBox) {
        this._replaceVisualClass(obcBox, ['obc-power-off', 'obc-power-on', 'obc-power-emergency'], this.getObcStateClass(powerOn, controller.eb_active ?? false));
      }

      const ebBadge = panel.querySelector('#train-eb-badge');
      if (ebBadge) {
        ebBadge.classList.toggle('hidden', !(controller.eb_active ?? false));
      }

      const pathDisplay = panel.querySelector('#train-path-id-val') as HTMLElement | null;
      const destinationDisplays = panel.querySelectorAll<HTMLElement>('.train-ato-destination-display');
      const resolvedPathId = controller.ato_info?.path_id || controller.assigned_path_id || null;
      if (pathDisplay) {
        pathDisplay.textContent = resolvedPathId ? this._middleTruncate(resolvedPathId, 22) : '—';
        pathDisplay.title = resolvedPathId || '';
      }
      if (destinationDisplays.length) {
        const onboardDestination =
          controller.ato_info?.destination_station_name ||
          controller.ato_info?.destination_block_id ||
          controller.ato_destination_block_id ||
          null;
        destinationDisplays.forEach((destinationDisplay) => {
          if (onboardDestination) {
            destinationDisplay.textContent = onboardDestination;
            destinationDisplay.title = onboardDestination;
          } else if (resolvedPathId) {
            const previousDestination = (destinationDisplay.textContent || '').trim();
            if (!previousDestination || previousDestination === 'No path') {
              destinationDisplay.textContent = 'Resolving…';
            }
          } else {
            destinationDisplay.textContent = 'No path';
            destinationDisplay.title = '';
          }
        });
      }

      // Plan ID and Run ID — derived from controller.route_run_id.
      const fullRunId = controller.route_run_id || null;
      const livePlanId = fullRunId ? fullRunId.split('#')[0] : train.assigned_route_id || null;
      const liveRunSuffix = fullRunId ? `#${fullRunId.split('#')[1] || ''}` : null;
      const planEl = panel.querySelector('#train-plan-id-val') as HTMLElement | null;
      if (planEl) {
        planEl.textContent = livePlanId ? this._middleTruncate(livePlanId, 22) : '—';
        planEl.title = livePlanId || '';
      }
      const runEl = panel.querySelector('#train-run-id-val') as HTMLElement | null;
      if (runEl) {
        runEl.textContent = liveRunSuffix || '—';
        runEl.title = fullRunId || '';
      }

      // Path-acquisition error banner — appears when the train is route-
      // assigned but cannot acquire a path, clears once it has authority.
      const pathErrorEl = panel.querySelector('#train-path-error-banner') as HTMLElement | null;
      if (pathErrorEl) {
        const livePathError = controller.path_error;
        if (livePathError && livePathError.text) {
          pathErrorEl.textContent = `⚠ No path: ${livePathError.text}`;
          pathErrorEl.classList.remove('hidden');
        } else {
          pathErrorEl.textContent = '';
          pathErrorEl.classList.add('hidden');
        }
      }

      const handleSlider = document.getElementById('train-handle-slider') as HTMLInputElement | null;
      const handleLabel = document.getElementById('handle-position-label') as HTMLElement | null;
      const handleDesc = document.getElementById('handle-description') as HTMLElement | null;
      const handleOrder = ['P5', 'P4', 'P3', 'P2', 'P1', 'N', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'EB'];
      const handleIndex = handleOrder.indexOf(handlePosition);

      if (handleSlider && handleIndex >= 0 && document.activeElement !== handleSlider) {
        handleSlider.value = String(handleIndex);
      }

      if (handleLabel) {
        handleLabel.textContent = handlePosition;
        this._replaceVisualClass(handleLabel, ['handle-power', 'handle-brake', 'handle-neutral', 'handle-eb'], this.getHandleVisualClass(handlePosition));
      }

      if (handleDesc) {
        if (handlePosition === 'N') {
          handleDesc.textContent = 'Coast (no power, no braking)';
        } else if (handlePosition.startsWith('P')) {
          handleDesc.textContent = `Power ${handlePosition.slice(1)} - Accelerating`;
        } else if (handlePosition === 'EB') {
          handleDesc.textContent = 'Emergency Brake';
        } else {
          handleDesc.textContent = `Brake ${handlePosition.slice(1)} - Decelerating`;
        }
      }

      // Update handle reason (ATO diagnostic)
      const handleReason = document.getElementById('handle-reason') as HTMLElement | null;
      if (handleReason) {
        handleReason.textContent = controller?.handle_reason || '';
      }

      // ── HMI live-updates: ATC / ATO / distance / ETA / alerts ──
      const atcLimitEl = document.getElementById('hmi-atc') as HTMLElement | null;
      if (atcLimitEl) {
        const atc = controller.atc_speed_limit;
        atcLimitEl.textContent = `ATC ${atc != null && Number.isFinite(atc) ? Math.round(atc) : '—'}`;
      }
      const targetSpeedEl = document.getElementById('hmi-ato') as HTMLElement | null;
      if (targetSpeedEl) {
        const tgt = controller.target_speed;
        targetSpeedEl.textContent = `ATO ${tgt != null && Number.isFinite(tgt) ? Math.round(tgt) : '—'}`;
        targetSpeedEl.classList.toggle('hidden', controlMode !== 'ato');
      }

      const distEl = document.getElementById('hmi-distance') as HTMLElement | null;
      const etaEl = document.getElementById('hmi-eta') as HTMLElement | null;
      const clearance = controller.clearance_ahead_cm;
      const liveSpeed = controller.current_speed ?? 0;
      if (distEl) {
        distEl.textContent =
          clearance != null && Number.isFinite(clearance) ? `${Math.round(clearance / 100)} m` : '—';
      }
      if (etaEl) {
        const etaTextLive = this._formatATOEta(controller, clearance, liveSpeed);
        etaEl.textContent = etaTextLive && etaTextLive !== '—' ? `ETA ${etaTextLive}` : '';
      }

      const ebAlertEl = document.getElementById('hmi-eb-alert') as HTMLElement | null;
      if (ebAlertEl) {
        ebAlertEl.classList.toggle('hidden', !(controller.eb_active ?? false));
      }

      const atoBranch = panel.querySelector('#ato-controls');
      const manualBranch = panel.querySelector('#manual-controls');
      if (atoBranch) atoBranch.classList.toggle('hidden', controlMode !== 'ato');
      if (manualBranch) manualBranch.classList.toggle('hidden', controlMode !== 'manual');

      // Manual mode-type toggle and slider visibility
      const handleBlock = document.getElementById('handle-mode-block') as HTMLElement | null;
      const powerBlock = document.getElementById('power-percent-block') as HTMLElement | null;
      const manualModeType = controller.manual_mode_type || 'simulation';
      if (handleBlock && powerBlock) {
        handleBlock.classList.toggle('hidden', manualModeType !== 'simulation');
        powerBlock.classList.toggle('hidden', manualModeType !== 'power_percent');
      }
      const handleToggleBtn = document.getElementById('train-manual-mode-handle-btn') as HTMLElement | null;
      const powerToggleBtn = document.getElementById('train-manual-mode-power-btn') as HTMLElement | null;
      if (handleToggleBtn) handleToggleBtn.classList.toggle('active', manualModeType === 'simulation');
      if (powerToggleBtn) powerToggleBtn.classList.toggle('active', manualModeType === 'power_percent');

      // Power-percent slider live value (only update when not dragging)
      const powerSlider = document.getElementById('train-power-percent-slider') as HTMLInputElement | null;
      const powerVal = document.getElementById('train-power-percent-val') as HTMLElement | null;
      const powerFill = document.getElementById('train-power-percent-fill') as HTMLElement | null;
      const pctValue = controller.manual_power_percent ?? 0;
      if (powerSlider && document.activeElement !== powerSlider) {
        powerSlider.value = String(pctValue);
      }
      if (powerVal) powerVal.textContent = String(pctValue);
      if (powerFill) powerFill.style.width = `${pctValue}%`;

      // ATO running badge restyle
      const atoBadgeEl = document.querySelector('#obc-ato-running-badge');
      if (atoBadgeEl) {
        if (atoRunning) {
          atoBadgeEl.textContent = '▶ ATO';
          atoBadgeEl.classList.remove('hidden', 'badge', 'badge-success', 'text-xs');
          atoBadgeEl.classList.add('tc-running-badge');
        } else {
          atoBadgeEl.textContent = '';
          atoBadgeEl.classList.add('hidden');
          atoBadgeEl.classList.remove('badge', 'badge-success', 'text-xs');
        }
      }

      // ── HMI read-only cells ──
      const simTimeEl = panel.querySelector('#hmi-time');
      if (simTimeEl) {
        const simTime = stateManager?.simulationTime;
        simTimeEl.textContent =
          typeof simTime === 'number' && Number.isFinite(simTime)
            ? this._formatSimClock(simTime, { subsecond: true })
            : '—';
      }
      const dwellRemEl = panel.querySelector('#hmi-dwell-remaining');
      const dwellUntilEl = panel.querySelector('#hmi-dwell-until');
      if (dwellRemEl && dwellUntilEl) {
        const lookahead = this._dwellLookahead;
        const isDwell = lookahead?.current_task_kind === 'dwell';
        const remaining = lookahead?.dwell_remaining_sec ?? null;
        const finishSec = lookahead?.dwell_finish_sim_sec ?? null;
        if (isDwell && remaining !== null) {
          dwellRemEl.textContent = `${Math.ceil(remaining)}s left`;
          dwellRemEl.classList.remove('hidden');
          if (finishSec !== null) {
            dwellUntilEl.textContent = `${this._formatSimClock(finishSec)} ←`;
            dwellUntilEl.classList.remove('hidden');
          } else {
            dwellUntilEl.classList.add('hidden');
          }
        } else {
          dwellRemEl.classList.add('hidden');
          dwellUntilEl.classList.add('hidden');
        }
      }
      const trainIdEl = panel.querySelector('#hmi-train-id');
      if (trainIdEl) {
        const carCount = train.car_number;
        trainIdEl.textContent = carCount ? `${train.id} · ${carCount} cars` : train.id;
      }
      const handleHmi = panel.querySelector('#hmi-handle');
      if (handleHmi) {
        handleHmi.textContent = handlePosition;
        this._replaceVisualClass(
          handleHmi,
          ['handle-power', 'handle-brake', 'handle-neutral', 'handle-eb'],
          this.getHandleVisualClass(handlePosition)
        );
      }
      const cabRunEl = panel.querySelector('#hmi-cab-run');
      if (cabRunEl) {
        const cab = controller.cab || '—';
        const liveRunSuffix = (controller.route_run_id || '').includes('#')
          ? `#${controller.route_run_id.split('#')[1] || ''}`
          : '';
        cabRunEl.textContent = liveRunSuffix ? `CAB ${cab} · RUN ${liveRunSuffix}` : `CAB ${cab}`;
      }

      const nextStopEl = panel.querySelector('#hmi-next-stop') as HTMLElement | null;
      if (nextStopEl) {
        const onboardDestination =
          controller.ato_info?.destination_station_name ||
          controller.ato_info?.destination_block_id ||
          controller.ato_destination_block_id ||
          null;
        if (onboardDestination) {
          nextStopEl.textContent = `Next: ${onboardDestination}`;
          nextStopEl.title = onboardDestination;
        } else {
          nextStopEl.textContent = '';
          nextStopEl.title = '';
        }
      }

      // ── Span end A / end B (Status zone, formerly Debug) ──
      const span = controller.span || null;
      const endA = span?.end_a || null;
      const endB = span?.end_b || null;
      const leadingA = span?.leading_end === 'a';
      const endALabel = panel.querySelector('#train-span-end-a-label');
      const endBLabel = panel.querySelector('#train-span-end-b-label');
      if (endALabel) endALabel.textContent = `End A ${leadingA ? '(HEAD)' : '(TAIL)'}`;
      if (endBLabel) endBLabel.textContent = `End B ${leadingA ? '(TAIL)' : '(HEAD)'}`;
      const endATileEl = panel.querySelector('#train-span-end-a-tile');
      if (endATileEl) endATileEl.textContent = endA?.tile ? `(${endA.tile[0]}, ${endA.tile[1]})` : '—';
      const endBTileEl = panel.querySelector('#train-span-end-b-tile');
      if (endBTileEl) endBTileEl.textContent = endB?.tile ? `(${endB.tile[0]}, ${endB.tile[1]})` : '—';
      const endADistEl = panel.querySelector('#train-span-end-a-dist');
      if (endADistEl) endADistEl.textContent = endA && Number.isFinite(endA.distance_cm) ? `${endA.distance_cm.toFixed(1)} cm` : '—';
      const endBDistEl = panel.querySelector('#train-span-end-b-dist');
      if (endBDistEl) endBDistEl.textContent = endB && Number.isFinite(endB.distance_cm) ? `${endB.distance_cm.toFixed(1)} cm` : '—';
      const endAEdgeEl = panel.querySelector('#train-span-end-a-edge');
      if (endAEdgeEl) endAEdgeEl.textContent = endA?.entry_edge || '—';
      const endBEdgeEl = panel.querySelector('#train-span-end-b-edge');
      if (endBEdgeEl) endBEdgeEl.textContent = endB?.entry_edge || '—';

      // EB reset button (Controls zone) — toggle visibility on eb_active.
      const ebResetBtn = panel.querySelector('#train-emergency-reset-btn');
      if (ebResetBtn) {
        ebResetBtn.classList.toggle('hidden', !(controller.eb_active ?? false));
      }
    }

    // ── Task-zone Path Index (sourced from dispatcher progress) ──
    const dispatcher: any = stateManager?.getDispatcherProgress(train.id);
    const pathIndexEl = panel.querySelector('#train-path-index-val');
    if (pathIndexEl) {
      if (
        dispatcher &&
        dispatcher.current_resource_index != null &&
        Array.isArray(dispatcher.resource_sequence) &&
        dispatcher.resource_sequence.length > 0
      ) {
        pathIndexEl.textContent = `${dispatcher.current_resource_index + 1} / ${dispatcher.resource_sequence.length}`;
      } else {
        pathIndexEl.textContent = '—';
      }
    }

    // ── Status-zone read-only Properties mirror (Train.* fields) ──
    const carEl = panel.querySelector('#train-status-car-val');
    if (carEl) carEl.textContent = train.car_number || '—';
    const lengthEl = panel.querySelector('#train-status-length-val');
    if (lengthEl) lengthEl.textContent = `${train.length_cm || 0} cm`;
    const maxSpeedEl = panel.querySelector('#train-status-maxspeed-val');
    if (maxSpeedEl) maxSpeedEl.textContent = `${train.max_speed || 100} km/h`;

    const powerOn = controller?.power_on ?? false;
    const controlMode = controller?.control_mode || 'ato';
    const isStopped = controller?.is_stopped ?? true;
    const activeCab = controller?.cab || null;

    this.updateCabButtons(panel, powerOn, isStopped, activeCab);
    this.updateModeButtons(panel, isStopped, controlMode);

    this.updateButtonStates(train);

    // Debug zone stays live across controller/dispatcher ticks.
    if (window.app && typeof window.app.updateTrainDebugPanel === 'function') {
      window.app.updateTrainDebugPanel(train);
    }
  }

  updateCabButtons(panel: HTMLElement, powerOn: boolean, isStopped: boolean, activeCab: any): void {
    const cabABtn = panel.querySelector('#train-cab-a-btn') as HTMLButtonElement | null;
    const cabBBtn = panel.querySelector('#train-cab-b-btn') as HTMLButtonElement | null;
    const cabOffBtn = panel.querySelector('#train-cab-off-btn') as HTMLButtonElement | null;

    if (cabABtn) {
      cabABtn.disabled = !powerOn || !isStopped;
      cabABtn.classList.toggle('active', activeCab === 'A');
    }
    if (cabBBtn) {
      cabBBtn.disabled = !powerOn || !isStopped;
      cabBBtn.classList.toggle('active', activeCab === 'B');
    }
    if (cabOffBtn) {
      const isOff = !activeCab;
      cabOffBtn.disabled = !powerOn || !isStopped || isOff;
      cabOffBtn.classList.toggle('active', isOff);
    }
  }

  updateModeButtons(panel: HTMLElement, isStopped: boolean, controlMode: string): void {
    const modeATOBtn = panel.querySelector('#train-mode-ato-btn') as HTMLButtonElement | null;
    const modeManualBtn = panel.querySelector('#train-mode-manual-btn') as HTMLButtonElement | null;

    if (modeATOBtn) {
      modeATOBtn.disabled = !isStopped;
      modeATOBtn.classList.toggle('active', controlMode === 'ato');
    }
    if (modeManualBtn) {
      modeManualBtn.disabled = !isStopped;
      modeManualBtn.classList.toggle('active', controlMode === 'manual');
    }
  }

  updateButtonStates(train: any): void {
    const controller: any = stateManager?.getController(train.id) || null;
    const state = controller?.state;
    const powerOn = controller?.power_on ?? false;
    // In v1, manual is a control mode (controller.control_mode), not a state.
    const isManual = controller?.control_mode === 'manual';

    const powerOnBtn = document.getElementById('train-power-on-btn') as HTMLButtonElement | null;
    const powerOffBtn = document.getElementById('train-power-off-btn') as HTMLButtonElement | null;
    if (powerOnBtn) {
      powerOnBtn.disabled = powerOn;
      powerOnBtn.classList.toggle('active', powerOn);
    }
    if (powerOffBtn) {
      powerOffBtn.disabled = !powerOn;
      powerOffBtn.classList.toggle('active', !powerOn);
    }

    ['north', 'east', 'south', 'west'].forEach((dir) => {
      const btn = document.getElementById(`train-dir-${dir}-btn`) as HTMLButtonElement | null;
      if (btn) btn.disabled = state !== 'idle';
    });

    const clearDirBtn = document.getElementById('train-clear-dir-btn') as HTMLButtonElement | null;
    if (clearDirBtn) clearDirBtn.disabled = state !== 'standby';

    const departBtn = document.getElementById('train-depart-btn') as HTMLButtonElement | null;
    const stopBtn = document.getElementById('train-stop-btn') as HTMLButtonElement | null;
    if (departBtn) departBtn.disabled = state !== 'standby';
    if (stopBtn) stopBtn.disabled = !(state && state.startsWith('running'));

    const manualOnBtn = document.getElementById('train-manual-on-btn') as HTMLButtonElement | null;
    const manualOffBtn = document.getElementById('train-manual-off-btn') as HTMLButtonElement | null;
    if (manualOnBtn) manualOnBtn.disabled = isManual;
    if (manualOffBtn) manualOffBtn.disabled = !isManual;
  }
}
