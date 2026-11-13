// editor-block-grouping.ts — Block grouping editor handler.
//
// A `Block` groups one or more `Segment`s onto one shared power feed and owns
// the occupancy state for that whole run (design_v2.md "Block").
// The stored grouping is just an id, a name, and member segment ids; track
// voltage and occupancy are runtime state driven by whichever train holds the
// block. A segment the operator has not grouped sits in a block of its own.
//
// This handler mirrors StationGroupEditorHandler: a list panel (browse + create)
// and a per-block detail panel (rename, add/remove member segments, ungroup).
// On top of the panel it adds the "assign on map" canvas flow — clicking a
// segment while the assign tool is active toggles that segment's membership in
// the active block. Owned by the Editor (`new BlockGroupingEditorHandler(this)`).
//
// This is the renamed power-district tool: grouping used to be a separate
// "power district" wrapped around blocks, and is now the block itself.
//
// Because the block owns the power feed, it also owns the hardware binding for
// it: the Hardware section (occupancy detector + track output ports) lives on
// this panel, keyed by block id. It is not on the Segment panel — a segment has
// no feed of its own.

import { showToast } from '../lib/toast';
import { copyIdButton } from '../lib/copy-id';
import type { EditorHost } from './types';
import { blockColorCss } from '../lib/block-color';
import {
  bindFieldAutoSave,
  parseText,
  showConfirmModal,
  applyStructuralLock,
  renderPolaritySegment,
  bindPolaritySegment,
} from '../lib/helpers';
import {
  fetchHardwareConfig,
  hardwareSectionHtml,
  bindHardwareSection,
  BLOCK_HW_SLOTS,
} from '../ui/hardware-binding';

/** Escape a string for safe use inside an HTML attribute value. */
function escAttr(s: unknown): string {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

/** Read the value of a text/number/select input by id. */
function inputValue(id: string): string | undefined {
  return (document.getElementById(id) as HTMLInputElement | null)?.value;
}

export class BlockGroupingEditorHandler {
  editor: EditorHost;

  constructor(editor: EditorHost) {
    this.editor = editor;
  }

  get api(): any {
    return this.editor.api;
  }

  /** All blocks as an array, from the editor's cached network snapshot. */
  _allBlocks(): any[] {
    return Object.values(this.editor.cachedNetwork?.blocks || {});
  }

  /**
   * Every segment on the layout, flattened out of the blocks that own them.
   * Each entry carries `_block_id` so the pickers can tell where it lives now.
   */
  _allSegments(): any[] {
    const out: any[] = [];
    for (const block of this._allBlocks()) {
      for (const segment of (block as any)?.segments || []) {
        out.push({ ...segment, _block_id: (block as any).block_id });
      }
    }
    return out;
  }

  /** Turn the block-grouping color overlay on/off while a block panel is open. */
  _setColorLayer(on: boolean): void {
    const renderer = this.editor.renderer;
    if (!renderer) return;
    if (renderer.showBlockGroupingColors === on) return;
    renderer.showBlockGroupingColors = on;
    renderer.requestRedraw?.({ world: true, live: true, reason: 'block-grouping-layer' });
  }

  // ── List panel ─────────────────────────────────────────────────────────

  async showPanel(): Promise<void> {
    this.editor._currentEditorType = 'block-list';
    this.editor._currentEditorId = null;

    const blocks = this._allBlocks();

    // A new grouping starts from a segment that is not already grouped with
    // others, so the create picker offers only sole-member segments; moving an
    // already-grouped segment happens in the detail "Add Segment" picker (which
    // moves it out of its old block).
    const groupedSegmentIds = new Set<string>();
    for (const b of blocks) {
      const ids = (b as any).segment_ids || [];
      if (ids.length > 1) for (const sid of ids) groupedSegmentIds.add(sid);
    }
    const ungrouped = this._allSegments().filter((s: any) => !groupedSegmentIds.has(s.segment_id));
    const segmentOptions = ungrouped
      .map((s: any) => `<option value="${escAttr(s.segment_id)}">${escAttr(s.name || s.segment_id)}</option>`)
      .join('');
    const canCreate = ungrouped.length > 0;

    const blockCards = blocks
      .map((b: any) => {
        const name = b.name && String(b.name).trim() ? String(b.name).trim() : '';
        const title = name || b.block_id;
        const count = (b.segment_ids || []).length;
        const held = b.occupied_by;
        const swatch = `<span class="bg-swatch" style="background:${blockColorCss(b.block_id)}"></span>`;
        const stateChip = held ? '<span class="chip chip-state chip-warn">LIVE</span>' : '';
        const meta = `${count} segment${count === 1 ? '' : 's'}${held ? ` · held by ${escAttr(held)}` : ''}`;
        return `
                    <div class="rr-card bg-row accent-info" data-block-id="${escAttr(b.block_id)}" role="button" tabindex="0" title="Open ${escAttr(title)}">
                        <div class="rr-card-head">
                            <div class="rr-card-title-col">
                                <div class="bg-row-name">${swatch}${escAttr(title)}</div>
                                ${name ? `<div class="rr-card-route font-mono">${escAttr(b.block_id)}</div>` : ''}
                            </div>
                            ${stateChip}
                        </div>
                        <div class="bg-row-meta text-muted text-xs">${meta}</div>
                    </div>`;
      })
      .join('');

    const content = `
            <div class="blocks-panel">
                <div class="bg-create-form">
                    <div class="control-section-header">Create Block</div>
                    <input id="new-block-id" class="property-input" placeholder="Block ID (optional)">
                    <input id="new-block-name" class="property-input" placeholder="Name">
                    <select id="new-block-segment" class="property-input" ${canCreate ? '' : 'disabled'}>
                        ${canCreate ? segmentOptions : '<option value="">All segments are already grouped</option>'}
                    </select>
                    <button id="create-block-btn" class="btn btn-outline btn-full" ${canCreate ? '' : 'disabled'}>Create Block</button>
                    <div class="text-muted text-xs mt-4">A block wires its segments to one power feed and occupies as one unit. An ungrouped segment is its own single-segment block.</div>
                </div>
                <div class="control-section-header">Existing Blocks</div>
                ${blocks.length === 0 ? '<div class="empty-state">No blocks yet. Draw some track first.</div>' : ''}
                <div class="bg-list">${blockCards}</div>
            </div>
        `;

    this.editor.showSecondarySidebar('Blocks', content);
    this._setColorLayer(true);
    setTimeout(() => this._initPanelHandlers(), 0);
  }

  _initPanelHandlers(): void {
    const createBtn = this.editor.$id('create-block-btn') as HTMLButtonElement | null;
    if (createBtn) {
      createBtn.onclick = async () => {
        const blockId = inputValue('new-block-id') || undefined;
        const name = inputValue('new-block-name') || '';
        const segment = inputValue('new-block-segment') || '';
        // Non-empty rule: a block must group at least one segment.
        if (!segment) {
          showToast('Pick an initial segment — a block must contain at least one segment.', 'error', 5000);
          return;
        }
        try {
          const created = await this.api.createBlock([segment], name, blockId ?? null);
          if (created?.error) {
            showToast(`Failed to create block: ${created.error}`, 'error', 5000);
            return;
          }
          await this.editor.host?.refreshNetwork();
          // Drop straight into the new block so the next step (add more
          // segments) is one click away.
          await this.showDetail(created.block_id);
        } catch (err: any) {
          showToast(`Failed to create block: ${err.message}`, 'error', 5000);
        }
      };
    }

    document.querySelectorAll<HTMLElement>('.bg-row').forEach((row) => {
      const open = () => {
        const id = row.dataset.blockId;
        if (id) this.showDetail(id);
      };
      row.onclick = open;
      row.onkeydown = (e: KeyboardEvent) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          open();
        }
      };
    });

    applyStructuralLock(this.editor.$id('secondary-sidebar-content'));
  }

  // ── Detail panel ───────────────────────────────────────────────────────

  async showDetail(blockId: string): Promise<void> {
    const resp = await this.api.getBlock(blockId);
    if (!resp || resp.error) {
      showToast(`Block ${blockId} not found`, 'error', 4000);
      await this.showPanel();
      return;
    }

    this.editor._currentEditorType = 'block-detail';
    this.editor._currentEditorId = blockId;
    // The assign tool adds clicked segments to whichever block is open.
    this.editor._activeBlockId = blockId;

    const block = resp;
    const memberIds: string[] = block.segment_ids || [];
    const members: any[] = block.segments || [];

    // Owner index so the add-picker can tag a segment that already belongs to
    // another block (adding it here moves it).
    const allSegments = this._allSegments();
    const ownerBySegment: Record<string, string> = {};
    for (const s of allSegments) ownerBySegment[s.segment_id] = s._block_id;
    const addable = allSegments.filter((s: any) => !memberIds.includes(s.segment_id));

    // Port binding for this block's feed (occupancy detector + track output).
    // Null when the fetch fails, in which case the section renders nothing.
    const hwCfg = await fetchHardwareConfig();
    const addOptions = addable
      .map((s: any) => {
        const owner = ownerBySegment[s.segment_id];
        const tag = owner && owner !== blockId ? ` — in ${owner}` : '';
        return `<option value="${escAttr(s.segment_id)}">${escAttr(s.name || s.segment_id)}${tag}</option>`;
      })
      .join('');

    const memberRows = members
      .map((s: any) => {
        const label = s.name && String(s.name).trim() ? String(s.name).trim() : s.segment_id;
        return `
                <div class="flex justify-between items-center mb-4" data-segment-id="${escAttr(s.segment_id)}">
                    <span class="font-mono text-sm">${escAttr(label)}</span>
                    <button class="btn btn-sm btn-secondary bg-remove-segment-btn" data-segment-id="${escAttr(s.segment_id)}">Remove</button>
                </div>`;
      })
      .join('');

    const held = block.occupied_by;
    const voltage = block.current_voltage || 0;
    // The feed banner reflects runtime state. In Editor mode no train holds a
    // block, so it reads idle; in Simulation it shows the holder + voltage.
    const feedBanner = held
      ? `<div class="banner banner-warn">
                    <div class="banner-label">Energized</div>
                    <div class="banner-value banner-text">Held by ${escAttr(held)} · ${voltage} V</div>
                </div>`
      : `<div class="banner">
                    <div class="banner-label">Power feed</div>
                    <div class="banner-value">Idle · 0 V — no train holds this block</div>
                </div>`;

    const accent = blockColorCss(blockId, 1);
    const content = `
            <div class="block-detail">
                <div class="entity-card" style="--card-accent: ${accent}">
                    <div class="entity-card-dot" style="background-color: ${accent}"></div>
                    <div class="entity-card-body">
                        <span class="entity-card-id">${escAttr(blockId)}</span>
                        <span class="entity-card-meta">${memberIds.length} segment${memberIds.length === 1 ? '' : 's'}</span>
                    </div>
                    ${copyIdButton(blockId, 'Block ID')}
                </div>

                <label class="property-label">Name</label>
                <div class="mb-12">
                    <input id="block-detail-name" class="property-input w-full" value="${escAttr(block.name || '')}" placeholder="Add a name…">
                </div>

                ${feedBanner}

                <div class="control-section-header">Member Segments (${memberIds.length})</div>
                ${memberRows || '<div class="text-muted text-sm mb-8">No segments in this block.</div>'}

                <div class="mt-12 mb-8">
                    <button id="block-assign-on-map-btn" class="btn btn-sm btn-accent btn-full"><i class="ti ti-click"></i> Add segments on map</button>
                    <div class="text-muted text-xs mt-4">Then click segments on the canvas to add them. Click a member segment again to remove it.</div>
                </div>

                <div class="mt-12 mb-8">
                    <div class="control-section-header">Add Segment</div>
                    <div class="flex gap-8 items-center">
                        <select id="block-detail-add-segment" class="property-input" ${addable.length === 0 ? 'disabled' : ''}>
                            ${addable.length === 0 ? '<option value="">Every segment is in this block</option>' : addOptions}
                        </select>
                        <button id="block-detail-add-btn" class="btn btn-sm btn-accent" ${addable.length === 0 ? 'disabled' : ''}>Add</button>
                    </div>
                    <div class="text-muted text-xs mt-4">Adding a segment that is in another block moves it here.</div>
                </div>

                <!-- Rail polarity sits with the hardware section, not with the
                     segment panel's "Allowed travel direction": that one is a
                     routing policy, this one is wiring, and it pairs with the
                     rail binding directly below. The flag lives ON that rail
                     binding and auto-saves with it (design_new.md "Power", Rail
                     polarity lives on the rail binding), so it is disabled
                     until the rails are bound. -->
                <div class="control-section control-section-divider mt-12 pt-12">
                    ${renderPolaritySegment(
                      'block',
                      { polarity_reversed: hwCfg?.block_track?.[blockId]?.reversed === true },
                      {
                        idBase: 'block-polarity',
                        disabled: !hwCfg?.block_track?.[blockId],
                        help: hwCfg?.block_track?.[blockId]
                          ? undefined
                          : 'Bind the track power channels below first — polarity is stored on that rail binding.',
                      }
                    )}
                </div>

                ${hardwareSectionHtml(hwCfg, blockId, BLOCK_HW_SLOTS)}

                <div class="mt-16 flex justify-between">
                    <button id="block-detail-back-btn" class="btn btn-sm btn-secondary" data-sim-keep>All Blocks</button>
                    <button id="block-detail-delete-btn" class="btn btn-sm btn-danger-ghost"><i class="ti ti-trash"></i> Ungroup block</button>
                </div>
            </div>
        `;

    const displayName = String(block.name || '').trim() || blockId;
    this.editor.showSecondarySidebar(displayName, content);
    // Put this block's F / R arrows on the map, the way the segment panel does.
    // They are what makes the Track direction control below checkable: F marks
    // the end a positive track voltage drives toward, which is the first leg of
    // the track Test sweep, so the operator can watch the loco and see whether
    // the wiring agrees (design_new.md "Power", A block voltage carries a
    // direction).
    this.editor.renderer?.setBlockDirectionFocus?.(blockId);
    this._setColorLayer(true);
    setTimeout(() => this._initDetailHandlers(blockId, hwCfg), 0);
  }

  _initDetailHandlers(blockId: string, hwCfg: any = null): void {
    // Hardware binding is per BLOCK (occupancy detector + track feed).
    bindHardwareSection(hwCfg, blockId);

    // Name auto-saves on blur (§5C) — a field flash is the only ack.
    const nameInput = this.editor.$id('block-detail-name') as HTMLInputElement | null;
    bindFieldAutoSave(nameInput, {
      parse: parseText,
      commit: (value) => this.api.updateBlock(blockId, { name: value }),
      onApplied: async () => {
        const title = nameInput?.value.trim() || blockId;
        this.editor.uiHandler?.updatePanelTitle('block-detail', blockId, title);
        await this.editor.host?.refreshNetwork();
      },
    });

    // Rail polarity auto-applies on click (§5C), same contract as the segment
    // panel's availability / direction controls: no success toast, and a reject
    // re-renders so the active cell rolls back to the server's value. The write
    // goes to the block's `block_track` hardware binding — PUT
    // /api/hardware/binding — which persists on its own, no layout save needed
    // (design_new.md "Power", Rail polarity lives on the rail binding).
    bindPolaritySegment('block-polarity', async (reversed, _payload) => {
      const binding = hwCfg?.block_track?.[blockId];
      if (!binding) {
        showToast('Bind the track power channels first — polarity is stored on that rail binding.', 'error', 4000);
        return;
      }
      try {
        const result = await this.api.updateTrackBinding(
          blockId, binding.chip_id, binding.forward, binding.reverse, reversed
        );
        if (result && result.error) {
          showToast(`Cannot change track direction: ${result.error}`, 'error', 4000);
          this.showDetail(blockId);
        } else {
          binding.reversed = reversed;
        }
      } catch (err: any) {
        console.error('Failed to update block track direction:', err);
        showToast(`Cannot change track direction: ${err.message || err}`, 'error', 4000);
        this.showDetail(blockId);
      }
    });

    const assignBtn = this.editor.$id('block-assign-on-map-btn') as HTMLButtonElement | null;
    if (assignBtn) {
      assignBtn.onclick = () => {
        this.editor._activeBlockId = blockId;
        this.editor._setSelectedTool('block-assign');
        this._setColorLayer(true);
        showToast('Click segments on the canvas to add them to this block. Click a member again to remove it.', 'info', 4000);
      };
    }

    const addBtn = this.editor.$id('block-detail-add-btn') as HTMLButtonElement | null;
    if (addBtn) {
      addBtn.onclick = async () => {
        const segmentId = inputValue('block-detail-add-segment');
        if (!segmentId) return;
        await this._addSegment(blockId, segmentId);
      };
    }

    document.querySelectorAll<HTMLElement>('.bg-remove-segment-btn').forEach((btn) => {
      btn.onclick = async () => {
        const segmentId = btn.dataset.segmentId;
        if (segmentId) await this._removeSegment(blockId, segmentId);
      };
    });

    const deleteBtn = this.editor.$id('block-detail-delete-btn') as HTMLButtonElement | null;
    if (deleteBtn) {
      deleteBtn.onclick = async () => {
        const confirmed = await showConfirmModal({
          title: 'Ungroup Block',
          message: `Ungroup block "${blockId}"?`,
          detail: 'This removes only the grouping; the segments remain and each becomes its own single-segment block.',
          confirmText: 'Ungroup',
          confirmVariant: 'danger',
        });
        if (!confirmed) return;
        try {
          await this.api.deleteBlock(blockId);
          if (this.editor._activeBlockId === blockId) this.editor._activeBlockId = null;
          await this.editor.host?.refreshNetwork();
          await this.showPanel();
        } catch (err: any) {
          showToast(`Failed to ungroup: ${err.message}`, 'error', 5000);
        }
      };
    }

    const backBtn = this.editor.$id('block-detail-back-btn') as HTMLButtonElement | null;
    if (backBtn) backBtn.onclick = () => this.showPanel();

    applyStructuralLock(this.editor.$id('secondary-sidebar-content'));
  }

  // ── Membership edits (shared by the picker and the assign tool) ──────────

  async _addSegment(blockId: string, segmentId: string): Promise<void> {
    try {
      const resp = await this.api.addSegmentToBlock(blockId, segmentId);
      if (resp?.error) {
        showToast(`Failed to add segment: ${resp.error}`, 'error', 5000);
        return;
      }
      await this.editor.host?.refreshNetwork();
      await this.showDetail(blockId);
    } catch (err: any) {
      showToast(`Failed to add segment: ${err.message}`, 'error', 5000);
    }
  }

  async _removeSegment(blockId: string, segmentId: string): Promise<void> {
    try {
      const resp = await this.api.removeSegmentFromBlock(blockId, segmentId);
      await this.editor.host?.refreshNetwork();
      if (resp?.block_dissolved) {
        // Removing the last segment dissolved the block; its detail view is
        // gone, so fall back to the list.
        if (this.editor._activeBlockId === blockId) this.editor._activeBlockId = null;
        await this.showPanel();
      } else {
        await this.showDetail(blockId);
      }
    } catch (err: any) {
      showToast(`Failed to remove segment: ${err.message}`, 'error', 5000);
    }
  }

  // ── Assign tool (canvas click) ───────────────────────────────────────────

  /**
   * Handle a canvas click while the assign tool is active: find the segment
   * under the clicked tile and toggle its membership in the active block.
   * Clicking a tile with no segment, or with no block open, just explains what
   * to do.
   */
  async handleAssignClick(gridPos: { x: number; y: number }): Promise<void> {
    const blockId = this.editor._activeBlockId;
    if (!blockId) {
      showToast('Open a block first, then choose "Add segments on map".', 'info', 4000);
      return;
    }
    const track = this.editor._tracksByKey().get(`${gridPos.x},${gridPos.y}`);
    const segmentId = track?.segment_id;
    if (!segmentId) {
      showToast('That tile is not in a segment — only segments can join a block.', 'info', 3500);
      return;
    }
    // Toggle: a segment already in this block leaves it; any other joins.
    const block: any = this.editor.cachedNetwork?.blocks?.[blockId];
    const isMember = !!block && (block.segment_ids || []).includes(segmentId);
    if (isMember) {
      await this._removeSegment(blockId, segmentId);
    } else {
      await this._addSegment(blockId, segmentId);
    }
  }
}
