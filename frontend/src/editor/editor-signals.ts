// editor-signals.ts — Signal Editor Handler.
//
// Migrated from static/js/editor-signals.js (longterm_todo.md item #7, Wave 2).
// Behavior is unchanged. Owned by the Editor (`new SignalEditorHandler(this)`).

import { showToast } from '../lib/toast';
import type { EditorHost } from './types';

export class SignalEditorHandler {
  editor: EditorHost;
  _signalId: string | null;
  _unsubscribe: (() => void) | null;
  _localState: any;

  constructor(editor: EditorHost) {
    this.editor = editor;
    this._signalId = null; // Currently displayed signal ID
    this._unsubscribe = null; // Unsubscribe function for StateManager
    this._localState = null; // Local copy for editing
  }

  get api(): any {
    return this.editor.api;
  }
  get cachedBlocks(): any {
    return this.editor.cachedBlocks;
  }
  set cachedBlocks(value: any) {
    this.editor.cachedBlocks = value;
  }
  get selectedElement(): any {
    return this.editor.selectedElement;
  }

  // Map a signal aspect to the §11 status-header left accent and the §7
  // state-chip variant. Green is the clear/default aspect → accent-success
  // (never --border-color, §11); yellow → warn; red → danger.
  getSignalStateAccent(state: string): { accentClass: string; chipClass: string } {
    const normalized = (state || 'green').toLowerCase();
    if (normalized === 'yellow') return { accentClass: 'accent-warn', chipClass: 'chip-warn' };
    if (normalized === 'red') return { accentClass: 'accent-danger', chipClass: 'chip-danger' };
    return { accentClass: 'accent-success', chipClass: 'chip-success' };
  }

  async showSignalsEditorForTile(track: any): Promise<void> {
    const signals = track.signals || [];

    if (signals.length === 0) {
      this.editor.hideSecondarySidebar();
      return;
    }

    const firstSignalId = signals[0].signal_id || signals[0].id;
    this.editor._currentEditorType = 'signal';
    this.editor._currentEditorId = firstSignalId;

    let signalsContent = '';
    const signalDetails = [];

    for (let i = 0; i < signals.length; i++) {
      const signalId = signals[i].signal_id || signals[i].id;
      const signal = await this.api.getSignal(signalId);
      if (!signal || signal.error) continue;

      signalDetails.push({ signal, index: i });
      const state = (signal.state || signal.signal_state || 'green').toUpperCase();
      const { accentClass, chipClass } = this.getSignalStateAccent(state);
      const directionText = signal.detection_direction
        ? `<span class="font-mono">${signal.detection_direction}</span>`
        : `<span class="text-danger">no direction</span>`;

      signalsContent += `
                <div${i > 0 ? ' class="control-section-divider mt-12 pt-12"' : ''}>
                    <!-- §11 Status header — identity + aspect pill, dynamic accent. -->
                    <div class="status-header ${accentClass}">
                        <div class="status-header-row">
                            <div class="status-header-id font-mono" title="${signal.signal_id}">${signal.signal_id}</div>
                            <span class="chip chip-state ${chipClass}">${state}</span>
                        </div>
                        <div class="status-header-ident">
                            <span class="status-header-role">Signal</span>
                            <span class="status-sep">·</span>
                            ${directionText}
                            ${signals.length > 1 ? `<span class="status-sep">·</span><span>${i + 1} of ${signals.length}</span>` : ''}
                        </div>
                    </div>

                    <!-- Editable Fields -->
                    <div class="control-section">
                        <div class="property-grid signal-grid-standard">
                            <div class="property-item">
                                <label class="property-label">Signal Name</label>
                                <input type="text" class="signal-name-input property-input" data-signal-id="${signal.signal_id}" value="${signal.name || ''}" placeholder="Enter signal name...">
                            </div>
                            <div class="property-item">
                                <label class="property-label">Block to Monitor</label>
                                <select class="signal-block-select property-input ${signal.monitored_block_id ? '' : 'property-input-warning'}" data-signal-id="${signal.signal_id}">
                                    <option value="">-- None (Manual Control) --</option>
                                    <option value="${signal.monitored_block_id || ''}" selected>${signal.monitored_block_id || 'None'} (Loading...)</option>
                                </select>
                                <div class="block-select-hint text-muted text-xs mt-4" data-signal-id="${signal.signal_id}">
                                    Signal shows RED when monitored block is occupied
                                </div>
                            </div>
                        </div>

                        <div class="mt-16">
                            <button class="save-signal-btn btn btn-accent btn-full text-base" data-signal-id="${signal.signal_id}">
                                Save Signal Properties
                            </button>
                        </div>
                    </div>
                </div>
            `;
    }

    const title = signals.length > 1 ? `Signals (${signals.length})` : 'Signal Editor';
    this.editor.showSecondarySidebar(title, signalsContent);

    for (const { signal } of signalDetails) {
      try {
        const result = await this.api.getReachableBlocks(signal.signal_id);
        const reachableBlockIds = result.reachable_blocks || [];

        const reachableBlocks = [];
        for (const blockId of reachableBlockIds) {
          let block = (this.cachedBlocks || []).find((b: any) => b.block_id === blockId);
          if (!block) {
            try {
              block = await this.api.getBlock(blockId);
              if (block && !block.error) {
                this.cachedBlocks = this.cachedBlocks || [];
                this.cachedBlocks.push(block);
              }
            } catch (err) {
              block = { block_id: blockId, name: null };
            }
          }
          if (block) reachableBlocks.push(block);
        }

        const blockSelect = document.querySelector<HTMLSelectElement>(
          `.signal-block-select[data-signal-id="${signal.signal_id}"]`
        );
        if (blockSelect) {
          const currentValue = signal.monitored_block_id;
          blockSelect.innerHTML = `
                        <option value="">-- None (Manual Control) --</option>
                        ${reachableBlocks
                          .map(
                            (b: any) => `
                            <option value="${b.block_id}" ${currentValue === b.block_id ? 'selected' : ''}>
                                ${b.block_id}${b.name ? ` (${b.name})` : ''}${b.station_names ? ` [${b.station_names.join(', ')}]` : ''}
                            </option>
                        `
                          )
                          .join('')}
                    `;

          const hintDiv = document.querySelector<HTMLElement>(
            `.block-select-hint[data-signal-id="${signal.signal_id}"]`
          );
          if (hintDiv) {
            if (reachableBlocks.length === 0) {
              hintDiv.innerHTML = '<span class="text-danger">No reachable blocks found.</span>';
            } else {
              hintDiv.innerHTML = `Signal shows RED when monitored block is occupied<br><span class="text-muted">${reachableBlocks.length} reachable block(s)</span>`;
            }
          }
        }
      } catch (err) {
        console.warn('Failed to fetch reachable blocks for', signal.signal_id, err);
      }
    }

    const saveBtns = document.querySelectorAll<HTMLElement>('.save-signal-btn');
    saveBtns.forEach((btn) => {
      btn.onclick = async () => {
        const signalId = btn.dataset.signalId;
        const nameInput = document.querySelector<HTMLInputElement>(
          `.signal-name-input[data-signal-id="${signalId}"]`
        );
        const blockSelect = document.querySelector<HTMLSelectElement>(
          `.signal-block-select[data-signal-id="${signalId}"]`
        );

        const updates: Record<string, unknown> = {};
        if (nameInput) updates.name = nameInput.value.trim() || null;
        if (blockSelect) updates.monitored_block_id = blockSelect.value || null;

        try {
          await this.api.updateSignal(signalId, updates);
          showToast('Signal updated successfully', 'success', 5000);
          await this.editor.host?.refreshNetwork();

          if (this.selectedElement) {
            await this.editor.selectElement({ x: this.selectedElement.x, y: this.selectedElement.y });
          }
        } catch (err: any) {
          console.error('Failed to save signal properties:', err);
          showToast('Failed to save signal properties: ' + err.message, 'error', 5000);
        }
      };
    });
  }

  initSignalEditor(track: any): void {
    const editSignalBtns = document.querySelectorAll<HTMLElement>('.edit-signal-btn');
    editSignalBtns.forEach((btn) => {
      btn.onclick = () => {
        this.showSignalsEditorForTile(track);
      };
    });
  }

  async showSignalEditor(signalId: string): Promise<void> {
    this.editor._currentEditorType = 'signal';
    this.editor._currentEditorId = signalId;

    const signal = await this.api.getSignal(signalId);
    if (!signal || signal.error) {
      showToast('Signal not found', 'error', 5000);
      return;
    }

    const state = (signal.state || signal.signal_state || 'green').toLowerCase();
    const { accentClass, chipClass } = this.getSignalStateAccent(state);
    const stateText = state.toUpperCase();
    const directionText = signal.detection_direction
      ? `<span class="font-mono">${signal.detection_direction}</span>`
      : `<span class="text-danger">no direction</span>`;

    const content = `
            <!-- §11 Status header — identity + aspect pill, dynamic accent. -->
            <div class="status-header ${accentClass}" id="signal-status-header">
                <div class="status-header-row">
                    <div class="status-header-id font-mono" title="${signal.signal_id}">${signal.signal_id}</div>
                    <span class="chip chip-state ${chipClass}" id="signal-state-chip">${stateText}</span>
                </div>
                <div class="status-header-ident">
                    <span class="status-header-role">Signal</span>
                    <span class="status-sep">·</span>
                    ${directionText}
                    <span class="status-sep">·</span>
                    <span class="font-mono">(${signal.x}, ${signal.y})</span>
                </div>
            </div>

            <!-- EDITABLE FIELDS SECTION -->
            <div class="control-section">
                <div class="control-section-header">Properties</div>

                <div class="property-grid signal-grid-standard">
                    <div class="property-item">
                        <label class="property-label">Signal Name</label>
                        <input type="text" id="signal-name-input" class="property-input" value="${signal.name || ''}" placeholder="Enter signal name...">
                    </div>
                    <div class="property-item">
                        <label class="property-label">Block to Monitor</label>
                        <select id="signal-block-select" class="property-input ${signal.monitored_block_id ? '' : 'property-input-warning'}">
                            <option value="">-- None (Manual Control) --</option>
                            <option value="${signal.monitored_block_id || ''}" selected>${signal.monitored_block_id || 'None'} (Loading...)</option>
                        </select>
                        <div id="block-select-hint" class="text-muted text-xs mt-4">
                            Signal shows RED when monitored block is occupied
                        </div>
                    </div>
                </div>

                <div class="mt-16">
                    <button id="save-signal-btn" class="btn btn-accent btn-full text-base">
                        Save Signal Properties
                    </button>
                </div>
            </div>
        `;

    this.editor.showSecondarySidebar('Signal Editor', content);

    try {
      const result = await this.api.getReachableBlocks(signalId);
      const reachableBlockIds = result.reachable_blocks || [];

      const reachableBlocks = [];
      for (const blockId of reachableBlockIds) {
        let block = (this.cachedBlocks || []).find((b: any) => b.block_id === blockId);
        if (!block) {
          try {
            block = await this.api.getBlock(blockId);
            if (block && !block.error) {
              this.cachedBlocks = this.cachedBlocks || [];
              this.cachedBlocks.push(block);
            }
          } catch (err) {
            block = { block_id: blockId, name: null };
          }
        }
        if (block) reachableBlocks.push(block);
      }

      const blockSelect = this.editor.$id('signal-block-select') as HTMLSelectElement | null;
      if (blockSelect) {
        const currentValue = signal.monitored_block_id;
        blockSelect.innerHTML = `
                    <option value="">-- None (Manual Control) --</option>
                    ${reachableBlocks
                      .map(
                        (b: any) => `
                        <option value="${b.block_id}" ${currentValue === b.block_id ? 'selected' : ''}>
                            ${b.block_id}${b.name ? ` (${b.name})` : ''}${b.station_names ? ` [${b.station_names.join(', ')}]` : ''}
                        </option>
                    `
                      )
                      .join('')}
                `;

        const hintDiv = this.editor.$id('block-select-hint');
        if (hintDiv) {
          if (reachableBlocks.length === 0) {
            hintDiv.innerHTML =
              '<span class="text-danger">No reachable blocks found. Check track connectivity.</span>';
          } else {
            hintDiv.innerHTML = `Signal shows RED when monitored block is occupied<br><span class="text-muted">${reachableBlocks.length} reachable block(s)</span>`;
          }
        }
      }
    } catch (err) {
      console.warn('Failed to fetch reachable blocks:', err);
    }

    const saveBtn = this.editor.$id('save-signal-btn') as HTMLButtonElement | null;
    if (saveBtn) {
      saveBtn.onclick = async () => {
        const nameInput = this.editor.$id('signal-name-input') as HTMLInputElement | null;
        const blockSelect = this.editor.$id('signal-block-select') as HTMLSelectElement | null;

        const updates: Record<string, unknown> = {};
        if (nameInput) updates.name = nameInput.value.trim() || null;
        if (blockSelect) updates.monitored_block_id = blockSelect.value || null;

        try {
          await this.api.updateSignal(signalId, updates);
          showToast('Signal updated successfully', 'success', 5000);
          await this.editor.host?.refreshNetwork();

          if (this.selectedElement) {
            await this.editor.selectElement({ x: this.selectedElement.x, y: this.selectedElement.y });
          }
        } catch (err: any) {
          console.error('Failed to save signal properties:', err);
          showToast('Failed to save signal properties: ' + err.message, 'error', 5000);
        }
      };
    }

    // ═══════════════════════════════════════════════════════════════
    // SUBSCRIPTION SETUP (NEW - Phase 4)
    // ═══════════════════════════════════════════════════════════════

    // Cleanup previous subscription
    this._cleanup();

    // Store current signal ID and initial state
    this._signalId = signalId;
    this._localState = JSON.parse(JSON.stringify(signal));

    // Subscribe to signal updates
    this._unsubscribe =
      this.editor.stateManager?.subscribe(
        'signals',
        signalId,
        (newSignal: any, oldSignal: any) => this._onSignalUpdate(newSignal, oldSignal),
        { immediate: false }
      ) ?? null;
  }

  /** Handle signal updates from StateManager. */
  _onSignalUpdate(newSignal: any, oldSignal: any): void {
    if (!newSignal) {
      // Signal was deleted
      showToast('Signal was deleted', 'warning');
      this.editor.hideSecondarySidebar();
      return;
    }

    // Check if user is actively editing
    const activeElement = document.activeElement;
    if (
      activeElement &&
      (activeElement.tagName === 'INPUT' ||
        activeElement.tagName === 'TEXTAREA' ||
        activeElement.tagName === 'SELECT')
    ) {
      // User is typing - don't interrupt
      return;
    }

    // Update local state and refresh display
    this._localState = JSON.parse(JSON.stringify(newSignal));
    this._updateSignalDisplay(newSignal);
  }

  /** Update signal display values without rebuilding the entire panel. */
  _updateSignalDisplay(signal: any): void {
    const state = (signal.state || signal.signal_state || 'green').toLowerCase();
    const { accentClass, chipClass } = this.getSignalStateAccent(state);
    const stateText = state.toUpperCase();

    // Re-drive the §11 status-header left accent and the §7 state chip in
    // place. The single-signal editor is the only path with a live
    // subscription; the multi-signal tile view has none.
    const header = this.editor.$id('signal-status-header');
    if (header) {
      header.classList.remove('accent-success', 'accent-warn', 'accent-danger');
      header.classList.add(accentClass);
    }
    const chip = this.editor.$id('signal-state-chip');
    if (chip) {
      chip.classList.remove('chip-success', 'chip-warn', 'chip-danger');
      chip.classList.add(chipClass);
      chip.textContent = stateText;
    }

    // The monitored block now lives only in the editable select; keep it in
    // sync when the value changes server-side. _onSignalUpdate already bails
    // when a form control is focused, so this never clobbers an active edit.
    const blockSelect = this.editor.$id('signal-block-select') as HTMLSelectElement | null;
    if (blockSelect && signal.monitored_block_id) {
      blockSelect.value = signal.monitored_block_id;
    }
  }

  /** Cleanup subscription. */
  _cleanup(): void {
    if (this._unsubscribe) {
      this._unsubscribe();
      this._unsubscribe = null;
    }
    this._signalId = null;
    this._localState = null;
  }

  renderSignalList(signals: any[]): string {
    if (!signals || signals.length === 0) {
      return '<div class="empty-state">No signals</div>';
    }

    return signals
      .map(
        (signal) => `
            <div class="signal-item">
                <span class="signal-list-id">${signal.signal_id || 'Unknown'}</span>
                <span class="signal-list-dir"> → ${signal.detection_direction || 'N/A'}</span>
                <span class="chip chip-state chip-${signal.state === 'green' ? 'success' : signal.state === 'yellow' ? 'warn' : 'danger'}" style="margin-left:auto">${(signal.state || 'red').toUpperCase()}</span>
            </div>
        `
      )
      .join('');
  }

  async invalidateSignalsInPath(signalsInPath: any[]): Promise<void> {
    if (!signalsInPath || signalsInPath.length === 0) return;

    for (const signal of signalsInPath) {
      try {
        const signalData = await this.api.getSignalAt(signal.x, signal.y);
        if (signalData && signalData.signal_id) {
          await this.api.updateSignal(signalData.signal_id, {
            monitored_block_id: null,
          });
        }
      } catch (err) {
        console.warn(
          `[SignalEditorHandler] Failed to invalidate signal at (${signal.x}, ${signal.y}):`,
          err
        );
      }
    }
  }
}
