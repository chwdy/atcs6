// editor-power-districts.ts — Power-district editor handler.
//
// A power district groups blocks onto one shared power feed (design_new.md
// "Power district") — the electrical counterpart of a logical station group. The
// stored model is just an id, a name, and member block ids; track voltage is
// runtime state driven by whichever train holds the district. A block the
// operator has not grouped is its own single-block feed.
//
// This handler mirrors StationGroupEditorHandler: a list panel (browse + create)
// and a per-district detail panel (rename, add/remove member blocks, delete).
// On top of the panel it adds the "assign on map" canvas flow — clicking a block
// while the assign tool is active toggles that block's membership in the active
// district. Owned by the Editor (`new PowerDistrictEditorHandler(this)`).

import { showToast } from '../lib/toast';
import type { EditorHost } from './types';
import { districtColorCss } from '../lib/district-color';
import {
  bindFieldAutoSave,
  parseText,
  showConfirmModal,
  applyStructuralLock,
} from '../lib/helpers';

/** Escape a string for safe use inside an HTML attribute value. */
function escAttr(s: unknown): string {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

/** Read the value of a text/number/select input by id. */
function inputValue(id: string): string | undefined {
  return (document.getElementById(id) as HTMLInputElement | null)?.value;
}

export class PowerDistrictEditorHandler {
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

  /** Turn the district color overlay on/off while a power panel is open. */
  _setColorLayer(on: boolean): void {
    const renderer = this.editor.renderer;
    if (!renderer) return;
    if (renderer.showPowerDistrictColors === on) return;
    renderer.showPowerDistrictColors = on;
    renderer.requestRedraw?.({ world: true, live: true, reason: 'power-district-layer' });
  }

  // ── List panel ─────────────────────────────────────────────────────────

  async showPanel(): Promise<void> {
    this.editor._currentEditorType = 'power-district-list';
    this.editor._currentEditorId = null;

    const resp = await this.api.listPowerDistricts();
    const districts = resp?.power_districts || [];

    // A new district starts from free blocks, so the create picker offers only
    // ungrouped blocks; moving an already-grouped block happens in the detail
    // "Add Block" picker (which moves it out of its old district).
    const grouped = new Set<string>();
    for (const d of districts) for (const bid of d.block_ids || []) grouped.add(bid);
    const ungrouped = this._allBlocks().filter((b: any) => !grouped.has(b.block_id));
    const blockOptions = ungrouped
      .map((b: any) => `<option value="${escAttr(b.block_id)}">${escAttr(b.name || b.block_id)}</option>`)
      .join('');
    const canCreate = ungrouped.length > 0;

    const districtCards = districts
      .map((d: any) => {
        const name = d.name && String(d.name).trim() ? String(d.name).trim() : '';
        const title = name || d.district_id;
        const count = (d.block_ids || []).length;
        const held = d.held_by;
        const swatch = `<span class="pd-swatch" style="background:${districtColorCss(d.district_id)}"></span>`;
        const stateChip = held ? '<span class="chip chip-state chip-warn">LIVE</span>' : '';
        const meta = `${count} block${count === 1 ? '' : 's'}${held ? ` · held by ${escAttr(held)}` : ''}`;
        return `
                    <div class="rr-card pd-row accent-info" data-district-id="${escAttr(d.district_id)}" role="button" tabindex="0" title="Open ${escAttr(title)}">
                        <div class="rr-card-head">
                            <div class="rr-card-title-col">
                                <div class="pd-row-name">${swatch}${escAttr(title)}</div>
                                ${name ? `<div class="rr-card-route font-mono">${escAttr(d.district_id)}</div>` : ''}
                            </div>
                            ${stateChip}
                        </div>
                        <div class="pd-row-meta text-muted text-xs">${meta}</div>
                    </div>`;
      })
      .join('');

    const content = `
            <div class="power-districts-panel">
                <div class="pd-create-form">
                    <div class="control-section-header">Create Power District</div>
                    <input id="new-pd-id" class="property-input" placeholder="District ID (optional)">
                    <input id="new-pd-name" class="property-input" placeholder="Name">
                    <select id="new-pd-block" class="property-input" ${canCreate ? '' : 'disabled'}>
                        ${canCreate ? blockOptions : '<option value="">All blocks are already grouped</option>'}
                    </select>
                    <button id="create-pd-btn" class="btn btn-outline btn-full" ${canCreate ? '' : 'disabled'}>Create District</button>
                    <div class="text-muted text-xs mt-4">A district wires its blocks to one power feed. Ungrouped blocks are their own single-block feeds.</div>
                </div>
                <div class="control-section-header">Existing Districts</div>
                ${districts.length === 0 ? '<div class="empty-state">No power districts yet. Every block is its own single-block feed.</div>' : ''}
                <div class="pd-list">${districtCards}</div>
            </div>
        `;

    this.editor.showSecondarySidebar('Power Districts', content);
    this._setColorLayer(true);
    setTimeout(() => this._initPanelHandlers(), 0);
  }

  _initPanelHandlers(): void {
    const createBtn = this.editor.$id('create-pd-btn') as HTMLButtonElement | null;
    if (createBtn) {
      createBtn.onclick = async () => {
        const districtId = inputValue('new-pd-id') || undefined;
        const name = inputValue('new-pd-name') || '';
        const block = inputValue('new-pd-block') || '';
        // Non-empty rule: a district must group at least one block.
        if (!block) {
          showToast('Pick an initial block — a district must contain at least one block.', 'error', 5000);
          return;
        }
        try {
          const created = await this.api.createPowerDistrict({
            district_id: districtId,
            name,
            block_ids: [block],
          });
          if (created?.error) {
            showToast(`Failed to create district: ${created.error}`, 'error', 5000);
            return;
          }
          await this.editor.host?.refreshNetwork();
          // Drop straight into the new district so the next step (add more
          // blocks) is one click away.
          await this.showDetail(created.district_id);
        } catch (err: any) {
          showToast(`Failed to create district: ${err.message}`, 'error', 5000);
        }
      };
    }

    document.querySelectorAll<HTMLElement>('.pd-row').forEach((row) => {
      const open = () => {
        const id = row.dataset.districtId;
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

  async showDetail(districtId: string): Promise<void> {
    const resp = await this.api.getPowerDistrict(districtId);
    if (!resp || resp.error) {
      showToast(`Power district ${districtId} not found`, 'error', 4000);
      await this.showPanel();
      return;
    }

    this.editor._currentEditorType = 'power-district-detail';
    this.editor._currentEditorId = districtId;
    // The assign tool adds clicked blocks to whichever district is open.
    this.editor._activeDistrictId = districtId;

    const district = resp;
    const memberIds: string[] = district.block_ids || [];
    const members: any[] = district.blocks || [];

    // Owner index so the add-picker can tag a block that already belongs to
    // another district (adding it here moves it).
    const listResp = await this.api.listPowerDistricts();
    const ownerByBlock: Record<string, string> = {};
    for (const d of listResp?.power_districts || []) {
      for (const bid of d.block_ids || []) ownerByBlock[bid] = d.district_id;
    }
    const addable = this._allBlocks().filter((b: any) => !memberIds.includes(b.block_id));
    const addOptions = addable
      .map((b: any) => {
        const owner = ownerByBlock[b.block_id];
        const tag = owner && owner !== districtId ? ` — in ${owner}` : '';
        return `<option value="${escAttr(b.block_id)}">${escAttr(b.name || b.block_id)}${tag}</option>`;
      })
      .join('');

    const memberRows = members
      .map((b: any) => {
        const label = b.name && String(b.name).trim() ? String(b.name).trim() : b.block_id;
        return `
                <div class="flex justify-between items-center mb-4" data-block-id="${escAttr(b.block_id)}">
                    <span class="font-mono text-sm">${escAttr(label)}</span>
                    <button class="btn btn-sm btn-secondary pd-remove-block-btn" data-block-id="${escAttr(b.block_id)}">Remove</button>
                </div>`;
      })
      .join('');

    const held = district.held_by;
    const voltage = district.voltage || 0;
    // The feed banner reflects runtime state. In Editor mode no train holds a
    // district, so it reads idle; in Simulation it shows the holder + voltage.
    const feedBanner = held
      ? `<div class="banner banner-warn">
                    <div class="banner-label">Energized</div>
                    <div class="banner-value banner-text">Held by ${escAttr(held)} · ${voltage} V</div>
                </div>`
      : `<div class="banner">
                    <div class="banner-label">Power feed</div>
                    <div class="banner-value">Idle · 0 V — no train holds this district</div>
                </div>`;

    const accent = districtColorCss(districtId, 1);
    const content = `
            <div class="power-district-detail">
                <div class="entity-card" style="--card-accent: ${accent}">
                    <div class="entity-card-dot" style="background-color: ${accent}"></div>
                    <div class="entity-card-body">
                        <span class="entity-card-id">${escAttr(districtId)}</span>
                        <span class="entity-card-meta">${memberIds.length} block${memberIds.length === 1 ? '' : 's'}</span>
                    </div>
                </div>

                <label class="property-label">Name</label>
                <div class="mb-12">
                    <input id="pd-detail-name" class="property-input w-full" value="${escAttr(district.name || '')}" placeholder="Add a name…">
                </div>

                ${feedBanner}

                <div class="control-section-header">Member Blocks (${memberIds.length})</div>
                ${memberRows || '<div class="text-muted text-sm mb-8">No blocks in this district.</div>'}

                <div class="mt-12 mb-8">
                    <button id="pd-assign-on-map-btn" class="btn btn-sm btn-accent btn-full"><i class="ti ti-click"></i> Add blocks on map</button>
                    <div class="text-muted text-xs mt-4">Then click blocks on the canvas to add them. Click a member block again to remove it.</div>
                </div>

                <div class="mt-12 mb-8">
                    <div class="control-section-header">Add Block</div>
                    <div class="flex gap-8 items-center">
                        <select id="pd-detail-add-block" class="property-input" ${addable.length === 0 ? 'disabled' : ''}>
                            ${addable.length === 0 ? '<option value="">Every block is in this district</option>' : addOptions}
                        </select>
                        <button id="pd-detail-add-btn" class="btn btn-sm btn-accent" ${addable.length === 0 ? 'disabled' : ''}>Add</button>
                    </div>
                    <div class="text-muted text-xs mt-4">Adding a block that is in another district moves it here.</div>
                </div>

                <div class="mt-16 flex justify-between">
                    <button id="pd-detail-back-btn" class="btn btn-sm btn-secondary" data-sim-keep>All Districts</button>
                    <button id="pd-detail-delete-btn" class="btn btn-sm btn-danger-ghost"><i class="ti ti-trash"></i> Delete district</button>
                </div>
            </div>
        `;

    this.editor.showSecondarySidebar('Power District', content);
    this._setColorLayer(true);
    setTimeout(() => this._initDetailHandlers(districtId), 0);
  }

  _initDetailHandlers(districtId: string): void {
    // Name auto-saves on blur (§5C) — a field flash is the only ack.
    bindFieldAutoSave(this.editor.$id('pd-detail-name') as HTMLInputElement | null, {
      parse: parseText,
      commit: (value) => this.api.updatePowerDistrict(districtId, { name: value }),
      onApplied: () => this.editor.host?.refreshNetwork(),
    });

    const assignBtn = this.editor.$id('pd-assign-on-map-btn') as HTMLButtonElement | null;
    if (assignBtn) {
      assignBtn.onclick = () => {
        this.editor._activeDistrictId = districtId;
        this.editor._setSelectedTool('power-district-assign');
        this._setColorLayer(true);
        showToast('Click blocks on the canvas to add them to this district. Click a member again to remove it.', 'info', 4000);
      };
    }

    const addBtn = this.editor.$id('pd-detail-add-btn') as HTMLButtonElement | null;
    if (addBtn) {
      addBtn.onclick = async () => {
        const blockId = inputValue('pd-detail-add-block');
        if (!blockId) return;
        await this._addBlock(districtId, blockId);
      };
    }

    document.querySelectorAll<HTMLElement>('.pd-remove-block-btn').forEach((btn) => {
      btn.onclick = async () => {
        const blockId = btn.dataset.blockId;
        if (blockId) await this._removeBlock(districtId, blockId);
      };
    });

    const deleteBtn = this.editor.$id('pd-detail-delete-btn') as HTMLButtonElement | null;
    if (deleteBtn) {
      deleteBtn.onclick = async () => {
        const confirmed = await showConfirmModal({
          title: 'Delete Power District',
          message: `Delete power district "${districtId}"?`,
          detail: 'This removes only the grouping; the blocks remain and each becomes its own single-block feed.',
          confirmText: 'Delete',
          confirmVariant: 'danger',
        });
        if (!confirmed) return;
        try {
          await this.api.deletePowerDistrict(districtId);
          if (this.editor._activeDistrictId === districtId) this.editor._activeDistrictId = null;
          await this.editor.host?.refreshNetwork();
          await this.showPanel();
        } catch (err: any) {
          showToast(`Failed to delete: ${err.message}`, 'error', 5000);
        }
      };
    }

    const backBtn = this.editor.$id('pd-detail-back-btn') as HTMLButtonElement | null;
    if (backBtn) backBtn.onclick = () => this.showPanel();

    applyStructuralLock(this.editor.$id('secondary-sidebar-content'));
  }

  // ── Membership edits (shared by the picker and the assign tool) ──────────

  async _addBlock(districtId: string, blockId: string): Promise<void> {
    try {
      const resp = await this.api.addBlockToDistrict(districtId, blockId);
      if (resp?.error) {
        showToast(`Failed to add block: ${resp.error}`, 'error', 5000);
        return;
      }
      await this.editor.host?.refreshNetwork();
      await this.showDetail(districtId);
    } catch (err: any) {
      showToast(`Failed to add block: ${err.message}`, 'error', 5000);
    }
  }

  async _removeBlock(districtId: string, blockId: string): Promise<void> {
    try {
      const resp = await this.api.removeBlockFromDistrict(districtId, blockId);
      await this.editor.host?.refreshNetwork();
      if (resp?.district_deleted) {
        // Removing the last block auto-deleted the district; its detail view is
        // gone, so fall back to the list.
        if (this.editor._activeDistrictId === districtId) this.editor._activeDistrictId = null;
        await this.showPanel();
      } else {
        await this.showDetail(districtId);
      }
    } catch (err: any) {
      showToast(`Failed to remove block: ${err.message}`, 'error', 5000);
    }
  }

  // ── Assign tool (canvas click) ───────────────────────────────────────────

  /**
   * Handle a canvas click while the assign tool is active: find the block under
   * the clicked tile and toggle its membership in the active district. Clicking a
   * tile with no block, or with no district open, just explains what to do.
   */
  async handleAssignClick(gridPos: { x: number; y: number }): Promise<void> {
    const districtId = this.editor._activeDistrictId;
    if (!districtId) {
      showToast('Open a power district first, then choose "Add blocks on map".', 'info', 4000);
      return;
    }
    const track = this.editor._tracksByKey().get(`${gridPos.x},${gridPos.y}`);
    const blockId = track?.block_section_id;
    if (!blockId) {
      showToast('That tile is not in a block — only blocks can join a power district.', 'info', 3500);
      return;
    }
    // Toggle: a block already in this district leaves it; any other block joins.
    const district = this.editor.cachedNetwork?.power_districts?.find(
      (d: any) => d.district_id === districtId,
    );
    const isMember = !!district && (district.block_ids || []).includes(blockId);
    if (isMember) {
      await this._removeBlock(districtId, blockId);
    } else {
      await this._addBlock(districtId, blockId);
    }
  }
}
