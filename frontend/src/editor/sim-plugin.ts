// sim-plugin.ts — registers the simulation editor handlers onto the core editor.
//
// The core Editor is a layout-only editor: it instantiates only the layout
// handlers (block, signal, platform, station, switch, tunnel, ui, multiSelect).
// This plugin adds the simulation handlers (trains, paths, routes, route-runs,
// speed profiles). The host (app.ts) registers it via editor.registerPlugin();
// a layout-only host simply does not.

import type { Editor, EditorPlugin } from './editor';
import type { ToolContribution } from './tool-registry';
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

  /**
   * The simulation tools: paths, trains, and routes. All are available in both
   * edit and operation (sim) mode — only the physical layout is frozen during
   * operation, so dispatching paths / trains / routes stays available.
   */
  tools(editor: Editor): ToolContribution[] {
    return [
      {
        id: 'path', label: 'New', icon: 'ti ti-plus', title: 'Create Path Between Blocks',
        group: 'Path', availableIn: ['edit', 'sim'],
        kind: { type: 'mode', factory: () => editor.pathHandler?.buildRouteToolMode() },
      },
      {
        id: 'extend-path', label: 'Extend', icon: 'ti ti-arrows-horizontal',
        title: 'Extend Existing Path',
        group: 'Path', availableIn: ['edit', 'sim'],
        kind: { type: 'mode', factory: () => editor.pathHandler?.buildExtendRouteMode() },
      },
      {
        id: 'cancel-path', label: 'Cancel', icon: 'ti ti-route-x', title: 'Cancel Path',
        group: 'Path', availableIn: ['edit', 'sim'],
        kind: { type: 'mode', factory: () => editor.pathHandler?.buildCancelRouteMode() },
      },
      {
        id: 'paths-panel', label: 'Paths', icon: 'ti ti-route', title: 'View Active Paths',
        group: 'Path', availableIn: ['edit', 'sim'],
        kind: { type: 'action', run: () => editor.showPathsPanel() },
      },

      {
        id: 'add-train', label: 'New', icon: 'ti ti-plus', title: 'Add New Train',
        group: 'Train', availableIn: ['edit', 'sim'],
        kind: { type: 'action', run: () => editor.showAddTrainDialog() },
      },
      {
        id: 'train-panel', label: 'Trains', icon: 'ti ti-train', title: 'Train Management',
        group: 'Train', availableIn: ['edit', 'sim'],
        kind: { type: 'action', run: () => editor.showTrainsPanel() },
      },
      {
        id: 'speed-profiles-panel', label: 'Profiles', icon: 'ti ti-chart-line',
        title: 'Speed Profiles — voltage/speed calibration curves',
        group: 'Train', availableIn: ['edit', 'sim'],
        kind: { type: 'action', run: () => editor.showSpeedProfilesPanel() },
      },

      {
        id: 'new-route', label: 'New', icon: 'ti ti-plus', title: 'Create New Route',
        group: 'Route', availableIn: ['edit', 'sim'],
        kind: { type: 'action', run: () => editor.routeHandler?.createNewRoute() },
      },
      {
        id: 'routes-panel', label: 'Routes', icon: 'ti ti-map-2', title: 'Train Route Editor',
        group: 'Route', availableIn: ['edit', 'sim'],
        kind: { type: 'action', run: () => editor.showRoutesPanel() },
      },
      {
        id: 'route-runs-panel', label: 'Runs', icon: 'ti ti-list-details', title: 'View Route Runs',
        group: 'Route', availableIn: ['edit', 'sim'],
        kind: { type: 'action', run: () => editor.routeRunHandler?.showRouteRunsPanel() },
      },
    ];
  }
}
