// Entry bundled by Vite into static/dist/main.js and loaded by
// static/index.html alongside the (shrinking) set of legacy classic scripts.
//
// As modules migrate (longterm_todo.md item #8, Phase 2+), they are imported
// here for their browser-global bridge side effects, so the not-yet-migrated
// classic scripts keep finding them on `window`. Module scripts are deferred,
// which runs this before DOMContentLoaded — i.e. before app.js bootstraps
// `new App()` — so the bridges are in place in time.
import './lib/camera';
