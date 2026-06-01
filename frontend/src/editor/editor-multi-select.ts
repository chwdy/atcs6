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
// Owned by the Editor (`new MultiSelectActionsHandler(this)`); bridged onto
// window.MultiSelectActionsHandler for the still-classic editor.js.

import type { EditorHost } from './types';
import { showConfirmModal } from '../lib/helpers';

interface SelectionSummary {
  tiles: any[];
  blockIds: Set<string>;
}

export class MultiSelectActionsHandler {
  editor: EditorHost;
  _lastSignature: string | null;

  constructor(editor: EditorHost) {
    this.editor = editor;
    this._lastSignature = null;
  }

  get panel(): HTMLElement | null {
    return document.getElementById('multi-select-panel');
  }
  get propertiesPanel(): HTMLElement | null {
    return document.getElementById('properties-panel');
  }
  get api(): any {
    return this.editor.api;
  }

  // Called by the editor whenever boxSelectState.selectedKeys mutates.
  // Toggles visibility against the properties panel and re-renders the body
  // only when the selection summary has actually changed (avoids tearing
  // down event handlers on every drag tick).
  update(selectedKeys: Set<string>): void {
    const panel = this.panel;
    if (!panel) return;

    const size = selectedKeys.size;
    if (size === 0) {
      panel.hidden = true;
      if (this.propertiesPanel) this.propertiesPanel.hidden = false;
      this._lastSignature = null;
      return;
    }

    panel.hidden = false;
    if (this.propertiesPanel) this.propertiesPanel.hidden = true;

    const summary = this._summarize(selectedKeys);
    const sig = `${summary.tiles.length}|${[...summary.blockIds].sort().join(',')}`;
    if (sig === this._lastSignature) return;
    this._lastSignature = sig;

    this._render(panel, summary);
  }

  _summarize(selectedKeys: Set<string>): SelectionSummary {
    const tracks = this.editor.cachedNetwork?.tracks || [];
    const byKey = new Map<string, any>();
    for (const t of tracks) byKey.set(`${t.x},${t.y}`, t);

    const tiles: any[] = [];
    const blockIds = new Set<string>();
    for (const key of selectedKeys) {
      const t = byKey.get(key);
      if (!t) continue;
      tiles.push(t);
      if (t.block_section_id) blockIds.add(t.block_section_id);
    }
    return { tiles, blockIds };
  }

  _affectedBlocks(blockIdSet: Set<string>): any[] {
    const cached = this.editor.cachedBlocks || [];
    const byId = new Map<string, any>(cached.map((b: any) => [b.block_id, b]));
    return [...blockIdSet].map((id) => byId.get(id)).filter(Boolean);
  }

  _render(panel: HTMLElement, summary: SelectionSummary): void {
    const tileCount = summary.tiles.length;
    const blockCount = summary.blockIds.size;
    const tileWord = tileCount === 1 ? 'tile' : 'tiles';
    const blockWord = blockCount === 1 ? 'block' : 'blocks';

    const blockChips =
      blockCount === 0
        ? '<span class="multi-select-empty">No blocks touched</span>'
        : [...summary.blockIds]
            .sort()
            .map(
              (id) =>
                `<span class="multi-select-block-chip" title="Block ${this._escape(id)}">${this._escape(id)}</span>`
            )
            .join('');

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

    panel.innerHTML = `
            <h3>Selection</h3>
            <div class="multi-select-counts">
                <strong>${tileCount}</strong> ${tileWord}
                <span class="multi-select-sep">·</span>
                <strong>${blockCount}</strong> ${blockWord} affected
            </div>
            <div class="multi-select-blocks">${blockChips}</div>

            <div class="multi-select-group">
                <div class="multi-select-group-label">Block membership</div>
                <div class="multi-select-toolbar">
                    <button type="button" class="btn btn-primary btn-size-sm" data-action="create-block">
                        New block from selection
                    </button>
                </div>
                <div class="multi-select-row">
                    <select class="multi-select-input" data-field="add-target-block" ${addDisabled}>
                        ${blockOptions}
                    </select>
                    <button type="button" class="btn btn-secondary btn-size-sm" data-action="add-to-block" ${addDisabled}>
                        Add
                    </button>
                </div>
                <div class="multi-select-toolbar">
                    <button type="button" class="btn btn-secondary btn-size-sm" data-action="remove-from-blocks" ${removeDisabled}>
                        Remove from ${blockCount === 1 ? 'block' : 'blocks'}
                    </button>
                </div>
            </div>

            ${propertiesSection}

            <div class="multi-select-group">
                <div class="multi-select-group-label">Destructive</div>
                <div class="multi-select-toolbar">
                    <button type="button" class="btn btn-danger btn-size-sm" data-action="delete-tiles">
                        Delete ${tileCount} ${tileWord}
                    </button>
                    <button type="button" class="btn btn-danger btn-size-sm" data-action="delete-blocks" ${blockCount === 0 ? 'disabled' : ''}>
                        Delete ${blockCount} ${blockWord}
                    </button>
                </div>
                <p class="multi-select-hint">
                    "Delete tiles" removes the tracks. "Delete blocks" tears down the block grouping but keeps the tiles.
                </p>
            </div>

            <div class="multi-select-toolbar">
                <button type="button" class="btn btn-secondary btn-size-sm" data-action="clear">Clear selection</button>
            </div>
        `;

    this._wireActions(panel);
    if (affectedBlocks.length > 0) this._wirePropertyInputs(panel, affectedBlocks);
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
            <div class="multi-select-group">
                <div class="multi-select-group-label">Block properties (${blocks.length} block${blocks.length !== 1 ? 's' : ''})</div>
                <div class="multi-select-field">
                    <label for="ms-speed">Max speed (km/h)</label>
                    <input id="ms-speed" type="number" class="multi-select-input" data-field="speed_limit_kmh"
                           value="${speedValue}" placeholder="${speedPlaceholder}" min="0" step="1" />
                </div>
                <div class="multi-select-field">
                    <label for="ms-radius">Min radius (cm)</label>
                    <input id="ms-radius" type="number" class="multi-select-input" data-field="min_radius_cm"
                           value="${radiusValue}" placeholder="${radiusPlaceholder}" min="0" step="1" />
                </div>
                <div class="multi-select-field multi-select-field-checkbox">
                    <label for="ms-disabled">Disabled</label>
                    <input id="ms-disabled" type="checkbox" data-field="disabled" ${disabledChecked} />
                </div>
                <div class="multi-select-field multi-select-field-checkbox">
                    <label for="ms-manual">Manual</label>
                    <input id="ms-manual" type="checkbox" data-field="manual" ${manualChecked} />
                </div>
                <div class="multi-select-toolbar">
                    <button type="button" class="btn btn-primary btn-size-sm" data-action="apply-properties">
                        Apply to ${blocks.length} block${blocks.length !== 1 ? 's' : ''}
                    </button>
                </div>
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
        window.showToast?.('Pick a block first', 'info', 2000);
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
    });
    if (!result) return;

    await this.api.createBlock(result.blockId, result.name || '');
    if (result.maxSpeed) {
      await this.api.updateBlock(result.blockId, { speed_limit_kmh: result.maxSpeed });
    }
    // force=true so tiles already in another block get reassigned, matching
    // the existing "Block New" tool's behaviour.
    await this.api.addTracksToBlock(result.blockId, positions, true);

    window.showToast?.(
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
    window.showToast?.(parts.join(' '), 'success', 3000);
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
      window.showToast?.('No selected tiles are in a block', 'info', 3000);
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
      window.showToast?.(
        `Removed ${removed} tile${removed !== 1 ? 's' : ''} from ${byBlock.size} block${byBlock.size !== 1 ? 's' : ''}`,
        'success',
        3000
      );
    } else {
      window.showToast?.(
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
    window.showToast?.(`Deleted ${removed} tile${removed !== 1 ? 's' : ''}`, 'success', 3000);
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
      window.showToast?.(`Deleted ${ok.length} block${ok.length !== 1 ? 's' : ''}`, 'success', 3000);
    } else {
      const sample = failed
        .slice(0, 3)
        .map((r) => `${r.id}: ${r.err.message || r.err}`)
        .join('; ');
      window.showToast?.(
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
      window.showToast?.('Nothing to apply — touch a field first', 'info', 2500);
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
      window.showToast?.(`Applied to ${ok.length} block${ok.length !== 1 ? 's' : ''}`, 'success', 3000);
    } else {
      const sample = failed
        .slice(0, 3)
        .map((r) => `${r.blockId}: ${r.err.message || r.err}`)
        .join('; ');
      const more = failed.length > 3 ? ` +${failed.length - 3} more` : '';
      window.showToast?.(
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
    window.showToast?.(`Failed to ${action}: ${err.message || err}`, 'error', 5000);
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
