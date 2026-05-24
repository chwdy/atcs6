> Part of the [ATCS6 design spec](README.md). Cross-cutting: [Core loop & latches](030-core-loop.md) · [Units & time](040-units.md) · [Glossary](010-glossary.md).

## TrackResource

**Resource base contract**

- `TrackResource` is the shared base class of `Block` and `Switch`. It carries the common identity surface every dispatcher-facing caller needs: `resource_id` (the entity's primary ID, projected as a `@property` over `block_id` / `switch_id`), `resource_type` (the literal string `"block"` or `"switch"`), `name`, `occupied_by`, and `disabled`. All other entity-specific fields stay on the concrete subclass.
- `TrackResource` declares no reserve/release/lock methods. Per the **BlockManager** and **SwitchManager** canonical homes, the manager layer is the sole mutator for reservation, occupancy, CAUTION, and lock fields; `Block` and `Switch` remain data containers in v1.
- `Path.resource_sequence[*].resolve(network)` returns the live `TrackResource` for an entry, or `None` if the resource is not registered. The entry remains the source of truth for path-local geometry (`path_tiles`, `entry_edge`, `exit_edge`); the helper is purely a registry read so callers that want polymorphic access to the underlying entity do not duplicate the `network.blocks.get(...)` / `network.switches.get(...)` branch.

## TrackSegment

**Path locking**

- Switch locking is owned entirely by the `Switch` entity (multi-owner `Switch.locks`); track-segment locking is not stored on `TrackSegment`.
- Temporary reservation direction lives at the block/path-plan level: `Block.reserved_direction` is the dispatcher-owned block reservation direction; `Path.resource_sequence[*].entry_edge` / `exit_edge` plus nested `path_tiles[*].entry_edge` / `exit_edge` carry the per-resource and per-tile traversal direction.
- `TrackSegment` keeps static geometry, classification, and `switch_id`. If the segment is a switch, callers resolve the active segment from `Switch.position` in `network.switches` via `switch_id`. `TrackSegment` does not store live switch state.

**Track-segment role and traversal**

- The retained `TrackSegment` schema in v1 is explicit:
  - persistent fields kept on `TrackSegment`: `x`, `y`, `track_bits`, `dead_end_ports`, `signal_ids`, `switch_id`, `length`, `curve_radius`, `wear_level`, `last_maintenance`, `maintenance_count`
  - computed read kept on `TrackSegment`: `track_type`
  - removed live runtime fields: `_switch_state`, `occupied_by`, `reserved_by`, `locked_by_path`, `locked_state`, `path_direction`
  - `TrackSegment` does not store station, platform, or block membership.
- Entity collection ownership in v1 is strictly hierarchical, and each level owns the IDs of the immediately lower level:
  - `Station.platform_ids: List[str]` is the canonical list of `Platform` IDs in that station group.
  - `Platform.ordered_blocks: List[str]` is the canonical ordered list of `Block` IDs in that platform.
  - `Block.ordered_tiles: List[BlockTileEntry]` is the canonical ordered list of `TrackSegment` tiles in that block, where each `BlockTileEntry` carries `(x, y, entry_edge)` — see the **Block** section for the full shape.
  - `TrackSegment` does not store any back-reference for station/platform/block membership.
  All four entities (`TrackSegment`, `Block`, `Platform`, `Station`) are registered with `RailwayNetwork`. Reverse lookups ("which platform owns this block?", "which block owns this tile?") are answered through `RailwayNetwork`-owned indices, not through fields stored on the lower-level entity.
- `TrackSegment` does not carry static or temporary traffic-direction policy in v1. Static allowed travel direction is block-owned, through `Block.allowed_travel_directions`; route/requested travel direction is route-owned, through `RouteExecutor` resolving `RouteTask.task_direction` into `ResolvedRouteTarget.requested_destination_direction`; temporary reservation direction is dispatcher-owned, through `Block.reserved_direction`; and finer-grained traversal frame data lives on `Path.resource_sequence[*]` and nested `path_tiles[*]`.
- Tile-to-platform / tile-to-block / tile-to-station lookups go through `RailwayNetwork`-owned reverse indices computed from `Block.ordered_tiles`, `Platform.ordered_blocks`, and `Station.platform_ids`. `station_id` is reserved for the logical multi-platform `Station` identifier; `platform_id` is reserved for the concrete `Platform` identifier; the tile-level entity stores neither.
- `TrackSegment` is not the authoritative store for live switch state or switch locks. Switch state lives on `Switch`; lock ownership is expressed through `Switch.locks` (for switches) and `Block.reserved_by_path` / `Block.reserved_for_train` (for blocks).
- If the segment classification says the tile is a switch, `TrackSegment.switch_id` should point to the authoritative `Switch` entity in the network entity database. Live switch state and switch lock ownership should live only on that `Switch` entity.
- Split planning-time traversal enumeration from live-state traversal reads.
- Live traversal helpers:
  - `TrackSegment.get_active_exits(entry_edge, resolver) -> List[str]`
  - `TrackSegment.get_active_entry_for_exit(exit_edge, resolver) -> Optional[str]`
  where `resolver` is the network or an equivalent switch-state resolver that can read `Switch.position` via `switch_id`.
- If the resolved owning `Switch` exists and `Switch.disabled == True`, the live traversal helpers return no usable traversal for that switch tile.
- Planning-time helper:
  - `TrackSegment.enumerate_traversals(entry_edge) -> List[(exit_edge, required_switch_state)]`
  This helper enumerates every legal geometric traversal through the tile independent of the switch's current live state. For an ordinary non-switch tile, it yields the one legal `(exit_edge, None)` pair. For a switch tile, it yields the legal `(exit_edge, required_switch_state)` pairs that dispatcher path search may acquire.
- `TrackSegment.enumerate_traversals(...)` remains a pure geometric helper and does not inspect `Switch.disabled`. Callers that resolve the owning switch through the network entity layer must drop all enumerated traversals for that switch tile when `Switch.disabled == True`, so pathfinding/wayfinding treats a disabled switch as absent connectivity.
- `TrackSegment.get_active_exits(...)` and `TrackSegment.get_active_entry_for_exit(...)` map one edge through the currently active segment selected by `Switch.position`. `TrackSegment.enumerate_traversals(...)` is the planner-facing geometry contract and must not consult live switch alignment.
- For Y-shape switch geometry in v1, branch-to-branch traversal is invalid. `TrackSegment.enumerate_traversals(...)` must not yield branch-to-branch options.
- `PathDispatcher` path construction/extension and BFS switch derivation use `TrackSegment.enumerate_traversals(...)`. `TrainPhysics._advance_end()`, tail tracing, and other live movement readers use the active-state helpers. Switch traversal never reads track-stored live state.

**Track-segment invariants**

- Switch-integrity while a train spans a switch: `Switch.occupied_by` plus the still-held `Switch.locks` entries prevent a switch from flipping out from under the spanning train. No global path lock is needed for this guarantee.
- Tail tracing still uses the live-state entry/exit helper, but it now resolves through the live `Switch.position` per the directed resolver contract in **Track-segment role and traversal** rather than through a separately persisted track-tile field.
