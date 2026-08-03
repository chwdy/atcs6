// AUTO-GENERATED — DO NOT EDIT BY HAND.
// Source of truth: runtime_payloads.py (Python @dataclass + Enum).
// Regenerate: conda run -n general python tools/gen_payload_types.py
// Drift guard: tests/test_payload_types_sync.py fails if this is stale.

export type AcquisitionMode = "all_or_nothing" | "best_effort";

export type PathAcquisitionResultKind = "reached_requested_target" | "truncated_grew" | "blocked_no_growth";

export type PathAcquisitionFailureKind = "no_viable_path" | "blocked_before_target" | "invalid_reverse" | "invalid_request" | "head_on_switch_no_block_authority" | "missing_or_inactive_path" | "train_already_has_active_path";

export type TargetResolutionFailureKind = "missing_target" | "target_unavailable";

export type FatalConflictKind = "occupied_by_other_train" | "reserved_for_other_train" | "switch_occupied_by_other_train" | "disabled_resource_entry" | "malformed_route_key" | "switch_intrusion";

export type ValidationErrorType = "unblocked_tile" | "block_occupied" | "block_reserved" | "block_disabled" | "block_manual" | "switch_locked" | "switch_manual" | "switch_disabled" | "flank_switch_conflict";

export interface ErrorRecord {
  text: string;
  origin: string;
}

export interface TileTraversal {
  tile_xy: [number, number];
  entry_edge: string;
  exit_edge: string;
  resource_type: "block" | "switch" | null;
  resource_id: string | null;
  block_id: string | null;
}

export interface PhysicsSpanUpdate {
  new_span: unknown;
  current_speed_kmh: number;
  added_tiles: TileTraversal[];
  removed_tiles: TileTraversal[];
}

export interface ResourceEvent {
  sequence_index: number | null;
  resource_type: "block" | "switch";
  resource_id: string;
  exit_edge: string | null;
  block_id: string | null;
  entry_edge: string | null;
  switch_exit_edge: string | null;
  traversal_direction: string | null;
}

export interface ControllerProgressReport {
  train_id: string;
  event_time: number;
  entered_resources: ResourceEvent[];
  tail_vacated_resources: ResourceEvent[];
  current_head_sequence_index: number | null;
  deviation: boolean;
  deviation_error: ErrorRecord | null;
}

export interface PendingPathCancelRequest {
  initiator: string;
  reason: string | null;
  deviation: boolean;
  error: ErrorRecord | null;
  route_progress_kind: "run_complete" | null;
}

export interface PhaseLatches {
  path_cancel_request: PendingPathCancelRequest | null;
  route_abort: boolean;
  power_off: boolean;
  route_deactivate: boolean;
}

export interface PathCompletionResult {
  success: boolean;
  path_id: string | null;
  new_status: unknown;
  reason: string | null;
}

export interface PathAcquisitionResult {
  success: boolean;
  result_kind: PathAcquisitionResultKind | null;
  failure_kind: PathAcquisitionFailureKind | null;
  path_id: string | null;
  requested_destination_block_id: string | null;
  requested_destination_direction: string | null;
  authority_end_block_id: string | null;
  resource_sequence: unknown[] | null;
  reason: string;
}

export interface CancellationResult {
  success: boolean;
  path_id: string | null;
  new_status: unknown;
  reason: string | null;
}

export interface Command {
  kind: string;
  payload: Record<string, unknown>;
  command_id: string;
  accepted_at: number;
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

export interface SwitchLockClaim {
  path_id: string;
  train_id: string;
  locked_state: number;
  source_switch_id: string | null;
  manual_override: boolean;
  route: string[] | null;
}

export interface ResolvedRouteTarget {
  requested_destination_block_id: string;
  requested_destination_direction: string;
}

export interface ValidationError {
  type: ValidationErrorType;
  message: string;
  tile: [number, number] | null;
  block_id: string | null;
  position: [number, number] | null;
  occupied_by: string | null;
  locked_by_path: string | null;
}

export interface PathSegment {
  tiles: [number, number][];
  distance_cm: number;
  required_switches: Record<string, number>;
  blocks: string[];
}

export interface ValidationResult {
  valid: boolean;
  errors: ValidationError[];
  path: PathSegment | null;
}
