// editor-blocks.ts — Block Editor Handler.
//
// Migrated from static/js/editor-blocks.js (longterm_todo.md item #7, Wave 2).
// Behavior is unchanged. Owned by the Editor (`new BlockEditorHandler(this)`).

import { stateManager } from '../lib/state-manager';
import { showToast } from '../lib/toast';
import type { EditorHost } from './types';
import {
  renderHeaderId,
  renderTileList,
  renderAvailabilitySegment,
  renderDirectionSegment,
  bindAvailabilitySegment,
  bindDirectionSegment,
  showConfirmModal,
  detectOrientation,
  calculateNextTile,
} from '../lib/helpers';

export class BlockEditorHandler {
  editor: EditorHost;
  _blockId: string | null;
  _unsubscribe: (() => void) | null;
  _localState: any;
  _isUserInteracting: boolean;

  constructor(editor: EditorHost) {
    this.editor = editor;
    this._blockId = null;
    this._unsubscribe = null;
    this._localState = null;
    this._isUserInteracting = false; // For tile picker drag operations
  }

  // Convenience getters for common editor properties
  get api(): any {
    return this.editor.api;
  }
  get cachedBlocks(): any {
    return this.editor.cachedBlocks;
  }
  set cachedBlocks(value: any) {
    this.editor.cachedBlocks = value;
  }
  get selectedBlockId(): any {
    return this.editor.selectedBlockId;
  }
  set selectedBlockId(value: any) {
    this.editor.selectedBlockId = value;
  }
  get selectedElement(): any {
    return this.editor.selectedElement;
  }

  // === Block Manager Methods ===

  setup(): void {
    // Block Manager UI has been removed - blocks are now managed via
    // the secondary sidebar block editor. Keep the block cache refresh.
    this.refreshList();
  }

  async refreshList(): Promise<void> {
    try {
      const blocks = await this.api.getBlocks();
      this.cachedBlocks = blocks;
      // No longer renders to UI - Block Manager panel removed
    } catch (err) {
      console.error('[BlockEditorHandler] Failed to refresh blocks:', err);
    }
  }

  select(blockId: string): void {
    this.selectedBlockId = blockId;
    // Block selection now only highlights tracks on canvas
    // UI list no longer exists
  }

  getColor(blockId: string): string {
    // Match renderer's HSL hash-based color generation
    if (!blockId) return '#666';
    let hash = 0;
    for (let i = 0; i < blockId.length; i++) {
      hash = blockId.charCodeAt(i) + ((hash << 5) - hash);
    }
    const hue = Math.abs(hash) % 360;
    return `hsl(${hue}, 70%, 50%)`;
  }

  getBlockStateClass(state: string): string {
    const map: Record<string, string> = {
      free: 'block-state-free',
      reserved: 'block-state-reserved',
      occupied: 'block-state-occupied',
      caution: 'block-state-caution',
    };
    return map[(state || '').toLowerCase()] || 'block-state-unknown';
  }

  // === Block Editor Panel ===

  async showEditor(blockId: string): Promise<void> {
    // Track this editor for auto-refresh
    this.editor._currentEditorType = 'block';
    this.editor._currentEditorId = blockId;

    // Fetch fresh block data from the API to ensure we have current state
    let block;
    try {
      block = await this.api.getBlock(blockId);
      if (block.error) {
        console.warn('[BlockEditorHandler] Block not found for editor:', blockId, block.error);
        return;
      }
    } catch (err) {
      // Fallback to cached data if API fails
      block = this.cachedBlocks?.find((b: any) => b.block_id === blockId);
      if (!block) {
        console.warn('[BlockEditorHandler] Block not found for editor:', blockId);
        return;
      }
    }

    // Resolve what forward / reverse mean physically for this block so the
    // map can draw F / R arrows at the block's terminal tiles while this
    // editor is open. The direction-info endpoint 404s (→ null) when a block
    // lacks geometry to disambiguate; we tolerate that.
    let directionGeometry = null;
    try {
      const [forward, reverse] = await Promise.all([
        this.api.getBlockDirectionInfo(blockId, 'forward'),
        this.api.getBlockDirectionInfo(blockId, 'reverse'),
      ]);
      directionGeometry = { forward: forward || null, reverse: reverse || null };
    } catch (err) {
      console.warn('[BlockEditorHandler] direction-info fetch failed:', err);
    }

    const blockColor = this.getColor(blockId);
    const blockStateClass = this.getBlockStateClass(block.state);

    const content = `
            <div class="block-editor-panel" data-block-id="${blockId}">
                ${renderHeaderId(blockId, blockColor)}

                <!-- INFO SECTION (Read-only, styled like Train Control) -->
                <div class="info-box-track mb-12">
                    <div class="flex justify-between items-start mb-2">
                        <span class="text-xs text-muted uppercase tracking-wider">State</span>
                        ${block.occupied_by ? `<span class="badge badge-warning text-xxs"><i class="ti ti-train"></i> ${block.occupied_by}</span>` : ''}
                    </div>

                    <div class="text-2xl font-bold mb-4 font-mono block-state-display ${blockStateClass}" id="block-state-display">
                        ${(block.state || 'free').toUpperCase()}
                    </div>

                    <div class="grid grid-cols-2 gap-y-2 gap-x-4 text-sm font-mono">
                        <div>
                            <span class="text-muted text-xs block">Track Count</span>
                            <span class="text-primary font-bold">${block.track_count || 0} tiles</span>
                        </div>
                        <div>
                            <span class="text-muted text-xs block">Length</span>
                            <span class="text-info">${block.length_cm || 0} cm</span>
                        </div>
                    </div>
                </div>

                <!-- TILES LIST (Scrollable with extend buttons) -->
                <div class="control-section mt-12">
                    <h4 class="text-sm text-muted mb-8">Tiles</h4>
                    ${renderTileList(block.sorted_positions, {
                      entityType: 'block',
                      extendStartBtnId: 'extend-block-start-btn',
                      extendEndBtnId: 'extend-block-end-btn',
                      removeBtnClass: 'delete-block-tile-btn',
                    })}
                </div>

                <!-- EDITABLE PROPERTIES SECTION -->
                <div class="control-section control-section-divider mt-12 pt-12">
                    <h4 class="text-sm text-muted mb-8">Properties</h4>

                    <div class="input-large-container mb-8">
                        <label class="input-large-label">Block Name</label>
                        <input type="text" id="block-name-input" class="input-large-field" value="${block.name || ''}" placeholder="Enter block name...">
                    </div>

                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="block-length-input">Length (cm)</label>
                        <input type="number" id="block-length-input" class="input-small-field input-small-field-compact" value="${block.length_cm || 0}" min="0" step="1">
                    </div>

                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="block-radius-input">Min Radius (cm)</label>
                        <input type="number" id="block-radius-input" class="input-small-field input-small-field-compact" value="${block.min_radius_cm || ''}" min="0" step="1" placeholder="Not set">
                    </div>

                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="block-speed-input">Max Speed (km/h)</label>
                        <input type="number" id="block-speed-input" class="input-small-field input-small-field-compact" value="${block.speed_limit_kmh || ''}" min="0" step="1" placeholder="Not set">
                    </div>

                    ${renderAvailabilitySegment('block', block, { idBase: 'block-availability' })}

                    ${renderDirectionSegment('block', block, { idBase: 'block-direction' })}

                    <div class="flex gap-8">
                        <button id="save-block-btn" class="btn btn-primary btn-sm flex-1"><i class="ti ti-device-floppy"></i> Save</button>
                        <button id="delete-block-btn" class="btn btn-danger btn-sm flex-1"><i class="ti ti-trash"></i> Delete</button>
                    </div>
                </div>
            </div>
        `;

    this.editor.showSecondarySidebar(`Block Editor`, content);

    // Draw the forward / reverse arrows on the map for this block.
    if (this.editor.renderer?.setBlockDirectionOverlay) {
      this.editor.renderer.setBlockDirectionOverlay(directionGeometry);
    }

    // Setup subscription before handlers
    this._setupSubscription(blockId, block);

    // Initialize event handlers after content is rendered
    setTimeout(() => this.initEditorHandlers(blockId), 0);
  }

  async initEditorHandlers(blockId: string): Promise<void> {
    const saveBtn = document.getElementById('save-block-btn') as HTMLButtonElement | null;
    const deleteBtn = document.getElementById('delete-block-btn') as HTMLButtonElement | null;
    const nameInput = document.getElementById('block-name-input') as HTMLInputElement | null;
    const lengthInput = document.getElementById('block-length-input') as HTMLInputElement | null;
    const radiusInput = document.getElementById('block-radius-input') as HTMLInputElement | null;
    const speedInput = document.getElementById('block-speed-input') as HTMLInputElement | null;

    // Availability segment (disabled / manual). Per docs/DESIGN_LANGUAGE.md §3.
    bindAvailabilitySegment('block-availability', async (_state, flags) => {
      try {
        const result = await this.api.updateBlock(blockId, flags);
        if (result && result.error) {
          showToast(`Cannot change availability: ${result.error}`, 'error', 4000);
          // Re-render to roll the active class back to the server's value.
          this.showEditor(blockId);
        } else {
          showToast(`Block ${blockId} availability updated`, 'info', 1500);
          await window.app?.refreshNetwork();
        }
      } catch (err: any) {
        console.error('Failed to update block availability:', err);
        showToast(`Cannot change availability: ${err.message || err}`, 'error', 4000);
        this.showEditor(blockId);
      }
    });

    // Allowed-travel-direction segment (forward / reverse / both).
    bindDirectionSegment('block-direction', async (_state, payload) => {
      try {
        const result = await this.api.updateBlock(blockId, payload);
        if (result && result.error) {
          showToast(`Cannot change travel direction: ${result.error}`, 'error', 4000);
          // Re-render to roll the active class back to the server's value.
          this.showEditor(blockId);
        } else {
          showToast(`Block ${blockId} travel direction updated`, 'info', 1500);
          await window.app?.refreshNetwork();
        }
      } catch (err: any) {
        console.error('Failed to update block travel direction:', err);
        showToast(`Cannot change travel direction: ${err.message || err}`, 'error', 4000);
        this.showEditor(blockId);
      }
    });

    // Save button
    if (saveBtn) {
      saveBtn.onclick = async () => {
        const updates: Record<string, unknown> = {};

        if (nameInput) {
          updates.name = nameInput.value.trim(); // Allow empty string
        }
        if (lengthInput) {
          const val = parseFloat(lengthInput.value);
          if (!isNaN(val) && val >= 0) {
            updates.length_cm = val;
          }
        }
        if (radiusInput) {
          const val = radiusInput.value.trim();
          if (val === '') {
            updates.min_radius_cm = null;
          } else {
            const numVal = parseFloat(val);
            if (!isNaN(numVal) && numVal >= 0) {
              updates.min_radius_cm = numVal;
            }
          }
        }
        if (speedInput) {
          const val = speedInput.value.trim();
          if (val === '') {
            updates.speed_limit_kmh = null;
          } else {
            const numVal = parseFloat(val);
            if (!isNaN(numVal) && numVal >= 0) {
              updates.speed_limit_kmh = numVal;
            }
          }
        }

        console.log('[BlockEditorHandler] Saving block updates:', blockId, updates);
        try {
          const result = await this.api.updateBlock(blockId, updates);
          console.log('[BlockEditorHandler] Block save result:', result);
          await window.app?.refreshNetwork();
          // Also refresh the blocks cache
          await this.refreshList();
          // Re-open to show updated values
          this.showEditor(blockId);
          // Also refresh main properties panel if it shows this block
          if (this.selectedElement?.block_section_id === blockId) {
            await this.editor.selectElement({ x: this.selectedElement.x, y: this.selectedElement.y });
          }
        } catch (err: any) {
          console.error('Failed to save block properties:', err);
          showToast('Failed to save block properties: ' + err.message, 'error', 5000);
        }
      };
    }

    // Delete button
    if (deleteBtn) {
      deleteBtn.onclick = async () => {
        const confirmed = await showConfirmModal({
          title: 'Delete Block',
          message: `Delete block "${blockId}"?`,
          detail: 'Tracks assigned to this block will be unassigned.',
          confirmText: 'Delete',
          confirmVariant: 'danger',
        });
        if (!confirmed) {
          return;
        }
        // Remember selected element before delete
        const selectedPos = this.selectedElement
          ? { x: this.selectedElement.x, y: this.selectedElement.y }
          : null;
        const wasShowingThisBlock = this.selectedElement?.block_section_id === blockId;
        try {
          await this.api.deleteBlock(blockId);
          await window.app?.refreshNetwork();
          this.editor.hideSecondarySidebar();
          // Refresh main properties panel if it was showing this block
          if (wasShowingThisBlock && selectedPos) {
            await this.editor.selectElement(selectedPos);
          }
        } catch (err: any) {
          console.error('Failed to delete block:', err);
          showToast('Failed to delete block: ' + err.message, 'error', 5000);
        }
      };
    }

    // Extend Start button
    const extendBlockStartBtn = document.getElementById('extend-block-start-btn') as HTMLButtonElement | null;
    if (extendBlockStartBtn) {
      extendBlockStartBtn.onclick = () => {
        this.extendBlock(blockId, 'start');
      };
    }

    // Extend End button
    const extendBlockEndBtn = document.getElementById('extend-block-end-btn') as HTMLButtonElement | null;
    if (extendBlockEndBtn) {
      extendBlockEndBtn.onclick = () => {
        this.extendBlock(blockId, 'end');
      };
    }

    // Tile delete buttons
    const deleteBlockTileBtns = document.querySelectorAll<HTMLElement>('.delete-block-tile-btn');
    deleteBlockTileBtns.forEach((btn) => {
      const x = parseInt(btn.dataset.x ?? '');
      const y = parseInt(btn.dataset.y ?? '');
      btn.onclick = async () => {
        try {
          await this.api.removeBlockTiles(blockId, [{ x, y }]);
          await window.app?.refreshNetwork();
          // Refresh block editor
          this.showEditor(blockId);
          // Refresh main properties panel if showing this tile
          if (this.selectedElement?.x === x && this.selectedElement?.y === y) {
            await this.editor.selectElement({ x, y });
          }
        } catch (err: any) {
          console.error('Failed to remove block tile:', err);
          showToast(`Failed to remove tile: ${err.message}`, 'error', 5000);
        }
      };
    });
  }

  // Auto-extend a block by one tile in the given direction.
  async extendBlock(blockId: string, direction: string): Promise<void> {
    console.log(`[BlockEditorHandler] Auto-extending block: ${blockId} (${direction})`);

    try {
      const block = await this.api.getBlock(blockId);
      if (!block || block.error) {
        throw new Error('Block not found');
      }

      const sortedPositions = block.sorted_positions || block.track_positions;
      if (!sortedPositions || sortedPositions.length === 0) {
        throw new Error('Block has no positions');
      }

      const tracks = this.editor.cachedNetwork?.tracks || [];
      const orientation = detectOrientation(sortedPositions, tracks);
      console.log(`[BlockEditorHandler] Detected block orientation: ${orientation}`);

      const nextTile = calculateNextTile(sortedPositions, orientation, direction, tracks);
      console.log(
        `[BlockEditorHandler] Auto-extending to tile (${nextTile.x}, ${nextTile.y}) with track bits ${nextTile.trackBits}`
      );

      await this.api.extendBlock(blockId, [[nextTile.x, nextTile.y]], false);
      await window.app?.refreshNetwork();

      this.showEditor(blockId);

      if (this.selectedElement?.x === nextTile.x && this.selectedElement?.y === nextTile.y) {
        await this.editor.selectElement({ x: nextTile.x, y: nextTile.y });
      }

      showToast(`Block extended ${direction} to (${nextTile.x}, ${nextTile.y})`, 'success', 3000);
    } catch (err: any) {
      console.error('Failed to auto-extend block:', err);
      showToast(`Failed to extend block: ${err.message}`, 'error', 5000);
    }
  }

  // === Block Dialog Methods ===

  async editDialog(blockId: string): Promise<void> {
    try {
      const blockData = await this.api.getBlock(blockId);
      if (blockData.error) {
        showToast(blockData.error, 'error', 5000);
        return;
      }

      // Show block properties modal
      const result = await this.showPropertiesModal({
        blockId: blockId,
        blockData: blockData,
      });

      if (!result) return; // User cancelled

      // Update block with new values
      await this.api.updateBlock(blockId, result);
      await this.refreshList();
    } catch (err: any) {
      console.error('[BlockEditorHandler] Failed to update block:', err);
      showToast('Failed to update block: ' + err.message, 'error', 5000);
    }
  }

  async delete(blockId: string): Promise<void> {
    const confirmed = await showConfirmModal({
      title: 'Delete Block',
      message: `Delete block "${blockId}"?`,
      detail: 'Tracks assigned to this block will be unassigned.',
      confirmText: 'Delete',
      confirmVariant: 'danger',
    });
    if (!confirmed) return;

    try {
      await this.api.deleteBlock(blockId);
      if (this.selectedBlockId === blockId) {
        this.selectedBlockId = null;
      }
      await this.refreshList();
      await window.app?.refreshNetwork();
    } catch (err: any) {
      console.error('[BlockEditorHandler] Failed to delete block:', err);
      showToast('Failed to delete block: ' + err.message, 'error', 5000);
    }
  }

  // === Block Modal Dialogs ===

  /** Show a modal dialog for block creation or extension. */
  showModal(options: { mode: string; trackCount: number; existingBlocks?: string[] }): Promise<any> {
    return new Promise((resolve) => {
      const { mode, trackCount, existingBlocks = [] } = options;

      // Create modal overlay
      const overlay = document.createElement('div');
      overlay.className = 'block-modal-overlay';

      let modalContent = '';

      if (mode === 'create') {
        // Create new block form
        const defaultId = `BLK_${Date.now().toString(16).toUpperCase().slice(-6)}`;
        modalContent = `
                    <div class="block-modal">
                        <h3>Create New Block</h3>
                        <div class="modal-body">
                            <p class="modal-info">Creating block with <strong>${trackCount}</strong> tracks</p>
                            <div class="form-group">
                                <label for="modal-block-id">Block ID</label>
                                <input type="text" id="modal-block-id" value="${defaultId}" autocomplete="off">
                            </div>
                            <div class="form-group">
                                <label for="modal-block-name">Block Name (optional)</label>
                                <input type="text" id="modal-block-name" placeholder="e.g., Main Line West" autocomplete="off">
                            </div>
                            <div class="form-group">
                                <label for="modal-max-speed">Max Speed km/h (optional)</label>
                                <input type="number" id="modal-max-speed" placeholder="e.g., 60" min="0" step="1">
                            </div>
                        </div>
                        <div class="modal-footer">
                            <button class="btn btn-primary" id="modal-confirm">Create Block</button>
                            <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
                        </div>
                    </div>
                `;
      } else if (mode === 'extend') {
        // Extend existing block
        if (existingBlocks.length === 0) {
          // No existing blocks - shouldn't happen in extend mode
          resolve(null);
          return;
        } else if (existingBlocks.length === 1) {
          // Single block to extend
          const blockId = existingBlocks[0];
          const block = this.cachedBlocks?.find((b: any) => b.block_id === blockId);
          modalContent = `
                        <div class="block-modal">
                            <h3>Extend Block</h3>
                            <div class="modal-body">
                                <p class="modal-info">Extending block <strong>${blockId}</strong></p>
                                ${block?.name ? `<p class="modal-subinfo">${block.name}</p>` : ''}
                                <p class="modal-detail">Adding <strong>${trackCount}</strong> tracks to this block.</p>
                                ${block ? `<p class="modal-detail">Current size: ${block.track_count || 0} tracks</p>` : ''}
                            </div>
                            <div class="modal-footer">
                                <button class="btn btn-primary" id="modal-confirm" data-block-id="${blockId}">Extend Block</button>
                                <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
                            </div>
                        </div>
                    `;
        } else {
          // Multiple blocks - user must choose
          const blockOptions = existingBlocks
            .map((blockId) => {
              const block = this.cachedBlocks?.find((b: any) => b.block_id === blockId);
              return `
                            <div class="block-selection-item" data-block-id="${blockId}">
                                <input type="radio" name="extend-block" value="${blockId}" id="block-${blockId}">
                                <label for="block-${blockId}">
                                    <span class="block-id">${blockId}</span>
                                    ${block?.name ? `<span class="block-name">${block.name}</span>` : ''}
                                    ${block?.station_names ? `<span class="block-name block-station-names">[${block.station_names.join(', ')}]</span>` : ''}
                                    <span class="block-track-count">${block?.track_count || 0} tracks</span>
                                </label>
                            </div>
                        `;
            })
            .join('');

          modalContent = `
                        <div class="block-modal">
                            <h3>Extend Block</h3>
                            <div class="modal-body">
                                <p class="modal-info">Path covers <strong>${existingBlocks.length}</strong> blocks</p>
                                <p class="modal-warning">Select which block to extend. Other block assignments will be overwritten.</p>
                                <div class="block-selection-list">
                                    ${blockOptions}
                                </div>
                            </div>
                            <div class="modal-footer">
                                <button class="btn btn-primary" id="modal-confirm" disabled>Extend Selected</button>
                                <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
                            </div>
                        </div>
                    `;
        }
      }

      overlay.innerHTML = modalContent;
      document.body.appendChild(overlay);

      // Focus first input
      const firstInput = overlay.querySelector<HTMLElement>('input[type="text"], input[type="number"]');
      if (firstInput) {
        setTimeout(() => firstInput.focus(), 50);
      }

      // Handle radio button selection for multi-block extend
      const radioButtons = overlay.querySelectorAll<HTMLInputElement>('input[name="extend-block"]');
      const confirmBtn = overlay.querySelector<HTMLButtonElement>('#modal-confirm');

      radioButtons.forEach((radio) => {
        radio.addEventListener('change', () => {
          if (confirmBtn) {
            confirmBtn.disabled = false;
            confirmBtn.dataset.blockId = radio.value;
          }
        });
      });

      // Handle confirm
      confirmBtn?.addEventListener('click', () => {
        if (mode === 'create') {
          const blockId = overlay.querySelector<HTMLInputElement>('#modal-block-id')?.value?.trim();
          const name = overlay.querySelector<HTMLInputElement>('#modal-block-name')?.value?.trim();
          const maxSpeed = overlay.querySelector<HTMLInputElement>('#modal-max-speed')?.value;

          if (!blockId) {
            showToast('Block ID is required', 'error', 5000);
            return;
          }

          overlay.remove();
          resolve({
            action: 'create',
            blockId,
            name: name || '',
            maxSpeed: maxSpeed ? parseFloat(maxSpeed) : null,
          });
        } else if (mode === 'extend') {
          const blockId = confirmBtn!.dataset.blockId;
          if (!blockId) {
            showToast('Please select a block', 'error', 5000);
            return;
          }

          overlay.remove();
          resolve({
            action: 'extend',
            blockId,
          });
        }
      });

      // Handle cancel
      overlay.querySelector('#modal-cancel')?.addEventListener('click', () => {
        overlay.remove();
        resolve(null);
      });

      // Handle overlay click (close)
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) {
          overlay.remove();
          resolve(null);
        }
      });

      // Handle Escape key
      const handleEscape = (e: KeyboardEvent) => {
        if (e.key === 'Escape') {
          overlay.remove();
          document.removeEventListener('keydown', handleEscape);
          resolve(null);
        }
      };
      document.addEventListener('keydown', handleEscape);

      // Handle Enter key for create mode
      if (mode === 'create') {
        const handleEnter = (e: KeyboardEvent) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            confirmBtn?.click();
          }
        };
        overlay.addEventListener('keydown', handleEnter);
      }
    });
  }

  /** Show a modal dialog for editing block properties. */
  showPropertiesModal(options: { blockId: string; blockData: any }): Promise<any> {
    return new Promise((resolve) => {
      const { blockId, blockData } = options;

      // Create modal overlay
      const overlay = document.createElement('div');
      overlay.className = 'modal-overlay';

      const modal = document.createElement('div');
      modal.className = 'modal';
      modal.innerHTML = `
                <h3>Edit Block Properties</h3>

                <!-- Info Display Group -->
                <div class="info-display-group">
                    <div class="info-display-header">Block Information</div>
                    <div class="info-display-item">
                        <span class="info-display-label">Block ID</span>
                        <span class="info-display-value">${blockId}</span>
                    </div>
                    <div class="info-display-item">
                        <span class="info-display-label">Current State</span>
                        <span class="info-badge state-${blockData.state || 'free'}">
                            ${(blockData.state || 'free').toUpperCase()}
                        </span>
                    </div>
                    ${
                      blockData.monitoring_signals && blockData.monitoring_signals.length > 0
                        ? `
                    <div class="info-display-item info-display-item-stacked">
                        <span class="info-display-label">Monitoring Signals</span>
                        <div class="signal-list signal-list-compact" id="monitoring-signals-list">
                            ${this.editor.renderSignalList ? this.editor.renderSignalList(blockData.monitoring_signals) : ''}
                        </div>
                    </div>
                    `
                        : ''
                    }
                </div>

                <!-- User Input Fields (Horizontal Layout) -->
                <div class="form-row-horizontal">
                    <label for="modal-block-name">Name</label>
                    <input type="text" id="modal-block-name" value="${blockData.name || ''}"
                           placeholder="Block name">
                </div>
                <div class="form-row-horizontal">
                    <label for="modal-block-length">Length (cm)</label>
                    <input type="number" id="modal-block-length" value="${blockData.length_cm || 0}"
                           min="0" step="0.1" placeholder="0.0">
                </div>
                <div class="form-row-horizontal">
                    <label for="modal-block-speed">Max Speed (km/h)</label>
                    <input type="number" id="modal-block-speed" value="${blockData.speed_limit_kmh || ''}"
                           min="0" step="1" placeholder="Optional">
                </div>

                <div class="button-group">
                    <button class="btn btn-primary" id="save-block-btn">Save</button>
                    <button class="btn btn-secondary" id="cancel-block-btn">Cancel</button>
                </div>
            `;

      overlay.appendChild(modal);
      document.body.appendChild(overlay);

      const nameInput = modal.querySelector<HTMLInputElement>('#modal-block-name')!;
      const lengthInput = modal.querySelector<HTMLInputElement>('#modal-block-length')!;
      const speedInput = modal.querySelector<HTMLInputElement>('#modal-block-speed')!;
      const saveBtn = modal.querySelector<HTMLButtonElement>('#save-block-btn')!;
      const cancelBtn = modal.querySelector<HTMLButtonElement>('#cancel-block-btn')!;

      // Focus first input
      setTimeout(() => nameInput.focus(), 50);

      // Handle save
      saveBtn.addEventListener('click', () => {
        const name = nameInput.value.trim();
        const lengthCm = parseFloat(lengthInput.value) || 0;
        const speedLimitKmh = speedInput.value.trim() ? parseFloat(speedInput.value) : null;

        if (lengthCm < 0) {
          showToast('Length cannot be negative', 'error', 5000);
          return;
        }

        if (speedLimitKmh !== null && speedLimitKmh <= 0) {
          showToast('Max speed must be positive', 'error', 5000);
          return;
        }

        document.body.removeChild(overlay);
        resolve({
          name,
          length_cm: lengthCm,
          speed_limit_kmh: speedLimitKmh,
        });
      });

      // Handle cancel
      cancelBtn.addEventListener('click', () => {
        document.body.removeChild(overlay);
        resolve(null);
      });

      // Handle Enter key
      const handleEnter = (e: KeyboardEvent) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          saveBtn.click();
        }
      };
      modal.addEventListener('keydown', handleEnter);

      // Handle Escape key
      const handleEscape = (e: KeyboardEvent) => {
        if (e.key === 'Escape') {
          document.body.removeChild(overlay);
          document.removeEventListener('keydown', handleEscape);
          resolve(null);
        }
      };
      document.addEventListener('keydown', handleEscape);

      // Handle overlay click
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) {
          document.body.removeChild(overlay);
          resolve(null);
        }
      });
    });
  }

  // ═══════════════════════════════════════════════════════════════
  // SUBSCRIPTION METHODS (Phase 4)
  // ═══════════════════════════════════════════════════════════════

  _setupSubscription(blockId: string, blockData: any): void {
    this._cleanup();
    this._blockId = blockId;
    this._localState = JSON.parse(JSON.stringify(blockData));

    this._unsubscribe =
      stateManager?.subscribe(
        'blocks',
        blockId,
        (newBlock: any, oldBlock: any) => this._onBlockUpdate(newBlock, oldBlock),
        { immediate: false }
      ) ?? null;
  }

  _onBlockUpdate(newBlock: any, oldBlock: any): void {
    if (!newBlock) {
      showToast('Block was deleted', 'warning');
      this.editor.hideSecondarySidebar();
      return;
    }

    if (this._isUserInteracting) return; // Don't interrupt tile picker drag

    const activeElement = document.activeElement;
    if (
      activeElement &&
      (activeElement.tagName === 'INPUT' ||
        activeElement.tagName === 'TEXTAREA' ||
        activeElement.tagName === 'SELECT')
    ) {
      return;
    }

    this._localState = JSON.parse(JSON.stringify(newBlock));
  }

  _cleanup(): void {
    if (this._unsubscribe) {
      this._unsubscribe();
      this._unsubscribe = null;
    }
    this._blockId = null;
    this._localState = null;
    this._isUserInteracting = false;
  }
}
