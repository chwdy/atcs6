// sim-plugin.ts — registers the simulation editor handlers onto the core editor.
//
// The core Editor is a layout-only editor: it instantiates only the layout
// handlers (block, signal, platform, station, switch, tunnel, ui, multiSelect).
// This plugin adds the simulation handlers (trains, paths, routes, route-runs,
// speed profiles). The host (app.ts) registers it via editor.registerPlugin();
// a layout-only host simply does not.

import type { Editor, EditorPlugin } from './editor';
import { TrainEditorHandler } from './editor-trains';
import { PathEditorHandler } from './editor-paths';
import { RouteEditorHandler } from './editor-routes';
import { RouteRunEditorHandler } from './editor-route-runs';
import { SpeedProfileEditorHandler } from './editor-speed-profiles';

export class SimEditorPlugin implements EditorPlugin {
  register(editor: Editor): void {
    editor.trainHandler = new TrainEditorHandler(editor);
    editor.pathHandler = new PathEditorHandler(editor);
    editor.routeHandler = new RouteEditorHandler(editor);
    editor.routeRunHandler = new RouteRunEditorHandler(editor);
    editor.speedProfileHandler = new SpeedProfileEditorHandler(editor);
  }
}
