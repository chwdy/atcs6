/// <reference types="vitest" />
import { resolve } from 'node:path';
import { defineConfig } from 'vite';

// Phase 0 of the frontend build pipeline (see longterm_todo.md item #8).
//
// Vite builds the new TypeScript entry (frontend/src/main.ts) into static/dist/,
// which Flask already serves through its catch-all static route
// (routes/static_files.py). The legacy classic scripts under static/js/*.js are
// untouched — static/index.html still loads them directly. As modules are
// migrated in later phases they move under frontend/src/ and drop out of the
// classic-script list in index.html.
export default defineConfig({
  // The dev server and build are rooted at frontend/, which holds the Vite dev
  // shell (index.html) and the TypeScript sources.
  root: 'frontend',
  build: {
    // Emit into the directory Flask serves. emptyOutDir is required because the
    // outDir sits outside the Vite root.
    outDir: '../static/dist',
    emptyOutDir: true,
    rollupOptions: {
      // Explicit script entry (not an HTML entry): Flask owns the production
      // page, so we only need the bundled module. Stable filename keeps the
      // index.html reference simple until the stub-page phase introduces
      // content-hashed names.
      // Absolute path: Rollup resolves `input` relative to the process CWD
      // (the repo root, where npm runs the script), not the Vite `root`.
      input: resolve(process.cwd(), 'frontend/src/main.ts'),
      output: { entryFileNames: 'main.js' },
    },
  },
  server: {
    // `npm run dev` runs Vite's HMR server for module work in later phases and
    // proxies API calls through to the running Flask app. Match the port to how
    // you launch server.py (CLAUDE.md uses --port 5007 in its examples).
    proxy: { '/api': 'http://127.0.0.1:5000' },
  },
  test: {
    // Vitest picks up co-located unit tests next to the modules they cover.
    include: ['src/**/*.test.ts'],
  },
});
