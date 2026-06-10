// toast.ts — global toast notifications (error / success / warning / info).
//
// Migrated out of app.js's toast IIFE (longterm_todo.md item #7, Wave 4). Every
// module imports { showToast, dismissToast } from here; the container is created
// lazily on the first toast so the module imports cleanly under Node (Vitest)
// where there is no DOM at import time.

// Monochrome Tabler glyphs, not color emoji (DESIGN_LANGUAGE.md §13/§14: no
// emoji in UI chrome). The glyph inherits the toast's status color via CSS.
const ICONS: Record<string, string> = {
  error: 'ti-circle-x',
  success: 'ti-circle-check',
  warning: 'ti-alert-triangle',
  info: 'ti-info-circle',
};

let toastCounter = 0;

function getContainer(): HTMLElement {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }
  return container;
}

/**
 * Show a toast notification.
 * @param message - Message to display
 * @param type - 'error', 'success', 'warning', or 'info'
 * @param duration - Duration in ms (default 4000; 0 = no auto-dismiss)
 * @returns The toast element.
 */
export function showToast(message: string, type: string = 'info', duration: number = 4000): HTMLElement {
  const toastId = `toast-${toastCounter++}`;

  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.id = toastId;
  toast.innerHTML = `
            <span class="toast-icon ti ${ICONS[type] || ICONS.info}"></span>
            <div class="toast-message">${message}</div>
            <button class="toast-close" aria-label="Close">×</button>
        `;

  getContainer().appendChild(toast);

  const closeBtn = toast.querySelector('.toast-close') as HTMLElement;
  closeBtn.onclick = () => dismissToast(toastId);

  if (duration > 0) {
    setTimeout(() => dismissToast(toastId), duration);
  }

  return toast;
}

/** Dismiss a toast by id. */
export function dismissToast(toastId: string): void {
  const toast = document.getElementById(toastId);
  if (!toast) return;

  toast.classList.add('toast-fade-out');

  setTimeout(() => {
    if (toast.parentNode) {
      toast.parentNode.removeChild(toast);
    }
  }, 300);
}
