/**
 * Entry point of the ?ui=next layout (docs/ui-layout-rewrite-plan.md).
 *
 * The new layout is built next to today's, in the same page. Opened with
 * `?ui=next`, it marks <html data-ui="next"> and <html data-screen="…">,
 * which hide each of today's regions that a new one has replaced
 * (static/css/layout/next-top-bar.css and each screen's stylesheet), and
 * mounts the new regions. Regions not rebuilt yet keep working as today,
 * so the app stays fully usable under the flag at every phase.
 */
import { effect } from '@preact/signals';
import { render } from 'preact';

import type { Editor } from '../editor/editor';
import { mountEditorScreen } from './editor/screen';
import { mountFirstRun, type FirstRunHost } from './first-run/mount';
import { mountHardwareSetup } from './hardware/screen';
import { showHardwareSetup } from './hardware/state';
import { mountLocomotivesScreen, type LocomotivesHost } from './locomotives';
import { mountLogsPanel } from './logs/logs-panel';
import { toggleLogs } from './logs/state';
import { screen, type Screen } from './state';
import { TopBar, type TopBarActions, type ToolWindow } from './top-bar';

/** Whether this page runs the new layout. */
export function isNextUI(search: string = window.location.search): boolean {
  return new URLSearchParams(search).get('ui') === 'next';
}

/** The App methods the new layout calls. Each is what one of today's header
 *  buttons runs, so both headers behave the same. */
export interface NextHost extends LocomotivesHost, FirstRunHost {
  editor: Editor;
  newLayout(): Promise<void>;
  openLoadLayoutDialog(): Promise<void>;
  openSaveLayoutDialog(): Promise<void>;
  setEditorMode(mode: 'edit' | 'sim'): void;
  openHardwareSetup(): void;
  activateEnvironment(): Promise<void>;
  togglePlayPause(): Promise<void>;
  setSimulationSpeed(speed: number): Promise<void>;
  resetLayout(): Promise<void>;
  clearFatalConflict(): Promise<void>;
  emergencyStop(): Promise<void>;
  openSettings(): void;
  restartServer(): Promise<void>;
}

/**
 * Pick a screen. Editor and Operate are today's two editor modes, each with
 * its own regions (./editor, ./operate). Hardware Setup is its own screen
 * (./hardware). Locomotives is its own screen (./locomotives), in Operate
 * mode.
 */
export function showScreen(host: NextHost, next: Screen): void {
  if (next === 'hardware') {
    showHardwareSetup();
    return;
  }
  host.setEditorMode(next === 'editor' ? 'edit' : 'sim');
  screen.value = next;
}

function toggleWindow(name: ToolWindow): void {
  const panel = (window as any)[name] as { toggle(): void | Promise<void> } | undefined;
  void panel?.toggle();
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
    togglePlayPause: () => host.togglePlayPause(),
    setSpeed: (speed) => host.setSimulationSpeed(speed),
    resetLayout: () => host.resetLayout(),
    clearConflict: () => host.clearFatalConflict(),
    emergencyStop: () => host.emergencyStop(),
    toggleWindow,
    toggleLogs,
    openSettings: () => host.openSettings(),
    restartServer: () => host.restartServer(),
  };
}

/** Mark the page as running the new layout and mount the regions built so far. */
export function mountNextUI(host: NextHost): void {
  document.documentElement.dataset.ui = 'next';
  // <html data-screen> names the picked screen, so each screen's CSS can hide
  // today's regions only while that screen is the one showing.
  effect(() => {
    document.documentElement.dataset.screen = screen.value;
  });
  const oldHeader = document.querySelector('.app-header');
  const slot = document.createElement('div');
  slot.className = 'next-top-bar-slot';
  oldHeader?.parentElement?.insertBefore(slot, oldHeader);
  render(<TopBar actions={topBarActions(host)} />, slot);
  mountEditorScreen(host.editor);
  mountHardwareSetup({
    activate: () => host.activateEnvironment(),
    showScreen: (next) => showScreen(host, next),
  });
  mountLocomotivesScreen(host, () => showScreen(host, 'locomotives'));
  mountLogsPanel();
  mountFirstRun(host, (next) => showScreen(host, next));
}
