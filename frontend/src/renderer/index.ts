// renderer/ — renderer-cluster modules migrated off the classic scripts
// (longterm_todo.md item #7).
//
// The barrel re-exports the renderer-cluster classes so consumers (renderer.ts,
// app.ts) import them as real ES symbols. main.ts also imports it so the whole
// cluster is pulled into the bundle.
import './renderer-tunnels';
import './renderer-debug';
import './renderer-blocks';
import './renderer-segments';
import './renderer-tracks';
import './renderer';


export { RendererTunnelHandler } from './renderer-tunnels';
export type {
  TunnelEndpoint,
  TunnelPair,
  TunnelHandlerRenderer,
} from './renderer-tunnels';

export { RendererDebugHandler } from './renderer-debug';
export type { DebugTrack, DebugHandlerRenderer } from './renderer-debug';

export { RendererBlockHandler, UNGROUPED_BLOCK_COLOR } from './renderer-blocks';
export type { BlockInfo, BlockTrack, BlockHandlerRenderer } from './renderer-blocks';

export { RendererSegmentHandler } from './renderer-segments';
export type { SegmentInfo, SegmentTrack, SegmentHandlerRenderer } from './renderer-segments';

export { RendererTrackHandler } from './renderer-tracks';
export type { Track, TrackHandlerRenderer } from './renderer-tracks';

export { Renderer } from './renderer';
