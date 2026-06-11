// editor-multi-select.ts — Multi-tile selection actions panel.
//
// Migrated from static/js/editor-multi-select.js (longterm_todo.md item #7,
// Wave 2). Behavior is unchanged. Renders the sidebar panel that appears while
// box/lasso/brush-select has a non-empty selection. Phases:
//   1. Selection summary (tile count, block count, affected-block chips, clear)
//   2. Block-membership actions: create-from-selection, add-to-existing,
//      remove-from-blocks
//   3. Block-property batch edits (max speed, min radius, disabled, manual)
//      with mixed-value detection and parallel PATCH apply.
//   4. Destructive: delete the selected tiles (tracks) or delete every block
//      the selection touches.
//
// Owned by the Editor (`new MultiSelectActionsHandler(this)`).

import { showToast } from '../lib/toast';
import type { EditorHost } from './types';
import { showConfirmModal } from '../lib/helpers';

interface SelectionSummary {
  tiles: any[];
  blockIds: Set<string>;
  platformIds: Set<string>;
  switchIds: Set<string>;
  signalIds: Set<string>;
}

export class MultiSelectActionsHandler {
  editor: EditorHost;
  _lastSignature: string | null;

  constructor(editor: EditorHost) {
    this.editor = editor;
    this._lastSignature = null;
  }

  // The selection actions live in the shared secondary sidebar now (the same
  // dedicated panel the block / path editors use), so the panel content element
  // is that sidebar's content area.
  get panel(): HTMLElement | null {
    return document.getElementById('secondary-sidebar-content');
  }
  get api(): any {
    return this.editor.api;
  }

  // Called by the editor whenever boxSelectState.selectedKeys mutates. The
  // selection actions live in the shared secondary sidebar (a dedicated panel,
  // like the block / path editors). Show it while the selection is non-empty
  // and tear it down the instant the selection clears — which is also what
  // leaving selection mode does, since every selection tool empties the set in
  // its onExit. Re-render only when the selection summary actually changes, and
  // refresh in place (no tab / history / persist churn) while the panel is
  // already open, so a brush stroke that grows the set tile-by-tile stays cheap.
  update(selectedKeys: Set<string>): void {
    const showing = this.editor._currentEditorType === 'multi-select';

    if (selectedKeys.size === 0) {
      if (showing) void this.editor.hideSecondarySidebar();
      this._lastSignature = null;
      return;
    }

    const summary = this._summarize(selectedKeys);
    const sig = [
      summary.tiles.length,
      [...summary.blockIds].sort().join(','),
      [...summary.platformIds].sort().join(','),
      [...summary.switchIds].sort().join(','),
      [...summary.signalIds].sort().join(','),
    ].join('|');

    // Already open: swap the body only, leaving the sidebar chrome alone.
    if (showing) {
      if (sig === this._lastSignature) return;
      this._lastSignature = sig;
      const contentEl = this.panel;
      if (contentEl) this._render(contentEl, summary);
      return;
    }

    // First non-empty selection: bring up the dedicated panel. Mark the editor
    // type first so showSecondarySidebar records it (and so update() knows the
    // panel is ours on the next tick).
    this._lastSignature = sig;
    this.editor._currentEditorType = 'multi-select';
    this.editor._currentEditorId = null;
    this.editor.showSecondarySidebar('Selection', this._buildHtml(summary));
    const contentEl = this.panel;
    if (contentEl) this._wire(contentEl, summary);
  }

  _summarize(selectedKeys: Set<string>): SelectionSummary {
    const tracks = this.editor.cachedNetwork?.tracks || [];
    const byKey = new Map<string, any>();
    for (const t of tracks) byKey.set(`${t.x},${t.y}`, t);

    const tiles: any[] = [];
    const blockIds = new Set<string>();
    const platformIds = new Set<string>();
    const switchIds = new Set<string>();
    const signalIds = new Set<string>();
    for (const key of selectedKeys) {
      const t = byKey.get(key);
      if (!t) continue;
      tiles.push(t);
      if (t.block_section_id) blockIds.add(t.block_section_id);
      // A tile's `station_id` holds the platform it belongs to (the field is
      // historically named; see editor.ts removePlatformTile(track.station_id)).
      if (t.station_id) platformIds.add(t.station_id);
      if (t.switch_id) switchIds.add(t.switch_id);
      for (const sid of t.signal_ids || []) signalIds.add(sid);
    }
    return { tiles, blockIds, platformIds, switchIds, signalIds };
  }

  _affectedBlocks(blockIdSet: Set<string>): any[] {
    const cached = this.editor.cachedBlocks || [];
    const byId = new Map<string, any>(cached.map((b: any) => [b.block_id, b]));
    return [...blockIdSet].map((id) => byId.get(id)).filter(Boolean);
  }

  // Render the panel body into `panel` and wire its controls. Used for the
  // cheap in-place refresh while the panel is already open (e.g. each tile of a
  // brush stroke).
  _render(panel: HTMLElement, summary: SelectionSummary): void {
    panel.innerHTML = this._buildHtml(summary);
    this._wire(panel, summary);
  }

  // Attach the action / property-input listeners to a freshly-rendered panel.
  _wire(panel: HTMLElement, summary: SelectionSummary): void {
    this._wireActions(panel);
    const affectedBlocks = this._affectedBlocks(summary.blockIds);
    if (affectedBlocks.length > 0) this._wirePropertyInputs(panel, affectedBlocks);
  }

  // The panel markup. Wrapped in `.multi-select-panel` so the few CSS rules
  // scoped to that class still apply now that the body lives in the shared
  // secondary sidebar. The sidebar header already shows the "Selection" title,
  // so the body carries only the Clear dismiss, not a second heading.
  _buildHtml(summary: SelectionSummary): string {
    const tileCount = summary.tiles.length;
    const blockCount = summary.blockIds.size;
    const tileWord = tileCount === 1 ? 'tile' : 'tiles';
    const blockWord = blockCount === 1 ? 'block' : 'blocks';

    const cachedBlocks = this.editor.cachedBlocks || [];
    const blockOptions =
      cachedBlocks.length === 0
        ? '<option value="" disabled>No blocks exist</option>'
        : '<option value="" disabled selected>Pick a block…</option>' +
          cachedBlocks
            .slice()
            .sort((a: any, b: any) => (a.block_id || '').localeCompare(b.block_id || ''))
            .map((b: any) => {
              const id = this._escape(b.block_id);
              const label = b.name ? `${id} — ${this._escape(b.name)}` : id;
              return `<option value="${id}">${label}</option>`;
            })
            .join('');

    const removeDisabled = blockCount === 0 ? 'disabled' : '';
    const addDisabled = cachedBlocks.length === 0 ? 'disabled' : '';

    const affectedBlocks = this._affectedBlocks(summary.blockIds);
    const propertiesSection =
      affectedBlocks.length === 0 ? '' : this._renderPropertiesSection(affectedBlocks);

    return `
            <div class="multi-select-panel">
                <div class="ms-header">
                    <button type="button" class="btn btn-flat ms-clear" data-action="clear" title="Clear selection">Clear</button>
                </div>
                <div class="status-header accent-accent ms-summary">
                    <div class="ms-counts">${this._countsHtml(summary)}</div>
                    <div class="chip-row ms-chips">${this._chipsHtml(summary)}</div>
                </div>

                ${propertiesSection}

                <div class="ms-group">
                    <div class="ms-group-label">Block membership</div>
                    <button type="button" class="btn btn-outline ms-btn-block" data-action="create-block">
                        New block from selection
                    </button>
                    <div class="ms-row">
                        <select class="ms-input" data-field="add-target-block" ${addDisabled}>
                            ${blockOptions}
                        </select>
                        <button type="button" class="btn btn-outline" data-action="add-to-block" ${addDisabled}>
                            Add
                        </button>
                    </div>
                    <button type="button" class="btn btn-outline ms-btn-block" data-action="remove-from-blocks" ${removeDisabled}>
                        Remove from ${blockCount === 1 ? 'block' : 'blocks'}
                    </button>
                </div>

                <div class="ms-group">
                    <div class="ms-group-label">Destructive</div>
                    <div class="ms-row ms-row-equal">
                        <button type="button" class="btn btn-danger" data-action="delete-tiles">
                            Delete ${tileCount} ${tileWord}
                        </button>
                        <button type="button" class="btn btn-danger" data-action="delete-blocks" ${blockCount === 0 ? 'disabled' : ''}>
                            Delete ${blockCount} ${blockWord}
                        </button>
                    </div>
                    <p class="ms-hint">
                        "Delete tiles" removes the tracks. "Delete blocks" tears down the block grouping but keeps the tiles.
                    </p>
                </div>
            </div>
        `;
  }

  // One count segment per entity type present in the selection. Each number is
  // tinted with the same hue as that type's chips below, so the colour itself
  // acts as the legend. Types with a zero count are dropped.
  _countsHtml(summary: SelectionSummary): string {
    const segs: string[] = [];
    const add = (n: number, sing: string, plur: string, cls: string): void => {
      if (n <= 0) return;
      segs.push(`<strong class="ms-count ${cls}">${n}</strong> ${n === 1 ? sing : plur}`);
    };
    add(summary.tiles.length, 'tile', 'tiles', 'ms-count-tile');
    add(summary.blockIds.size, 'block', 'blocks', 'ms-count-block');
    add(summary.platformIds.size, 'platform', 'platforms', 'ms-count-platform');
    add(summary.switchIds.size, 'switch', 'switches', 'ms-count-switch');
    add(summary.signalIds.size, 'signal', 'signals', 'ms-count-signal');
    // Each segment keeps its number + word together (nowrap) and carries its
    // own trailing separator. Segments are joined by a real space — the only
    // break opportunity — so a long line wraps between segments with the · at
    // the end of a line, never orphaned at the start of the next, and never
    // overflowing.
    return segs
      .map(
        (s, i) =>
          `<span class="ms-count-seg">${s}${i < segs.length - 1 ? '<span class="ms-sep"> ·</span>' : ''}</span>`
      )
      .join(' ');
  }

  // Color-coded chips for every entity the selection touches. Structural
  // entities (block / platform / switch / signal) come first so their hues are
  // visible without scrolling; the raw track tiles follow, capped so a large
  // box-select can't render thousands of chips.
  _chipsHtml(summary: SelectionSummary): string {
    const TILE_CAP = 50;
    const chip = (label: string, cls: string, title: string): string =>
      `<span class="chip ${cls}" title="${this._escape(title)}">${this._escape(label)}</span>`;
    const parts: string[] = [];

    [...summary.blockIds].sort().forEach((id) => parts.push(chip(id, 'chip-accent', `Block ${id}`)));
    [...summary.platformIds]
      .sort()
      .forEach((id) => parts.push(chip(id, 'chip-info', `Platform ${id}`)));
    [...summary.switchIds]
      .sort()
      .forEach((id) => parts.push(chip(id, 'chip-warn', `Switch ${id}`)));
    [...summary.signalIds]
      .sort()
      .forEach((id) => parts.push(chip(id, 'chip-success', `Signal ${id}`)));

    summary.tiles
      .slice(0, TILE_CAP)
      .forEach((t) => parts.push(chip(`${t.x},${t.y}`, 'chip-muted', `Tile (${t.x}, ${t.y})`)));
    const overflow = summary.tiles.length - TILE_CAP;
    if (overflow > 0) {
      parts.push(
        `<span class="chip chip-muted" title="${overflow} more tile${overflow !== 1 ? 's' : ''}">+${overflow}</span>`
      );
    }

    if (parts.length === 0) return '<span class="ms-empty">No entities</span>';
    return parts.join('');
  }

  _renderPropertiesSection(blocks: any[]): string {
    const speeds = new Set(blocks.map((b) => b.speed_limit_kmh ?? null));
    const radii = new Set(blocks.map((b) => b.min_radius_cm ?? null));
    const disabledSet = new Set(blocks.map((b) => !!b.disabled));
    const manualSet = new Set(blocks.map((b) => !!b.manual));

    const speedShared = speeds.size === 1 ? [...speeds][0] : null;
    const radiusShared = radii.size === 1 ? [...radii][0] : null;
    const speedMixed = speeds.size > 1;
    const radiusMixed = radii.size > 1;

    const speedValue = speedShared == null ? '' : String(speedShared);
    const radiusValue = radiusShared == null ? '' : String(radiusShared);
    const speedPlaceholder = speedMixed ? 'Mixed' : 'Not set';
    const radiusPlaceholder = radiusMixed ? 'Mixed' : 'Not set';

    const disabledChecked = !disabledSet.has(true) ? '' : disabledSet.size === 1 ? 'checked' : '';
    const manualChecked = !manualSet.has(true) ? '' : manualSet.size === 1 ? 'checked' : '';

    return `
            <div class="ms-group">
                <div class="ms-group-label">Block properties (${blocks.length} block${blocks.length !== 1 ? 's' : ''})</div>
                <div class="ms-field">
                    <label class="ms-field-label" for="ms-speed">Max speed</label>
                    <div class="ms-field-control">
                        <input id="ms-speed" type="number" class="ms-input ms-input-num" data-field="speed_limit_kmh"
                               value="${speedValue}" placeholder="${speedPlaceholder}" min="0" step="1" />
                        <span class="ms-unit">km/h</span>
                    </div>
                </div>
                <div class="ms-field">
                    <label class="ms-field-label" for="ms-radius">Min radius</label>
                    <div class="ms-field-control">
                        <input id="ms-radius" type="number" class="ms-input ms-input-num" data-field="min_radius_cm"
                               value="${radiusValue}" placeholder="${radiusPlaceholder}" min="0" step="1" />
                        <span class="ms-unit">cm</span>
                    </div>
                </div>
                <div class="ms-flags">
                    <label class="checkbox-wrapper">
                        <input type="checkbox" class="checkbox-input" data-field="disabled" ${disabledChecked} />
                        <span class="checkbox-custom"></span>
                        <span class="checkbox-label">Disabled</span>
                    </label>
                    <label class="checkbox-wrapper">
                        <input type="checkbox" class="checkbox-input" data-field="manual" ${manualChecked} />
                        <span class="checkbox-custom"></span>
                        <span class="checkbox-label">Manual</span>
                    </label>
                </div>
                <button type="button" class="btn btn-accent ms-btn-block" data-action="apply-properties">
                    Apply to ${blocks.length} block${blocks.length !== 1 ? 's' : ''}
                </button>
            </div>
        `;
  }

  _wireActions(panel: HTMLElement): void {
    panel.querySelector('[data-action="clear"]')?.addEventListener('click', () => {
      this.editor.clearMultiSelection();
    });
    panel.querySelector('[data-action="create-block"]')?.addEventListener('click', () => {
      this._createBlockFromSelection().catch((err) => this._reportError('create block', err));
    });
    panel.querySelector('[data-action="add-to-block"]')?.addEventListener('click', () => {
      const sel = panel.querySelector<HTMLSelectElement>('[data-field="add-target-block"]');
      const targetId = sel?.value;
      if (!targetId) {
        showToast('Pick a block first', 'info', 2000);
        return;
      }
      this._addSelectionToBlock(targetId).catch((err) => this._reportError('add to block', err));
    });
    panel.querySelector('[data-action="remove-from-blocks"]')?.addEventListener('click', () => {
      this._removeSelectionFromBlocks().catch((err) => this._reportError('remove from blocks', err));
    });
    panel.querySelector('[data-action="delete-tiles"]')?.addEventListener('click', () => {
      this._deleteSelectedTiles().catch((err) => this._reportError('delete tiles', err));
    });
    panel.querySelector('[data-action="delete-blocks"]')?.addEventListener('click', () => {
      this._deleteAffectedBlocks().catch((err) => this._reportError('delete blocks', err));
    });
  }

  _wirePropertyInputs(panel: HTMLElement, affectedBlocks: any[]): void {
    // Mark mixed-value checkboxes as indeterminate (a visual "third state").
    const disabledSet = new Set(affectedBlocks.map((b) => !!b.disabled));
    const manualSet = new Set(affectedBlocks.map((b) => !!b.manual));
    if (disabledSet.size > 1) {
      const c = panel.querySelector<HTMLInputElement>('[data-field="disabled"]');
      if (c) c.indeterminate = true;
    }
    if (manualSet.size > 1) {
      const c = panel.querySelector<HTMLInputElement>('[data-field="manual"]');
      if (c) c.indeterminate = true;
    }

    // Track which inputs the user has explicitly touched. Only touched
    // fields end up in the PATCH payload — untouched ones leave existing
    // block values untouched even if their displayed value is empty/mixed.
    const propertyFields = ['speed_limit_kmh', 'min_radius_cm', 'disabled', 'manual'];
    propertyFields.forEach((field) => {
      const el = panel.querySelector<HTMLInputElement>(`[data-field="${field}"]`);
      if (!el) return;
      const evt = el.type === 'checkbox' ? 'change' : 'input';
      el.addEventListener(evt, () => {
        el.dataset.touched = 'true';
        if (el.type === 'checkbox') el.indeterminate = false;
      });
    });

    panel.querySelector('[data-action="apply-properties"]')?.addEventListener('click', () => {
      this._applyPropertiesToBlocks(affectedBlocks).catch((err) =>
        this._reportError('apply properties', err)
      );
    });
  }

  // === Block-membership actions ===

  async _createBlockFromSelection(): Promise<void> {
    const positions = this._selectedPositions();
    if (positions.length === 0) return;

    const result = await this.editor.showBlockModal({
      mode: 'create',
      trackCount: positions.length,
      anchorTiles: positions.map(([x, y]) => ({ x, y })),
    });
    if (!result) return;

    const options: Record<string, unknown> = {};
    if (result.maxSpeed) options.speed_limit_kmh = result.maxSpeed;
    if (result.minRadius != null) options.min_radius_cm = result.minRadius;
    if (result.slope != null) options.grade_percent = result.slope;
    await this.api.createBlock(result.blockId, result.name || '', options);
    // force=true so tiles already in another block get reassigned, matching
    // the existing "Block New" tool's behaviour.
    await this.api.addTracksToBlock(result.blockId, positions, true);

    showToast(
      `Created block ${result.blockId} with ${positions.length} tile${positions.length !== 1 ? 's' : ''}`,
      'success',
      3000
    );
    await this._afterMutation();
  }

  async _addSelectionToBlock(targetBlockId: string): Promise<void> {
    const positions = this._selectedPositions();
    if (positions.length === 0) return;

    const proceed = await this._confirm({
      title: `Add ${positions.length} tile${positions.length !== 1 ? 's' : ''} to ${targetBlockId}?`,
      message: 'Tiles already assigned to another block will be reassigned.',
      confirmText: 'Add',
      confirmVariant: 'primary',
    });
    if (!proceed) return;

    const resp = await this.api.addTracksToBlock(targetBlockId, positions, true);
    const added = resp?.added ?? 0;
    const reassigned = resp?.reassigned ?? 0;
    const parts = [`Added ${added} to ${targetBlockId}`];
    if (reassigned) parts.push(`(${reassigned} reassigned)`);
    showToast(parts.join(' '), 'success', 3000);
    await this._afterMutation();
  }

  async _removeSelectionFromBlocks(): Promise<void> {
    const positions = this._selectedPositions();
    if (positions.length === 0) return;

    const tracks = this.editor.cachedNetwork?.tracks || [];
    const byKey = new Map<string, any>();
    for (const t of tracks) byKey.set(`${t.x},${t.y}`, t);

    const byBlock = new Map<string, number[][]>();
    for (const [x, y] of positions) {
      const t = byKey.get(`${x},${y}`);
      if (!t || !t.block_section_id) continue;
      const list = byBlock.get(t.block_section_id) || [];
      list.push([x, y]);
      byBlock.set(t.block_section_id, list);
    }
    if (byBlock.size === 0) {
      showToast('No selected tiles are in a block', 'info', 3000);
      return;
    }

    const tileTotal = [...byBlock.values()].reduce((n, list) => n + list.length, 0);
    const proceed = await this._confirm({
      title: `Remove ${tileTotal} tile${tileTotal !== 1 ? 's' : ''} from ${byBlock.size} block${byBlock.size !== 1 ? 's' : ''}?`,
      message: 'Only edge tiles can be removed. The server will reject interior tiles.',
      confirmText: 'Remove',
      confirmVariant: 'danger',
    });
    if (!proceed) return;

    const tasks = [...byBlock.entries()].map(([blockId, posList]) =>
      this.api
        .removeBlockTiles(blockId, posList)
        .then((resp: any) => ({ blockId, resp, err: null }))
        .catch((err: any) => ({ blockId, resp: null, err }))
    );
    const results = await Promise.all(tasks);

    let removed = 0;
    const errorBlocks: string[] = [];
    for (const { blockId, resp, err } of results) {
      if (err) {
        errorBlocks.push(blockId);
        continue;
      }
      removed += Array.isArray(resp?.removed) ? resp.removed.length : resp?.removed ?? 0;
    }
    if (errorBlocks.length === 0) {
      showToast(
        `Removed ${removed} tile${removed !== 1 ? 's' : ''} from ${byBlock.size} block${byBlock.size !== 1 ? 's' : ''}`,
        'success',
        3000
      );
    } else {
      showToast(
        `Removed ${removed} tile${removed !== 1 ? 's' : ''}, but ${errorBlocks.length} block${errorBlocks.length !== 1 ? 's' : ''} failed: ${errorBlocks.join(', ')}`,
        'warning',
        5000
      );
    }
    await this._afterMutation();
  }

  // === Destructive actions ===

  async _deleteSelectedTiles(): Promise<void> {
    const positions = this._selectedPositions();
    if (positions.length === 0) return;

    // Reuse the editor's existing bulk-delete helper: it handles the
    // server's 409 impact pre-check (orphan signals / platforms /
    // switches) and prompts the user with a force-confirm if needed.
    const result = await this.editor._bulkDeleteWithConfirm(positions);
    if (!result) return; // user cancelled, or no-op

    const removed = Array.isArray(result.removed)
      ? result.removed.length
      : result.removed ?? positions.length;
    showToast(`Deleted ${removed} tile${removed !== 1 ? 's' : ''}`, 'success', 3000);
    await this._afterMutation();
  }

  async _deleteAffectedBlocks(): Promise<void> {
    const summary = this._summarize(this.editor.boxSelectState.selectedKeys);
    const blockIds = [...summary.blockIds];
    if (blockIds.length === 0) return;

    const proceed = await this._confirm({
      title: `Delete ${blockIds.length} block${blockIds.length !== 1 ? 's' : ''}?`,
      message: 'The block groupings will be removed. The tiles stay in place but become unassigned.',
      confirmText: 'Delete blocks',
      confirmVariant: 'danger',
    });
    if (!proceed) return;

    const tasks = blockIds.map((id) =>
      this.api
        .deleteBlock(id)
        .then(() => ({ id, err: null }))
        .catch((err: any) => ({ id, err }))
    );
    const results = await Promise.all(tasks);
    const ok = results.filter((r) => !r.err);
    const failed = results.filter((r) => r.err);

    if (failed.length === 0) {
      showToast(`Deleted ${ok.length} block${ok.length !== 1 ? 's' : ''}`, 'success', 3000);
    } else {
      const sample = failed
        .slice(0, 3)
        .map((r) => `${r.id}: ${r.err.message || r.err}`)
        .join('; ');
      showToast(
        `Deleted ${ok.length}, ${failed.length} failed — ${sample}`,
        'warning',
        5000
      );
    }
    await this._afterMutation();
  }

  // === Block-property batch edit ===

  async _applyPropertiesToBlocks(affectedBlocks: any[]): Promise<void> {
    const panel = this.panel;
    if (!panel) return;

    const payload: Record<string, unknown> = {};
    const speedEl = panel.querySelector<HTMLInputElement>('[data-field="speed_limit_kmh"]');
    if (speedEl?.dataset.touched === 'true') {
      const v = speedEl.value.trim();
      if (v !== '') {
        const num = Number(v);
        if (Number.isFinite(num)) payload.speed_limit_kmh = num;
      }
    }
    const radiusEl = panel.querySelector<HTMLInputElement>('[data-field="min_radius_cm"]');
    if (radiusEl?.dataset.touched === 'true') {
      const v = radiusEl.value.trim();
      if (v !== '') {
        const num = Number(v);
        if (Number.isFinite(num)) payload.min_radius_cm = num;
      }
    }
    const disabledEl = panel.querySelector<HTMLInputElement>('[data-field="disabled"]');
    if (disabledEl?.dataset.touched === 'true') {
      payload.disabled = disabledEl.checked;
    }
    const manualEl = panel.querySelector<HTMLInputElement>('[data-field="manual"]');
    if (manualEl?.dataset.touched === 'true') {
      payload.manual = manualEl.checked;
    }

    if (Object.keys(payload).length === 0) {
      showToast('Nothing to apply — touch a field first', 'info', 2500);
      return;
    }

    const tasks = affectedBlocks.map((b) =>
      this.api
        .updateBlock(b.block_id, payload)
        .then(() => ({ blockId: b.block_id, err: null }))
        .catch((err: any) => ({ blockId: b.block_id, err }))
    );
    const results = await Promise.all(tasks);

    const ok = results.filter((r) => !r.err);
    const failed = results.filter((r) => r.err);

    if (failed.length === 0) {
      showToast(`Applied to ${ok.length} block${ok.length !== 1 ? 's' : ''}`, 'success', 3000);
    } else {
      const sample = failed
        .slice(0, 3)
        .map((r) => `${r.blockId}: ${r.err.message || r.err}`)
        .join('; ');
      const more = failed.length > 3 ? ` +${failed.length - 3} more` : '';
      showToast(
        `Applied to ${ok.length}, ${failed.length} failed — ${sample}${more}`,
        'warning',
        6000
      );
    }

    // Refresh cached blocks so the next render shows new values. Don't
    // clear the selection — user may want to make several adjustments.
    if (typeof this.editor.refreshBlocksList === 'function') {
      await this.editor.refreshBlocksList();
    }
    this._lastSignature = null;
    this.update(this.editor.boxSelectState.selectedKeys);
  }

  // === Helpers ===

  _selectedPositions(): number[][] {
    return [...this.editor.boxSelectState.selectedKeys].map((k: string) => {
      const [xs, ys] = k.split(',');
      return [Number(xs), Number(ys)];
    });
  }

  async _afterMutation(): Promise<void> {
    this.editor.boxSelectState.selectedKeys = new Set();
    this.editor._applyBoxSelection(new Set());
    await this.editor.refreshAndSelect(null);
    if (typeof this.editor.refreshBlocksList === 'function') {
      await this.editor.refreshBlocksList();
    }
  }

  async _confirm({
    title,
    message,
    confirmText,
    confirmVariant,
  }: {
    title: string;
    message: string;
    confirmText?: string;
    confirmVariant?: string;
  }): Promise<boolean> {
    if (typeof showConfirmModal === 'function') {
      return showConfirmModal({ title, message, confirmText, confirmVariant });
    }
    return window.confirm(`${title}\n\n${message}`);
  }

  _reportError(action: string, err: any): void {
    console.error(`[MultiSelect] ${action} failed:`, err);
    showToast(`Failed to ${action}: ${err.message || err}`, 'error', 5000);
  }

  _escape(s: unknown): string {
    return String(s).replace(
      /[&<>"']/g,
      (ch) =>
        (({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }) as Record<
          string,
          string
        >)[ch]
    );
  }
}
