// Entry bundled by Vite into static/dist/main.js and loaded by
// static/index.html alongside the legacy classic scripts.
//
// Phase 0 (longterm_todo.md item #8): this module is intentionally INERT. It
// proves the TypeScript -> bundle -> Flask-served pipeline end to end without
// touching any legacy global. Later phases replace classic scripts with real
// modules that own actual behavior.
import { spineGreeting } from './lib/spine';

declare global {
  interface Window {
    /** Inert Phase-0 marker; lets the spine be confirmed from the console. */
    __viteSpine?: string;
  }
}

const message = spineGreeting('atcs6');
console.info(`[vite-spine] ${message}`);
window.__viteSpine = message;
