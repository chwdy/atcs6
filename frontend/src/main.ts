// Entry bundled by Vite into static/dist/main.js — the single script loaded by
// static/index.html (longterm_todo.md item #7 is complete: every classic script
// has migrated into this bundle).
//
// Import order still matters during the bridge-decommission step: each module
// installs its browser-global bridge as a side effect on import, and the
// modules that have not yet switched to ES imports read those globals. The
// foundation cluster (Wave 1) is imported first so its bridges exist before the
// renderer/panel/editor modules evaluate.
import './lib/constants';
import './lib/api';
import './lib/state-manager';
import './lib/helpers';
import './lib/pathfinders';
import './lib/camera';
import './renderer';
import './panels/train-monitor';
import './panels/inspector';
// The Editor facade (item #7, Wave 3) imports the 11 handler classes directly.
import './editor/editor';
// The application bootstrap (item #7, Wave 4 — the last classic script). Imported
// last; it registers a DOMContentLoaded handler that constructs `new App()` once
// the DOM is ready, by which point every module above has evaluated.
import './app';
