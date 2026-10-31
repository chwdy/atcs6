// Ambient types for the runtime singletons still read off `window`
// (longterm_todo.md item #7). These are not module exports — they are live
// instances installed on the global object at startup, so consumers reach them
// through `window` rather than an import:
//   - window.app — the App instance (app.ts sets it on DOMContentLoaded); the
//     editor handlers and panels call app methods (refreshNetwork, selectTrain,
//     showFullJSONViewer, …).
//   - window.virtualBench — the floating Virtual Bench, which self-bootstraps
//     (panels/virtual-bench) so Hardware Setup can close it.
// Loosely typed — only the members the consumers touch.
interface Window {
  app?: {
    editor?: { showFullJSONViewer(data: unknown, title: string): void; [key: string]: any };
    // The editor handlers and panels call app methods like refreshNetwork().
    [key: string]: any;
  };

  // The singleton floating Virtual Bench (virtual-bench.ts) — the Virtual
  // Hardware workbench.
  virtualBench?: { init(): void; toggle(): void; show(): void; hide(): void };
}
