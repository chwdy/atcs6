// Operator-facing virtual-hardware fault taxonomy, mirrored from the backend
// (hardware/fake.py: VIRTUAL_HARDWARE_FAULT_KINDS / VIRTUAL_HARDWARE_FAULT_TARGETS
// / FAULT_TARGET_COMPATIBILITY). `PUT /api/virtual-hardware/fault` injects or
// clears one fault against a target; `GET /api/virtual-hardware` returns the
// list of injected records under `injected_faults`.
//
// These six kinds are a behavioral layer above the fake device's low-level
// transport fault names: the operator names one of these kinds against a
// target, and the backend reproduces it on the transport. An incompatible
// (target, fault) pair is rejected by the backend, not translated — so the UI
// only ever offers the valid pairs below.

export type VirtualFaultKind =
  | 'transaction_refusal'
  | 'timeout'
  | 'device_disconnect'
  | 'invalid_reading'
  | 'input_hold_or_flicker'
  | 'output_disagrees_with_command';

export type VirtualFaultTarget =
  | 'controller'
  | 'chip'
  | 'port'
  | 'transaction'
  | 'readback';

/** One injected fault as `GET /api/virtual-hardware` reports it under
 *  `injected_faults`. `operation` narrows a transaction fault to the read or
 *  write side; it is null for the whole transaction and for the other kinds. */
export interface VirtualFaultRecord {
  target_kind: string;
  target_id: string | null;
  operation: string | null;
  fault: string;
}

/** Body of `PUT /api/virtual-hardware/fault`. `fault` is required to inject and
 *  omitted to clear; `operation` is optional. */
export interface VirtualFaultRequest {
  action: 'inject' | 'clear';
  target_kind: string;
  target_id: string;
  operation?: string | null;
  fault?: string | null;
}

/** Which fault kinds each target may carry. The backend rejects any other pair
 *  rather than translating it (hardware/fake.py FAULT_TARGET_COMPATIBILITY). */
export const FAULT_TARGET_COMPATIBILITY: Record<VirtualFaultTarget, VirtualFaultKind[]> = {
  controller: ['device_disconnect'],
  chip: ['device_disconnect'],
  transaction: ['transaction_refusal', 'timeout'],
  port: ['input_hold_or_flicker', 'output_disagrees_with_command'],
  readback: ['invalid_reading', 'output_disagrees_with_command'],
};

// The target order the dev panel shows: the controller and a chip drop off the
// bus, then a transaction fails, then a port or readback misbehaves.
const TARGET_ORDER: VirtualFaultTarget[] = [
  'controller',
  'chip',
  'transaction',
  'port',
  'readback',
];

export interface FaultPair {
  targetKind: VirtualFaultTarget;
  fault: VirtualFaultKind;
}

/** Every valid (target, fault) pair the operator can inject — one per button in
 *  the dev panel. Derived from FAULT_TARGET_COMPATIBILITY so the button row can
 *  never drift from what the backend accepts. */
export const VIRTUAL_FAULT_PAIRS: FaultPair[] = TARGET_ORDER.flatMap((targetKind) =>
  FAULT_TARGET_COMPATIBILITY[targetKind].map((fault) => ({ targetKind, fault }))
);

const FAULT_LABELS: Record<VirtualFaultKind, string> = {
  transaction_refusal: 'refusal',
  timeout: 'timeout',
  device_disconnect: 'disconnect',
  invalid_reading: 'invalid reading',
  input_hold_or_flicker: 'input hold/flicker',
  output_disagrees_with_command: 'output mismatch',
};

function faultLabel(fault: string): string {
  return FAULT_LABELS[fault as VirtualFaultKind] || fault;
}

/** Button label for a pair, e.g. "transaction: refusal". */
export function faultPairLabel(pair: FaultPair): string {
  return `${pair.targetKind}: ${faultLabel(pair.fault)}`;
}

/** One injected record as a single readable line, e.g.
 *  "transaction IN1 (write): timeout" or "controller CTRL: disconnect". */
export function describeFaultRecord(r: VirtualFaultRecord): string {
  const id = r.target_id ? ` ${r.target_id}` : '';
  const op = r.operation ? ` (${r.operation})` : '';
  return `${r.target_kind}${id}${op}: ${faultLabel(r.fault)}`;
}

/** Build an inject request, or null when `targetId` is blank — the backend
 *  requires a non-empty target_id. `operation` is optional and only meaningful
 *  for transaction faults, where it narrows the fault to 'read' or 'write'. */
export function injectFaultBody(
  pair: FaultPair,
  targetId: string,
  operation?: string
): VirtualFaultRequest | null {
  const id = (targetId || '').trim();
  if (!id) return null;
  const op = (operation || '').trim();
  return {
    action: 'inject',
    target_kind: pair.targetKind,
    target_id: id,
    operation: op || null,
    fault: pair.fault,
  };
}

/** Build a clear request that removes one injected record. `fault` is omitted
 *  (the backend requires it absent for a clear); the record is matched on
 *  target_kind + target_id + operation. */
export function clearFaultBody(r: VirtualFaultRecord): VirtualFaultRequest {
  return {
    action: 'clear',
    target_kind: r.target_kind,
    target_id: r.target_id || '',
    operation: r.operation ?? null,
  };
}
