// editor-switches.ts — Switch Editor Handler.
//
// Migrated from static/js/editor-switches.js (longterm_todo.md item #7, Wave 2 —
// editor handler cluster). Behavior is unchanged from the classic file.
//
// Owned by the Editor (`new SwitchEditorHandler(this)`).

import { stateManager } from '../lib/state-manager';
import { showToast } from '../lib/toast';
import type { EditorHost } from './types';
import { renderAvailabilitySegment, bindAvailabilitySegment, showConfirmModal } from '../lib/helpers';

/** Read the trimmed/raw value of a text/number input by id. */
function inputValue(id: string): string | undefined {
  return (document.getElementById(id) as HTMLInputElement | null)?.value;
}

export class SwitchEditorHandler {
  editor: EditorHost;
  _switchId: string | null;
  _unsubscribe: (() => void) | null;
  _localState: any;
  _isDirty: boolean;

  constructor(editor: EditorHost) {
    this.editor = editor;
    this._switchId = null;
    this._unsubscribe = null;
    this._localState = null;
    this._isDirty = false;
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
    // (design_new.md:651); read the lock owner directly from it. The
    // first entry's `path_id` is the representative "locked by" path for
    // the banner.
    const lockEntries = Array.isArray(switchData.locks) ? switchData.locks : [];
    const lockedByPath = lockEntries.length > 0 ? lockEntries[0].path_id || null : null;
    const isLocked = lockedByPath !== null;
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
                        <button class="edit-btn flank-nav-btn font-mono" data-switch-id="${neighbourId}" title="Open ${neighbourId} editor">${neighbourId}</button>
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
                        <span class="font-mono">${trainId}</span>
                        <span class="flex-1"></span>
                        <span class="text-muted text-xs">from</span>
                        <button class="edit-btn flank-nav-btn font-mono" data-switch-id="${sourceId}" title="Open ${sourceId} editor">${sourceId}</button>
                        <span class="chip chip-state ${stateChip}">${stateLabel}</span>
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

            <!-- §6 banner — surfaces the lock owner when locked. Hidden when free. -->
            ${
              isLocked
                ? `
            <div class="banner banner-danger">
                <div class="banner-label">Locked by</div>
                <div class="banner-value banner-text">${lockedByPath}</div>
            </div>
            `
                : ''
            }

            <!-- Flank Protection — read-only: neighbouring switches this one mutually flank-protects. -->
            <div class="control-section">
                <div class="control-section-header">Flank Protection${flankEntries.length ? ` (${flankEntries.length})` : ''}</div>
                ${
                  flankEntries.length
                    ? `<div class="text-muted text-xs mb-8">Neighbours held at their safe state while this switch's route is locked.</div>${flankRows}`
                    : `<div class="text-muted text-xs">None — no nearby switches require mutual locking.</div>`
                }
            </div>

            <!-- Train Protection — read-only, runtime: train-owned protection currently held on this switch. -->
            <div class="control-section">
                <div class="control-section-header">Train Protection${protectionClaims.length ? ` (${protectionClaims.length})` : ''}</div>
                ${
                  protectionClaims.length
                    ? `<div class="text-muted text-xs mb-8">Active train-owned protection held while a train occupies the source switch.</div>${protectionRows}`
                    : `<div class="text-muted text-xs">None — no train is currently protecting this switch.</div>`
                }
            </div>

            <!-- Controls -->
            <div class="control-section">
                <div class="control-section-header">Controls</div>
                <div class="flex gap-8">
                    <button id="toggle-switch-btn" class="btn btn-secondary btn-sm flex-1" ${isLocked ? 'disabled' : ''}>
                        Toggle State
                    </button>
                </div>
                ${isLocked ? '<div class="text-warning text-xs mt-8">Switch is locked and cannot be toggled.</div>' : ''}
            </div>

            <!-- LENGTH PROPERTIES SECTION -->
            <div class="control-section">
                <div class="control-section-header">Path Lengths (cm)</div>
                <div class="input-small-row mb-8">
                    <label class="input-small-label input-small-label-inline" for="switch-main-length">MAIN Path Length</label>
                    <input type="number" id="switch-main-length" class="input-small-field input-small-field-compact" value="${switchData.main_length_cm || 0}" min="0" step="0.1" placeholder="Length in cm">
                </div>
                <div class="input-small-row mb-8">
                    <label class="input-small-label input-small-label-inline" for="switch-diverge-length">DIVERGE Path Length</label>
                    <input type="number" id="switch-diverge-length" class="input-small-field input-small-field-compact" value="${switchData.diverge_length_cm || 0}" min="0" step="0.1" placeholder="Length in cm">
                </div>
            </div>

            <!-- RADIUS PROPERTIES SECTION -->
            <div class="control-section">
                <div class="control-section-header">Path Radii (cm)</div>
                <div class="input-small-row mb-8">
                    <label class="input-small-label input-small-label-inline" for="switch-main-radius">MAIN Path Radius</label>
                    <input type="number" id="switch-main-radius" class="input-small-field input-small-field-compact" value="${switchData.main_radius_cm || ''}" min="0" step="0.1" placeholder="None (straight)">
                </div>
                <div class="input-small-row mb-8">
                    <label class="input-small-label input-small-label-inline" for="switch-diverge-radius">DIVERGE Path Radius</label>
                    <input type="number" id="switch-diverge-radius" class="input-small-field input-small-field-compact" value="${switchData.diverge_radius_cm || ''}" min="0" step="0.1" placeholder="None (straight)">
                </div>
            </div>

            <!-- OTHER PROPERTIES SECTION -->
            <div class="control-section">
                <div class="control-section-header">Properties</div>
                <div class="input-large-container mb-8">
                    <label class="input-large-label">Switch Name</label>
                    <input type="text" id="switch-name" class="input-large-field" value="${switchData.name || ''}" placeholder="Switch name...">
                </div>
                <div class="property-item mb-8">
                    <label class="property-label">Notes</label>
                    <input type="text" id="switch-notes" class="property-input" value="${switchData.notes || ''}" placeholder="Notes...">
                </div>

                ${renderAvailabilitySegment('switch', switchData, { idBase: 'switch-availability' })}

                <div class="flex gap-8 mt-16">
                    <button id="save-switch-btn" class="btn btn-secondary btn-sm flex-1">Save Properties</button>
                    <button id="delete-switch-btn" class="btn btn-danger btn-sm">Delete</button>
                </div>
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
          showToast(`Switch ${switchData.switch_id} availability updated`, 'info', 1500);
          await window.app?.refreshNetwork();
        }
      } catch (err: any) {
        console.error('Failed to update switch availability:', err);
        showToast(`Cannot change availability: ${err.message || err}`, 'error', 4000);
        this.showEditor(switchData.switch_id);
      }
    });

    // Toggle switch state
    const toggleBtn = document.getElementById('toggle-switch-btn') as HTMLButtonElement | null;
    if (toggleBtn && !toggleBtn.disabled) {
      toggleBtn.onclick = async () => {
        try {
          // Get current track state to determine toggle direction
          const track = await this.api.getTrack(switchData.x, switchData.y);
          const currentState = track?.switch_state ?? 0;
          const newState = currentState === 0 ? 1 : 0;

          await this.api.setSwitchState(switchData.switch_id, newState);
          showToast('Switch state toggled', 'success', 2000);
          await window.app?.refreshNetwork();
          // Refresh editor
          await this.showEditor(switchData.switch_id);
        } catch (err: any) {
          console.error('[SwitchEditorHandler] Failed to toggle switch:', err);
          showToast('Failed to toggle switch: ' + err.message, 'error', 5000);
        }
      };
    }

    // Save properties
    const saveBtn = document.getElementById('save-switch-btn') as HTMLButtonElement | null;
    if (saveBtn) {
      saveBtn.onclick = async () => {
        const updates = {
          name: inputValue('switch-name')?.trim() || '',
          notes: inputValue('switch-notes')?.trim() || '',
          main_length_cm: parseFloat(inputValue('switch-main-length') || '') || 0,
          diverge_length_cm: parseFloat(inputValue('switch-diverge-length') || '') || 0,
          main_radius_cm: parseFloat(inputValue('switch-main-radius') || '') || null,
          diverge_radius_cm: parseFloat(inputValue('switch-diverge-radius') || '') || null,
        };

        try {
          await this.api.updateSwitch(switchData.switch_id, updates);
          showToast('Switch properties saved', 'success', 2000);
          await window.app?.refreshNetwork();
        } catch (err: any) {
          console.error('[SwitchEditorHandler] Failed to save switch:', err);
          showToast('Failed to save switch: ' + err.message, 'error', 5000);
        }
      };
    }

    // Delete switch
    const deleteBtn = document.getElementById('delete-switch-btn') as HTMLButtonElement | null;
    if (deleteBtn) {
      deleteBtn.onclick = async () => {
        const confirmed = await showConfirmModal({
          title: 'Delete Switch',
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
          await window.app?.refreshNetwork();
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
    const sidebar = document.getElementById('secondary-sidebar-content');
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
    this._localState = JSON.parse(JSON.stringify(switchData));
    this._isDirty = false;

    this._unsubscribe =
      stateManager?.subscribe(
        'switches',
        switchData.switch_id,
        (newSwitch: any, oldSwitch: any) => this._onSwitchUpdate(newSwitch, oldSwitch),
        { immediate: false }
      ) ?? null;
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

    if (this._isDirty) return;

    this._localState = JSON.parse(JSON.stringify(newSwitch));
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

  _cleanup(): void {
    if (this._unsubscribe) {
      this._unsubscribe();
      this._unsubscribe = null;
    }
    this._switchId = null;
    this._localState = null;
    this._isDirty = false;
  }
}
