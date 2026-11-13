// AUTO-GENERATED — DO NOT EDIT BY HAND.
// Source of truth: runtime_payloads.py (Python @dataclass + Enum).
// Regenerate: conda run -n general python tools/gen_payload_types.py
// Drift guard: tests/test_payload_types_sync.py fails if this is stale.

export type FatalConflictKind = "occupied_by_other_train" | "reserved_for_other_train" | "switch_occupied_by_other_train" | "disabled_resource_entry" | "malformed_route_key" | "switch_intrusion";

export type ValidationErrorType = "unblocked_tile" | "block_occupied" | "block_reserved" | "block_disabled" | "block_manual" | "switch_locked" | "switch_manual" | "switch_disabled" | "flank_switch_conflict" | "meet_way_kept";

export interface ErrorRecord {
  text: string;
  origin: string;
}

export interface FatalPhysicalEntryConflict {
  resource_type: string;
  resource_id: string;
  incoming_train_id: string;
  prior_owner_train_id: string | null;
  conflict_kind: FatalConflictKind;
  incoming_path_id: string | null;
  prior_owner_path_id: string | null;
}

export interface HardwareFailure {
  component: string;
  operation: string;
  message: string;
  resource_id: string | null;
}

export interface OccupyResult {
  outcome: string;
  reason: string | null;
}

export interface ValidationError {
  type: ValidationErrorType;
  message: string;
  tile: [number, number] | null;
  block_id: string | null;
  position: [number, number] | null;
  occupied_by: string | null;
  locked_by_path: string | null;
  kept_for_train: string | null;
}
