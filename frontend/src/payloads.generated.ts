// AUTO-GENERATED — DO NOT EDIT BY HAND.
// Source of truth: runtime_payloads.py (Python @dataclass + Enum).
// Regenerate: conda run -n general python tools/gen_payload_types.py
// Drift guard: tests/test_payload_types_sync.py fails if this is stale.

export interface ErrorRecord {
  text: string;
  origin: string;
}

export interface HardwareFailure {
  component: string;
  operation: string;
  message: string;
  resource_id: string | null;
}
