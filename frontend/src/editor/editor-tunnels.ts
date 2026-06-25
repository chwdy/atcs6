// editor-tunnels.ts — Tunnel Editor Handler.
//
// A tunnel is a first-class selectable entity. With the Inspect tool, clicking
// a tunnel mouth (the purple "T" glyph at a paired dead-end) opens this panel,
// which shows both endpoints and a Delete button. Deleting removes only the
// pairing; both underlying dead-end tracks remain.
//
// Owned by the Editor (`new TunnelEditorHandler(this)`). Selection highlight is
// kept on the renderer (`renderer.selectedTunnel`), so that is the single
// source of truth; this handler only sets/clears it.

import { showToast } from '../lib/toast';
import { showConfirmModal, applyStructuralLock } from '../lib/helpers';
import type { EditorHost } from './types';

interface TunnelEndpoint {
  x: number;
  y: number;
  edge: string;
}

interface TunnelPair {
  a: TunnelEndpoint;
  b: TunnelEndpoint;
}

export class TunnelEditorHandler {
  editor: EditorHost;

  constructor(editor: EditorHost) {
    this.editor = editor;
  }

  get api(): any {
    return this.editor.api;
  }

  /** The "T{n}" label matching the on-canvas glyph, or "Tunnel" if not found. */
  _labelFor(pair: TunnelPair): string {
    const list = this.editor.renderer?.currentNetwork?.tunnels;
    if (Array.isArray(list)) {
      const idx = list.findIndex((p: TunnelPair) => this._samePair(p, pair));
      if (idx >= 0) return `T${idx + 1}`;
    }
    return 'Tunnel';
  }

  /** True when two pair records describe the same tunnel, ignoring A/B order. */
  _samePair(a: TunnelPair, b: TunnelPair): boolean {
    const eq = (p: TunnelEndpoint, q: TunnelEndpoint) =>
      Number(p.x) === Number(q.x) &&
      Number(p.y) === Number(q.y) &&
      String(p.edge).toUpperCase() === String(q.edge).toUpperCase();
    return (eq(a.a, b.a) && eq(a.b, b.b)) || (eq(a.a, b.b) && eq(a.b, b.a));
  }

  /** Open the per-tunnel inspector for a pair and highlight it on the canvas. */
  async showEditor(pair: TunnelPair): Promise<void> {
    this.editor._currentEditorType = 'tunnel';
    this.editor._currentEditorId = this._endpointKey(pair.a);

    const label = this._labelFor(pair);
    const a = pair.a;
    const b = pair.b;
    const content = `
            <!-- Status header — identity + type pill. -->
            <div class="status-header accent-info">
                <div class="status-header-row">
                    <div class="status-header-id font-mono">${label}</div>
                    <span class="chip chip-state chip-info">Tunnel</span>
                </div>
                <div class="status-header-ident">
                    <span class="status-header-role">Tunnel</span>
                    <span class="status-sep">·</span>
                    <span class="font-mono">(${a.x}, ${a.y}).${a.edge} ↔ (${b.x}, ${b.y}).${b.edge}</span>
                </div>
            </div>

            <!-- Endpoints — the two paired dead-end edges. -->
            <div class="control-section">
                <div class="control-section-header">Endpoints</div>
                <div class="text-muted text-xs mb-8">A tunnel pairs two dead-end edges so they act as one tile-to-tile connection.</div>
                <div class="flex gap-8 mb-8">
                    <span class="text-muted text-xs">A</span>
                    <span class="flex-1"></span>
                    <span class="font-mono">(${a.x}, ${a.y}) · ${a.edge}</span>
                </div>
                <div class="flex gap-8 mb-8">
                    <span class="text-muted text-xs">B</span>
                    <span class="flex-1"></span>
                    <span class="font-mono">(${b.x}, ${b.y}) · ${b.edge}</span>
                </div>
            </div>

            <!-- Controls -->
            <div class="control-section">
                <div class="control-section-header">Controls</div>
                <button id="delete-tunnel-btn" class="btn btn-danger btn-sm">Delete Tunnel</button>
                <div class="text-muted text-xs mt-8">Removes the pairing only — both dead-end tracks remain.</div>
            </div>
        `;

    this.editor.showSecondarySidebar('Tunnel', content);
    // Set the highlight AFTER showSecondarySidebar: opening any panel clears a
    // stale tunnel highlight (see Editor.showSecondarySidebar), so re-set ours.
    this.editor.renderer?.setSelectedTunnel?.(pair);

    const deleteBtn = this.editor.$id('delete-tunnel-btn') as HTMLButtonElement | null;
    if (deleteBtn) {
      deleteBtn.onclick = () => this.delete(pair, { confirm: true });
    }

    // Tunnels are pure layout structure — Delete freezes in Simulation mode.
    applyStructuralLock(this.editor.$id('secondary-sidebar-content'));
  }

  /** Delete a tunnel pairing. Shared by the Delete button and the Delete key. */
  async delete(pair: TunnelPair, opts: { confirm?: boolean } = {}): Promise<void> {
    if (opts.confirm) {
      const confirmed = await showConfirmModal({
        title: 'Delete Tunnel',
        message: `Delete tunnel "${this._labelFor(pair)}"?`,
        detail: 'The pairing will be removed, but both dead-end tracks remain.',
        confirmText: 'Delete',
        confirmVariant: 'danger',
      });
      if (!confirmed) {
        return;
      }
    }

    try {
      // Either endpoint identifies the pair on the server.
      await this.api.deleteTunnel(pair.a.x, pair.a.y, pair.a.edge);
      showToast('Tunnel deleted', 'success', 2000);
      this.clearSelection();
      await this.editor.host?.refreshNetwork();
      this.editor.hideSecondarySidebar();
    } catch (err: any) {
      console.error('[TunnelEditorHandler] Failed to delete tunnel:', err);
      showToast('Failed to delete tunnel: ' + err.message, 'error', 5000);
    }
  }

  /** Drop the current tunnel highlight. */
  clearSelection(): void {
    this.editor.renderer?.setSelectedTunnel?.(null);
  }

  _endpointKey(ep: TunnelEndpoint): string {
    return `${ep.x},${ep.y},${String(ep.edge).toUpperCase()}`;
  }
}
