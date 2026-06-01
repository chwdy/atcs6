// Ambient declarations for the still-classic globals that migrated modules
// consume until their defining files (constants.js / helpers.js) also migrate
// off the classic-script soup (longterm_todo.md item #7).
//
// Each symbol is provided at runtime by a classic <script> in static/index.html
// and resolved off the global object; these declarations exist only to satisfy
// `tsc`. As each defining file migrates to a real module, delete the matching
// line here and import the symbol instead.

declare function trackBitsToEdges(trackBits: number): Set<string>;
declare function getActiveSegments(trackBits: number): Array<[string, string]>;
declare function isCrossing(trackBits: number): boolean;

declare const BIT_TO_EDGES: Record<number, [string, string]>;
declare function getBlockStateColor(state: string, alpha?: number): string;
declare function getLabelSpacingMultiplier(): number;

declare const TRACK_WIDTH: number;
declare const DEBUG_BLOCK_WIDTH: number;
declare const TRACK_BITS: { NS: number; EW: number; NE: number; NW: number; SE: number; SW: number };

// Optional: classic scripts may or may not define it; renderer-trains guards
// its use with `typeof DIRECTION_PREFERRED_QUADRANTS !== 'undefined'`.
declare const DIRECTION_PREFERRED_QUADRANTS: Record<string, string[]> | undefined;

// The classic StateManager instance (state-manager.js) exposed on window. The
// renderer-trains snapshot fallback reads a live controller off it; the
// train-monitor panel additionally reads the live runtime fields
// (current_speed / state / speed_limit_reason) the TrainController exposes per
// design_new.md "Data Flow To UI".
interface Window {
  stateManager?: {
    getController(trainId: string): {
      cab?: string | null;
      facing_direction?: string | null;
      current_speed?: number | null;
      state?: string | null;
      speed_limit_reason?: string | null;
      span?: {
        occupied_tiles: Array<[number, number]>;
        leading_end: string;
        end_a: { tile: [number, number]; distance_cm: number; entry_edge: string };
        end_b: { tile: [number, number]; distance_cm: number; entry_edge: string };
      } | null;
    } | null | undefined;
    // The editor handlers (Wave 2) also call subscribe / updateFromNetwork /
    // etc.; keep this permissive until the panels and handlers import the real
    // StateManager type after Wave 4.
    [key: string]: any;
  };

  // Classic globals the migrated panels still reach for until their defining
  // files migrate: the toast helper (app.js), the App instance (app.js), and
  // the API client (api.js). Loosely typed — only the members the panels touch.
  showToast?: (message: string, kind?: string, durationMs?: number) => void;
  dismissToast?: (toastId: string) => void;
  app?: {
    editor?: { showFullJSONViewer(data: unknown, title: string): void; [key: string]: any };
    // The editor handlers (Wave 2) call app methods like refreshNetwork();
    // keep this permissive until app.js migrates (Wave 4).
    [key: string]: any;
  };
  api?: {
    getScale(): Promise<{ scale?: number }>;
    getNetwork(): Promise<{ trains?: Array<{ id?: string; train_id?: string }> } | null>;
    getInspectorEntityTrain(trainId: string): Promise<unknown | null>;
    [key: string]: unknown;
  };

  // The singleton inspector panel (inspector.ts) self-bootstraps onto window so
  // the classic btn-inspector-toggle handler and app.js can reach it.
  inspectorPanel?: { init(): void; toggle(): void; show(): void; hide(): void };
}
