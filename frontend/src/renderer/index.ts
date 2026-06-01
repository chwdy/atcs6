// renderer/ — renderer-cluster modules migrated off the classic scripts
// (longterm_todo.md item #7).
//
// Each module attaches its browser-global bridge on import, so the
// not-yet-migrated classic renderer scripts keep finding it on `window`.
// main.ts imports this barrel for those side effects. The re-exports let
// future migrated modules import these as real ES symbols instead of via
// `window`.
import './render-snapshot';
import './renderer-tunnels';
import './renderer-debug';
import './renderer-signals';
import './renderer-stations';
import './renderer-routes';
import './renderer-blocks';
import './renderer-platforms';
import './renderer-tracks';
import './renderer-trains';
import './renderer';

export { buildRenderSnapshot } from './render-snapshot';
export type {
  RenderSnapshot,
  RenderSnapshotTrain,
  RenderSnapshotSpan,
} from './render-snapshot';

export { RendererTunnelHandler } from './renderer-tunnels';
export type {
  TunnelEndpoint,
  TunnelPair,
  TunnelHandlerRenderer,
} from './renderer-tunnels';

export { RendererDebugHandler } from './renderer-debug';
export type { DebugTrack, DebugHandlerRenderer } from './renderer-debug';

export { RendererSignalHandler } from './renderer-signals';
export type { Signal, SignalTrack, SignalHandlerRenderer } from './renderer-signals';

export { RendererStationHandler } from './renderer-stations';
export type { Station, StationHandlerRenderer } from './renderer-stations';

export { RendererRouteHandler } from './renderer-routes';
export type { Route, RouteHandlerRenderer } from './renderer-routes';

export { RendererBlockHandler } from './renderer-blocks';
export type { BlockInfo, BlockTrack, BlockHandlerRenderer } from './renderer-blocks';

export { RendererPlatformHandler } from './renderer-platforms';
export type { Platform, PlatformHandlerRenderer } from './renderer-platforms';

export { RendererTrackHandler } from './renderer-tracks';
export type { Track, TrackHandlerRenderer } from './renderer-tracks';

export { RendererTrainHandler } from './renderer-trains';
export type { Train, TrainSnap, TrainHandlerRenderer } from './renderer-trains';

export { Renderer } from './renderer';
