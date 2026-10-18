import { describe, expect, it } from 'vitest';

import { holdReasonText } from './hold-reason';
import { trainDisplayStatus } from './train-status';

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

  it('still says something for a code it does not know', () => {
    expect(holdReasonText({ code: 'new_code', question: 4, detail: null })).toBe('Held: new_code.');
  });
});
