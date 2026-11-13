/**
 * Entry point of the page layout (docs/ui-layout-rewrite-plan.md): the top
 * bar and the five screens.
 *
 * It marks <html data-screen="editor|hardware|operate|locomotives|logs"> with the
 * picked screen, so each screen's stylesheet can show its own regions and
 * hide the others' while it is the one showing.
 */
import { effect } from '@preact/signals';
import { render } from 'preact';

import type { Editor } from '../editor/editor';
import { mountEditorScreen } from './editor/screen';
import { mountFirstRun, type FirstRunHost } from './first-run/mount';
import { mountHardwareSetup } from './hardware/screen';
import { showHardwareSetup } from './hardware/state';
import { mountLocomotivesScreen, type LocomotivesHost } from './locomotives';
import { mountLogsScreen } from './logs/logs-screen';
import { screen, type Screen } from './state';
import { TopBar, type TopBarActions } from './top-bar';

/** The App methods the layout calls: the session and layout actions. */
export interface NextHost extends LocomotivesHost, FirstRunHost {
  editor: Editor;
  newLayout(): Promise<void>;
  openLoadLayoutDialog(): Promise<void>;
  openSaveLayoutDialog(): Promise<void>;
  setEditorMode(mode: 'edit' | 'sim'): void;
  activateEnvironment(): Promise<void>;
  resetLayout(): Promise<void>;
  clearFatalConflict(): Promise<void>;
  emergencyStop(): Promise<void>;
  openSettings(): void;
  restartServer(): Promise<void>;
}

/**
 * Pick a screen. Editor and Operate are today's two editor modes, each with
 * its own regions (./editor, ./operate). Hardware Setup is its own screen
 * (./hardware). Locomotives (./locomotives) and Logs (./logs) are their own
 * screens, in Operate mode.
 */
export function showScreen(host: NextHost, next: Screen): void {
  if (next === 'hardware') {
    showHardwareSetup();
    return;
  }
  host.setEditorMode(next === 'editor' ? 'edit' : 'sim');
  screen.value = next;
}

export function topBarActions(host: NextHost): TopBarActions {
  return {
    newLayout: () => host.newLayout(),
    openLayout: () => host.openLoadLayoutDialog(),
    saveLayout: () => host.openSaveLayoutDialog(),
    showScreen: (next) => showScreen(host, next),
    // The session-state chip opens Hardware Setup at step 1, choose the environment.
    openHardwareSetup: () => showHardwareSetup(1),
    activate: () => host.activateEnvironment(),
    resetLayout: () => host.resetLayout(),
    clearConflict: () => host.clearFatalConflict(),
    emergencyStop: () => host.emergencyStop(),
    openSettings: () => host.openSettings(),
    restartServer: () => host.restartServer(),
  };
}

/** Mount the top bar and the screens. */
export function mountNextUI(host: NextHost): void {
  // <html data-screen> names the picked screen, so each screen's CSS shows
  // its regions only while that screen is the one showing.
  effect(() => {
    document.documentElement.dataset.screen = screen.value;
  });
  const container = document.querySelector('.app-container');
  const slot = document.createElement('div');
  slot.className = 'next-top-bar-slot';
  container?.prepend(slot);
  render(<TopBar actions={topBarActions(host)} />, slot);
  mountEditorScreen(host.editor);
  mountHardwareSetup({
    activate: () => host.activateEnvironment(),
    showScreen: (next) => showScreen(host, next),
  });
  mountLocomotivesScreen(host, () => showScreen(host, 'locomotives'));
  mountLogsScreen(host);
  mountFirstRun(host, (next) => showScreen(host, next));
}
