// editor-toolbar.ts — Primary tool-rail controller.
//
// Lifted out of the Editor god-class (behavior unchanged). Renders the floating
// tool rail from the editor's tool-contribution list, dispatches toolbar
// clicks, and manages the group popovers. The tool-contribution builders
// (coreTools / _collectToolContributions) and the toolbar-adjacent editor state
// (selectedTool, _toolsById, modes, handlers) stay on the Editor; this handler
// reaches them via `this.editor`.
//
// Owned by the Editor (`new ToolbarHandler(this)`).
import type { EditorHost } from './types';
import { renderToolGroups } from './tool-registry';

export class ToolbarHandler {
  editor: EditorHost;
  constructor(editor: EditorHost) {
    this.editor = editor;
  }

  /**
   * (Re)render the tool palette from the registry into `#tool-groups`, filtered
   * to the current editor mode, and rebuild the id→contribution lookup. The rail
   * shows Inspect plus one main button per group; a group button opens a flyout
   * popover of that group's tools. Tools whose `availableIn` excludes the current
   * mode are not rendered, and a group with no remaining tools shows no button.
   * Safe to call repeatedly; no-ops if the mount is absent.
   */
  renderToolbar(): void {
    const mount = this.editor.$id('tool-groups');
    if (!mount) return;
    const contributions = this.editor._collectToolContributions();
    this.editor._toolsById = new Map(contributions.map((c: any) => [c.id, c]));
    renderToolGroups(contributions, mount, {
      activeToolId: this.editor.selectedTool || 'inspect',
      mode: this.editor.editorMode,
    });
  }

  /**
   * Delegated click handler for the tool palette. A group button toggles its
   * flyout popover; a tool button (in the rail or a popover) is dispatched and
   * the popover is closed.
   */
  async _onToolbarClick(e: Event): Promise<void> {
    const t = e.target as HTMLElement;

    const groupBtn = t.closest<HTMLElement>('.tool-group-btn');
    if (groupBtn) {
      const group = groupBtn.closest<HTMLElement>('.tool-group');
      const wasOpen = group?.classList.contains('open');
      this._closeToolPopovers();
      if (group && !wasOpen) this._openGroupPopover(group);
      return;
    }

    const toolBtn = t.closest<HTMLElement>('[data-tool]');
    if (toolBtn) {
      await this._dispatchTool(toolBtn);
      this._closeToolPopovers();
    }
  }

  /**
   * Dispatch a tool button by its contribution `kind`: action tools run and
   * leave the active tool alone; tool/mode tools become the active tool (mode
   * tools also enter their editor mode).
   */
  async _dispatchTool(btn: HTMLElement): Promise<void> {
    const newTool = btn.dataset.tool || '';
    const contribution = this.editor._toolsById.get(newTool);
    const oldTool = this.editor.selectedTool;

    // Action tools (panels/dialogs): run and leave the active tool unchanged.
    if (contribution?.kind.type === 'action') {
      contribution.kind.run();
      return;
    }

    // Exit any active mode before switching tools.
    if (this.editor.activeMode) {
      await this.editor.exitMode();
    }

    // Auto-toggle block debug when entering/leaving block tools.
    const isBlockTool = ['block', 'extend-block'].includes(newTool);
    const wasBlockTool = ['block', 'extend-block'].includes(oldTool);
    if (isBlockTool !== wasBlockTool) {
      this.editor.renderer.showBlockColorsOnly = isBlockTool;
      this.editor.host?.refreshNetwork();
    }

    if (contribution?.kind.type === 'mode') {
      // enterMode sets the active tool via _setSelectedTool(mode.toolName).
      await this.editor.enterMode(contribution.kind.factory());
    } else {
      this.editor._setSelectedTool(newTool);
    }

    this.editor.previewPath = null;
    this.editor.previewTracks = null;
    // Drop any lingering build/hover ghost from the previous tool.
    this.editor.renderer.setBuildPreview(null);
    this.editor.renderer.setHoverPreview(null);
    this.editor.hoverPosition = null;
    this.editor.hoverValid = false;
    this.editor.renderer.requestRedraw();
  }

  /** Open a group's flyout popover, positioned beside its button (clamped to the viewport). */
  _openGroupPopover(group: HTMLElement): void {
    const btn = group.querySelector<HTMLElement>('.tool-group-btn');
    const pop = group.querySelector<HTMLElement>('.tool-popover');
    if (!btn || !pop) return;
    group.classList.add('open');
    btn.setAttribute('aria-expanded', 'true');
    // The popover is position:fixed (so the sidebar's overflow can't clip it);
    // place it just right of the button, then nudge up if it would overflow.
    const r = btn.getBoundingClientRect();
    pop.style.left = `${Math.round(r.right + 6)}px`;
    const margin = 8;
    let top = r.top;
    const h = pop.offsetHeight;
    if (top + h + margin > window.innerHeight) {
      top = Math.max(margin, window.innerHeight - h - margin);
    }
    pop.style.top = `${Math.round(top)}px`;
  }

  /** Close any open tool-group popover. */
  _closeToolPopovers(): void {
    const mount: HTMLElement | null = this.editor.$id('tool-groups');
    mount
      ?.querySelectorAll<HTMLElement>('.tool-group.open')
      .forEach((g) => {
        g.classList.remove('open');
        g.querySelector('.tool-group-btn')?.setAttribute('aria-expanded', 'false');
      });
  }
}
