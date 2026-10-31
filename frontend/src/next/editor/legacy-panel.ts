/**
 * Hosting today's entity panels inside the Editor screen's right panel.
 *
 * Today's panels (segment, block, switch, signal, platform, station, tunnel,
 * the tile panel, the lists) write their HTML into
 * `#secondary-sidebar-content` and then find their inputs by id. Rather than
 * copy that HTML, the right panel moves three of today's nodes into itself
 * while the Editor screen shows, and moves them back when it stops showing:
 * the body, the title, and the "← Switches" back link (all three written by
 * the panel host, editor/editor-ui.ts). Off the Editor screen they wait in a
 * hidden holder in static/index.html. Every panel then renders into the new panel unchanged. Panels are
 * ported off this one at a time (docs/ui-layout-rewrite-plan.md §10).
 */

/** Today's nodes the right panel takes over, and which of its slots each goes in. */
const ADOPTED: { id: string; slot: 'header' | 'body' }[] = [
  { id: 'secondary-nav-parent', slot: 'header' },
  { id: 'secondary-sidebar-title', slot: 'header' },
  { id: 'secondary-sidebar-content', slot: 'body' },
];

/**
 * Move today's panel nodes into `slots`. Returns a function that moves them
 * back to where they were. A node that has since left its slot (another
 * screen took it) is left where it is, so two screens can hand the nodes
 * over in either order.
 */
export function adoptLegacyPanel(
  slots: { header: HTMLElement; body: HTMLElement },
  doc: Document = document,
): () => void {
  const moved: { node: HTMLElement; parent: Node; next: Node | null; slot: HTMLElement }[] = [];
  for (const { id, slot } of ADOPTED) {
    const node = doc.getElementById(id);
    if (!node?.parentNode) continue;
    moved.push({ node, parent: node.parentNode, next: node.nextSibling, slot: slots[slot] });
    slots[slot].appendChild(node);
  }
  return () => {
    // Back in reverse order, so a node whose old neighbour was also moved
    // finds that neighbour home again.
    for (const m of [...moved].reverse()) {
      if (m.node.parentNode !== m.slot) continue;
      const next = m.next && m.next.parentNode === m.parent ? m.next : null;
      m.parent.insertBefore(m.node, next);
    }
  };
}

/**
 * Call `onChange(open)` whenever today's panel opens or closes. The panel
 * host (editor/editor-ui.ts) marks an open panel with the `visible` class on
 * `#secondary-sidebar`; every way of opening or closing one goes through that
 * class. Returns a function
 * that stops watching.
 */
export function watchLegacyPanelOpen(onChange: (open: boolean) => void, doc: Document = document): () => void {
  const container = doc.getElementById('secondary-sidebar');
  if (!container) return () => {};
  const report = () => onChange(container.classList.contains('visible'));
  report();
  const observer = new MutationObserver(report);
  observer.observe(container, { attributes: true, attributeFilter: ['class'] });
  return () => observer.disconnect();
}

/**
 * Fold the tile panel's track bits (the NS/EW/NE/NW/SE/SW checkboxes, the
 * compass and the bit readout, rendered by Editor.showTileEditor) into a
 * closed "Advanced" section at the end of the panel.
 *
 * `content` is `#secondary-sidebar-content`. Runs on every render of it. Changing a bit re-draws the tile
 * panel, so the section stays open for the tile it was opened on and starts
 * closed for any other tile.
 */
export function watchTileBits(content: HTMLElement, tileKey: () => string | null): () => void {
  const doc = content.ownerDocument;
  let openFor: string | null = null;
  const fold = () => {
    const panel = childWithClass(content, 'tile-editor');
    if (!panel || childWithClass(panel, 'next-advanced')) return;
    const bits = childWithClass(panel, 'track-bits-editor');
    if (!bits) return;
    const shape = childWithClass(panel, 'tile-bits-shape');
    const key = tileKey();

    const details = doc.createElement('details');
    details.className = 'tc-disclosure next-advanced';
    details.open = key !== null && key === openFor;
    const summary = doc.createElement('summary');
    summary.textContent = 'Advanced';
    const inner = doc.createElement('div');
    inner.className = 'tc-disclosure-body';
    const note = doc.createElement('p');
    note.className = 'next-advanced-note';
    note.textContent = 'Track bits: which of the six track pieces this tile carries.';
    inner.append(note, bits);
    if (shape) inner.append(shape);
    details.append(summary, inner);
    details.addEventListener('toggle', () => {
      openFor = details.open ? key : null;
    });
    panel.append(details);
  };
  fold();
  // Look once the whole new panel is in place. Browsers already report after
  // the update; happy-dom (the unit tests) reports during it.
  const observer = new MutationObserver(() => queueMicrotask(fold));
  observer.observe(content, { childList: true });
  return () => observer.disconnect();
}

function childWithClass(el: Element, cls: string): HTMLElement | null {
  return ([...el.children].find((c) => c.classList.contains(cls)) as HTMLElement | undefined) ?? null;
}
