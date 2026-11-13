// Canvas layers — extra information the map can draw on top of the layout
// (track bits, coordinates, block voltages, stuck-train arrows, …). Each layer
// is on or off per viewer and remembered in localStorage, like the background
// grid (grid-preference.ts), whose env-injection this mirrors so it unit-tests
// without a DOM.
//
// Data flow: the renderer asks canvasLayers.isOn(id) when it draws; the top
// bar's View menu turns layers on and off; App subscribes and repaints the
// canvas layer each one draws on. Until 2026-10-31 these were checkboxes in the
// Settings dialog that the renderer looked up by element id.

const LAYERS_KEY = 'atcs6-canvas-layers';

export type LayerId =
  | 'bits'
  | 'coords'
  | 'edges'
  | 'tile-directions'
  | 'blocks'
  | 'block-dir'
  | 'oneway-dir'
  | 'block-voltage'
  | 'block-length'
  | 'block-radius'
  | 'block-speed-limit'
  | 'switches'
  | 'tunnel-links';

export interface LayerDef {
  id: LayerId;
  label: string;
  group: 'Track' | 'Blocks' | 'Infrastructure';
  /** Which canvas pass draws it, so a toggle repaints that pass: the world
   *  pass (layout), the live pass (arrows), or both. */
  redraw: 'world' | 'live' | 'both';
  /** On for a viewer who has never changed it. */
  defaultOn: boolean;
}

/** Every layer, in the order the View menu lists them. */
export const LAYERS: readonly LayerDef[] = [
  { id: 'bits', label: 'Track bits', group: 'Track', redraw: 'world', defaultOn: false },
  { id: 'coords', label: 'Coordinates', group: 'Track', redraw: 'world', defaultOn: false },
  { id: 'edges', label: 'Edge labels (N/E/S/W)', group: 'Track', redraw: 'world', defaultOn: false },
  // Each tile's forward-travel direction, from its block's tile order.
  { id: 'tile-directions', label: 'Forward direction', group: 'Track', redraw: 'live', defaultOn: false },
  { id: 'blocks', label: 'Blocks', group: 'Blocks', redraw: 'world', defaultOn: false },
  // Each block's forward (F) and reverse (R) end arrows.
  { id: 'block-dir', label: 'Block direction', group: 'Blocks', redraw: 'live', defaultOn: false },
  // An arrow on each tile of a block that allows only one travel direction.
  { id: 'oneway-dir', label: 'One-way direction', group: 'Blocks', redraw: 'live', defaultOn: false },
  { id: 'block-voltage', label: 'Block voltage', group: 'Blocks', redraw: 'world', defaultOn: false },
  { id: 'block-length', label: 'Block length', group: 'Blocks', redraw: 'world', defaultOn: false },
  { id: 'block-radius', label: 'Block radius', group: 'Blocks', redraw: 'world', defaultOn: false },
  { id: 'block-speed-limit', label: 'Block speed limit', group: 'Blocks', redraw: 'world', defaultOn: false },
  { id: 'switches', label: 'Switch states', group: 'Infrastructure', redraw: 'world', defaultOn: false },
  { id: 'tunnel-links', label: 'Tunnel links', group: 'Infrastructure', redraw: 'world', defaultOn: false },
];

const LAYER_IDS = new Set<string>(LAYERS.map((l) => l.id));

/** The definition of one layer. */
export function layerDef(id: LayerId): LayerDef {
  return LAYERS.find((l) => l.id === id)!;
}

/** Storage the layers need from their environment (see memoryLayersEnv). */
export interface LayersEnv {
  getStored(): string | null;
  setStored(value: string): void;
}

/** LayersEnv backed by localStorage. Access is wrapped because it throws in
 *  private-mode browsers and is absent in node tests; then the defaults apply
 *  and changes last for this page only. */
export function browserLayersEnv(storageKey: string = LAYERS_KEY): LayersEnv {
  return {
    getStored: () => {
      try {
        return localStorage.getItem(storageKey);
      } catch {
        return null;
      }
    },
    setStored: (value) => {
      try {
        localStorage.setItem(storageKey, value);
      } catch {
        /* storage unavailable — applies for this page only */
      }
    },
  };
}

/** In-memory LayersEnv for tests. */
export function memoryLayersEnv(initial?: string | null): LayersEnv {
  let stored = initial ?? null;
  return {
    getStored: () => stored,
    setStored: (value) => {
      stored = value;
    },
  };
}

export type LayerListener = (id: LayerId, on: boolean) => void;

/** Which layers are on, persisted as the list of layer ids that are on. */
export class CanvasLayers {
  private readonly env: LayersEnv;
  private readonly on: Set<LayerId>;
  private readonly listeners = new Set<LayerListener>();

  constructor(env: LayersEnv) {
    this.env = env;
    this.on = this.load();
  }

  /** The stored list, or the defaults when nothing usable is stored. Ids the
   *  app no longer has are dropped. */
  private load(): Set<LayerId> {
    const raw = this.env.getStored();
    if (raw !== null) {
      try {
        const ids = JSON.parse(raw);
        if (Array.isArray(ids)) return new Set(ids.filter((id) => LAYER_IDS.has(id)) as LayerId[]);
      } catch {
        /* unreadable — fall through to the defaults */
      }
    }
    return new Set(LAYERS.filter((l) => l.defaultOn).map((l) => l.id));
  }

  isOn(id: LayerId): boolean {
    return this.on.has(id);
  }

  /** Turn a layer on or off, remember it, and tell subscribers. */
  set(id: LayerId, on: boolean): void {
    if (on === this.on.has(id)) return;
    if (on) this.on.add(id);
    else this.on.delete(id);
    this.env.setStored(JSON.stringify([...this.on]));
    this.listeners.forEach((cb) => cb(id, on));
  }

  toggle(id: LayerId): void {
    this.set(id, !this.on.has(id));
  }

  /** Subscribe to changes; returns an unsubscribe function. */
  subscribe(cb: LayerListener): () => void {
    this.listeners.add(cb);
    return () => {
      this.listeners.delete(cb);
    };
  }
}

/** The app-wide layers, backed by localStorage. */
export const canvasLayers = new CanvasLayers(browserLayersEnv());
