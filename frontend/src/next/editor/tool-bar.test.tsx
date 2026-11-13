// @vitest-environment happy-dom
import { render } from 'preact';
import { act } from 'preact/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { onEditorScreenKey } from './screen';
import { activeToolId } from './state';
import { EditorToolBar, type ToolBarActions } from './tool-bar';

const buttonNamed = (root: HTMLElement, text: string): HTMLButtonElement => {
  const found = [...root.querySelectorAll('button')].find((b) => b.textContent?.trim().endsWith(text));
  if (!found) throw new Error(`no button "${text}"`);
  return found;
};

describe('Editor tool bar', () => {
  let root: HTMLElement;
  let actions: ToolBarActions;

  const show = (toolId: string) => act(() => {
    activeToolId.value = toolId;
  });

  beforeEach(() => {
    root = document.createElement('div');
    document.body.appendChild(root);
    actions = { pick: vi.fn(), showBlockList: vi.fn() };
    activeToolId.value = 'inspect';
    act(() => render(<EditorToolBar actions={actions} />, root));
  });

  afterEach(() => {
    act(() => render(null, root));
    root.remove();
  });

  it('shows the six tools with their number keys, and marks the one in hand', () => {
    const tools = [...root.querySelectorAll('.ned-tool')];
    expect(tools.map((b) => b.textContent)).toEqual([
      '1Select', '2Track', '3Switch', '4Tunnel', '5Segment', '6Block',
    ]);
    expect(root.querySelector('.ned-tool.active')!.textContent).toBe('1Select');
  });

  it('picks a tool’s first variant, and leaves the tool in hand alone', () => {
    buttonNamed(root, 'Segment').click();
    expect(actions.pick).toHaveBeenCalledWith('segment');
    show('box-select');
    buttonNamed(root, 'Select').click();
    expect(actions.pick).toHaveBeenCalledTimes(1);
  });

  it('shows only the picked tool’s variants, with the active one checked', () => {
    show('extend-segment');
    const variants = [...root.querySelectorAll('.ned-variants [role="radio"]')];
    expect(variants.map((b) => b.textContent?.trim())).toEqual(['New', 'Extend', 'Remove']);
    expect(variants.map((b) => b.getAttribute('aria-checked'))).toEqual(['false', 'true', 'false']);
    (variants[2] as HTMLButtonElement).click();
    expect(actions.pick).toHaveBeenCalledWith('remove-segment-tile');
  });

  it('gives a one-action tool a plain hint instead of variant buttons', () => {
    show('tunnel');
    expect(root.querySelector('.ned-variants')).toBeNull();
    expect(root.querySelector('.ned-hint')!.textContent).toContain('join them as a tunnel');
  });

  it('offers the block list next to Assign', () => {
    show('block-assign');
    buttonNamed(root, 'Block list').click();
    expect(actions.showBlockList).toHaveBeenCalled();
  });

  it('shows no options row for a tool this screen does not carry', () => {
    show('path');
    expect(root.querySelector('.ned-tool.active')).toBeNull();
    expect(root.querySelector('.ned-options')).toBeNull();
  });
});

describe('Editor screen number keys', () => {
  const press = (key: string, init: KeyboardEventInit = {}) => ({
    e: new KeyboardEvent('keydown', { key, cancelable: true, ...init }),
  });

  it('picks the bar tool', () => {
    activeToolId.value = 'inspect';
    const actions = { pick: vi.fn(), showBlockList: vi.fn() };
    const { e } = press('5');
    onEditorScreenKey(e, actions);
    expect(actions.pick).toHaveBeenCalledWith('segment');
    expect(e.defaultPrevented).toBe(true);
  });

  it('leaves keys typed into a field, and modified keys, alone', () => {
    const actions = { pick: vi.fn(), showBlockList: vi.fn() };
    const input = document.createElement('input');
    document.body.append(input);
    input.focus();
    onEditorScreenKey(press('2').e, actions);
    input.remove();
    onEditorScreenKey(press('2', { metaKey: true }).e, actions);
    onEditorScreenKey(press('a').e, actions);
    expect(actions.pick).not.toHaveBeenCalled();
  });
});
