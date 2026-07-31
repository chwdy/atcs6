// connection-monitor.ts — notices when the server goes away, and says what
// happened to it.
//
// A killed server cannot push anything to the browser, so the client has to
// notice on its own. Two things are surfaced:
//
//   * While the server is gone — a non-blocking banner. The layout stays on
//     screen and pannable; it is simply frozen, and the banner says so rather
//     than letting stale trains read as live ones.
//   * Once a server answers again — what the *previous* process's exit looked
//     like, read from `/api/server/shutdown-report`. That record is the only
//     place a SIGKILL shows up at all: the process that took it never got to
//     say anything, so the evidence is the run marker the next boot finds.
//     See shutdown.py.
//
// A dropped connection is not automatically a dead server (a laptop sleeping,
// a proxy hiccup), so `boot_id` from /api/health decides: same id means the
// same process is still there and the page is still valid; a different id
// means a new process, and the page must reload because every id it holds
// came from the old one.

import { showToast } from '../lib/toast';

const HEALTH_POLL_MS = 1000;
// Two consecutive failures before the banner. A single failed poll is usually
// a request that raced the 200ms sim tick, and flashing a scary banner at
// every blip trains people to ignore it.
const FAILURES_BEFORE_BANNER = 2;
// The 200ms sim poll only runs while the simulation is playing. Sitting in the
// editor with it paused, nothing would ever touch the server, so a dead one
// would go unnoticed until the next click. This is the idle floor.
const IDLE_PROBE_MS = 5000;

interface ShutdownRecord {
  reason?: string;
  clean?: boolean;
  safe_state_applied?: boolean;
  signal_name?: string | null;
  pid?: number;
  started_at?: string;
  ended_at?: string;
  ran_for_seconds?: number | null;
  detail?: string;
}

let bootId: string | null = null;
let consecutiveFailures = 0;
let banner: HTMLElement | null = null;
let pollTimer: ReturnType<typeof setTimeout> | null = null;
let disconnectedSince = 0;
let reloading = false;

/** Human-readable duration for the shutdown summary. */
export function formatRunFor(seconds: number | null | undefined): string {
  if (seconds == null || !isFinite(seconds) || seconds < 0) return 'an unknown time';
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

/**
 * One sentence describing how a server process ended.
 *
 * Exported for the unit test — the wording is the whole point of the feature,
 * so it is worth pinning rather than asserting on DOM.
 */
export function describeShutdown(record: ShutdownRecord): { message: string; type: string } {
  const ran = formatRunFor(record.ran_for_seconds);

  if (record.clean === false) {
    return {
      type: 'error',
      message:
        `Previous server run (pid ${record.pid ?? '?'}) was killed after ${ran} ` +
        `without shutting down. Track outputs were never driven to the safe state — ` +
        `check the layout is de-energized.`,
    };
  }

  if (record.safe_state_applied === false) {
    return {
      type: 'error',
      message:
        `Previous server run stopped after ${ran}, but driving outputs to the safe ` +
        `state FAILED. Track voltages and switch coils may still be energized.`,
    };
  }

  if (record.signal_name) {
    return {
      type: 'warning',
      message:
        `Previous server run ended on ${record.signal_name} after ${ran}. ` +
        `Outputs were driven to the safe state.`,
    };
  }

  return {
    type: 'info',
    message: `Previous server run ended cleanly after ${ran}.`,
  };
}

function ensureBanner(): HTMLElement {
  if (banner) return banner;
  banner = document.createElement('div');
  banner.className = 'connection-banner';
  banner.setAttribute('role', 'status');
  banner.innerHTML = `
    <span class="connection-banner-icon ti ti-plug-connected-x" aria-hidden="true"></span>
    <span class="connection-banner-text">Lost connection to server</span>
    <span class="connection-banner-detail"></span>
  `;
  document.body.appendChild(banner);
  return banner;
}

function updateBanner(): void {
  const el = ensureBanner();
  const detail = el.querySelector('.connection-banner-detail') as HTMLElement;
  const secs = Math.round((Date.now() - disconnectedSince) / 1000);
  detail.textContent = `retrying… (${secs}s) — the layout below is frozen, not live`;
}

function hideBanner(): void {
  banner?.remove();
  banner = null;
}

async function fetchHealth(): Promise<{ boot_id?: string } | null> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 800);
    const res = await fetch('/api/health', { signal: controller.signal, cache: 'no-store' });
    clearTimeout(timer);
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

async function fetchShutdownReport(): Promise<ShutdownRecord | null> {
  try {
    const res = await fetch('/api/server/shutdown-report', { cache: 'no-store' });
    if (!res.ok) return null;
    const body = await res.json();
    return body?.previous ?? null;
  } catch {
    return null;
  }
}

async function poll(): Promise<void> {
  pollTimer = null;
  const health = await fetchHealth();

  if (!health) {
    updateBanner();
    pollTimer = setTimeout(poll, HEALTH_POLL_MS);
    return;
  }

  const newBoot = health.boot_id ?? null;
  if (bootId && newBoot && newBoot !== bootId) {
    // A different process is answering. Report how the old one died, then
    // reload — every id this page holds belongs to the dead process.
    reloading = true;
    hideBanner();
    const record = await fetchShutdownReport();
    if (record && (record.clean === false || record.signal_name || record.safe_state_applied === false)) {
      const { message, type } = describeShutdown(record);
      showToast(`${message} Reloading…`, type, 6000);
      setTimeout(() => window.location.reload(), 2500);
    } else {
      // A file-change reload in dev — nothing worth interrupting for.
      window.location.reload();
    }
    return;
  }

  // Same process, or we never learned an id: the connection just blipped.
  consecutiveFailures = 0;
  hideBanner();
  bootId = newBoot ?? bootId;
}

/**
 * Report a failed API call. Only connection-level failures count — an HTTP
 * error carries a `status` and means the server is alive and answering.
 */
export function noteRequestFailure(err: unknown): void {
  if (reloading) return;
  if (err && typeof err === 'object' && 'status' in (err as any) && (err as any).status) return;

  consecutiveFailures += 1;
  if (consecutiveFailures < FAILURES_BEFORE_BANNER) return;

  if (!disconnectedSince) disconnectedSince = Date.now();
  updateBanner();
  if (!pollTimer) pollTimer = setTimeout(poll, HEALTH_POLL_MS);
}

/** Report a successful API call — clears the failure streak. */
export function noteRequestSuccess(): void {
  if (reloading) return;
  consecutiveFailures = 0;
  if (disconnectedSince) {
    disconnectedSince = 0;
    hideBanner();
  }
}

/**
 * Learn this process's boot id and report the previous run if it ended badly.
 * Called once at app start — an operator who was not watching the terminal
 * when the last process was killed still finds out here.
 */
export async function startConnectionMonitor(): Promise<void> {
  const health = await fetchHealth();
  bootId = health?.boot_id ?? null;

  setInterval(async () => {
    // Only the idle case: once disconnected, `poll` is already driving at 1s.
    if (reloading || disconnectedSince || pollTimer) return;
    const probe = await fetchHealth();
    if (!probe) {
      // A failed probe is worth the banner on its own — nothing else is
      // talking to the server to corroborate it.
      consecutiveFailures = FAILURES_BEFORE_BANNER;
      noteRequestFailure(new Error('idle health probe failed'));
    } else if (bootId && probe.boot_id && probe.boot_id !== bootId) {
      // Restarted between probes without us ever seeing a failed request.
      disconnectedSince = Date.now();
      poll();
    }
  }, IDLE_PROBE_MS);

  const record = await fetchShutdownReport();
  if (!record) return;
  // Only an unsafe ending is worth an unsolicited toast on page load; a clean
  // stop from yesterday is noise.
  if (record.clean === false || record.safe_state_applied === false) {
    const { message, type } = describeShutdown(record);
    showToast(message, type, 0);
  }
}
