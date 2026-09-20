/**
 * The ATO Go / Stop pair tracks the controller's `engaged` bit — the ATO
 * go/stop bit — on every state update, not only when the panel is drawn.
 *
 * The panel used to set the pair once, at draw time, so after Go the reply
 * carried `engaged: true` and the buttons stayed as they were: Go enabled and
 * Stop disabled for as long as the panel stayed open.
 *
 * There is no DOM in this project's vitest setup, so `document` is stubbed with
 * just the two buttons the rule touches.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TrainEditorHandler } from './editor-trains';
import { stateManager } from '../lib/state-manager';

type FakeButton = { disabled: boolean; classList: { toggle: ReturnType<typeof vi.fn> } };

function fakeButton(disabled: boolean): FakeButton {
  return { disabled, classList: { toggle: vi.fn() } };
}

describe('ATO Go / Stop buttons follow engaged', () => {
  let buttons: Record<string, FakeButton>;
  let handler: any;

  beforeEach(() => {
    // As drawn for a powered, not-yet-engaged ATO train.
    buttons = {
      'train-ato-go-btn': fakeButton(false),
      'train-ato-stop-btn': fakeButton(true),
    };
    vi.stubGlobal('document', { getElementById: (id: string) => buttons[id] ?? null });
    handler = new TrainEditorHandler({ cachedNetwork: { tracks: [] } } as any);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('enables Stop and disables Go once the controller reports engaged', () => {
    stateManager.updateEntity('controllers', 'T_ATO', { power_on: true, engaged: true });
    handler.updateButtonStates({ id: 'T_ATO' });
    expect(buttons['train-ato-go-btn'].disabled).toBe(true);
    expect(buttons['train-ato-stop-btn'].disabled).toBe(false);
  });

  it('enables Go and disables Stop again after Stop', () => {
    stateManager.updateEntity('controllers', 'T_ATO2', { power_on: true, engaged: true });
    handler.updateButtonStates({ id: 'T_ATO2' });
    stateManager.updateEntity('controllers', 'T_ATO2', { power_on: true, engaged: false });
    handler.updateButtonStates({ id: 'T_ATO2' });
    expect(buttons['train-ato-go-btn'].disabled).toBe(false);
    expect(buttons['train-ato-stop-btn'].disabled).toBe(true);
  });

  it('keeps Go disabled while the train is powered off', () => {
    stateManager.updateEntity('controllers', 'T_OFF', { power_on: false, engaged: false });
    handler.updateButtonStates({ id: 'T_OFF' });
    expect(buttons['train-ato-go-btn'].disabled).toBe(true);
    expect(buttons['train-ato-stop-btn'].disabled).toBe(true);
  });
});
