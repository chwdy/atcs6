// Entry bundled by Vite into static/dist/main.js and loaded by
// static/index.html alongside the (shrinking) set of legacy classic scripts.
//
// As modules migrate (longterm_todo.md item #8, Phase 2+), they are imported
// here for their browser-global bridge side effects, so the not-yet-migrated
// classic scripts keep finding them on `window`. Module scripts are deferred,
// which runs this before DOMContentLoaded — i.e. before app.js bootstraps
// `new App()` — so the bridges are in place in time.
// Foundation cluster (item #7, Wave 1) — imported first so their global
// bridges are installed before the renderer/panel modules (and, post-parse,
// the classic editor scripts) read them.
import './lib/constants';
import './lib/api';
import './lib/state-manager';
import './lib/helpers';
import './lib/pathfinders';
import './lib/camera';
import './renderer';
import './panels/train-monitor';
import './panels/inspector';
// Editor handler cluster (item #7, Wave 2) — bridged onto window.<Handler> so
// the still-classic editor.js can `new` each one until editor.js migrates.
import './editor/editor-switches';
import './editor/editor-signals';
import './editor/editor-multi-select';
import './editor/editor-stations';
import './editor/editor-route-runs';
import './editor/editor-blocks';
import './editor/editor-paths';
import './editor/editor-ui';
import './editor/editor-trains';
import './editor/editor-routes';
