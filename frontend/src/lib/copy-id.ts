// copy-id.ts — the "copy this identifier" affordance.
//
// Entity ids (block, segment, train, switch, tunnel) are
// opaque strings the operator does not read so much as *carry*: into a filter
// box, a bug report, an API call, a test fixture. Every identity display clamps
// them to one line with an ellipsis (DESIGN_LANGUAGE.md §5 "Overflow:
// identifiers vs names"), so the differentiating tail is exactly the part that
// is hardest to retype by eye. Each canonical identity display therefore
// carries a copy button next to the id.
//
// The button is markup-only. Panels drop `copyIdButton(id, 'Segment ID')` into
// their template string and wire nothing: a single delegated listener on
// `document` serves every button in the app. That matters because these panels
// rebuild their innerHTML on each poll — a per-panel handler would have to be
// re-bound after every render, and one missed re-bind is a silently dead
// button.

import { showToast } from './toast';

/** Escape for use inside a double-quoted HTML attribute. */
function escAttr(value: string): string {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * Markup for a copy button bound to `id`.
 *
 * @param id    The identifier to place on the clipboard.
 * @param label What the id names, for the tooltip and the screen-reader label
 *              ("Segment ID", "Train ID"). Panels pass their own noun so the
 *              button says *which* id it copies when several sit on one screen.
 * @returns The button's HTML, or `''` when there is no id to copy — so a caller
 *          can splat this into a template without guarding the empty case.
 */
export function copyIdButton(id: string | null | undefined, label: string = 'ID'): string {
  const value = String(id ?? '').trim();
  if (!value) return '';
  installCopyIdHandler();
  const title = `Copy ${label}`;
  return (
    `<button type="button" class="copy-id-btn" data-copy-id="${escAttr(value)}"` +
    ` title="${escAttr(title)}" aria-label="${escAttr(`${title}: ${value}`)}">` +
    `<i class="ti ti-copy"></i></button>`
  );
}

/** Put `text` on the clipboard. Resolves false when neither route works. */
async function writeClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Fall through — a denied permission or a non-secure context lands here.
    }
  }
  // execCommand fallback. navigator.clipboard is undefined on a plain-http
  // origin, which is how this dev server is reached from any machine that is
  // not the one running it (http://<host>:5007). The API is deprecated but is
  // the only thing that works there.
  try {
    const scratch = document.createElement('textarea');
    scratch.value = text;
    scratch.setAttribute('readonly', '');
    scratch.style.position = 'fixed';
    scratch.style.top = '-1000px';
    scratch.style.opacity = '0';
    document.body.appendChild(scratch);
    scratch.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(scratch);
    return ok;
  } catch {
    return false;
  }
}

/** Flash the button's glyph to a checkmark as the in-place acknowledgement. */
function flashCopied(btn: HTMLElement): void {
  const glyph = btn.querySelector('i');
  if (!glyph) return;
  glyph.classList.remove('ti-copy');
  glyph.classList.add('ti-copy-check');
  btn.classList.add('copy-id-btn-done');
  setTimeout(() => {
    // The panel may have re-rendered under us by now; the node is then detached
    // and this is a harmless no-op on garbage.
    glyph.classList.remove('ti-copy-check');
    glyph.classList.add('ti-copy');
    btn.classList.remove('copy-id-btn-done');
  }, 1200);
}

let handlerInstalled = false;

/**
 * Install the one delegated click handler that serves every copy button.
 * Idempotent — `copyIdButton()` calls it, so callers normally need not.
 */
export function installCopyIdHandler(): void {
  if (handlerInstalled || typeof document === 'undefined') return;
  handlerInstalled = true;

  // Capture phase, not bubble. Identity displays sit inside clickable shells —
  // an entity card opens its editor, a list row selects — and those handlers are
  // bound on the shell, below `document`, so they run *before* a bubble-phase
  // listener here. Capturing at the document lets stopPropagation() cut the
  // event off before it ever reaches them: copying an id must not also navigate.
  document.addEventListener(
    'click',
    (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target && target.closest ? (target.closest('[data-copy-id]') as HTMLElement | null) : null;
      if (!btn) return;
      e.preventDefault();
      e.stopPropagation();

      const value = btn.getAttribute('data-copy-id') || '';
      if (!value) return;
      writeClipboard(value).then((ok) => {
        if (ok) {
          flashCopied(btn);
          showToast(`Copied ${value}`, 'success', 1600);
        } else {
          showToast(`Could not copy ${value}`, 'error', 3000);
        }
      });
    },
    true
  );
}
