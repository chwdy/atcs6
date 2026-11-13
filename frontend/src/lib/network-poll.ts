/**
 * The network poll that sends the layout only when it changed.
 *
 * design_new.md "Data Flow To UI", Layout version: `GET /api/network` names
 * the layout with `layout_version`. When the poll passes the version it
 * already holds and the layout has not changed, the server leaves the layout
 * out and sends only the live fields (`live_blocks`, `live_switches`,
 * `live_tracks`). This poller keeps the layout from the last full reply and
 * lays the live fields over it, so callers always get the full network shape.
 * Messages from the network stream (`lib/network-stream.ts`) carry the same
 * replies and go through the same cache, through `apply`.
 *
 * An entity whose live fields did not change keeps the same object from poll
 * to poll, so StateManager's change check (`a === b`) skips it without
 * serializing it.
 */

/** Snapshot keys the server leaves out while the layout is unchanged. */
const LAYOUT_KEYS = [
  'tracks',
  'blocks',
  'switches',
  'tunnels',
] as const;

type Entity = Record<string, any>;

export interface NetworkPollResult {
  network: any;
  /** True when this reply carried the full layout: the first poll, or the
   *  first poll after the layout changed. */
  layoutChanged: boolean;
}

function sameValue(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (a === null || b === null || typeof a !== 'object' || typeof b !== 'object') return false;
  return JSON.stringify(a) === JSON.stringify(b);
}

/** `cached` with `live` laid over it; `cached` itself when nothing differs. */
export function withLiveFields(cached: Entity, live: Entity | undefined): Entity {
  if (!live) return cached;
  for (const key of Object.keys(live)) {
    if (!sameValue(cached[key], live[key])) return { ...cached, ...live };
  }
  return cached;
}

function mergeById(cached: Record<string, Entity>, live: Record<string, Entity> | undefined) {
  if (!cached || !live) return cached;
  let changed = false;
  const out: Record<string, Entity> = {};
  for (const [id, entity] of Object.entries(cached)) {
    const merged = withLiveFields(entity, live[id]);
    if (merged !== entity) changed = true;
    out[id] = merged;
  }
  return changed ? out : cached;
}

function mergeTracks(cached: Entity[], live: Entity[] | undefined): Entity[] {
  if (!cached || !live || live.length === 0) return cached;
  const liveByKey = new Map(live.map((t) => [`${t.x},${t.y}`, t]));
  let changed = false;
  const out = cached.map((track) => {
    const merged = withLiveFields(track, liveByKey.get(`${track.x},${track.y}`));
    if (merged !== track) changed = true;
    return merged;
  });
  return changed ? out : cached;
}

export class NetworkPoller {
  private layoutVersion: string | null = null;
  private layout: Entity | null = null;

  constructor(private readonly fetchNetwork: (layoutVersion?: string) => Promise<any>) {}

  /** The `layout_version` of the cached layout, or undefined when none is
   *  cached. Naming it gets a reply without the layout while it still holds. */
  get heldLayoutVersion(): string | undefined {
    return this.layout ? this.layoutVersion ?? undefined : undefined;
  }

  async poll(): Promise<NetworkPollResult> {
    const reply = await this.fetchNetwork(this.heldLayoutVersion);
    return this.apply(reply) ?? this.takeFullReply(await this.fetchNetwork());
  }

  /**
   * Take one reply, from the poll or from the network stream: a full reply
   * replaces the cached layout, a live-only reply is laid over it.
   *
   * Returns null for a live-only reply that names a version other than the
   * one now held (a full reply sent later already replaced the layout): the
   * caller asks for the full network rather than lay these live fields over a
   * different layout.
   */
  apply(reply: any): NetworkPollResult | null {
    if (!reply?.layout_unchanged) return this.takeFullReply(reply);
    if (!this.layout || reply.layout_version !== this.layoutVersion) return null;
    return { network: this.merge(reply), layoutChanged: false };
  }

  private takeFullReply(reply: any): NetworkPollResult {
    if (reply && typeof reply.layout_version === 'string') {
      const layout: Entity = {};
      for (const key of LAYOUT_KEYS) layout[key] = reply[key];
      this.layout = layout;
      this.layoutVersion = reply.layout_version;
    } else {
      this.layout = null;
      this.layoutVersion = null;
    }
    return { network: reply, layoutChanged: true };
  }

  private merge(reply: any): any {
    const { live_blocks, live_switches, live_tracks, ...rest } = reply;
    const layout = this.layout as Entity;
    layout.blocks = mergeById(layout.blocks, live_blocks);
    layout.switches = mergeById(layout.switches, live_switches);
    layout.tracks = mergeTracks(layout.tracks, live_tracks);
    return { ...rest, ...layout };
  }
}
