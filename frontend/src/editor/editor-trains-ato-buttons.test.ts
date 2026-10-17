/**
 * The ATO Go / Stop pair tracks the controller's `engaged` bit — the ATO
 * go/stop bit — on every state update, not only when the panel is drawn. Go
 * also needs a route assigned to the train (design_new.md "ATO go"): with no
 * route it stays disabled and says why.
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

type FakeButton = { disabled: boolean; title: string; classList: { toggle: ReturnType<typeof vi.fn> } };

function fakeButton(disabled: boolean): FakeButton {
  return { disabled, title: '', classList: { toggle: vi.fn() } };
}

const ATO = { power_on: true, train_status: 'ato' };

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
    stateManager.updateEntity('assignedRoutes', 'T_ATO', 'R1');
    stateManager.updateEntity('controllers', 'T_ATO', { ...ATO, engaged: true });
    handler.updateButtonStates({ id: 'T_ATO' });
    expect(buttons['train-ato-go-btn'].disabled).toBe(true);
    expect(buttons['train-ato-stop-btn'].disabled).toBe(false);
  });

  it('enables Go and disables Stop again after Stop', () => {
    stateManager.updateEntity('assignedRoutes', 'T_ATO2', 'R1');
    stateManager.updateEntity('controllers', 'T_ATO2', { ...ATO, engaged: true });
    handler.updateButtonStates({ id: 'T_ATO2' });
    stateManager.updateEntity('controllers', 'T_ATO2', { ...ATO, engaged: false });
    handler.updateButtonStates({ id: 'T_ATO2' });
    expect(buttons['train-ato-go-btn'].disabled).toBe(false);
    expect(buttons['train-ato-go-btn'].title).toBe('');
    expect(buttons['train-ato-stop-btn'].disabled).toBe(true);
  });

  it('keeps Go disabled, with the reason, while no route is assigned', () => {
    stateManager.updateEntity('controllers', 'T_NOROUTE', { ...ATO, engaged: false });
    handler.updateButtonStates({ id: 'T_NOROUTE' });
    expect(buttons['train-ato-go-btn'].disabled).toBe(true);
    expect(buttons['train-ato-go-btn'].title).toBe('Assign a route first');

    stateManager.updateEntity('assignedRoutes', 'T_NOROUTE', 'R1');
    handler.updateButtonStates({ id: 'T_NOROUTE' });
    expect(buttons['train-ato-go-btn'].disabled).toBe(false);
  });

  it('keeps Go disabled while the train is powered off', () => {
    stateManager.updateEntity('controllers', 'T_OFF', { power_on: false, engaged: false });
    handler.updateButtonStates({ id: 'T_OFF' });
    expect(buttons['train-ato-go-btn'].disabled).toBe(true);
    expect(buttons['train-ato-stop-btn'].disabled).toBe(true);
  });
});
