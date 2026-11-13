/**
 * The network snapshot, pushed by the server instead of polled.
 *
 * design_v2.md "Network stream": `GET /api/network/stream`
 * is a server-sent event stream. Each message is the reply `GET /api/network`
 * would give, with the layout part only when it changed, sent every 200 ms
 * and right after any request that changes state. Each message goes through
 * the poller's cache (`NetworkPoller.apply`), so a pushed snapshot and a
 * one-off read lay their live fields over the same layout.
 *
 * When the stream drops (it fails to open, closes, or goes 5 s without a
 * message), this falls back to reading `GET /api/network` every 200 ms —
 * those requests are also how the connection monitor notices a dead server —
 * and tries a new stream every 2 s. The first message on a new stream stops
 * the polling. While the browser tab is hidden the stream is closed, and a
 * new one opens when the tab shows again.
 */
import type { NetworkPoller, NetworkPollResult } from './network-poll';

/** The part of `EventSource` this uses; tests pass a fake. */
export interface EventSourceLike {
  onmessage: ((event: { data: string }) => void) | null;
  onerror: ((event: unknown) => void) | null;
  close(): void;
}

type VisibilityDoc = Pick<Document, 'visibilityState' | 'addEventListener' | 'removeEventListener'>;

export interface NetworkStreamOptions {
  poller: NetworkPoller;
  /** A pushed snapshot, already laid over the cached layout. */
  onMessage: (result: NetworkPollResult) => void | Promise<void>;
  /** Read `GET /api/network` once and show it. Called every `pollIntervalMs`
   *  while the stream is down, and when a message cannot be laid over the
   *  cached layout. */
  pollOnce: () => void | Promise<void>;
  url?: string;
  openEventSource?: (url: string) => EventSourceLike;
  doc?: VisibilityDoc;
  pollIntervalMs?: number;
  retryMs?: number;
  silenceMs?: number;
}

export class NetworkStream {
  private readonly url: string;
  private readonly openEventSource: (url: string) => EventSourceLike;
  private readonly doc: VisibilityDoc | undefined;
  private readonly pollIntervalMs: number;
  private readonly retryMs: number;
  private readonly silenceMs: number;

  private source: EventSourceLike | null = null;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private silenceTimer: ReturnType<typeof setTimeout> | null = null;
  private polling = false;
  private started = false;

  constructor(private readonly options: NetworkStreamOptions) {
    this.url = options.url ?? '/api/network/stream';
    this.openEventSource =
      options.openEventSource ?? ((url) => new EventSource(url) as unknown as EventSourceLike);
    this.doc = options.doc ?? (typeof document === 'undefined' ? undefined : document);
    this.pollIntervalMs = options.pollIntervalMs ?? 200;
    this.retryMs = options.retryMs ?? 2000;
    this.silenceMs = options.silenceMs ?? 5000;
  }

  /** True while the stream is down and `GET /api/network` is read instead. */
  get isPolling(): boolean {
    return this.pollTimer !== null;
  }

  start(): void {
    if (this.started) return;
    this.started = true;
    this.doc?.addEventListener('visibilitychange', this.onVisibilityChange);
    if (!this.hidden()) this.open();
  }

  stop(): void {
    if (!this.started) return;
    this.started = false;
    this.doc?.removeEventListener('visibilitychange', this.onVisibilityChange);
    this.closeAll();
  }

  private hidden(): boolean {
    return this.doc?.visibilityState === 'hidden';
  }

  private readonly onVisibilityChange = (): void => {
    if (!this.started) return;
    this.closeAll();
    if (!this.hidden()) this.open();
  };

  private open(): void {
    this.closeSource();
    const held = this.options.poller.heldLayoutVersion;
    const url = held ? `${this.url}?layout_version=${encodeURIComponent(held)}` : this.url;
    let source: EventSourceLike;
    try {
      source = this.openEventSource(url);
    } catch {
      this.drop();
      return;
    }
    this.source = source;
    source.onmessage = (event) => {
      if (this.source === source) this.onStreamMessage(event.data);
    };
    source.onerror = () => {
      if (this.source === source) this.drop();
    };
    this.armSilenceTimer();
  }

  private onStreamMessage(data: string): void {
    let reply: any;
    try {
      reply = JSON.parse(data);
    } catch {
      this.drop();
      return;
    }
    this.stopPolling();
    this.armSilenceTimer();
    const result = this.options.poller.apply(reply);
    // A live-only message for a layout other than the cached one: read the
    // full network instead (design_v2.md "Layout version").
    Promise.resolve(result ? this.options.onMessage(result) : this.options.pollOnce()).catch((e) =>
      console.error('[NetworkStream] Showing a message failed:', e)
    );
  }

  /** The stream failed: read `GET /api/network` until a new one works. */
  private drop(): void {
    this.closeSource();
    if (!this.started || this.hidden()) return;
    this.startPolling();
    if (!this.retryTimer) {
      this.retryTimer = setTimeout(() => {
        this.retryTimer = null;
        if (this.started && !this.hidden()) this.open();
      }, this.retryMs);
    }
  }

  private startPolling(): void {
    if (this.pollTimer) return;
    const tick = async () => {
      // Skipped while the previous read is still running, so a slow server
      // never piles requests up.
      if (this.polling) return;
      this.polling = true;
      try {
        await this.options.pollOnce();
      } finally {
        this.polling = false;
      }
    };
    void tick();
    this.pollTimer = setInterval(() => void tick(), this.pollIntervalMs);
  }

  private stopPolling(): void {
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.pollTimer = null;
  }

  private armSilenceTimer(): void {
    if (this.silenceTimer) clearTimeout(this.silenceTimer);
    this.silenceTimer = setTimeout(() => {
      this.silenceTimer = null;
      this.drop();
    }, this.silenceMs);
  }

  private closeSource(): void {
    if (this.silenceTimer) clearTimeout(this.silenceTimer);
    this.silenceTimer = null;
    if (!this.source) return;
    const source = this.source;
    this.source = null;
    source.onmessage = null;
    source.onerror = null;
    source.close();
  }

  private closeAll(): void {
    this.closeSource();
    this.stopPolling();
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }
}
