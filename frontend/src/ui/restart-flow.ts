/**
 * Server restart + reconnect overlay — extracted from app.ts.
 *
 * Shows a blocking "Restarting server…" overlay and polls /api/health until the
 * new process answers (then reloads the page) or the timeout elapses (then
 * offers a manual reload). Fully self-contained: no App state, just DOM + fetch.
 */
export function startRestartReconnectFlow(
  { timeoutMs = 60000, pollIntervalMs = 500 }: { timeoutMs?: number; pollIntervalMs?: number } = {},
): void {
  const existing = document.querySelector('.restart-overlay');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.className = 'app-modal-overlay restart-overlay';
  overlay.innerHTML = `
            <div class="app-modal restart-card">
                <div class="restart-spinner" aria-hidden="true"></div>
                <div class="restart-title">Restarting server…</div>
                <div class="restart-sub">Reconnecting when the new process is ready.</div>
            </div>
        `;
  document.body.appendChild(overlay);

  const startedAt = Date.now();
  let stopped = false;

  const finishSuccess = () => {
    stopped = true;
    setTimeout(() => window.location.reload(), 300);
  };

  const finishTimeout = () => {
    stopped = true;
    const card = overlay.querySelector('.restart-card')!;
    card.innerHTML = `
                <div class="restart-title">Server did not come back</div>
                <div class="restart-sub">Check the terminal where the server was launched, then refresh manually.</div>
                <div class="restart-actions">
                    <button class="btn btn-secondary btn-sm" id="restart-reload-btn">Reload</button>
                </div>
            `;
    card.querySelector('#restart-reload-btn')!.addEventListener('click', () => {
      window.location.reload();
    });
  };

  const tick = async () => {
    if (stopped) return;
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 800);
      const res = await fetch('/api/health', { signal: controller.signal });
      clearTimeout(timer);
      if (res.ok) {
        finishSuccess();
        return;
      }
    } catch (_e) {
      // Expected during exec — keep polling.
    }
    if (Date.now() - startedAt >= timeoutMs) {
      finishTimeout();
      return;
    }
    setTimeout(tick, pollIntervalMs);
  };

  setTimeout(tick, pollIntervalMs);
}
