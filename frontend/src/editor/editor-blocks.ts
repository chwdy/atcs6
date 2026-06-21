// editor-blocks.ts — Block Editor Handler.
//
// Migrated from static/js/editor-blocks.js (longterm_todo.md item #7, Wave 2).
// Behavior is unchanged. Owned by the Editor (`new BlockEditorHandler(this)`).

import { stateManager } from '../lib/state-manager';
import { showToast } from '../lib/toast';
import type { EditorHost } from './types';
import {
  renderTileList,
  renderAvailabilitySegment,
  renderDirectionSegment,
  bindAvailabilitySegment,
  bindDirectionSegment,
  showConfirmModal,
  detectOrientation,
  calculateNextTile,
  escapeAttr,
  bindFieldAutoSave,
  parseText,
  parseRequiredMmToCm,
  parseOptionalMmToCm,
  parseOptionalInt,
} from '../lib/helpers';
import { anchorToTiles } from '../lib/popover-anchor';

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

  // Map a block's runtime state to the §11 status-header left accent and the
  // §7 state-chip variant. The block's runtime state is always one of
  // free / reserved / occupied / caution (block.py:198); disabled / manual are
  // separate availability flags edited by the segment below, not states here.
  // FREE inherits cyan (--info-color) per the §1 block-state palette; RESERVED
  // is the green "enter" state. The default is a meaningful palette color
  // (info), never --border-color (§11 "Status header block").
  getBlockStateAccent(state: string): { accentClass: string; chipClass: string } {
    const map: Record<string, { accentClass: string; chipClass: string }> = {
      free: { accentClass: 'accent-info', chipClass: 'chip-info' },
      reserved: { accentClass: 'accent-success', chipClass: 'chip-success' },
      occupied: { accentClass: 'accent-danger', chipClass: 'chip-danger' },
      caution: { accentClass: 'accent-warn', chipClass: 'chip-warn' },
    };
    return map[(state || '').toLowerCase()] || { accentClass: 'accent-info', chipClass: 'chip-info' };
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

    const stateText = (block.state || 'free').toUpperCase();
    const { accentClass, chipClass } = this.getBlockStateAccent(block.state);
    const trackCount = block.track_count || 0;
    const lengthCm = block.length_cm || 0;

    // Lengths and radii are shown in millimetres at the UI boundary; the
    // backend stays in centimetres, so the panel converts on display (×10)
    // and on save (÷10). Speed stays km/h. An unset radius/speed means
    // "no limit" (null), shown as a placeholder rather than a number.
    const lengthMm = Math.round(lengthCm * 10);
    const radiusMm = block.min_radius_cm != null ? Math.round(block.min_radius_cm * 10) : '';
    const speedKmh = block.speed_limit_kmh != null ? block.speed_limit_kmh : '';

    // Predict the next tile each Extend button would add so the button shows
    // the coordinate instead of a generic "Start/End". calculateNextTile
    // throws when there's no clean single extension (edge tile lacks two
    // exits, or both neighbours already belong to the block) — fall back to a
    // disabled button whose tooltip carries the reason.
    const sortedPositions = block.sorted_positions || block.track_positions || [];
    const tracks = this.editor.cachedNetwork?.tracks || [];
    const predictExtend = (
      dir: 'start' | 'end'
    ): { label: string; disabled: boolean; title: string; icon: string } => {
      try {
        if (!sortedPositions.length) throw new Error('no tiles yet');
        const orientation = detectOrientation(sortedPositions, tracks);
        const next = calculateNextTile(sortedPositions, orientation, dir, tracks);
        return {
          label: `Extend tiles (${next.x}, ${next.y})`,
          disabled: false,
          title: `Add tile (${next.x}, ${next.y}) to this block`,
          icon: '',
        };
      } catch (err: any) {
        return {
          label: 'Extend tiles',
          disabled: true,
          title: `Can't extend here: ${err?.message || 'no clean next tile'}`,
          icon: '',
        };
      }
    };

    const content = `
            <div class="block-editor-panel" data-block-id="${blockId}">
                <!-- §11 Status header — identity + state pill, dynamic accent.
                     The display name is editable inline here (auto-saves on
                     blur), so identity lives in the title rather than being
                     buried in a property field. -->
                <div class="status-header ${accentClass}">
                    <div class="status-header-row">
                        <div class="status-header-id font-mono" title="${escapeAttr(blockId)}">${blockId}</div>
                        <span class="chip chip-state ${chipClass}">${stateText}</span>
                    </div>
                    <input type="text" id="block-name-input" class="status-header-name-input"
                        value="${escapeAttr(block.name || '')}" placeholder="Add a name…"
                        title="Display name — click to edit" aria-label="Block display name">
                    <div class="status-header-ident">
                        <span class="status-header-role">Block</span>
                        <span class="status-sep">·</span>
                        <span class="font-mono">${trackCount} tile${trackCount === 1 ? '' : 's'}</span>
                        <span class="status-sep">·</span>
                        <span class="font-mono" id="block-length-summary">${lengthMm} mm</span>
                    </div>
                </div>

                <!-- §6 banner — surfaces the occupying train. Hidden when clear. -->
                ${
                  block.occupied_by
                    ? `
                <div class="banner banner-danger">
                    <div class="banner-label">Occupied by</div>
                    <div class="banner-value banner-text">${block.occupied_by}</div>
                </div>
                `
                    : ''
                }

                <!-- TILES LIST — the heading lives in the tile-list header
                     ("Tiles (N)"), so there's no separate section h4 above it.
                     Click a tile row to flash it on the layout. -->
                <div class="control-section mt-12">
                    ${renderTileList(sortedPositions, {
                      entityType: 'block',
                      extendStartBtnId: 'extend-block-start-btn',
                      extendEndBtnId: 'extend-block-end-btn',
                      removeBtnClass: 'delete-block-tile-btn',
                      headerLabel: 'Tiles',
                      extendStart: predictExtend('start'),
                      extendEnd: predictExtend('end'),
                    })}
                </div>

                <!-- EDITABLE PROPERTIES — every field auto-saves on blur
                     (§5C / §19 auto-apply); there is no Save button. Lengths
                     and radii are entered as whole millimetres. -->
                <div class="control-section control-section-divider mt-12 pt-12">
                    <h4 class="text-sm text-muted mb-8">Properties</h4>

                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="block-length-input">Length (mm)</label>
                        <input type="number" id="block-length-input" class="input-small-field input-small-field-compact" value="${lengthMm}" min="0" step="1" inputmode="numeric">
                    </div>

                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="block-radius-input">Min Radius (mm)</label>
                        <input type="number" id="block-radius-input" class="input-small-field input-small-field-compact" value="${radiusMm}" min="0" step="1" inputmode="numeric" placeholder="No limit" title="Tightest curve on this block. Empty = no curve limit (any train may pass).">
                    </div>

                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="block-speed-input">Max Speed (km/h)</label>
                        <input type="number" id="block-speed-input" class="input-small-field input-small-field-compact" value="${speedKmh}" min="0" step="1" inputmode="numeric" placeholder="No limit">
                    </div>

                    ${renderAvailabilitySegment('block', block, {
                      idBase: 'block-availability',
                      explanations: {
                        normal: 'automatic routing can plan through and reserve this block.',
                        manual:
                          'automatic routing skips it; an operator route with override (Quick Route) can still use it.',
                        disabled: 'off-limits to all trains; treated as missing track.',
                      },
                    })}

                    ${renderDirectionSegment('block', block, {
                      idBase: 'block-direction',
                      label: 'Allowed travel direction',
                      help: 'Forward and reverse follow the direction arrows shown on the layout. Automatic routing obeys this; an operator route with override (Quick Route) can ignore it.',
                    })}
                </div>

                <!-- Deleting the whole block is a block-level action, not a
                     property edit — kept apart at the foot as a quiet danger
                     button. The confirm modal is the §19 backstop. -->
                <div class="control-section control-section-divider mt-12 pt-12">
                    <button id="delete-block-btn" class="btn btn-danger-ghost btn-sm w-full"><i class="ti ti-trash"></i> Delete block</button>
                </div>
            </div>
        `;

    this.editor.showSecondarySidebar(`Block Editor`, content);

    // Focus the block direction arrows on this block so its F / R arrows show
    // on the map while the editor is open (the renderer derives them from the
    // block's tile geometry).
    if (this.editor.renderer?.setBlockDirectionFocus) {
      this.editor.renderer.setBlockDirectionFocus(blockId);
    }

    // Setup subscription before handlers
    this._setupSubscription(blockId, block);

    // Initialize event handlers after content is rendered
    setTimeout(() => this.initEditorHandlers(blockId), 0);
  }

  async initEditorHandlers(blockId: string): Promise<void> {
    const deleteBtn = document.getElementById('delete-block-btn') as HTMLButtonElement | null;
    const nameInput = document.getElementById('block-name-input') as HTMLInputElement | null;
    const lengthInput = document.getElementById('block-length-input') as HTMLInputElement | null;
    const radiusInput = document.getElementById('block-radius-input') as HTMLInputElement | null;
    const speedInput = document.getElementById('block-speed-input') as HTMLInputElement | null;

    // Availability segment (normal / manual / disabled). Auto-applies on click
    // (§5C); no success toast. On reject, re-render so the active cell rolls
    // back to the server's value.
    bindAvailabilitySegment('block-availability', async (_state, flags) => {
      try {
        const result = await this.api.updateBlock(blockId, flags);
        if (result && result.error) {
          showToast(`Cannot change availability: ${result.error}`, 'error', 4000);
          this.showEditor(blockId);
        } else {
          await window.app?.refreshNetwork();
        }
      } catch (err: any) {
        console.error('Failed to update block availability:', err);
        showToast(`Cannot change availability: ${err.message || err}`, 'error', 4000);
        this.showEditor(blockId);
      }
    });

    // Allowed-travel-direction segment (both / forward / reverse). Same
    // auto-apply contract as availability above.
    bindDirectionSegment('block-direction', async (_state, payload) => {
      try {
        const result = await this.api.updateBlock(blockId, payload);
        if (result && result.error) {
          showToast(`Cannot change travel direction: ${result.error}`, 'error', 4000);
          this.showEditor(blockId);
        } else {
          await window.app?.refreshNetwork();
        }
      } catch (err: any) {
        console.error('Failed to update block travel direction:', err);
        showToast(`Cannot change travel direction: ${err.message || err}`, 'error', 4000);
        this.showEditor(blockId);
      }
    });

    // --- Per-field auto-save (§5C / §19) -----------------------------------
    // Each property field commits on blur (or Enter) via the shared
    // `bindFieldAutoSave` wiring — no Save button, no success toast, a flash as
    // the ack. The panel is never re-rendered on a field save, so focus and
    // scroll survive. `refreshAfterSave` repaints the canvas and keeps the main
    // Properties pane in sync if it shows this block.
    const refreshAfterSave = async (): Promise<void> => {
      await window.app?.refreshNetwork();
      if (this.selectedElement?.block_section_id === blockId) {
        await this.editor.selectElement({ x: this.selectedElement.x, y: this.selectedElement.y });
      }
    };

    // Name — optional free text; empty is allowed (identity falls back to id).
    bindFieldAutoSave(nameInput, {
      parse: parseText,
      commit: (value) => this.api.updateBlock(blockId, { name: value }),
      onApplied: refreshAfterSave,
    });

    // Length — required whole millimetres ≥ 0, stored as cm (÷10).
    bindFieldAutoSave(lengthInput, {
      parse: parseRequiredMmToCm('Length'),
      commit: (value) => this.api.updateBlock(blockId, { length_cm: value }),
      onApplied: async () => {
        const summary = document.getElementById('block-length-summary');
        if (summary && lengthInput) {
          summary.textContent = `${parseInt(lengthInput.value || '0', 10) || 0} mm`;
        }
        await refreshAfterSave();
      },
    });

    // Min radius — optional mm ≥ 0, stored as cm (÷10). Empty means "no curve
    // limit" (null), NOT 0 — 0 would reject every train.
    bindFieldAutoSave(radiusInput, {
      parse: parseOptionalMmToCm('Min radius'),
      commit: (value) => this.api.updateBlock(blockId, { min_radius_cm: value }),
      onApplied: refreshAfterSave,
    });

    // Max speed — optional whole km/h ≥ 0. Empty means "no limit" (null).
    bindFieldAutoSave(speedInput, {
      parse: parseOptionalInt('Max speed'),
      commit: (value) => this.api.updateBlock(blockId, { speed_limit_kmh: value }),
      onApplied: refreshAfterSave,
    });

    // Delete button
    if (deleteBtn) {
      deleteBtn.onclick = async () => {
        const confirmed = await showConfirmModal({
          title: 'Delete block',
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

    // Click a tile row (anywhere but the Remove button) → center the layout on
    // that tile and flash it, so a coordinate in the list maps to a place on
    // the map.
    const tileRows = document.querySelectorAll<HTMLElement>('.block-editor-panel .tile-list-item');
    tileRows.forEach((row) => {
      row.addEventListener('click', (e) => {
        if ((e.target as HTMLElement).closest('button')) return; // let Remove handle its own click
        const x = parseInt(row.dataset.x ?? '', 10);
        const y = parseInt(row.dataset.y ?? '', 10);
        if (Number.isNaN(x) || Number.isNaN(y)) return;
        const renderer = this.editor.renderer;
        // Recenter the view on the tile (panToWorld handles the isometric math),
        // then repaint the world layer at the new offset.
        renderer?.camera?.panToWorld?.(x, y);
        renderer?.requestRedraw?.({ world: true, live: true, reason: 'locate-tile' });
        // Yellow "locate" highlight — not a danger flash (§17 keeps red for
        // danger). highlightTrack lights the rail through the tile on top of the
        // tile diamond, so both the boundary and the track section stand out.
        renderer?.flashTiles?.([[x, y]], {
          rgb: '255, 217, 61',
          fillAlpha: 0.4,
          durationMs: 900,
          highlightTrack: true,
        });
      });
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
  showModal(options: {
    mode: string;
    trackCount: number;
    existingBlocks?: string[];
    anchorTiles?: Array<{ x: number; y: number }>;
  }): Promise<any> {
    return new Promise((resolve) => {
      const { mode, trackCount, existingBlocks = [], anchorTiles } = options;

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
                            <div class="form-row">
                                <div class="form-group">
                                    <label for="modal-min-radius">Min Radius (cm)</label>
                                    <input type="number" id="modal-min-radius" placeholder="e.g., 300" min="0" step="1">
                                </div>
                                <div class="form-group">
                                    <label for="modal-slope">Slope (%)</label>
                                    <input type="number" id="modal-slope" placeholder="e.g., 1.5" step="0.1">
                                </div>
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

      // Anchor the dialog over the tiles it edits so it hovers on the canvas and
      // follows pan/zoom, instead of a full-screen centered modal. Falls back to
      // the centered modal when no anchor tiles are supplied.
      let anchorCleanup = (): void => {};
      const card = overlay.querySelector<HTMLElement>('.block-modal');
      if (anchorTiles && anchorTiles.length > 0 && card) {
        overlay.classList.add('anchored');
        card.classList.add('anchored');
        anchorCleanup = anchorToTiles(card, anchorTiles);
      }

      const close = (): void => {
        anchorCleanup();
        document.removeEventListener('keydown', handleEscape);
        overlay.remove();
      };

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
          const minRadius = overlay.querySelector<HTMLInputElement>('#modal-min-radius')?.value;
          const slope = overlay.querySelector<HTMLInputElement>('#modal-slope')?.value;

          if (!blockId) {
            showToast('Block ID is required', 'error', 5000);
            return;
          }

          close();
          resolve({
            action: 'create',
            blockId,
            name: name || '',
            maxSpeed: maxSpeed ? parseFloat(maxSpeed) : null,
            minRadius: minRadius ? parseFloat(minRadius) : null,
            slope: slope !== undefined && slope !== '' ? parseFloat(slope) : null,
          });
        } else if (mode === 'extend') {
          const blockId = confirmBtn!.dataset.blockId;
          if (!blockId) {
            showToast('Please select a block', 'error', 5000);
            return;
          }

          close();
          resolve({
            action: 'extend',
            blockId,
          });
        }
      });

      // Handle cancel
      overlay.querySelector('#modal-cancel')?.addEventListener('click', () => {
        close();
        resolve(null);
      });

      // Handle overlay click (close)
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) {
          close();
          resolve(null);
        }
      });

      // Handle Escape key
      const handleEscape = (e: KeyboardEvent) => {
        if (e.key === 'Escape') {
          close();
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
