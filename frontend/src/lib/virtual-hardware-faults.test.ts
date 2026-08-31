import { describe, expect, it } from 'vitest';

import {
  FAULT_TARGET_COMPATIBILITY,
  VIRTUAL_FAULT_PAIRS,
  clearFaultBody,
  describeFaultRecord,
  faultPairLabel,
  injectFaultBody,
} from './virtual-hardware-faults';

// The taxonomy mirrors hardware/fake.py. These specs pin the contract the dev
// panels build their fault controls from: only valid (target, fault) pairs are
// offered, and the request bodies match `PUT /api/virtual-hardware/fault`.

describe('VIRTUAL_FAULT_PAIRS', () => {
  it('offers exactly the eight valid (target, fault) pairs', () => {
    // Derived from the compatibility map, so the count is the sum of its lists.
    const expected = Object.values(FAULT_TARGET_COMPATIBILITY).reduce(
      (n, kinds) => n + kinds.length,
      0
    );
    expect(VIRTUAL_FAULT_PAIRS).toHaveLength(expected);
    expect(expected).toBe(8);
  });

  it('never pairs a fault with an incompatible target', () => {
    for (const pair of VIRTUAL_FAULT_PAIRS) {
      expect(FAULT_TARGET_COMPATIBILITY[pair.targetKind]).toContain(pair.fault);
    }
  });

  it('covers device_disconnect on both the controller and a chip', () => {
    const disconnects = VIRTUAL_FAULT_PAIRS.filter((p) => p.fault === 'device_disconnect')
      .map((p) => p.targetKind);
    expect(disconnects).toEqual(['controller', 'chip']);
  });
});

describe('injectFaultBody', () => {
  it('builds an inject request from a pair and a target id', () => {
    expect(injectFaultBody({ targetKind: 'transaction', fault: 'timeout' }, 'IN1', 'write')).toEqual({
      action: 'inject',
      target_kind: 'transaction',
      target_id: 'IN1',
      operation: 'write',
      fault: 'timeout',
    });
  });

  it('leaves operation null when none is given', () => {
    const body = injectFaultBody({ targetKind: 'controller', fault: 'device_disconnect' }, 'CTRL');
    expect(body).toMatchObject({ operation: null, fault: 'device_disconnect' });
  });

  it('drops a stale operation on a non-transaction fault (op is transaction-only)', () => {
    // The op input is shared across inject buttons; a controller disconnect must
    // not carry a leftover 'write' from a prior transaction injection.
    const body = injectFaultBody({ targetKind: 'controller', fault: 'device_disconnect' }, 'CTRL', 'write');
    expect(body).toMatchObject({ operation: null, fault: 'device_disconnect' });
  });

  it('trims the target id and returns null when it is blank', () => {
    expect(injectFaultBody({ targetKind: 'chip', fault: 'device_disconnect' }, '  IN2 ')?.target_id).toBe('IN2');
    expect(injectFaultBody({ targetKind: 'chip', fault: 'device_disconnect' }, '   ')).toBeNull();
    expect(injectFaultBody({ targetKind: 'chip', fault: 'device_disconnect' }, '')).toBeNull();
  });
});

describe('clearFaultBody', () => {
  it('carries the record fault so a clear removes only that one record', () => {
    const body = clearFaultBody({ target_kind: 'transaction', target_id: 'IN1', operation: 'read', fault: 'refusal' });
    expect(body).toEqual({
      action: 'clear',
      target_kind: 'transaction',
      target_id: 'IN1',
      operation: 'read',
      fault: 'refusal',
    });
  });

  it('keeps a null operation null', () => {
    const body = clearFaultBody({ target_kind: 'controller', target_id: 'CTRL', operation: null, fault: 'device_disconnect' });
    expect(body.operation).toBeNull();
  });
});

describe('labels', () => {
  it('names a pair by target and a plain fault word', () => {
    expect(faultPairLabel({ targetKind: 'port', fault: 'output_disagrees_with_command' })).toBe('port: output mismatch');
  });

  it('describes an injected record with its id and operation', () => {
    expect(describeFaultRecord({ target_kind: 'transaction', target_id: 'IN1', operation: 'write', fault: 'timeout' }))
      .toBe('transaction IN1 (write): timeout');
    expect(describeFaultRecord({ target_kind: 'controller', target_id: 'CTRL', operation: null, fault: 'device_disconnect' }))
      .toBe('controller CTRL: disconnect');
  });
});
