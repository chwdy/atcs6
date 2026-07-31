// editor-segments.ts — Segment Editor Handler (the geometry editor).
//
// A `Segment` is the geometry unit: the run of tiles you draw, plus its length,
// min radius, grade and speed limit. Those edits go to `/api/segments`.
//
// Availability (disabled / manual), allowed travel direction and occupancy are
// NOT geometry — they belong to the `Block` that owns this segment, so the
// panel routes those reads/writes to `/api/blocks/<blockId>`. The two ids are
// tracked separately throughout (`segmentId` vs `blockId`): for a single-segment
// block they happen to be equal, which is exactly why they must never be mixed.
//
// Hardware port binding is NOT here: power is fed per block, so the Hardware
// section lives on the Block panel (`editor-block-grouping.ts`).
//
// Owned by the Editor (`new SegmentEditorHandler(this)`).

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
  formatDuration,
  bindFieldAutoSave,
  parseText,
  parseRequiredMmToCm,
  parseOptionalMmToCm,
  parseOptionalInt,
  applyStructuralLock,
} from '../lib/helpers';
import { anchorToTiles } from '../lib/popover-anchor';

export class SegmentEditorHandler {
  editor: EditorHost;
  _unsubscribe: (() => void) | null;
  _isUserInteracting: boolean;

  constructor(editor: EditorHost) {
    this.editor = editor;
    this._unsubscribe = null;
    this._isUserInteracting = false; // For tile picker drag operations
  }

  // Convenience getters for common editor properties
  get api(): any {
    return this.editor.api;
  }
  get cachedSegments(): any {
    return this.editor.cachedSegments;
  }
  set cachedSegments(value: any) {
    this.editor.cachedSegments = value;
  }
  get selectedSegmentId(): any {
    return this.editor.selectedSegmentId;
  }
  set selectedSegmentId(value: any) {
    this.editor.selectedSegmentId = value;
  }
  get selectedElement(): any {
    return this.editor.selectedElement;
  }

  // === Segment cache ===

  setup(): void {
    // There is no Segment Manager panel — segments are edited through the
    // secondary sidebar editor below. Keep the segment cache refreshed.
    this.refreshList();
  }

  async refreshList(): Promise<void> {
    try {
      const segments = await this.api.getSegments();
      this.cachedSegments = segments;
    } catch (err) {
      console.error('[SegmentEditorHandler] Failed to refresh segments:', err);
    }
  }

  getColor(segmentId: string): string {
    // Match renderer's HSL hash-based color generation
    if (!segmentId) return '#666';
    let hash = 0;
    for (let i = 0; i < segmentId.length; i++) {
      hash = segmentId.charCodeAt(i) + ((hash << 5) - hash);
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

  // === Segment Editor Panel ===

  async showEditor(segmentId: string): Promise<void> {
    // Track this editor for auto-refresh
    this.editor._currentEditorType = 'segment';
    this.editor._currentEditorId = segmentId;

    // Fetch fresh segment geometry from the API to ensure we have current state
    let segment;
    try {
      segment = await this.api.getSegment(segmentId);
      if (segment.error) {
        console.warn('[SegmentEditorHandler] Segment not found for editor:', segmentId, segment.error);
        return;
      }
    } catch (err) {
      // Fallback to cached data if API fails
      segment = this.cachedSegments?.find((s: any) => s.segment_id === segmentId);
      if (!segment) {
        console.warn('[SegmentEditorHandler] Segment not found for editor:', segmentId);
        return;
      }
    }

    // The owning block carries availability, travel direction and occupancy —
    // none of which are geometry. `block_id` comes off the segment payload; for
    // a single-segment block it equals `segmentId`, but it is read explicitly so
    // a grouped segment still edits the right block.
    const blockId: string = segment.block_id || segmentId;
    let block: any = {};
    try {
      const fetched = await this.api.getBlock(blockId);
      if (fetched && !fetched.error) block = fetched;
    } catch (err) {
      console.warn('[SegmentEditorHandler] Owning block not readable:', blockId, err);
    }

    // Occupancy / availability state comes from the owning block.
    const stateText = (block.state || 'free').toUpperCase();
    const { accentClass, chipClass } = this.getBlockStateAccent(block.state);
    // Geometry comes from the segment.
    const trackCount = segment.track_count || 0;
    const lengthCm = segment.length_cm || 0;

    // Lengths and radii are shown in millimetres at the UI boundary; the
    // backend stays in centimetres, so the panel converts on display (×10)
    // and on save (÷10). Speed stays km/h. An unset radius/speed means
    // "no limit" (null), shown as a placeholder rather than a number.
    const lengthMm = Math.round(lengthCm * 10);
    const radiusMm = segment.min_radius_cm != null ? Math.round(segment.min_radius_cm * 10) : '';
    const speedKmh = segment.speed_limit_kmh != null ? segment.speed_limit_kmh : '';

    // Predict the next tile each Extend button would add so the button shows
    // the coordinate instead of a generic "Start/End". calculateNextTile
    // throws when there's no clean single extension (edge tile lacks two
    // exits, or both neighbours already belong to the segment) — fall back to a
    // disabled button whose tooltip carries the reason.
    // `ordered_tiles` is the segment's traversal order ([x, y, edge] entries);
    // fall back to the unordered position list when it is absent.
    const sortedPositions: Array<[number, number]> =
      (segment.ordered_tiles || []).map((t: [number, number, string]) => [t[0], t[1]] as [number, number]);
    if (sortedPositions.length === 0) {
      for (const pos of segment.track_positions || []) sortedPositions.push([pos[0], pos[1]]);
    }
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
          title: `Add tile (${next.x}, ${next.y}) to this segment`,
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
            <div class="segment-editor-panel" data-segment-id="${segmentId}" data-block-id="${blockId}">
                <!-- §11 Status header — identity + state pill, dynamic accent.
                     The display name is editable inline here (auto-saves on
                     blur), so identity lives in the title rather than being
                     buried in a property field. -->
                <div class="status-header ${accentClass}">
                    <div class="status-header-row">
                        <div class="status-header-id font-mono" title="${escapeAttr(segmentId)}">${segmentId}</div>
                        <span class="chip chip-state ${chipClass}">${stateText}</span>
                    </div>
                    <input type="text" id="segment-name-input" class="status-header-name-input"
                        value="${escapeAttr(segment.name || '')}" placeholder="Add a name…"
                        title="Display name — click to edit" aria-label="Segment display name">
                    <div class="status-header-ident">
                        <span class="status-header-role">Segment</span>
                        <span class="status-sep">·</span>
                        <span class="font-mono">${trackCount} tile${trackCount === 1 ? '' : 's'}</span>
                        <span class="status-sep">·</span>
                        <span class="font-mono" id="segment-length-summary">${lengthMm} mm</span>
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
                      entityType: 'segment',
                      extendStartBtnId: 'extend-segment-start-btn',
                      extendEndBtnId: 'extend-segment-end-btn',
                      removeBtnClass: 'delete-segment-tile-btn',
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
                        <label class="input-small-label input-small-label-inline" for="segment-length-input">Length (mm)</label>
                        <input type="number" id="segment-length-input" class="input-small-field input-small-field-compact" value="${lengthMm}" min="0" step="1" inputmode="numeric">
                    </div>

                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="segment-radius-input">Min Radius (mm)</label>
                        <input type="number" id="segment-radius-input" class="input-small-field input-small-field-compact" value="${radiusMm}" min="0" step="1" inputmode="numeric" placeholder="No limit" title="Tightest curve on this segment. Empty = no curve limit (any train may pass).">
                    </div>

                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="segment-speed-input">Max Speed (km/h)</label>
                        <input type="number" id="segment-speed-input" class="input-small-field input-small-field-compact" value="${speedKmh}" min="0" step="1" inputmode="numeric" placeholder="No limit">
                    </div>

                    <!-- Availability and travel direction are BLOCK policy, not
                         segment geometry: they apply to the whole block this
                         segment belongs to, so they read and write the owning
                         block. -->
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

                <!-- Maintenance — read-only usage counter (design_new.md
                     "Maintenance counters"); accrues while the simulation runs.
                     Occupancy time is measured on the owning block. -->
                <div class="control-section control-section-divider mt-12 pt-12">
                    <h4 class="text-sm text-muted mb-8">Maintenance</h4>
                    <div class="input-small-row">
                        <span class="input-small-label input-small-label-inline">Usage time</span>
                        <span class="font-mono" title="Total time a train has occupied this block">${formatDuration(block.occupied_seconds || 0)}</span>
                    </div>
                </div>

                <!-- Deleting the whole segment is a segment-level action, not a
                     property edit — kept apart at the foot as a quiet danger
                     button. The confirm modal is the §19 backstop. -->
                <div class="control-section control-section-divider mt-12 pt-12">
                    <button id="delete-segment-btn" class="btn btn-danger-ghost btn-sm w-full"><i class="ti ti-trash"></i> Delete segment</button>
                </div>
            </div>
        `;

    this.editor.showSecondarySidebar(`Segment Editor`, content);

    // Focus the block direction arrows on the owning block so its F / R arrows
    // show on the map while the editor is open (the renderer derives them from
    // the block's tile geometry).
    if (this.editor.renderer?.setBlockDirectionFocus) {
      this.editor.renderer.setBlockDirectionFocus(blockId);
    }

    // Setup subscription before handlers
    this._setupSubscription(blockId);

    // Initialize event handlers after content is rendered
    setTimeout(() => this.initEditorHandlers(segmentId, blockId), 0);
  }

  async initEditorHandlers(segmentId: string, blockId: string): Promise<void> {
    const deleteBtn = this.editor.$id('delete-segment-btn') as HTMLButtonElement | null;
    const nameInput = this.editor.$id('segment-name-input') as HTMLInputElement | null;
    const lengthInput = this.editor.$id('segment-length-input') as HTMLInputElement | null;
    const radiusInput = this.editor.$id('segment-radius-input') as HTMLInputElement | null;
    const speedInput = this.editor.$id('segment-speed-input') as HTMLInputElement | null;

    // Availability segment (normal / manual / disabled). Auto-applies on click
    // (§5C); no success toast. On reject, re-render so the active cell rolls
    // back to the server's value.
    bindAvailabilitySegment('block-availability', async (_state, flags) => {
      try {
        const result = await this.api.updateBlock(blockId, flags);
        if (result && result.error) {
          showToast(`Cannot change availability: ${result.error}`, 'error', 4000);
          this.showEditor(segmentId);
        } else {
          await this.editor.host?.refreshNetwork();
        }
      } catch (err: any) {
        console.error('Failed to update block availability:', err);
        showToast(`Cannot change availability: ${err.message || err}`, 'error', 4000);
        this.showEditor(segmentId);
      }
    });

    // Allowed-travel-direction segment (both / forward / reverse). Same
    // auto-apply contract as availability above.
    bindDirectionSegment('block-direction', async (_state, payload) => {
      try {
        const result = await this.api.updateBlock(blockId, payload);
        if (result && result.error) {
          showToast(`Cannot change travel direction: ${result.error}`, 'error', 4000);
          this.showEditor(segmentId);
        } else {
          await this.editor.host?.refreshNetwork();
        }
      } catch (err: any) {
        console.error('Failed to update block travel direction:', err);
        showToast(`Cannot change travel direction: ${err.message || err}`, 'error', 4000);
        this.showEditor(segmentId);
      }
    });

    // --- Per-field auto-save (§5C / §19) -----------------------------------
    // Each property field commits on blur (or Enter) via the shared
    // `bindFieldAutoSave` wiring — no Save button, no success toast, a flash as
    // the ack. The panel is never re-rendered on a field save, so focus and
    // scroll survive. `refreshAfterSave` repaints the canvas and keeps the main
    // Properties pane in sync if it shows this segment.
    const refreshAfterSave = async (): Promise<void> => {
      await this.editor.host?.refreshNetwork();
      if (this.selectedElement?.segment_id === segmentId) {
        await this.editor.selectElement({ x: this.selectedElement.x, y: this.selectedElement.y });
      }
    };

    // Name — optional free text; empty is allowed (identity falls back to id).
    bindFieldAutoSave(nameInput, {
      parse: parseText,
      commit: (value) => this.api.updateSegment(segmentId, { name: value }),
      onApplied: refreshAfterSave,
    });

    // Length — required whole millimetres ≥ 0, stored as cm (÷10).
    bindFieldAutoSave(lengthInput, {
      parse: parseRequiredMmToCm('Length'),
      commit: (value) => this.api.updateSegment(segmentId, { length_cm: value }),
      onApplied: async () => {
        const summary = this.editor.$id('segment-length-summary');
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
      commit: (value) => this.api.updateSegment(segmentId, { min_radius_cm: value }),
      onApplied: refreshAfterSave,
    });

    // Max speed — optional whole km/h ≥ 0. Empty means "no limit" (null).
    bindFieldAutoSave(speedInput, {
      parse: parseOptionalInt('Max speed'),
      commit: (value) => this.api.updateSegment(segmentId, { speed_limit_kmh: value }),
      onApplied: refreshAfterSave,
    });

    // Delete button
    if (deleteBtn) {
      deleteBtn.onclick = async () => {
        const confirmed = await showConfirmModal({
          title: 'Delete segment',
          message: `Delete segment "${segmentId}"?`,
          detail: 'Tiles assigned to this segment will be unassigned.',
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
        const wasShowingThisSegment = this.selectedElement?.segment_id === segmentId;
        try {
          await this.api.deleteSegment(segmentId);
          await this.editor.host?.refreshNetwork();
          this.editor.hideSecondarySidebar();
          // Refresh main properties panel if it was showing this segment
          if (wasShowingThisSegment && selectedPos) {
            await this.editor.selectElement(selectedPos);
          }
        } catch (err: any) {
          console.error('Failed to delete segment:', err);
          showToast('Failed to delete segment: ' + err.message, 'error', 5000);
        }
      };
    }

    // Extend Start button
    const extendSegmentStartBtn = this.editor.$id('extend-segment-start-btn') as HTMLButtonElement | null;
    if (extendSegmentStartBtn) {
      extendSegmentStartBtn.onclick = () => {
        this.extendSegment(segmentId, 'start');
      };
    }

    // Extend End button
    const extendSegmentEndBtn = this.editor.$id('extend-segment-end-btn') as HTMLButtonElement | null;
    if (extendSegmentEndBtn) {
      extendSegmentEndBtn.onclick = () => {
        this.extendSegment(segmentId, 'end');
      };
    }

    // Tile delete buttons
    const deleteSegmentTileBtns = document.querySelectorAll<HTMLElement>('.delete-segment-tile-btn');
    deleteSegmentTileBtns.forEach((btn) => {
      const x = parseInt(btn.dataset.x ?? '');
      const y = parseInt(btn.dataset.y ?? '');
      btn.onclick = async () => {
        try {
          await this.api.removeSegmentTiles(segmentId, [{ x, y }]);
          await this.editor.host?.refreshNetwork();
          // Refresh segment editor
          this.showEditor(segmentId);
          // Refresh main properties panel if showing this tile
          if (this.selectedElement?.x === x && this.selectedElement?.y === y) {
            await this.editor.selectElement({ x, y });
          }
        } catch (err: any) {
          console.error('Failed to remove segment tile:', err);
          showToast(`Failed to remove tile: ${err.message}`, 'error', 5000);
        }
      };
    });

    // Click a tile row (anywhere but the Remove button) → center the layout on
    // that tile and flash it, so a coordinate in the list maps to a place on
    // the map.
    const tileRows = document.querySelectorAll<HTMLElement>('.segment-editor-panel .tile-list-item');
    tileRows.forEach((row) => {
      row.addEventListener('click', (e) => {
        if ((e.target as HTMLElement).closest('button')) return; // let Remove handle its own click
        const x = parseInt(row.dataset.x ?? '', 10);
        const y = parseInt(row.dataset.y ?? '', 10);
        if (Number.isNaN(x) || Number.isNaN(y)) return;
        // Recenter on the tile and flash it, so a coordinate in the list maps to
        // a place on the map. The locate flash's yellow comes from the theme.
        this.editor.highlighter.show({ tiles: [[x, y]] }, { channel: 'locate', reveal: 'center' });
      });
    });

    // In Simulation mode the physical layout is frozen: structural fields and
    // buttons go read-only while the availability / direction segments stay
    // live. The lock re-applies on every render and on mode flips.
    applyStructuralLock(this.editor.$id('secondary-sidebar-content'));
  }

  // Auto-extend a segment by one tile in the given direction.
  async extendSegment(segmentId: string, direction: string): Promise<void> {
    console.log(`[SegmentEditorHandler] Auto-extending segment: ${segmentId} (${direction})`);

    try {
      const segment = await this.api.getSegment(segmentId);
      if (!segment || segment.error) {
        throw new Error('Segment not found');
      }

      const sortedPositions: Array<[number, number]> =
        (segment.ordered_tiles || []).map(
          (t: [number, number, string]) => [t[0], t[1]] as [number, number],
        );
      if (sortedPositions.length === 0) {
        for (const pos of segment.track_positions || []) sortedPositions.push([pos[0], pos[1]]);
      }
      if (sortedPositions.length === 0) {
        throw new Error('Segment has no positions');
      }

      const tracks = this.editor.cachedNetwork?.tracks || [];
      const orientation = detectOrientation(sortedPositions, tracks);
      console.log(`[SegmentEditorHandler] Detected segment orientation: ${orientation}`);

      const nextTile = calculateNextTile(sortedPositions, orientation, direction, tracks);
      console.log(
        `[SegmentEditorHandler] Auto-extending to tile (${nextTile.x}, ${nextTile.y}) with track bits ${nextTile.trackBits}`
      );

      await this.api.extendSegment(segmentId, [[nextTile.x, nextTile.y]], false);
      await this.editor.host?.refreshNetwork();

      this.showEditor(segmentId);

      if (this.selectedElement?.x === nextTile.x && this.selectedElement?.y === nextTile.y) {
        await this.editor.selectElement({ x: nextTile.x, y: nextTile.y });
      }

      showToast(`Segment extended ${direction} to (${nextTile.x}, ${nextTile.y})`, 'success', 3000);
    } catch (err: any) {
      console.error('Failed to auto-extend segment:', err);
      showToast(`Failed to extend segment: ${err.message}`, 'error', 5000);
    }
  }

  async delete(segmentId: string): Promise<void> {
    const confirmed = await showConfirmModal({
      title: 'Delete Segment',
      message: `Delete segment "${segmentId}"?`,
      detail: 'Tiles assigned to this segment will be unassigned.',
      confirmText: 'Delete',
      confirmVariant: 'danger',
    });
    if (!confirmed) return;

    try {
      await this.api.deleteSegment(segmentId);
      if (this.selectedSegmentId === segmentId) {
        this.selectedSegmentId = null;
      }
      await this.refreshList();
      await this.editor.host?.refreshNetwork();
    } catch (err: any) {
      console.error('[SegmentEditorHandler] Failed to delete segment:', err);
      showToast('Failed to delete segment: ' + err.message, 'error', 5000);
    }
  }

  // === Segment Modal Dialogs ===

  /** Show a modal dialog for segment creation or extension. */
  showModal(options: {
    mode: string;
    trackCount: number;
    existingSegments?: string[];
    anchorTiles?: Array<{ x: number; y: number }>;
  }): Promise<any> {
    return new Promise((resolve) => {
      const { mode, trackCount, existingSegments = [], anchorTiles } = options;

      // Create modal overlay
      const overlay = document.createElement('div');
      overlay.className = 'segment-modal-overlay';

      let modalContent = '';

      if (mode === 'create') {
        // Create new segment form
        const defaultId = `SEG_${Date.now().toString(16).toUpperCase().slice(-6)}`;
        modalContent = `
                    <div class="segment-modal">
                        <h3>Create New Segment</h3>
                        <div class="modal-body">
                            <p class="modal-info">Creating segment with <strong>${trackCount}</strong> tracks</p>
                            <div class="form-group">
                                <label for="modal-segment-id">Segment ID</label>
                                <input type="text" id="modal-segment-id" value="${defaultId}" autocomplete="off">
                            </div>
                            <div class="form-group">
                                <label for="modal-segment-name">Segment Name (optional)</label>
                                <input type="text" id="modal-segment-name" placeholder="e.g., Main Line West" autocomplete="off">
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
                            <button class="btn btn-primary" id="modal-confirm">Create Segment</button>
                            <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
                        </div>
                    </div>
                `;
      } else if (mode === 'extend') {
        // Extend existing segment
        if (existingSegments.length === 0) {
          // No existing segments - shouldn't happen in extend mode
          resolve(null);
          return;
        } else if (existingSegments.length === 1) {
          // Single segment to extend
          const segmentId = existingSegments[0];
          const segment = this.cachedSegments?.find((s: any) => s.segment_id === segmentId);
          modalContent = `
                        <div class="segment-modal">
                            <h3>Extend Segment</h3>
                            <div class="modal-body">
                                <p class="modal-info">Extending segment <strong>${segmentId}</strong></p>
                                ${segment?.name ? `<p class="modal-subinfo">${segment.name}</p>` : ''}
                                <p class="modal-detail">Adding <strong>${trackCount}</strong> tracks to this segment.</p>
                                ${segment ? `<p class="modal-detail">Current size: ${segment.track_count || 0} tracks</p>` : ''}
                            </div>
                            <div class="modal-footer">
                                <button class="btn btn-primary" id="modal-confirm" data-segment-id="${segmentId}">Extend Segment</button>
                                <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
                            </div>
                        </div>
                    `;
        } else {
          // Multiple segments - user must choose
          const segmentOptions = existingSegments
            .map((segmentId) => {
              const segment = this.cachedSegments?.find((s: any) => s.segment_id === segmentId);
              return `
                            <div class="segment-selection-item" data-segment-id="${segmentId}">
                                <input type="radio" name="extend-segment" value="${segmentId}" id="segment-${segmentId}">
                                <label for="segment-${segmentId}">
                                    <span class="segment-id">${segmentId}</span>
                                    ${segment?.name ? `<span class="segment-name">${segment.name}</span>` : ''}
                                    ${segment?.station_names ? `<span class="segment-name segment-station-names">[${segment.station_names.join(', ')}]</span>` : ''}
                                    <span class="segment-track-count">${segment?.track_count || 0} tracks</span>
                                </label>
                            </div>
                        `;
            })
            .join('');

          modalContent = `
                        <div class="segment-modal">
                            <h3>Extend Segment</h3>
                            <div class="modal-body">
                                <p class="modal-info">Path covers <strong>${existingSegments.length}</strong> segments</p>
                                <p class="modal-warning">Select which segment to extend. Other segment assignments will be overwritten.</p>
                                <div class="segment-selection-list">
                                    ${segmentOptions}
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
      const card = overlay.querySelector<HTMLElement>('.segment-modal');
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

      // Handle radio button selection for multi-segment extend
      const radioButtons = overlay.querySelectorAll<HTMLInputElement>('input[name="extend-segment"]');
      const confirmBtn = overlay.querySelector<HTMLButtonElement>('#modal-confirm');

      radioButtons.forEach((radio) => {
        radio.addEventListener('change', () => {
          if (confirmBtn) {
            confirmBtn.disabled = false;
            confirmBtn.dataset.segmentId = radio.value;
          }
        });
      });

      // Handle confirm
      confirmBtn?.addEventListener('click', () => {
        if (mode === 'create') {
          const segmentId = overlay.querySelector<HTMLInputElement>('#modal-segment-id')?.value?.trim();
          const name = overlay.querySelector<HTMLInputElement>('#modal-segment-name')?.value?.trim();
          const maxSpeed = overlay.querySelector<HTMLInputElement>('#modal-max-speed')?.value;
          const minRadius = overlay.querySelector<HTMLInputElement>('#modal-min-radius')?.value;
          const slope = overlay.querySelector<HTMLInputElement>('#modal-slope')?.value;

          if (!segmentId) {
            showToast('Segment ID is required', 'error', 5000);
            return;
          }

          close();
          resolve({
            action: 'create',
            segmentId,
            name: name || '',
            maxSpeed: maxSpeed ? parseFloat(maxSpeed) : null,
            minRadius: minRadius ? parseFloat(minRadius) : null,
            slope: slope !== undefined && slope !== '' ? parseFloat(slope) : null,
          });
        } else if (mode === 'extend') {
          const segmentId = confirmBtn!.dataset.segmentId;
          if (!segmentId) {
            showToast('Please select a segment', 'error', 5000);
            return;
          }

          close();
          resolve({
            action: 'extend',
            segmentId,
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

  // ═══════════════════════════════════════════════════════════════
  // SUBSCRIPTION METHODS (Phase 4)
  // ═══════════════════════════════════════════════════════════════

  // The panel tracks its OWNING BLOCK in the state manager: `blocks` is the
  // only track-model collection the network payload publishes, and it is what
  // carries the occupancy/availability this panel shows.
  _setupSubscription(blockId: string): void {
    this._cleanup();

    this._unsubscribe =
      this.editor.stateManager?.subscribe(
        'blocks',
        blockId,
        (newBlock: any, oldBlock: any) => this._onBlockUpdate(newBlock, oldBlock),
        { immediate: false }
      ) ?? null;
  }

  _onBlockUpdate(newBlock: any, oldBlock: any): void {
    if (!newBlock) {
      showToast('Segment was deleted', 'warning');
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
  }

  _cleanup(): void {
    if (this._unsubscribe) {
      this._unsubscribe();
      this._unsubscribe = null;
    }
    this._isUserInteracting = false;
  }
}
