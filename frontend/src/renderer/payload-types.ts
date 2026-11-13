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
import type { SegmentInfo } from './renderer-segments';

/** A tunnel mouth pair as it appears in the network payload. */
export interface TunnelPayloadEntry {
  a: { x: number; y: number; edge: string };
  b: { x: number; y: number; edge: string };
}

/** A switch entry in the network payload (geometry config + optional sim state). */
export interface SwitchPayloadEntry {
  main_length_cm?: number;
  diverge_length_cm?: number;
}

/**
 * The layout half: geometry + config the reusable component draws and edits.
 *
 * The track model is tile -> segment -> block. `blocks` is the only entity map
 * the server sends for the track model: each `BlockInfo` carries its power /
 * occupancy state plus its member `segments` (the geometry). Per-segment
 * geometry is read by flattening those members — see `segmentsFromBlocks`.
 */
export interface LayoutPayload {
  tracks?: Track[];
  blocks?: Record<string, BlockInfo>;
  switches?: Record<string, SwitchPayloadEntry | undefined>;
  tunnels?: TunnelPayloadEntry[];
}

/**
 * Flatten every block's member segments into a `segment_id -> SegmentInfo` map.
 *
 * The network payload nests geometry under the owning block, so this is the one
 * place that derives the geometry index the segment overlays read. Keeping it a
 * derived read means block membership and segment geometry can never drift.
 */
export function segmentsFromBlocks(
  blocks: Record<string, BlockInfo> | null | undefined,
): Record<string, SegmentInfo> {
  const out: Record<string, SegmentInfo> = {};
  for (const block of Object.values(blocks || {})) {
    for (const segment of block?.segments || []) {
      if (segment?.segment_id) out[segment.segment_id] = segment;
    }
  }
  return out;
}

/** The simulation half: the runtime overlay the sim plugin draws on top. */
export interface SimState {
  operation_mode?: string;
  layout_version?: string;
  trains?: Array<{ id: string; num_cars?: number }>;
}

/**
 * The combined payload as the server returns it today. Identical in shape to
 * the previous inline `NetworkPayload`; sim fields are optional so a layout-only
 * payload is a valid `NetworkPayload`.
 */
export type NetworkPayload = LayoutPayload & Partial<SimState>;
