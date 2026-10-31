/**
 * What the Operate screen is showing (docs/ui-layout-rewrite-plan.md §5),
 * and the App methods it calls.
 */
import { signal } from '@preact/signals';

import type { NextHost } from '../mount';

/**
 * What the right panel shows. A train, block or switch is drawn by this
 * screen's own panels. `panel` is one of today's panels (a route, a path, a
 * route run, a station, Add Train, …) shown as it is, inside the right panel
 * (see panel-host.ts).
 */
export type OperateSelection =
  | { kind: 'train'; id: string }
  | { kind: 'block'; id: string }
  | { kind: 'switch'; id: string }
  | { kind: 'panel'; title: string }
  | null;

export const selection = signal<OperateSelection>(null);

/** Whether the left list is open; folding it gives the map room. */
export const listOpen = signal(true);

/** Whether the train cards row is folded to one line per card; folding it
 *  gives the map room. */
export const cardsFolded = signal(false);

/** The App methods and editor handlers the Operate screen uses. App provides
 *  all of them; they are the same ones today's panels call. */
export interface OperateHost extends NextHost {
  editor: NextHost['editor'] & {
    nextPanelHost: import('../../editor/editor').NextPanelHost | null;
    editorMode: string;
    _currentEditorType: string | null;
    _currentEditorId: string | null;
    renderer: any;
    uiHandler: { cleanupPanelSubscriptions(): void };
    showAddTrainDialog(): void;
    showPathEditor(pathId: string): void | Promise<void>;
    enterMode(mode: any): Promise<void>;
    exitMode(): Promise<void>;
    activeMode: any;
    pathHandler?: any;
    routeHandler?: any;
    routeRunHandler?: any;
    stationGroupHandler?: any;
  };
  selectTrain(trainId: string, options?: { showMonitor?: boolean }): void;
  deselectTrain(): void;
  holdAll(): Promise<void>;
  refreshNetwork(): Promise<void>;
  selectedTrainId: string | null;
  trainMonitor: any;
  operationEnvironment: string | null;
}
