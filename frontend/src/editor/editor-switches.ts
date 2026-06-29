// editor-switches.ts — Switch Editor Handler.
//
// Migrated from static/js/editor-switches.js (longterm_todo.md item #7, Wave 2 —
// editor handler cluster). Behavior is unchanged from the classic file.
//
// Owned by the Editor (`new SwitchEditorHandler(this)`).

import { showToast } from '../lib/toast';
import type { EditorHost } from './types';
import {
  renderAvailabilitySegment,
  bindAvailabilitySegment,
  showConfirmModal,
  escapeAttr,
  formatDuration,
  bindFieldAutoSave,
  parseText,
  parseRequiredMmToCm,
  parseOptionalMmToCm,
  applyStructuralLock,
} from '../lib/helpers';

export class SwitchEditorHandler {
  editor: EditorHost;
  _switchId: string | null;
  _unsubscribe: (() => void) | null;
  // Switches-list panel (showSwitchesPanel): the last-fetched list plus its
  // background poller, mirroring the routes-list panel in editor-routes.ts.
  switches: any[];
  _switchesListInterval: ReturnType<typeof setInterval> | null;
  _switchesListIntervalMs: number;

  constructor(editor: EditorHost) {
    this.editor = editor;
    this._switchId = null;
    this._unsubscribe = null;
    this.switches = [];
    this._switchesListInterval = null;
    this._switchesListIntervalMs = 1500;
  }

  // Convenience getters for common editor properties
  get api(): any {
    return this.editor.api;
  }
  get selectedElement(): any {
    return this.editor.selectedElement;
  }

  // === Switch Editor Panel ===

  async showEditor(switchId: string): Promise<void> {
    // Track this editor for auto-refresh
    this.editor._currentEditorType = 'switch';
    this.editor._currentEditorId = switchId;

    // Fetch switch data
    let switchData;
    try {
      switchData = await this.api.getSwitch(switchId);
      if (switchData.error) {
        showToast('Switch not found', 'error', 5000);
        return;
      }
    } catch (err) {
      console.error('[SwitchEditorHandler] Failed to get switch:', err);
      showToast('Failed to load switch', 'error', 5000);
      return;
    }

    // Get current track state for switch_state
    let track;
    try {
      track = await this.api.getTrack(switchData.x, switchData.y);
    } catch (err) {
      console.error('[SwitchEditorHandler] Failed to get track:', err);
    }

    const currentState = track?.switch_state ?? 0;
    const stateText = currentState === 0 ? 'MAIN' : 'DIVERGE';
    const accentClass = currentState === 0 ? 'accent-success' : 'accent-warn';
    const chipClass = currentState === 0 ? 'chip-success' : 'chip-warn';
    // Switch lock ownership lives on the Switch entity's `locks` array
    // (design_new.md:651). `isLocked` gates the Toggle control below; the
    // individual lock entries render as rows in the Train Protection section
    // (a route lock is the pre-occupancy half of holding the switch).
    const lockEntries = Array.isArray(switchData.locks) ? switchData.locks : [];
    const isLocked = lockEntries.length > 0;
    const switchType = switchData.switch_type || 'standard';

    // Flank protection — read-only, topology-derived (design_new.md:686). Each
    // entry is [neighbour_switch_id, blocking_state]; the neighbour is held at
    // its SAFE state (= 1 - blocking_state) while this switch's route is locked.
    // Mirrors the `lockEntries` guard above; the field is always present in the
    // live `to_dict` payload (switch.py:665).
    const flankEntries: Array<[string, number]> = Array.isArray(switchData.flank_protection)
      ? switchData.flank_protection
      : [];
    const flankRows = flankEntries
      .map(([neighbourId, blockingState]) => {
        const safeState = 1 - Number(blockingState);
        const safeLabel = safeState === 0 ? 'MAIN' : 'DIVERGE';
        const safeChip = safeState === 0 ? 'chip-success' : 'chip-warn';
        const blockLabel = Number(blockingState) === 0 ? 'MAIN' : 'DIVERGE';
        return `
                    <div class="flex gap-8 mb-8" title="Conflict if neighbour set to ${blockLabel} · held safe at ${safeLabel}">
                        <button class="edit-btn flank-nav-btn font-mono" data-sim-keep data-switch-id="${neighbourId}" title="Open ${neighbourId} editor">${neighbourId}</button>
                        <span class="flex-1"></span>
                        <span class="chip chip-state ${safeChip}">${safeLabel}</span>
                    </div>`;
      })
      .join('');

    // Train protection — read-only, runtime. Train-owned physical protection
    // held on this switch after a train head entered a source switch and its
    // flank lock was converted (design_new.md:665). Each entry is
    // [train_id, source_switch_id, locked_state]; empty unless a sim is running.
    const protectionClaims: Array<[string, string, number]> = Array.isArray(
      switchData.train_protection_claims,
    )
      ? switchData.train_protection_claims
      : [];
    const protectionRows = protectionClaims
      .map(([trainId, sourceId, lockedState]) => {
        const stateLabel = Number(lockedState) === 0 ? 'MAIN' : 'DIVERGE';
        const stateChip = Number(lockedState) === 0 ? 'chip-success' : 'chip-warn';
        return `
                    <div class="flex gap-8 mb-8" title="Train ${trainId} holds this switch at ${stateLabel}, sourced from ${sourceId}">
                        <span class="font-mono text-sm">${trainId}</span>
                        <span class="flex-1"></span>
                        <span class="text-muted text-xs">from</span>
                        <button class="edit-btn flank-nav-btn font-mono" data-sim-keep data-switch-id="${sourceId}" title="Open ${sourceId} editor">${sourceId}</button>
                        <span class="chip chip-state ${stateChip}">${stateLabel}</span>
                    </div>`;
      })
      .join('');

    // Route locks — a path holds this switch at `locked_state` for `train_id`
    // before the train arrives. This is the pre-occupancy half of the same
    // "what is holding this switch" story as train_protection_claims, so the two
    // render together in the Train Protection section rather than as a separate
    // top-of-panel banner. Each entry:
    // { path_id, source_switch_id, train_id, locked_state, claim_role }.
    const lockRows = lockEntries
      .map((lock: any) => {
        const lockedState = Number(lock.locked_state);
        const stateLabel = lockedState === 0 ? 'MAIN' : 'DIVERGE';
        const stateChip = lockedState === 0 ? 'chip-success' : 'chip-warn';
        const trainId = lock.train_id || '—';
        const pathId = lock.path_id || '';
        const roleNote = lock.claim_role === 'flank' ? ' as flank protection' : '';
        return `
                    <div class="mb-8" title="Route ${pathId} holds this switch at ${stateLabel} for train ${trainId}${roleNote}">
                        <div class="flex gap-8">
                            <span class="font-mono text-sm">${trainId}</span>
                            <span class="flex-1"></span>
                            <span class="chip chip-state ${stateChip}">${stateLabel}</span>
                        </div>
                        <div class="banner-value text-muted text-xs">${pathId}</div>
                    </div>`;
      })
      .join('');

    const content = `
            <!-- §11 Status header — identity + state pill, dynamic accent. -->
            <div class="status-header ${accentClass}">
                <div class="status-header-row">
                    <div class="status-header-id font-mono" title="${switchData.switch_id}">${switchData.switch_id}</div>
                    <span class="chip chip-state ${chipClass}">${stateText}</span>
                </div>
                <div class="status-header-ident">
                    <span class="status-header-role">Switch</span>
                    <span class="status-sep">·</span>
                    <span class="font-mono">${switchType}</span>
                    <span class="status-sep">·</span>
                    <span class="font-mono">(${switchData.x}, ${switchData.y})</span>
                </div>
            </div>

            <!-- Flank Protection — read-only: neighbouring switches this one mutually flank-protects. -->
            <div class="control-section">
                <div class="control-section-header">Flank Protection${flankEntries.length ? ` (${flankEntries.length})` : ''}</div>
                ${
                  flankEntries.length
                    ? `<div class="text-muted text-xs mb-8">Neighbours held at their safe state while this switch's route is locked.</div>${flankRows}`
                    : `<div class="text-muted text-xs">None — no nearby switches require mutual locking.</div>`
                }
            </div>

            <!-- Train Protection — read-only: everything currently holding this
                 switch for a train. A route lock (a path holding the switch ahead
                 of its train) and train-owned protection (held once the train
                 occupies the source switch) are two phases of the same hold, so
                 they share this section rather than a separate lock banner. -->
            <div class="control-section">
                <div class="control-section-header">Train Protection${
                  lockEntries.length + protectionClaims.length
                    ? ` (${lockEntries.length + protectionClaims.length})`
                    : ''
                }</div>
                ${
                  lockEntries.length || protectionClaims.length
                    ? `<div class="text-muted text-xs mb-8">Routes and trains currently holding this switch.</div>${lockRows}${protectionRows}`
                    : `<div class="text-muted text-xs">None — no train is currently holding this switch.</div>`
                }
            </div>

            <!-- Controls -->
            <div class="control-section">
                <div class="control-section-header">Controls</div>
                <div class="flex gap-8">
                    <button id="toggle-switch-btn" class="btn btn-secondary btn-sm flex-1" data-sim-keep ${isLocked ? 'disabled' : ''}>
                        Toggle State
                    </button>
                </div>
                ${isLocked ? '<div class="text-warning text-xs mt-8">Switch is locked and cannot be toggled.</div>' : ''}
            </div>

            <!-- LENGTH PROPERTIES SECTION — whole millimetres; every field
                 auto-saves on blur (§5C), so there is no Save button. -->
            <div class="control-section">
                <div class="control-section-header">Path Lengths (mm)</div>
                <div class="input-small-row mb-8">
                    <label class="input-small-label input-small-label-inline" for="switch-main-length">MAIN Path Length</label>
                    <input type="number" id="switch-main-length" class="input-small-field input-small-field-compact" value="${Math.round((switchData.main_length_cm || 0) * 10)}" min="0" step="1" inputmode="numeric">
                </div>
                <div class="input-small-row mb-8">
                    <label class="input-small-label input-small-label-inline" for="switch-diverge-length">DIVERGE Path Length</label>
                    <input type="number" id="switch-diverge-length" class="input-small-field input-small-field-compact" value="${Math.round((switchData.diverge_length_cm || 0) * 10)}" min="0" step="1" inputmode="numeric">
                </div>
            </div>

            <!-- RADIUS PROPERTIES SECTION — empty = "None (straight)" (null). -->
            <div class="control-section">
                <div class="control-section-header">Path Radii (mm)</div>
                <div class="input-small-row mb-8">
                    <label class="input-small-label input-small-label-inline" for="switch-main-radius">MAIN Path Radius</label>
                    <input type="number" id="switch-main-radius" class="input-small-field input-small-field-compact" value="${switchData.main_radius_cm != null ? Math.round(switchData.main_radius_cm * 10) : ''}" min="0" step="1" inputmode="numeric" placeholder="None (straight)">
                </div>
                <div class="input-small-row mb-8">
                    <label class="input-small-label input-small-label-inline" for="switch-diverge-radius">DIVERGE Path Radius</label>
                    <input type="number" id="switch-diverge-radius" class="input-small-field input-small-field-compact" value="${switchData.diverge_radius_cm != null ? Math.round(switchData.diverge_radius_cm * 10) : ''}" min="0" step="1" inputmode="numeric" placeholder="None (straight)">
                </div>
            </div>

            <!-- OTHER PROPERTIES SECTION -->
            <div class="control-section">
                <div class="control-section-header">Properties</div>
                <div class="input-large-container mb-8">
                    <label class="input-large-label">Switch Name</label>
                    <input type="text" id="switch-name" class="input-large-field" value="${escapeAttr(switchData.name || '')}" placeholder="Switch name...">
                </div>
                <div class="property-item mb-8">
                    <label class="property-label">Notes</label>
                    <input type="text" id="switch-notes" class="property-input" value="${escapeAttr(switchData.notes || '')}" placeholder="Notes...">
                </div>

                ${renderAvailabilitySegment('switch', switchData, { idBase: 'switch-availability' })}
            </div>

            <!-- Maintenance — read-only wear counter (design_new.md "Maintenance
                 counters"); counts real alignment changes during simulation. -->
            <div class="control-section">
                <div class="control-section-header">Maintenance</div>
                <div class="input-small-row">
                    <span class="input-small-label input-small-label-inline">Times flipped</span>
                    <span class="font-mono" title="Number of times this switch has changed alignment">${switchData.flip_count || 0}</span>
                </div>
            </div>

            <!-- Deleting the switch config is a switch-level action, not a
                 property edit — kept apart at the foot as a quiet danger button. -->
            <div class="control-section control-section-divider mt-12 pt-12">
                <button id="delete-switch-btn" class="btn btn-danger-ghost btn-sm w-full"><i class="ti ti-trash"></i> Delete switch</button>
            </div>
        `;

    this.editor.showSecondarySidebar('Switch Editor', content);

    // Bind event handlers
    this.bindEventHandlers(switchData);
  }

  bindEventHandlers(switchData: any): void {
    // Availability segment (disabled / manual). Per docs/DESIGN_LANGUAGE.md
    // §3 segmented control and design_new.md "Availability flag REST API
    // surface". Toggling `disabled = True` runs admin-reject preconditions
    // on the server (HTTP 409); toggling `manual` always succeeds.
    bindAvailabilitySegment('switch-availability', async (_state, flags) => {
      try {
        const result = await this.api.updateSwitch(switchData.switch_id, flags);
        if (result && result.error) {
          showToast(`Cannot change availability: ${result.error}`, 'error', 4000);
          this.showEditor(switchData.switch_id);
        } else {
          await this.editor.host?.refreshNetwork();
        }
      } catch (err: any) {
        console.error('Failed to update switch availability:', err);
        showToast(`Cannot change availability: ${err.message || err}`, 'error', 4000);
        this.showEditor(switchData.switch_id);
      }
    });

    // Toggle switch state
    const toggleBtn = this.editor.$id('toggle-switch-btn') as HTMLButtonElement | null;
    if (toggleBtn && !toggleBtn.disabled) {
      toggleBtn.onclick = async () => {
        try {
          // Get current track state to determine toggle direction
          const track = await this.api.getTrack(switchData.x, switchData.y);
          const currentState = track?.switch_state ?? 0;
          const newState = currentState === 0 ? 1 : 0;

          await this.api.setSwitchState(switchData.switch_id, newState);
          showToast('Switch state toggled', 'success', 2000);
          await this.editor.host?.refreshNetwork();
          // Refresh editor
          await this.showEditor(switchData.switch_id);
        } catch (err: any) {
          console.error('[SwitchEditorHandler] Failed to toggle switch:', err);
          showToast('Failed to toggle switch: ' + err.message, 'error', 5000);
        }
      };
    }

    // Per-field auto-save (§5C / §19). Each field commits on blur via the
    // shared `bindFieldAutoSave` wiring — no Save button, no success toast, a
    // flash as the ack. These fields don't change the canvas, so they skip
    // refreshNetwork (which would re-render the panel through the subscription
    // and wipe the flash); the live subscription still reflects sim-driven
    // state once the field loses focus.
    const sid = switchData.switch_id;
    const saveSwitchField = (field: string) => (value: unknown) =>
      this.api.updateSwitch(sid, { [field]: value });
    bindFieldAutoSave(this.editor.$id('switch-name') as HTMLInputElement | null, {
      parse: parseText,
      commit: saveSwitchField('name'),
    });
    bindFieldAutoSave(this.editor.$id('switch-notes') as HTMLInputElement | null, {
      parse: parseText,
      commit: saveSwitchField('notes'),
    });
    bindFieldAutoSave(this.editor.$id('switch-main-length') as HTMLInputElement | null, {
      parse: parseRequiredMmToCm('Main length'),
      commit: saveSwitchField('main_length_cm'),
    });
    bindFieldAutoSave(this.editor.$id('switch-diverge-length') as HTMLInputElement | null, {
      parse: parseRequiredMmToCm('Diverge length'),
      commit: saveSwitchField('diverge_length_cm'),
    });
    bindFieldAutoSave(this.editor.$id('switch-main-radius') as HTMLInputElement | null, {
      parse: parseOptionalMmToCm('Main radius'),
      commit: saveSwitchField('main_radius_cm'),
    });
    bindFieldAutoSave(this.editor.$id('switch-diverge-radius') as HTMLInputElement | null, {
      parse: parseOptionalMmToCm('Diverge radius'),
      commit: saveSwitchField('diverge_radius_cm'),
    });

    // Delete switch
    const deleteBtn = this.editor.$id('delete-switch-btn') as HTMLButtonElement | null;
    if (deleteBtn) {
      deleteBtn.onclick = async () => {
        const confirmed = await showConfirmModal({
          title: 'Delete switch',
          message: `Delete switch "${switchData.switch_id}"?`,
          detail: 'The switch configuration will be removed, but track geometry will remain.',
          confirmText: 'Delete',
          confirmVariant: 'danger',
        });
        if (!confirmed) {
          return;
        }

        try {
          await this.api.deleteSwitch(switchData.switch_id);
          showToast('Switch deleted', 'success', 2000);
          await this.editor.host?.refreshNetwork();
          this.editor.hideSecondarySidebar();
        } catch (err: any) {
          console.error('[SwitchEditorHandler] Failed to delete switch:', err);
          showToast('Failed to delete switch: ' + err.message, 'error', 5000);
        }
      };
    }

    // Flank / train-protection cross-navigation: clicking a switch reference in
    // either read-only section opens that switch's editor (same data-switch-id
    // pattern as the track-inspector Edit buttons in editor.ts). showEditor
    // re-fetches and re-renders, so no manual teardown is needed here.
    const sidebar: HTMLElement | null = this.editor.$id('secondary-sidebar-content');
    sidebar?.querySelectorAll<HTMLButtonElement>('.flank-nav-btn').forEach((btn) => {
      btn.onclick = () => {
        const targetId = btn.dataset.switchId;
        if (targetId) {
          this.showEditor(targetId);
        }
      };
    });

    // ═══════════════════════════════════════════════════════════════
    // SUBSCRIPTION SETUP (Phase 4)
    // ═══════════════════════════════════════════════════════════════

    this._cleanup();
    this._switchId = switchData.switch_id;

    this._unsubscribe =
      this.editor.stateManager?.subscribe(
        'switches',
        switchData.switch_id,
        (newSwitch: any, oldSwitch: any) => this._onSwitchUpdate(newSwitch, oldSwitch),
        { immediate: false }
      ) ?? null;

    // Freeze structural fields in Simulation mode; Toggle State, availability,
    // and the flank-navigation links stay live (see applyStructuralLock).
    applyStructuralLock(this.editor.$id('secondary-sidebar-content'));
  }

  _onSwitchUpdate(newSwitch: any, oldSwitch: any): void {
    if (!newSwitch) {
      showToast('Switch was deleted', 'warning');
      this.editor.hideSecondarySidebar();
      return;
    }

    const activeElement = document.activeElement;
    if (
      activeElement &&
      (activeElement.tagName === 'INPUT' ||
        activeElement.tagName === 'TEXTAREA' ||
        activeElement.tagName === 'SELECT')
    ) {
      return;
    }

    this._updateSwitchDisplay(newSwitch);
  }

  async _updateSwitchDisplay(switchData: any): Promise<void> {
    // The status header chrome + lock banner are structural (the banner
    // appears/disappears based on lock state, and the state pill's text
    // and accent class both change with switch state). Re-render via
    // showEditor so every part stays in sync; _onSwitchUpdate already
    // skips this path while the user is editing form fields.
    await this.showEditor(switchData.switch_id);
  }

  // === Switches List Panel ===

  /** Show the switches list panel — every switch on the layout. */
  async showSwitchesPanel(): Promise<void> {
    // Leaving any per-switch editor — drop its live subscription and any
    // running list poller before we (re)build the list.
    this._cleanup();

    this.editor._currentEditorType = 'switches-list';
    this.editor._currentEditorId = null;

    try {
      const result = await this.api.getSwitches();
      this.switches = Array.isArray(result) ? result : [];
    } catch (err: any) {
      console.error('[SwitchEditorHandler] Failed to load switches:', err);
      showToast('Failed to load switches: ' + err.message, 'error', 5000);
      this.switches = [];
    }

    this._renderSwitchesListSidebar();
    this._startSwitchesListPoller();
  }

  /** Re-render the switches list from `this.switches` without re-fetching.
   *  Shared by showSwitchesPanel and the background list poller so the list
   *  stays live (position, lock, occupancy) without a manual refresh. */
  _renderSwitchesListSidebar(): void {
    const switches = [...this.switches].sort((a, b) =>
      String(a.switch_id).localeCompare(String(b.switch_id), undefined, { numeric: true }),
    );

    let content = '<div class="switches-panel">';

    if (switches.length === 0) {
      content += `
            <div class="empty-state">
                <div class="mb-8">No switches on the layout</div>
                <div class="text-sm">Switches appear here when you place switch track.</div>
            </div>`;
    } else {
      const n = switches.length;
      content += `<div class="switches-count text-muted text-xs mb-8">${n} switch${n !== 1 ? 'es' : ''}</div>`;
      content += '<div class="switches-list">';
      for (const sw of switches) {
        content += this.renderSwitchListItem(sw);
      }
      content += '</div>';
    }

    content += '</div>';

    this.editor.showSecondarySidebar('Switches', content);
    setTimeout(() => this.initSwitchesPanelHandlers(), 0);
  }

  renderSwitchListItem(sw: any): string {
    const position = Number(sw.position) || 0;
    const stateText = position === 0 ? 'MAIN' : 'DIVERGE';
    const stateChip = position === 0 ? 'chip-success' : 'chip-warn';

    const isLocked = Array.isArray(sw.locks) && sw.locks.length > 0;
    const isOccupied = sw.occupied_by != null && sw.occupied_by !== '';
    const isDisabled = !!sw.disabled;
    const isManual = !!sw.manual;

    // §6 left-border accent: a disabled switch reads as muted; otherwise the
    // accent mirrors the per-switch editor (MAIN → success, DIVERGE → warn).
    const accent = isDisabled ? 'accent-muted' : position === 0 ? 'accent-success' : 'accent-warn';

    const name = sw.name && String(sw.name).trim() ? String(sw.name).trim() : '';
    const title = name || sw.switch_id;
    // With a display name the id drops to the secondary caption; without one the
    // id is the title and the caption is just the tile coordinates.
    const caption = name ? `${sw.switch_id} · (${sw.x}, ${sw.y})` : `(${sw.x}, ${sw.y})`;

    const chips = [`<span class="chip chip-state ${stateChip}">${stateText}</span>`];
    if (isLocked) chips.push('<span class="chip chip-danger">Locked</span>');
    if (isOccupied)
      chips.push(`<span class="chip chip-warn">Occupied · ${this._esc(sw.occupied_by)}</span>`);
    if (isDisabled) chips.push('<span class="chip chip-muted">Disabled</span>');
    if (isManual) chips.push('<span class="chip chip-muted">Manual</span>');

    return `
            <div class="rr-card switch-row ${accent}" data-switch-id="${this._esc(sw.switch_id)}" role="button" tabindex="0" title="Open ${this._esc(title)}">
                <div class="rr-card-head">
                    <div class="rr-card-title-col">
                        <div class="rr-card-train">${this._esc(title)}</div>
                        <div class="rr-card-route font-mono">${this._esc(caption)}</div>
                    </div>
                </div>
                <div class="chip-row">${chips.join('')}</div>
            </div>`;
  }

  initSwitchesPanelHandlers(): void {
    document.querySelectorAll<HTMLElement>('.switch-row').forEach((row) => {
      const open = () => {
        const id = row.dataset.switchId;
        if (id) this.showEditor(id);
      };
      row.onclick = open;
      row.onkeydown = (e: KeyboardEvent) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          open();
        }
      };
    });
  }

  /** Start the switches-list background poller. Keeps the list live (position,
   *  lock, occupancy) without a manual refresh. */
  _startSwitchesListPoller(): void {
    this._stopSwitchesListPoller();
    this._switchesListInterval = setInterval(
      () =>
        this._syncSwitchesListFromServer().catch((err) => {
          console.debug('[SwitchEditorHandler] switches-list sync failed:', err);
        }),
      this._switchesListIntervalMs,
    );
  }

  _stopSwitchesListPoller(): void {
    if (this._switchesListInterval) {
      clearInterval(this._switchesListInterval);
      this._switchesListInterval = null;
    }
  }

  /** Pull the latest switches and re-render only when something the list shows
   *  actually changed. Skips the DOM swap while the cursor is over the list so a
   *  re-render can't steal a row click. */
  async _syncSwitchesListFromServer(): Promise<void> {
    // Panel changed under us (navigated to a switch editor / closed) — stop.
    if (this.editor._currentEditorType !== 'switches-list') {
      this._stopSwitchesListPoller();
      return;
    }
    if (typeof document !== 'undefined' && document.hidden) return;
    if (this._isSwitchesListHovered()) return;

    const result = await this.api.getSwitches();
    const next = Array.isArray(result) ? result : [];
    if (this._switchesListSignature(next) === this._switchesListSignature(this.switches)) {
      return;
    }
    this.switches = next;
    this._renderSwitchesListSidebar();
  }

  /** Signature of just the fields the list renders, so unrelated payload churn
   *  doesn't trigger a needless DOM swap. */
  _switchesListSignature(switches: any[]): string {
    return JSON.stringify(
      (switches || []).map((s) => [
        s.switch_id,
        s.name,
        s.position,
        Array.isArray(s.locks) ? s.locks.length : 0,
        s.occupied_by ?? null,
        !!s.disabled,
        !!s.manual,
      ]),
    );
  }

  /** True when the cursor is over the open switches list. */
  _isSwitchesListHovered(): boolean {
    const el = this.editor.$q('.switches-panel');
    if (!el || typeof el.matches !== 'function') return false;
    try {
      return el.matches(':hover');
    } catch (_) {
      return false;
    }
  }

  _esc(s: any): string {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  _cleanup(): void {
    this._stopSwitchesListPoller();
    if (this._unsubscribe) {
      this._unsubscribe();
      this._unsubscribe = null;
    }
    this._switchId = null;
  }
}
