/**
 * A button that opens a drop-down menu, for the top bar.
 *
 * It looks like the tile context menu (`.tile-context-menu*` in
 * components/context-menu.css) but hangs under its button, and it has three
 * kinds of row: an action closes the menu and runs; a choice shows a check on
 * the current option, closes and runs; a toggle shows a check while it is on
 * and keeps the menu open, so several can be flipped in one go.
 */
import { useEffect, useRef, useState } from 'preact/hooks';

export interface MenuItem {
  label: string;
  /** Tabler icon class for an action row. */
  icon?: string;
  /** 'action' (default), 'choice' or 'toggle'; see the file comment. */
  kind?: 'action' | 'choice' | 'toggle';
  /** For a choice or toggle row: whether it shows the check. */
  checked?: boolean;
  /** Muted right-aligned text. */
  detail?: string;
  emphasis?: 'danger' | 'accent';
  onSelect: () => void | Promise<void>;
}

export interface MenuSection {
  heading?: string;
  items: MenuItem[];
}

export interface MenuButtonProps {
  /** Visible button text; omit for an icon-only button. */
  label?: string;
  icon?: string;
  /** Tooltip and accessible name. */
  title: string;
  /** Which edge of the button the menu lines up with. */
  align?: 'left' | 'right';
  /** Read each time the menu draws, so it always shows the current state. */
  sections: () => MenuSection[];
}

export function MenuButton({ label, icon, title, align = 'left', sections }: MenuButtonProps) {
  const [open, setOpen] = useState(false);
  // Bumped after a toggle so the open menu re-reads `sections`.
  const [, setRedraws] = useState(0);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const pick = (item: MenuItem) => {
    if (item.kind === 'toggle') {
      void item.onSelect();
      setRedraws((n) => n + 1);
      return;
    }
    setOpen(false);
    void item.onSelect();
  };

  const shown = open ? sections().filter((s) => s.items.length > 0) : [];

  return (
    <div class="ntb-menu-wrap" ref={root}>
      <button
        type="button"
        class={label ? 'btn' : 'btn btn-icon-only'}
        title={title}
        aria-label={title}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        {icon && <i class={icon} aria-hidden="true" />}
        {label && <span>{label}</span>}
        {label && <i class="ti ti-chevron-down ntb-caret" aria-hidden="true" />}
      </button>
      {open && (
        <div class={`tile-context-menu ntb-menu ntb-menu--${align}`} role="menu" aria-label={title}>
          {shown.map((section, i) => (
            <div key={i} role="group">
              {i > 0 && <div class="tile-context-menu-sep" />}
              {section.heading && <div class="tile-context-menu-heading">{section.heading}</div>}
              {section.items.map((item) => (
                <MenuRow key={item.label} item={item} onPick={() => pick(item)} />
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function MenuRow({ item, onPick }: { item: MenuItem; onPick: () => void }) {
  const kind = item.kind ?? 'action';
  const role = kind === 'toggle' ? 'menuitemcheckbox' : kind === 'choice' ? 'menuitemradio' : 'menuitem';
  // A choice or toggle row shows a check while it is on and an empty slot
  // otherwise, so the labels stay lined up.
  const icon = kind === 'action' ? (item.icon ?? '') : item.checked ? 'ti ti-check' : '';
  const classes = ['tile-context-menu-item'];
  if (item.emphasis) classes.push(`tile-context-menu-item--${item.emphasis}`);
  return (
    <button
      type="button"
      role={role}
      aria-checked={kind === 'action' ? undefined : !!item.checked}
      class={classes.join(' ')}
      onClick={onPick}
    >
      <i class={`tile-context-menu-icon ${icon}`} aria-hidden="true" />
      <span class="tile-context-menu-text">
        <span class="tile-context-menu-label">{item.label}</span>
      </span>
      {item.detail && <span class="tile-context-menu-detail">{item.detail}</span>}
    </button>
  );
}
