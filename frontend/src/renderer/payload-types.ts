// payload-types.ts — the network payload the renderer + handlers read, split
// into its layout half and its simulation half.
//
// `LayoutPayload` is the geometry + config the reusable layout component draws
// and edits. `SimState` is the runtime overlay (trains today; per-entity sim
// state such as occupancy/voltage/aspect rides as optional fields on the entity
// types themselves, so it is simply absent from a layout-only payload).
//
// `NetworkPayload = LayoutPayload & Partial<SimState>` keeps the existing shape
// byte-for-byte: every consumer that reads the combined payload is unchanged.

import type { Track } from './renderer-tracks';
import type { BlockInfo } from './renderer-blocks';
import type { Platform } from './renderer-platforms';
import type { Station } from './renderer-stations';

/** A tunnel mouth pair as it appears in the network payload. */
export interface TunnelPayloadEntry {
  a: { x: number; y: number; edge: string };
  b: { x: number; y: number; edge: string };
}

/** A switch entry in the network payload (geometry config + optional sim state). */
export interface SwitchPayloadEntry {
  occupied_by?: unknown;
  train_protection_claims?: unknown[];
  locks?: unknown[];
  main_length_cm?: number;
  diverge_length_cm?: number;
}

/** The layout half: geometry + config the reusable component draws and edits. */
export interface LayoutPayload {
  tracks?: Track[];
  blocks?: Record<string, BlockInfo>;
  platforms?: Platform[];
  stations?: Station[];
  switches?: Record<string, SwitchPayloadEntry | undefined>;
  tunnels?: TunnelPayloadEntry[];
}

/** The simulation half: the runtime overlay the sim plugin draws on top. */
export interface SimState {
  trains?: Array<{ id: string; num_cars?: number }>;
}

/**
 * The combined payload as the server returns it today. Identical in shape to
 * the previous inline `NetworkPayload`; sim fields are optional so a layout-only
 * payload is a valid `NetworkPayload`.
 */
export type NetworkPayload = LayoutPayload & Partial<SimState>;
