// Ambient types for the two runtime singletons still read off `window`
// (longterm_todo.md item #7). These are not module exports — they are live
// instances installed on the global object at startup, so consumers reach them
// through `window` rather than an import:
//   - window.app — the App instance (app.ts sets it on DOMContentLoaded); the
//     editor handlers and panels call app methods (refreshNetwork, selectTrain,
//     showFullJSONViewer, …).
//   - window.diagnosticsPanel — the diagnostics panel singleton, which
//     self-bootstraps (panels/diagnostics) so the toolbar toggle can reach it.
// Loosely typed — only the members the consumers touch.
interface Window {
  app?: {
    editor?: { showFullJSONViewer(data: unknown, title: string): void; [key: string]: any };
    // The editor handlers and panels call app methods like refreshNetwork().
    [key: string]: any;
  };

  // The singleton diagnostics panel (diagnostics.ts) self-bootstraps onto window
  // so the toolbar diagnostics toggle can reach it.
  diagnosticsPanel?: { init(): void; toggle(): void; show(): void; hide(): void };

  // The singleton raw-occupancy monitor (occupancy-monitor.ts), on window for
  // the same reason: its toolbar toggle is plain markup in index.html.
  occupancyMonitor?: { init(): void; toggle(): void; show(): void; hide(): void };
}
