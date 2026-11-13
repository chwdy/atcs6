// @vitest-environment happy-dom
import { render } from 'preact';
import { act } from 'preact/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { loadWindowState, memoryWindowPersistenceEnv } from '../../lib/window-persistence';
import { screen } from '../state';
import { EditorPanel, PANEL_WINDOW_ID, type PanelActions } from './right-panel';
import { panelFloating, panelOpen } from './state';

const flush = () => new Promise((r) => setTimeout(r, 0));

describe('Editor right panel', () => {
  let root: HTMLElement;
  let inspector: HTMLElement;
  let actions: PanelActions;
  const persistence = memoryWindowPersistenceEnv();

  beforeEach(() => {
    inspector = document.createElement('aside');
    inspector.id = 'secondary-sidebar';
    inspector.innerHTML = `
      <div class="secondary-sidebar-header">
        <button id="secondary-nav-parent" hidden></button>
        <h3 id="secondary-sidebar-title">SEG_1</h3>
      </div>
      <div class="secondary-sidebar-content" id="secondary-sidebar-content"><p>segment panel</p></div>`;
    root = document.createElement('div');
    document.body.append(inspector, root);
    actions = {
      close: vi.fn(),
      showSwitches: vi.fn(),
      showBlocks: vi.fn(),
      openTileKey: () => null,
    };
    screen.value = 'editor';
    panelOpen.value = false;
    act(() => render(<EditorPanel actions={actions} persistence={persistence} />, root));
  });

  afterEach(() => {
    act(() => render(null, root));
    document.body.replaceChildren();
  });

  it('says how to select while nothing is selected', () => {
    expect(root.querySelector('.ned-panel-empty')!.textContent).toContain('Click a piece of track');
    expect(root.querySelector<HTMLElement>('.ned-panel-legacy')!.hidden).toBe(true);
  });

  it('shows today’s panel while one is open on the Editor screen', async () => {
    await act(async () => {
      inspector.classList.add('visible');
      await flush();
    });
    expect(root.querySelector('.ned-panel-empty')).toBeNull();
    expect(root.querySelector('.ned-panel-legacy #secondary-sidebar-content')!.textContent).toBe('segment panel');
    expect(root.querySelector('.ned-panel-title #secondary-sidebar-title')!.textContent).toBe('SEG_1');
    root.querySelector<HTMLButtonElement>('button[aria-label="Close"]')!.click();
    expect(actions.close).toHaveBeenCalled();
  });

  it('gives today’s panel back when another screen shows', async () => {
    await act(async () => {
      screen.value = 'operate';
      await flush();
    });
    expect(document.getElementById('secondary-sidebar-content')!.parentElement).toBe(inspector);
    await act(async () => {
      screen.value = 'editor';
      await flush();
    });
    expect(document.getElementById('secondary-sidebar-content')!.parentElement!.className).toBe('ned-panel-legacy');
  });

  it('opens the lists from the header’s list button', async () => {
    await act(async () => {
      root.querySelector<HTMLButtonElement>('button[aria-label^="Lists"]')!.click();
    });
    const items = [...root.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')];
    expect(items.map((b) => b.textContent?.trim())).toEqual(['Switches', 'Blocks']);
    items[1].click();
    expect(actions.showBlocks).toHaveBeenCalled();
  });

  it('pops out into a floating window and docks back, saving where it was', async () => {
    const panel = root.querySelector<HTMLElement>('.ned-panel')!;
    await act(async () => {
      root.querySelector<HTMLButtonElement>('button[aria-label="Pop out into a window"]')!.click();
    });
    expect(panel.classList.contains('floating-window')).toBe(true);
    expect(panelFloating.value).toBe(true);
    expect(loadWindowState(persistence, PANEL_WINDOW_ID)?.floating).toBe(true);
    await act(async () => {
      root.querySelector<HTMLButtonElement>('button[aria-label="Dock the panel"]')!.click();
    });
    expect(panel.classList.contains('floating-window')).toBe(false);
    expect(loadWindowState(persistence, PANEL_WINDOW_ID)?.floating).toBe(false);
  });
});
