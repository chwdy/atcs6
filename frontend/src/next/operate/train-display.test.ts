import { describe, expect, it } from 'vitest';

import type { TrainView } from './live';
import { atoStatus, cardControl, holdLine, trainStatus } from './train-display';

function view(controller: any, extra: Partial<TrainView> = {}): TrainView {
  return {
    id: 'T1',
    train: { id: 'T1' },
    controller,
    operatorStatus: null,
    assignedRouteId: null,
    routeRun: null,
    dispatcher: null,
    ...extra,
  };
}

describe('cardControl: the card main control follows train_status', () => {
  it('offers Go to a stopped ATO train, with why it is unavailable', () => {
    const v = view({ train_status: 'ato', power_on: true, engaged: false, is_stopped: true });
    expect(cardControl(v)).toEqual({ kind: 'go', disabledReason: 'Assign a route first' });
    expect(cardControl({ ...v, assignedRouteId: 'R1' })).toEqual({ kind: 'go', disabledReason: null });
  });

  it('offers Stop once ATO is engaged', () => {
    expect(cardControl(view({ train_status: 'ato', power_on: true, engaged: true }))).toEqual({ kind: 'stop' });
  });

  it('offers the handle, or the power percentage, to a manual train', () => {
    expect(cardControl(view({ train_status: 'manual', manual_mode_type: 'simulation' }))).toEqual({ kind: 'handle' });
    expect(cardControl(view({ train_status: 'manual', manual_mode_type: 'power_percent' }))).toEqual({
      kind: 'power-percent',
    });
  });

  it('offers Start route to a powered-off train whose route can start, otherwise Power on', () => {
    const off = view({ train_status: 'off', is_stopped: true });
    expect(cardControl(off)).toEqual({ kind: 'power-on' });
    expect(cardControl({ ...off, assignedRouteId: 'R1' })).toEqual({ kind: 'start-route' });
  });

  it('offers Release under the emergency brake and nothing while calibrating', () => {
    expect(cardControl(view({ train_status: 'eb' }))).toEqual({ kind: 'release' });
    expect(cardControl(view({ train_status: 'calibrating' }))).toEqual({ kind: 'none', text: 'Calibrating' });
  });

  it('offers nothing to an unidentified train', () => {
    const v = view({ train_status: 'off' }, { train: { id: 'U1', is_unidentified: true } });
    expect(cardControl(v)).toEqual({ kind: 'none', text: 'Unidentified' });
  });
});

describe('status words', () => {
  it('reads train_status as off before the first snapshot', () => {
    expect(trainStatus(view(undefined))).toBe('off');
  });

  it('shows ato_status only for an ATO train', () => {
    expect(atoStatus(view({ train_status: 'manual' }, { operatorStatus: { ato_status: 'OFF' } }))).toBeNull();
    expect(atoStatus(view({ train_status: 'ato' }, { operatorStatus: { ato_status: 'held' } }))).toBe('HELD');
  });

  it('shows the hold reason only while the train stands', () => {
    const hold = { code: 'no_route', question: 3, detail: null };
    expect(holdLine(view({ is_stopped: true }, { operatorStatus: { hold_reason: hold } }))).toBe(
      'No route is assigned.'
    );
    expect(holdLine(view({ is_stopped: false }, { operatorStatus: { hold_reason: hold } }))).toBeNull();
  });
});
