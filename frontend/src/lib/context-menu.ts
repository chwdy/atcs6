/**
 * Floating context menu — a small viewport-positioned menu shown on right-click.
 *
 * Generic on purpose: callers pass sections of items, each with a label, icon,
 * and an onSelect callback. The editor uses it for the per-tile entity menu
 * (every switch / block / segment on a tile, plus inline
 * actions like throwing a switch), but nothing here knows about tiles.
 *
 * One menu exists at a time; opening a new one closes the previous. It closes
 * on outside pointer-down, Escape, window blur, or after an item is chosen.
 */

export interface ContextMenuItem {
  label: string;
  /**
   * Second line under the label, mono and muted — the entity's stable id when
   * the label is its display name. Two rows can share a name (a block and its
   * own segment do), so this is what tells them apart. Omit it when the label
   * is already the id.
   */
  sublabel?: string;
  /** Tabler icon class, e.g. 'ti ti-train'. Defaults to a neutral dot. */
  icon?: string;
  /** Right-aligned muted text, e.g. the entity type ("Switch"). */
  detail?: string;
  /** Runs after the menu closes. */
  onSelect: () => void | Promise<void>;
  /** Visual emphasis for destructive or primary actions. */
  emphasis?: 'danger' | 'accent';
}

export interface ContextMenuSection {
  /** Optional small uppercase heading above the section. */
  heading?: string;
  items: ContextMenuItem[];
}

let openMenuEl: HTMLElement | null = null;
let detach: (() => void) | null = null;

/** Close the open context menu, if any. Safe to call when none is open. */
export function closeContextMenu(): void {
  if (detach) {
    detach();
    detach = null;
  }
  if (openMenuEl) {
    openMenuEl.remove();
    openMenuEl = null;
  }
}

/**
 * Show a context menu at viewport coordinates (use clientX / clientY from the
 * triggering event). Empty sections are dropped; if nothing is left to show,
 * no menu opens and any existing one is still closed.
 */
export function showContextMenu(
  clientX: number,
  clientY: number,
  sections: ContextMenuSection[],
): void {
  closeContextMenu();

  const usable = sections.filter((s) => s.items.length > 0);
  if (usable.length === 0) return;

  const menu = document.createElement('div');
  menu.className = 'tile-context-menu';
  menu.setAttribute('role', 'menu');

  usable.forEach((section, si) => {
    if (si > 0) {
      const sep = document.createElement('div');
      sep.className = 'tile-context-menu-sep';
      menu.appendChild(sep);
    }
    if (section.heading) {
      const h = document.createElement('div');
      h.className = 'tile-context-menu-heading';
      h.textContent = section.heading;
      menu.appendChild(h);
    }
    for (const item of section.items) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'tile-context-menu-item';
      if (item.emphasis) row.classList.add(`tile-context-menu-item--${item.emphasis}`);
      row.setAttribute('role', 'menuitem');
      row.innerHTML = `
        <i class="${item.icon || 'ti ti-point'} tile-context-menu-icon" aria-hidden="true"></i>
        <span class="tile-context-menu-text">
          <span class="tile-context-menu-label">${escapeHtml(item.label)}</span>
          ${item.sublabel ? `<span class="tile-context-menu-sub font-mono">${escapeHtml(item.sublabel)}</span>` : ''}
        </span>
        ${item.detail ? `<span class="tile-context-menu-detail">${escapeHtml(item.detail)}</span>` : ''}
      `;
      row.addEventListener('click', (e) => {
        e.stopPropagation();
        closeContextMenu();
        void item.onSelect();
      });
      menu.appendChild(row);
    }
  });

  document.body.appendChild(menu);
  openMenuEl = menu;

  // Position at the cursor, nudged back inside the viewport if it would overflow.
  const margin = 6;
  const w = menu.offsetWidth;
  const h = menu.offsetHeight;
  let left = clientX;
  let top = clientY;
  if (left + w > window.innerWidth - margin) left = Math.max(margin, window.innerWidth - w - margin);
  if (top + h > window.innerHeight - margin) top = Math.max(margin, window.innerHeight - h - margin);
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;

  const onPointerDown = (e: Event) => {
    if (openMenuEl && !openMenuEl.contains(e.target as Node)) closeContextMenu();
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Escape') closeContextMenu();
  };
  document.addEventListener('pointerdown', onPointerDown, true);
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('blur', closeContextMenu);
  detach = () => {
    document.removeEventListener('pointerdown', onPointerDown, true);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('blur', closeContextMenu);
  };
}

function escapeHtml(s: string): string {
  return s.replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] as string,
  );
}
