import { describe, expect, it } from 'vitest';

import type { TrainView } from './live';
import { atoStatus, cardControl, holdLine, trainCardTone, trainStatus } from './train-display';

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

describe('trainCardTone: the card edge says whether the train needs the operator', () => {
  const held = (code: string, detail: string | null = null) =>
    view({ is_stopped: true }, { operatorStatus: { hold_reason: { code, question: 2, detail } } });

  it('is green while the train moves and blank while it stands for no reason', () => {
    expect(trainCardTone(view({ is_stopped: false }, { operatorStatus: { hold_reason: null } }))).toBe('ok');
    expect(trainCardTone(view({ is_stopped: true }, { operatorStatus: { hold_reason: null } }))).toBe('none');
    expect(trainCardTone(view(undefined))).toBe('none');
  });

  it('keeps danger for the emergency brake, the cut-out and a lost train', () => {
    expect(trainCardTone(held('emergency_brake'))).toBe('danger');
    expect(trainCardTone(held('emergency_cut_out'))).toBe('danger');
    expect(trainCardTone(held('detection_lost'))).toBe('danger');
  });

  it('warns when the train waits on the operator', () => {
    expect(trainCardTone(held('unidentified'))).toBe('warn');
    expect(trainCardTone(held('ato_not_engaged'))).toBe('warn');
    expect(trainCardTone(held('no_route'))).toBe('warn');
  });

  it('tells a failed plan from the railway making the train wait', () => {
    expect(trainCardTone(held('no_reservation', 'planning_failed'))).toBe('warn');
    expect(trainCardTone(held('reservation_end', 'wedge'))).toBe('warn');
    expect(trainCardTone(held('no_reservation', 'meet_hold'))).toBe('info');
    expect(trainCardTone(held('reservation_end', 'BLK_3'))).toBe('info');
    expect(trainCardTone(held('no_reservation'))).toBe('info');
    expect(trainCardTone(held('dwelling'))).toBe('info');
    expect(trainCardTone(held('atc_limit', 'signal_red'))).toBe('info');
  });

  it('leaves the edge blank when the train stands by choice or the session is stopped', () => {
    expect(trainCardTone(held('powered_off'))).toBe('none');
    expect(trainCardTone(held('driver_idle'))).toBe('none');
    expect(trainCardTone(held('paused'))).toBe('none');
    expect(trainCardTone(held('halted', 'layout_emergency_cut_out'))).toBe('none');
  });
});
