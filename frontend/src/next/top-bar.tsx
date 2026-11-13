/**
 * The top bar.
 *
 * Left: the app name, the Layout menu and the five screen tabs. Right: the
 * status control (environment, session state, and the one next-step button),
 * Emergency stop, and the View and Settings menus.
 *
 * Its styles are the `app-header` rules in static/css/layout/shell.css and
 * static/css/layout/next-top-bar.css. It runs no logic of its own: every
 * control calls a `TopBarActions` method, which App implements.
 */
import { useState } from 'preact/hooks';

import { canvasLayers, LAYERS, type LayerDef } from '../lib/canvas-layers';
import { gridPreference } from '../lib/grid-preference';
import { environmentLabel, sessionHint, sessionLabel } from '../lib/session-controls';
import { themeManager, type ThemeChannel } from '../lib/theme';
import { MenuButton, type MenuSection } from './menu';
import { NEXT_STEP_BUTTON, nextStep, type NextStep } from './next-step';
import { operationEnvironment, screen, sessionState, type Screen } from './state';

export interface TopBarActions {
  newLayout(): void | Promise<void>;
  openLayout(): void | Promise<void>;
  saveLayout(): void | Promise<void>;
  showScreen(screen: Screen): void;
  openHardwareSetup(): void;
  activate(): Promise<void>;
  stopAll(): Promise<void>;
  openSettings(): void;
  restartServer(): Promise<void>;
}

const SCREENS: { id: Screen; label: string }[] = [
  { id: 'editor', label: 'Editor' },
  { id: 'hardware', label: 'Hardware Setup' },
  { id: 'operate', label: 'Operate' },
  { id: 'logs', label: 'Logs' },
];

export function TopBar({ actions }: { actions: TopBarActions }) {
  return (
    <header class="app-header next-top-bar">
      <div class="header-zone">
        <div class="logo">
          <span class="logo-mark" aria-hidden="true" />
          <h1>RAIL</h1>
        </div>
        <MenuButton
          label="Layout"
          icon="ti ti-file"
          title="New, open or save a layout"
          sections={() => [
            {
              items: [
                { label: 'New', icon: 'ti ti-file-plus', onSelect: actions.newLayout },
                { label: 'Open…', icon: 'ti ti-folder-open', onSelect: actions.openLayout },
                { label: 'Save…', icon: 'ti ti-device-floppy', onSelect: actions.saveLayout },
              ],
            },
          ]}
        />
        <ScreenTabs actions={actions} />
      </div>
      <div class="header-zone ntb-right">
        <StatusControl actions={actions} />
        <div>
          <button
            type="button"
            class="btn ntb-estop"
            title="Stop every train at once"
            onClick={() => void actions.stopAll()}
          >
            <i class="ti ti-hand-stop" aria-hidden="true" /> Stop all
          </button>
        </div>
        <div>
          <MenuButton label="View" icon="ti ti-palette" title="Themes and canvas layers" align="right" sections={viewSections} />
          <MenuButton
            icon="ti ti-settings"
            title="Settings"
            align="right"
            sections={() => [
              { items: [{ label: 'Settings…', icon: 'ti ti-settings', onSelect: actions.openSettings }] },
              { items: [{ label: 'Restart server', icon: 'ti ti-refresh', emphasis: 'danger', onSelect: actions.restartServer }] },
            ]}
          />
        </div>
      </div>
    </header>
  );
}

function ScreenTabs({ actions }: { actions: TopBarActions }) {
  const current = screen.value;
  return (
    <div class="mode-switch" role="tablist" aria-label="Screen">
      {SCREENS.map((s) => (
        <button
          key={s.id}
          type="button"
          role="tab"
          aria-selected={current === s.id}
          class={current === s.id ? 'mode-btn active' : 'mode-btn'}
          onClick={() => actions.showScreen(s.id)}
        >
          {s.label}
        </button>
      ))}
    </div>
  );
}

function StatusControl({ actions }: { actions: TopBarActions }) {
  const state = sessionState.value;
  const env = operationEnvironment.value;
  const step = nextStep(state);
  const [busy, setBusy] = useState(false);

  const envName = environmentLabel(env);
  const label = sessionLabel(state);
  const tooltip = [envName ? `${envName}: ${label}.` : `${label}.`, sessionHint(state), 'Click to open Hardware Setup.']
    .filter(Boolean)
    .join(' ');

  const stepAction: Record<NextStep, () => Promise<void>> = {
    activate: actions.activate,
  };
  const runStep = async (s: NextStep) => {
    setBusy(true);
    try {
      await stepAction[s]();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div class="ntb-status">
      <button
        type="button"
        class="session-chip ntb-chip"
        data-state={state?.value ?? ''}
        data-halt={state?.halt_reason ?? ''}
        title={tooltip}
        onClick={actions.openHardwareSetup}
      >
        <span class="session-chip-env">{envName}</span>
        <span class="session-chip-state">{label}</span>
      </button>
      {step && (
        <button
          type="button"
          class="btn btn-primary"
          title={NEXT_STEP_BUTTON[step].title}
          disabled={busy}
          onClick={() => void runStep(step)}
        >
          <i class={NEXT_STEP_BUTTON[step].icon} aria-hidden="true" /> {NEXT_STEP_BUTTON[step].label}
        </button>
      )}
    </div>
  );
}

function themeSection(heading: string, channel: ThemeChannel): MenuSection {
  const current = channel.current();
  return {
    heading,
    items: channel.themes().map((t) => ({
      label: t.label,
      kind: 'choice' as const,
      checked: t.name === current,
      onSelect: () => channel.set(t.name),
    })),
  };
}

/** The View menu's layer groups, in the order LAYERS lists them. */
const LAYER_GROUPS: LayerDef['group'][] = [...new Set(LAYERS.map((layer) => layer.group))];

function viewSections(): MenuSection[] {
  return [
    themeSection('Editor theme', themeManager.chrome),
    themeSection('Layout theme', themeManager.canvas),
    {
      items: [
        {
          label: 'Show grid',
          kind: 'toggle',
          checked: gridPreference.visible(),
          onSelect: () => gridPreference.toggle(),
        },
      ],
    },
    ...LAYER_GROUPS.map((group) => ({
      heading: `Layers · ${group}`,
      items: LAYERS.filter((layer) => layer.group === group).map((layer) => ({
        label: layer.label,
        kind: 'toggle' as const,
        checked: canvasLayers.isOn(layer.id),
        onSelect: () => canvasLayers.toggle(layer.id),
      })),
    })),
  ];
}
