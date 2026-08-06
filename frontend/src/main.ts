// Entry bundled by Vite into static/dist/main.js — the single script loaded by
// static/index.html (longterm_todo.md item #7 is complete: every classic script
// has migrated into this bundle).
//
// These imports assemble the bundle. app.ts (last) transitively imports most of
// them for their exports; they are listed explicitly here as the module
// manifest and to guarantee inclusion. panels/diagnostics is imported purely for
// its side effect — it self-bootstraps the diagnostics panel onto window — and app
// registers the DOMContentLoaded handler that constructs `new App()`.
import './lib/constants';
import './lib/theme';
import './lib/api';
import './lib/state-manager';
import './lib/helpers';
import './lib/pathfinders';
import './lib/camera';
import './renderer';
import './panels/train-monitor';
import './panels/diagnostics';
import './panels/occupancy-monitor';
import './panels/occupancy-bench';
import './panels/virtual-bench';
// The Editor facade (item #7, Wave 3) imports the 11 handler classes directly.
import './editor/editor';
// The application bootstrap (item #7, Wave 4 — the last classic script). Imported
// last; it registers a DOMContentLoaded handler that constructs `new App()` once
// the DOM is ready, by which point every module above has evaluated.
import './app';
