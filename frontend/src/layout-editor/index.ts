// layout-editor/index.ts — the public API of the reusable layout-editor
// component.
//
// This barrel is the one import surface a separate host (e.g. a future
// model-railway layout-editor MVP in its own repo) consumes. It re-exports the
// renderer, the editor, the data contract, the payload types, and the extension
// points (renderer overlays + editor plugins). The simulation pieces
// (SimRendererOverlay, SimEditorPlugin) are exported too so the current app can
// compose them, but a layout-only host simply does not import them.
//
// Nothing in the app imports this yet — it is additive, the documented entry
// point for reuse. Physically relocating these modules into a standalone
// package directory is the remaining step (P5); this barrel pins the surface so
// that move is a mechanical follow-up rather than an API design exercise.

// --- Renderer (layout drawing) ---
export { Renderer } from '../renderer/renderer';
export { buildRenderSnapshot } from '../renderer/render-snapshot';

// --- Renderer overlay extension point + the sim overlay implementation ---
export type { RendererOverlay, OverlayPhase } from '../renderer/renderer-overlay';
export { SimRendererOverlay } from '../renderer/sim-overlay';

// --- Network payload types (layout half / sim half / combined) ---
export type {
  LayoutPayload,
  SimState,
  NetworkPayload,
  TunnelPayloadEntry,
  SwitchPayloadEntry,
} from '../renderer/payload-types';

// --- Editor (layout editing) + its extension points ---
export { Editor } from '../editor/editor';
export type { EditorPlugin, HostApp } from '../editor/editor';

// --- Sim editor plugin (registers the sim handlers onto the editor) ---
export { SimEditorPlugin } from '../editor/sim-plugin';

// --- Data contract: what a host's backend client must implement ---
export type { LayoutDataClient, SimClient, FullDataClient } from '../lib/layout-data-client';
export { API, api } from '../lib/api';
