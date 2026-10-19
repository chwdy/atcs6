import { describe, expect, it } from 'vitest';

import { holdReasonText } from './hold-reason';
import { atoIsDriving, startRouteUnavailableReason, trainDisplayStatus } from './train-status';

describe('trainDisplayStatus', () => {
  it('shows the overall status for a train that is not in ATO', () => {
    expect(trainDisplayStatus({ train_status: 'eb' })).toBe('eb');
    expect(trainDisplayStatus({ train_status: 'calibrating' })).toBe('calibrating');
  });

  it('shows the read-model ATO status for an ATO train, in lower case', () => {
    expect(trainDisplayStatus({ train_status: 'ato' }, { ato_status: 'RUNNING' })).toBe('running');
  });

  it('falls back to ready when the read model has not arrived', () => {
    expect(trainDisplayStatus({ train_status: 'ato' })).toBe('ready');
  });
});

describe('atoIsDriving', () => {
  it('is true while ATO pursues a running route run, moving or not', () => {
    for (const atoStatus of ['RUNNING', 'HELD', 'DWELLING']) {
      expect(atoIsDriving({ train_status: 'ato', engaged: true }, { ato_status: atoStatus })).toBe(true);
    }
  });

  it('is false for an engaged train whose route has finished', () => {
    expect(atoIsDriving({ train_status: 'ato', engaged: true }, { ato_status: 'READY' })).toBe(false);
  });

  it('is false for a train that is not in ATO', () => {
    expect(atoIsDriving({ train_status: 'eb' }, { ato_status: 'OFF' })).toBe(false);
    expect(atoIsDriving({ train_status: 'manual' })).toBe(false);
  });
});

describe('holdReasonText', () => {
  it('is null when nothing holds the train', () => {
    expect(holdReasonText(null)).toBeNull();
  });

  it('puts the detail into the sentence', () => {
    expect(
      holdReasonText({ code: 'pending_action', question: 3, detail: 'stopping to power off' })
    ).toBe('Waiting: stopping to power off.');
    expect(holdReasonText({ code: 'calibrating', question: 2, detail: 'speed' })).toBe(
      'A speed calibration run is driving the train.'
    );
  });

  it('reads the error origin behind a missing reservation as words', () => {
    expect(holdReasonText({ code: 'no_reservation', question: 3, detail: 'no_viable_path' })).toBe(
      'No reservation yet (no viable path).'
    );
  });

  it('still says something for a code it does not know', () => {
    expect(holdReasonText({ code: 'new_code', question: 4, detail: null })).toBe('Held: new_code.');
  });
});

describe('startRouteUnavailableReason', () => {
  const stoppedOff = { train_status: 'off', is_stopped: true };

  it('is available from OFF with a route assigned', () => {
    expect(startRouteUnavailableReason(stoppedOff, 'R1', { pending_actions: [] })).toBeNull();
  });

  it('needs a route', () => {
    expect(startRouteUnavailableReason(stoppedOff, null)).toBe('Assign a route first');
  });

  it('is refused under EB, while calibrating, or while moving', () => {
    expect(startRouteUnavailableReason({ train_status: 'eb', is_stopped: true }, 'R1')).toMatch(/emergency/);
    expect(startRouteUnavailableReason({ train_status: 'calibrating', is_stopped: true }, 'R1')).toMatch(
      /calibration/
    );
    expect(startRouteUnavailableReason({ train_status: 'manual', is_stopped: false }, 'R1')).toMatch(/Stop/);
  });

  it('waits for a pending power-off', () => {
    const status = { pending_actions: [{ latch: 'power_off', label: 'stopping to power off' }] };
    expect(startRouteUnavailableReason({ train_status: 'manual', is_stopped: true }, 'R1', status)).toBe(
      'A power-off is pending'
    );
  });

  it('is pointless on an engaged ATO train', () => {
    expect(
      startRouteUnavailableReason({ train_status: 'ato', is_stopped: true, engaged: true }, 'R1')
    ).toBe('The route is already running');
  });
});

describe('holdReasonText for the first question', () => {
  it('names the way back for each session state', () => {
    expect(holdReasonText({ code: 'paused', question: 1, detail: null })).toMatch(/Press Play/);
    expect(holdReasonText({ code: 'setup', question: 1, detail: null })).toMatch(/Activate/);
    expect(
      holdReasonText({ code: 'halted', question: 1, detail: 'layout_emergency_cut_out' })
    ).toMatch(/Reset/);
    expect(holdReasonText({ code: 'halted', question: 1, detail: 'not_activated' })).toMatch(
      /not activated/
    );
  });
});
