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
      // Explicit script entries (not HTML entries): Flask owns the production
      // pages, so we only need the bundled modules. Stable filenames keep the
      // index.html / debug.html references simple until the stub-page phase
      // introduces content-hashed names.
      //   main  → static/dist/main.js  (loaded by static/index.html)
      //   debug → static/dist/debug.js (loaded by static/debug.html, the
      //           /debug object-browser page served by debug_inspector.py)
      // Absolute paths: Rollup resolves `input` relative to the process CWD
      // (the repo root, where npm runs the script), not the Vite `root`.
      input: {
        main: resolve(process.cwd(), 'frontend/src/main.ts'),
        debug: resolve(process.cwd(), 'frontend/src/debug/debug-inspector.ts'),
      },
      output: { entryFileNames: '[name].js' },
    },
  },
  // The ?ui=next layout (docs/ui-layout-rewrite-plan.md) writes its panels as
  // Preact components in .tsx files; compile their JSX against Preact.
  esbuild: { jsx: 'automatic', jsxImportSource: 'preact' },
  server: {
    // `npm run dev` runs Vite's HMR server for module work in later phases and
    // proxies API calls through to the running Flask app. Set ATCS6_API_PORT to
    // match how you launch server.py (CLAUDE.md uses --port 5007 in its
    // examples); it defaults to the project's standard development port 5007.
    proxy: { '/api': `http://127.0.0.1:${process.env.ATCS6_API_PORT ?? '5007'}` },
  },
  test: {
    // Vitest picks up co-located unit tests next to the modules they cover.
    // Component tests (.test.tsx) render into happy-dom; each one opts in with
    // a `// @vitest-environment happy-dom` comment, so the rest stay in node.
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
  },
});
