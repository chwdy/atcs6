// api.ts — Thin HTTP client over the Flask `/api` surface. Implements the
// `LayoutDataClient` + `SimClient` data contract (see layout-data-client.ts).
//
// Migrated from static/js/api.js (longterm_todo.md item #7, Wave 1 — foundation
// cluster). Behavior is unchanged from the classic file: each method is a thin
// wrapper over `_request`, the single fetch/JSON/error-normalization choke point.
//
// Part of the Vite bundle; the single client instance is imported as `{ api }`
// by the app, panels, and editor.

// Caller-supplied fetch options plus the custom `returnNullOn404` flag stripped
// before the request reaches fetch().
import type { LayoutDataClient, SimClient } from './layout-data-client';

interface RequestOptions {
  method?: string;
  headers?: Record<string, string>;
  body?: unknown;
  returnNullOn404?: boolean;
  [key: string]: unknown;
}

interface RequestConfig {
  method?: string;
  headers: Record<string, string>;
  body?: unknown;
  returnNullOn404?: boolean;
  [key: string]: unknown;
}

// `implements` makes the compiler verify this client satisfies both halves of
// the data contract — the layout surface the reusable editor/renderer need and
// the sim surface the sim plugin needs.
export class API implements LayoutDataClient, SimClient {
  baseUrl: string;

  constructor(baseUrl = '/api') {
    this.baseUrl = baseUrl;
  }

  /**
   * Centralized request handler to reduce boilerplate.
   * @param endpoint - API endpoint (e.g., '/network')
   * @param options - Fetch options (method, headers, body, etc.)
   * @param caller - Name of the calling method for logging
   * @returns JSON response or null
   */
  async _request(endpoint: string, options: RequestOptions = {}, caller = 'request'): Promise<any> {
    const url = `${this.baseUrl}${endpoint}`;
    const config: RequestConfig = {
      method: 'GET',
      headers: {},
      ...options,
    };

    // Handle body serialization and Content-Type
    if (config.body && typeof config.body !== 'string') {
      config.headers['Content-Type'] = 'application/json';
      config.body = JSON.stringify(config.body);
    }

    // Helper options (not passed to fetch)
    const returnNullOn404 = config.returnNullOn404;
    delete config.returnNullOn404;

    try {
      const response = await fetch(url, config as RequestInit);

      if (response.status === 404 && returnNullOn404) {
        return null;
      }

      if (!response.ok) {
        let errorMessage = `HTTP ${response.status}: ${response.statusText}`;
        let errorData: any = null;
        try {
          errorData = await response.json();
          if (errorData.error) errorMessage = errorData.error;
        } catch (e) {
          try {
            const text = await response.text();
            if (text) errorMessage = `HTTP ${response.status}: ${text}`;
          } catch (e2) {}
        }
        const error = new Error(errorMessage) as Error & { status?: number; data?: unknown };
        error.status = response.status;
        error.data = errorData;
        if (errorData) {
          Object.assign(error, errorData);
        }
        throw error;
      }

      if (response.status === 204) return null;

      return await response.json();
    } catch (e) {
      console.error(`[API] ${caller} failed:`, e);
      throw e;
    }
  }

  async getNetwork(): Promise<any> {
    return this._request('/network', {}, 'getNetwork');
  }

  /**
   * Get the inspector snapshot for a single train.
   * Returns null when the train was deleted between polls.
   */
  async getInspectorEntityTrain(trainId: string): Promise<any> {
    return this._request(
      `/inspector/entity/train/${encodeURIComponent(trainId)}`,
      { returnNullOn404: true },
      'getInspectorEntityTrain'
    );
  }

  /**
   * Get current network layout as StepRecorder steps.
   * Used by the scenario recorder to embed layout in scenario files.
   */
  async getNetworkSteps(): Promise<any> {
    const data = await this._request('/network/steps', {}, 'getNetworkSteps');
    return data?.steps || [];
  }

  // Individual track operations

  /**
   * Get the track at a tile. Returns null when the tile has no track —
   * callers probe empty cells routinely (e.g. selecting an empty tile in
   * the editor), so a 404 here is an expected miss, not an error. Real
   * failures (network, 5xx) still throw and log.
   */
  async getTrack(x: number, y: number): Promise<any> {
    return this._request(`/track/${x}/${y}`, { returnNullOn404: true }, 'getTrack');
  }

  async updateTrack(x: number, y: number, updates: unknown): Promise<any> {
    return this._request(
      `/track/${x}/${y}`,
      {
        method: 'PATCH',
        body: updates,
      },
      'updateTrack'
    );
  }

  async deleteTrack(x: number, y: number): Promise<any> {
    return this._request(
      `/track/${x}/${y}`,
      {
        method: 'DELETE',
      },
      'deleteTrack'
    );
  }

  async addTracksBulk(tracks: unknown): Promise<any> {
    return this._request(
      '/tracks/bulk',
      {
        method: 'POST',
        body: { tracks },
      },
      'addTracksBulk'
    );
  }

  async deleteTracksBulk(positions: unknown, { force = false }: { force?: boolean } = {}): Promise<any> {
    return this._request(
      '/tracks/bulk-delete',
      {
        method: 'POST',
        body: { positions, force },
      },
      'deleteTracksBulk'
    );
  }

  async createTrain(trainData: unknown): Promise<any> {
    return this._request(
      '/trains',
      {
        method: 'POST',
        body: trainData,
      },
      'createTrain'
    );
  }

  /** Get all trains in the network. */
  async getTrains(): Promise<any> {
    const result = await this._request('/trains', {}, 'getTrains');
    return result.trains || [];
  }

  /**
   * Map of train_id -> assigned route_id (or null), from the /trains sibling
   * payload's `assigned_routes_by_train_id`. Used to tell which trains are
   * free for assignment.
   */
  async getAssignedRoutesByTrain(): Promise<any> {
    const result = await this._request('/trains', {}, 'getTrains');
    return result.assigned_routes_by_train_id || {};
  }

  /** Delete a train. */
  async deleteTrain(trainId: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}`,
      {
        method: 'DELETE',
      },
      'deleteTrain'
    );
  }

  /** Update train properties. */
  async updateTrain(trainId: string, updates: unknown): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}`,
      {
        method: 'PATCH',
        body: updates,
      },
      'updateTrain'
    );
  }

  /**
   * Control simulation state.
   * @param action - 'start', 'stop', 'reset', 'set_speed'
   * @param speed - Simulation speed multiplier
   */
  async controlSimulation(action: string, speed: number | null = null): Promise<any> {
    const data: { action: string; speed?: number } = { action };
    if (speed !== null) {
      data.speed = speed;
    }
    return this._request(
      '/simulation/control',
      {
        method: 'POST',
        body: data,
      },
      'controlSimulation'
    );
  }

  /**
   * Restart the server process. The server flushes autosave, then
   * replaces itself via os.execv. Caller should immediately switch
   * the UI into a reconnect-polling state.
   */
  async restartServer(): Promise<any> {
    return this._request(
      '/server/restart',
      {
        method: 'POST',
      },
      'restartServer'
    );
  }

  /**
   * Lightweight server health check. Used by the reconnect flow to
   * detect when a restarted server is back up.
   */
  async healthCheck(): Promise<any> {
    return this._request('/health', {}, 'healthCheck');
  }

  /**
   * Read the persisted hardware configuration (design_new.md "Power -> Hardware
   * configuration"): `{ config: {enabled, controller, chips, block_track,
   * block_occupancy, switch_output} }`. Each chip is enriched with derived
   * role / bus / port_count.
   */
  async getHardwareConfig(): Promise<any> {
    return this._request('/hardware/config', {}, 'getHardwareConfig');
  }

  /**
   * Persist a new hardware configuration (chip inventory + port bindings). The
   * server validates it and writes it to the app-managed config file; it takes
   * effect on the next session build (a server restart), not the running
   * session. A rejected request (e.g. a chip missing a required parameter, or a
   * binding to an out-of-range port) throws with the server's error message.
   */
  async setHardwareConfig(config: any): Promise<any> {
    return this._request(
      '/hardware/config',
      { method: 'PUT', body: { config } },
      'setHardwareConfig'
    );
  }

  /** The selectable controller / chip catalog for the Hardware Setup dropdowns. */
  async getHardwareCatalog(): Promise<any> {
    return this._request('/hardware/catalog', {}, 'getHardwareCatalog');
  }

  /**
   * Which blocks and switches in the LOADED layout have no binding, and which
   * saved bindings name entities the layout no longer has.
   *
   * Saving a config only validates the bindings that exist, so a half-wired
   * layout saves and enables cleanly — and then, in real mode, an unbound block
   * reads permanently clear and an unbound switch cannot be aligned. This is
   * how the setup screen names those gaps instead of leaving them to be found
   * as a train that stops moving.
   */
  async getHardwareCoverage(): Promise<any> {
    return this._request('/hardware/coverage', {}, 'getHardwareCoverage');
  }

  /**
   * Open the controller and report the I2C addresses answering right now
   * (Hardware Setup's Detect / Rescan). Opens a transient device at
   * commissioning time, returning `{ok:false}` gracefully if none is attached or
   * a real session already holds it. `config` is the config being edited.
   * Shift registers do not answer a scan — they are proven with the interactive
   * port test instead.
   */
  async scanHardwareBus(config: any): Promise<any> {
    return this._request(
      '/hardware/scan',
      { method: 'POST', body: { config } },
      'scanHardwareBus'
    );
  }

  /**
   * Pulse one output port ('595 bit / PCA9685 pair) or read one input port
   * ('165 bit) so a single wire can be confirmed before binding.
   *
   * The pulse is held long enough to read on a multimeter (the server's
   * PORT_TEST_PULSE_S, currently 1 s). `pulseS` overrides that width for a
   * slower instrument; the server clamps it and echoes back the width used.
   */
  async runHardwarePortTest(
    config: any,
    chipId: string,
    port: number,
    action: 'pulse_output' | 'read_input' | 'drive_sequence',
    pulseS?: number
  ): Promise<any> {
    const body: any = { config, chip_id: chipId, port, action };
    if (pulseS !== undefined) body.pulse_s = pulseS;
    return this._request(
      '/hardware/port-test',
      { method: 'POST', body },
      'runHardwarePortTest'
    );
  }

  /**
   * Read both of one block's occupancy detectors and report which decides.
   * Returns the '165 bit, the current-sense reading, the drive state read back
   * off the PWM chip, and the verdict that follows. Drives nothing.
   */
  async runHardwareOccupancyTest(config: any, blockId: string): Promise<any> {
    return this._request(
      '/hardware/occupancy-test',
      { method: 'POST', body: { config, block_id: blockId } },
      'runHardwareOccupancyTest'
    );
  }

  /**
   * Diagnostic read-back of the last detection tick, pre-debounce: per block the
   * raw read, the confirmed verdict, the live debounce counters, and — on the
   * real driver — which source answered and what it measured. Plus the USB link
   * state (null in sim mode, which has no link to lose).
   *
   * Reads recorded state only; it issues no bus traffic, so polling it fast
   * cannot perturb the tick or the detectors it is reporting on.
   */
  async getRawOccupancy(options?: RequestOptions): Promise<any> {
    return this._request('/hardware/occupancy/raw', options ?? {}, 'getRawOccupancy');
  }

  /** Set (or clear, when chipId is null) one port binding, keyed by logical id.
   * `extra` carries the current map's per-channel calibration (baseline/threshold mA). */
  async updateHardwareBinding(
    map: string,
    logicalId: string,
    chipId: string | null,
    port: number | null,
    extra?: { baseline_ma?: number | null; threshold_ma?: number | null }
  ): Promise<any> {
    return this._request(
      '/hardware/binding',
      { method: 'PUT', body: { map, logical_id: logicalId, chip_id: chipId, port, ...(extra || {}) } },
      'updateHardwareBinding'
    );
  }

  /** Set (or clear, when chipId is null) one block's track wiring: the two PWM
   * channels driving its rails, chosen independently (not a fixed pair).
   * `reversed` is the rail-polarity flag stored on the same binding; leave it
   * undefined to keep the stored value across a channel edit. */
  async updateTrackBinding(
    blockId: string,
    chipId: string | null,
    forward: number | null,
    reverse: number | null,
    reversed?: boolean
  ): Promise<any> {
    return this._request(
      '/hardware/binding',
      {
        method: 'PUT',
        body: {
          map: 'block_track',
          logical_id: blockId,
          chip_id: chipId,
          forward,
          reverse,
          ...(reversed === undefined ? {} : { reversed }),
        },
      },
      'updateTrackBinding'
    );
  }

  /**
   * Sweep one block's track through both polarities — full voltage one way,
   * off, then the other way — so a loco on it runs, stops, and reverses.
   */
  async runHardwareTrackTest(config: any, blockId: string): Promise<any> {
    return this._request(
      '/hardware/track-test',
      { method: 'POST', body: { config, block_id: blockId } },
      'runHardwareTrackTest'
    );
  }

  /** Set (or clear, when chipId is null) one switch's throw wiring: a coil port
   * per side plus an optional motor-driver standby bit. */
  async updateSwitchBinding(
    switchId: string,
    chipId: string | null,
    portMain: number | null,
    portDiverge: number | null,
    portStandby: number | null
  ): Promise<any> {
    return this._request(
      '/hardware/binding',
      {
        method: 'PUT',
        body: {
          map: 'switch_output',
          logical_id: switchId,
          chip_id: chipId,
          port_main: portMain,
          port_diverge: portDiverge,
          port_standby: portStandby,
        },
      },
      'updateSwitchBinding'
    );
  }

  /** Open a held commissioning session on the controller (for live input / sweep). */
  async commissionOpen(config: any): Promise<any> {
    await this._request(
      '/operation/commissioning/enter',
      { method: 'POST', body: {} },
      'enterCommissioning'
    );
    return this._request(
      '/hardware/commission/open',
      { method: 'POST', body: { config } },
      'commissionOpen'
    );
  }

  /** Drive one output port on the held session (duty 0..1: PWM level, or on/off). */
  async commissionOutput(chipId: string, port: number, duty: number): Promise<any> {
    return this._request(
      '/hardware/commission/output',
      { method: 'POST', body: { chip_id: chipId, port, duty } },
      'commissionOutput'
    );
  }

  /** Pulse one shift-out bit for the switch-coil pulse width on the held session,
   * raising `standbyPort` for the pulse when the motor driver needs one and
   * holding `offPort` (the driver's other input) low. A coil must never be held
   * energized, so every '595 bit is driven this way.
   *
   * The default width is the runtime throw (10 ms), so a throw test reproduces
   * an operating throw. Pass `pulseS` only to measure the bit rather than watch
   * the points — that holds a coil far longer than a throw does. */
  async commissionPulse(
    chipId: string,
    port: number,
    standbyPort?: number | null,
    offPort?: number | null,
    pulseS?: number
  ): Promise<any> {
    const body: any = {
      chip_id: chipId,
      port,
      standby_port: standbyPort ?? null,
      off_port: offPort ?? null,
    };
    if (pulseS !== undefined) body.pulse_s = pulseS;
    return this._request(
      '/hardware/commission/pulse',
      { method: 'POST', body },
      'commissionPulse'
    );
  }

  /** Throw one switch to `position` (0 = MAIN, 1 = DIVERGE) — the switch
   * editor's Test throw.
   *
   * A real throw, not a coil poke: the server commands the alignment through
   * SwitchManager, so `Switch.position` follows the direction being tested and
   * the app never ends up believing the points are somewhere they are not. It
   * picks the mechanism itself — the running session's switch driver when one
   * is wired, the commissioning session's coil pulse otherwise. */
  async switchThrowTest(config: any, switchId: string, position: 0 | 1): Promise<any> {
    return this._request(
      '/hardware/switch-test',
      { method: 'POST', body: { config, switch_id: switchId, position } },
      'switchThrowTest'
    );
  }

  /** Read every input bit on a shift-in chip from the held session (live view). */
  async commissionInputs(chipId: string): Promise<any> {
    return this._request(
      '/hardware/commission/inputs',
      { method: 'POST', body: { chip_id: chipId } },
      'commissionInputs'
    );
  }

  /** Read every channel's current (mA) on an INA3221 from the held session. */
  async commissionCurrents(chipId: string): Promise<any> {
    return this._request(
      '/hardware/commission/currents',
      { method: 'POST', body: { chip_id: chipId } },
      'commissionCurrents'
    );
  }

  /** Close the held commissioning session (de-energizes + releases the device). */
  async commissionClose(): Promise<any> {
    const closed = await this._request(
      '/hardware/commission/close',
      { method: 'POST', body: {} },
      'commissionClose'
    );
    await this._request(
      '/operation/commissioning/exit',
      { method: 'POST', body: {} },
      'exitCommissioning'
    );
    return closed;
  }

  /**
   * Develop mode (fake hardware) input-poke surface. These act on the LIVE
   * session's fake device, so the real occupancy pipeline reads the injected
   * state back on its next tick. Available only when the running session booted
   * on the fake device (`session.develop_active`); otherwise they answer 409.
   */

  /** Read the fake device's per-block / per-switch state for the dev panel. */
  async getHwDebugState(): Promise<any> {
    return this._request('/debug/hw', {}, 'getHwDebugState');
  }

  /** Set a block's fake '165 occupancy bit. */
  async setHwDebugOccupancy(blockId: string, occupied: boolean): Promise<any> {
    return this._request(
      '/debug/hw/occupancy',
      { method: 'POST', body: { block_id: blockId, occupied } },
      'setHwDebugOccupancy'
    );
  }

  /** Set a block's fake current-sense reading (mA); decides occupancy only
   * while the block is driven. */
  async setHwDebugCurrent(blockId: string, currentMa: number): Promise<any> {
    return this._request(
      '/debug/hw/current',
      { method: 'POST', body: { block_id: blockId, current_ma: currentMa } },
      'setHwDebugCurrent'
    );
  }

  // === Segment Geometry (the geometry axis: /api/segments) ===
  //
  // A `Segment` owns geometry only — ordered tiles, length, min radius, grade
  // and speed limit. Occupancy / reservation / power / grouping live on its
  // owning `Block` under the block surface below. Every id in this section is a
  // SEGMENT id; passing a block id here addresses the wrong axis (for a
  // single-segment block the two ids happen to be equal, which is exactly why
  // they must not be mixed up).

  /** Get all segments (geometry + owning block id + tile positions). */
  async getSegments(): Promise<any> {
    return this._request('/segments', {}, 'getSegments');
  }

  /** Get a single segment's geometry, continuity status and owning block id. */
  async getSegment(segmentId: string): Promise<any> {
    return this._request(`/segments/${encodeURIComponent(segmentId)}`, {}, 'getSegment');
  }

  /**
   * Create a segment holding the given track positions. A fresh segment gets
   * its own single-segment block; group it with others via `createBlock`.
   */
  async createSegment(
    segmentId: string | null = null,
    positions: unknown = [],
    name = '',
    options: Record<string, unknown> = {}
  ): Promise<any> {
    return this._request(
      '/segments',
      {
        method: 'POST',
        body: {
          segment_id: segmentId,
          positions,
          name,
          ...options,
        },
      },
      'createSegment'
    );
  }

  /** Update a segment's geometry (name / length / radius / grade / speed). */
  async updateSegment(segmentId: string, data: unknown): Promise<any> {
    return this._request(
      `/segments/${encodeURIComponent(segmentId)}`,
      {
        method: 'PATCH',
        body: data,
      },
      'updateSegment'
    );
  }

  /** Delete a segment's geometry (and its owning block if it was the last member). */
  async deleteSegment(segmentId: string): Promise<any> {
    return this._request(
      `/segments/${encodeURIComponent(segmentId)}`,
      {
        method: 'DELETE',
      },
      'deleteSegment'
    );
  }

  /** Add tracks to a segment. */
  async addTracksToSegment(segmentId: string, positions: unknown, force = false): Promise<any> {
    return this._request(
      `/segments/${encodeURIComponent(segmentId)}/tracks`,
      {
        method: 'POST',
        body: { positions, force },
      },
      'addTracksToSegment'
    );
  }

  /** Remove tiles from a segment. */
  async removeSegmentTiles(segmentId: string, positions: unknown): Promise<any> {
    return this._request(
      `/segments/${encodeURIComponent(segmentId)}/tracks`,
      {
        method: 'DELETE',
        body: { positions },
      },
      'removeSegmentTiles'
    );
  }

  /**
   * Extend a segment with new track positions.
   * Uses the /extend endpoint which supports signal removal with force=true.
   */
  async extendSegment(
    segmentId: string,
    positions: unknown,
    force = false,
    forceOverwrite = false
  ): Promise<any> {
    return this._request(
      `/segments/${encodeURIComponent(segmentId)}/extend`,
      {
        method: 'POST',
        body: { positions, force, force_overwrite: forceOverwrite },
      },
      'extendSegment'
    );
  }

  /** Check if a segment can be extended to a specific position. */
  async canExtendSegment(segmentId: string, x: number, y: number): Promise<any> {
    return this._request(
      `/segments/${encodeURIComponent(segmentId)}/can_extend`,
      {
        method: 'POST',
        body: { x, y },
      },
      'canExtendSegment'
    );
  }

  /** Get the start and end tiles of a segment, in traversal order. */
  async getSegmentEndTiles(segmentId: string): Promise<any> {
    return this._request(
      `/segments/${encodeURIComponent(segmentId)}/end_tiles`,
      {},
      'getSegmentEndTiles'
    );
  }

  // === Block Management (grouping + power + occupancy: /api/blocks) ===
  //
  // A `Block` groups one or more segments and owns occupancy, reservation,
  // availability, power and the static travel policy. Geometry is NOT edited
  // here — that is the segment surface above. Every id in this section is a
  // BLOCK id.

  /** Get all blocks (member segments, occupancy, power, track counts). */
  async getBlocks(): Promise<any> {
    return this._request('/blocks', {}, 'getBlocks');
  }

  /**
   * Group existing segments into one block. Creating track geometry is a
   * segment operation (`createSegment`); this only forms the grouping.
   */
  async createBlock(segmentIds: string[], name = '', blockId: string | null = null): Promise<any> {
    return this._request(
      '/blocks',
      {
        method: 'POST',
        body: {
          block_id: blockId,
          segment_ids: segmentIds,
          name,
        },
      },
      'createBlock'
    );
  }

  /** Get a single block by ID (member segments, occupancy, positions). */
  async getBlock(blockId: string): Promise<any> {
    return this._request(`/blocks/${encodeURIComponent(blockId)}`, {}, 'getBlock');
  }

  /**
   * Update block config / availability (name, is_holding_point, disabled,
   * manual, allowed_travel_directions). Geometry is rejected here — patch the
   * member segments instead.
   */
  async updateBlock(blockId: string, data: unknown): Promise<any> {
    return this._request(
      `/blocks/${encodeURIComponent(blockId)}`,
      {
        method: 'PATCH',
        body: data,
      },
      'updateBlock'
    );
  }

  /**
   * Dissolve a block grouping (ungroup). Member segments return to their own
   * single-segment blocks; the track geometry survives.
   */
  async deleteBlock(blockId: string): Promise<any> {
    return this._request(
      `/blocks/${encodeURIComponent(blockId)}`,
      {
        method: 'DELETE',
      },
      'deleteBlock'
    );
  }

  /** Add a member segment to either end of a block's run. */
  async addSegmentToBlock(blockId: string, segmentId: string): Promise<any> {
    return this._request(
      `/blocks/${encodeURIComponent(blockId)}/segments`,
      {
        method: 'POST',
        body: { segment_id: segmentId },
      },
      'addSegmentToBlock'
    );
  }

  /**
   * Remove a member segment from either end of a block's run. The segment
   * returns to its own single-segment block; removing the last member
   * dissolves the block (response carries `block_dissolved`).
   */
  async removeSegmentFromBlock(blockId: string, segmentId: string): Promise<any> {
    return this._request(
      `/blocks/${encodeURIComponent(blockId)}/segments/${encodeURIComponent(segmentId)}`,
      {
        method: 'DELETE',
      },
      'removeSegmentFromBlock'
    );
  }

  /** Get current track voltage for a block. */
  async getBlockVoltage(blockId: string): Promise<any> {
    return this._request(
      `/blocks/${encodeURIComponent(blockId)}/voltage`,
      {},
      'getBlockVoltage'
    );
  }

  // === Signal Management ===

  /** Create a new signal. */
  async createSignal(
    x: number,
    y: number,
    detectionDirection: string,
    signalType = 'block',
    name: string | null = null,
    autoConnect = true
  ): Promise<any> {
    return this._request(
      '/signal',
      {
        method: 'POST',
        body: {
          x,
          y,
          detection_direction: detectionDirection,
          signal_type: signalType,
          name,
          auto_connect: autoConnect,
        },
      },
      'createSignal'
    );
  }

  /** Get a signal by ID. */
  async getSignal(signalId: string): Promise<any> {
    return this._request(`/signal/${encodeURIComponent(signalId)}`, {}, 'getSignal');
  }

  /** Get a signal at specific coordinates. */
  async getSignalAt(x: number, y: number): Promise<any> {
    return this._request(
      `/signal/${x}/${y}`,
      {
        returnNullOn404: true,
      },
      'getSignalAt'
    );
  }

  /** Update signal properties. */
  async updateSignal(signalId: string, updates: unknown): Promise<any> {
    return this._request(
      `/signal/${encodeURIComponent(signalId)}`,
      {
        method: 'PATCH',
        body: updates,
      },
      'updateSignal'
    );
  }

  /** Delete a signal. */
  async deleteSignal(signalId: string): Promise<any> {
    return this._request(
      `/signal/${encodeURIComponent(signalId)}`,
      {
        method: 'DELETE',
      },
      'deleteSignal'
    );
  }

  /** Delete signal at specific coordinates. */
  async deleteSignalAt(x: number, y: number): Promise<any> {
    return this._request(
      `/signal/${x}/${y}`,
      {
        method: 'DELETE',
      },
      'deleteSignalAt'
    );
  }

  /** Validate signal placement. */
  async validateSignalPlacement(x: number, y: number): Promise<any> {
    return this._request(`/signal/validate/${x}/${y}`, {}, 'validateSignalPlacement');
  }

  /** Get blocks reachable from a signal. */
  async getReachableBlocks(signalId: string): Promise<any> {
    return this._request(
      `/signal/${encodeURIComponent(signalId)}/reachable-blocks`,
      {},
      'getReachableBlocks'
    );
  }

  // === Station API Methods ===

  // === Platform API Methods (new naming, same physical entity) ===

  async createPlatform(data: unknown): Promise<any> {
    return this._request(
      '/platform',
      {
        method: 'POST',
        body: data,
      },
      'createPlatform'
    );
  }

  async getPlatform(platformId: string): Promise<any> {
    return this._request(
      `/platform/${encodeURIComponent(platformId)}`,
      {
        returnNullOn404: true,
      },
      'getPlatform'
    );
  }

  async updatePlatform(platformId: string, updates: unknown): Promise<any> {
    return this._request(
      `/platform/${encodeURIComponent(platformId)}`,
      {
        method: 'PATCH',
        body: updates,
      },
      'updatePlatform'
    );
  }

  async deletePlatform(platformId: string): Promise<any> {
    return this._request(
      `/platform/${encodeURIComponent(platformId)}`,
      {
        method: 'DELETE',
      },
      'deletePlatform'
    );
  }

  async validatePlatformPlacement(x: number, y: number): Promise<any> {
    return this._request(`/platform/validate/${x}/${y}`, {}, 'validatePlatformPlacement');
  }

  async extendPlatform(platformId: string, positions: unknown): Promise<any> {
    return this._request(
      `/platform/${encodeURIComponent(platformId)}/extend`,
      {
        method: 'POST',
        body: { positions },
      },
      'extendPlatform'
    );
  }

  async removePlatformTile(platformId: string, x: number, y: number): Promise<any> {
    return this._request(
      `/platform/${encodeURIComponent(platformId)}/remove_tile`,
      {
        method: 'POST',
        body: { x, y },
      },
      'removePlatformTile'
    );
  }

  async assignPlatformTileToBlock(platformId: string, x: number, y: number, blockId: string): Promise<any> {
    return this._request(
      `/platform/${encodeURIComponent(platformId)}/assign_tile`,
      {
        method: 'POST',
        body: { x, y, block_id: blockId },
      },
      'assignPlatformTileToBlock'
    );
  }

  /** Legacy compatibility wrapper: get physical platform by ID. */
  async getStation(stationId: string): Promise<any> {
    return this._request(
      `/platform/${encodeURIComponent(stationId)}`,
      {
        returnNullOn404: true,
      },
      'getStation'
    );
  }

  // === Logical Station Group API Methods ===

  async createStationGroup(data: unknown): Promise<any> {
    return this._request(
      '/station',
      {
        method: 'POST',
        body: data,
      },
      'createStationGroup'
    );
  }

  async listStationGroups(): Promise<any> {
    return this._request('/station', {}, 'listStationGroups');
  }

  async deleteStationGroup(stationId: string): Promise<any> {
    return this._request(
      `/station/${encodeURIComponent(stationId)}`,
      {
        method: 'DELETE',
      },
      'deleteStationGroup'
    );
  }

  async updateStationGroup(stationId: string, patch: unknown): Promise<any> {
    return this._request(
      `/station/${encodeURIComponent(stationId)}`,
      {
        method: 'PATCH',
        body: patch,
      },
      'updateStationGroup'
    );
  }

  async addPlatformToStation(
    stationId: string,
    platformId: string,
    force = false
  ): Promise<any> {
    return this._request(
      `/station/${encodeURIComponent(stationId)}/add-platform`,
      {
        method: 'POST',
        body: { platform_id: platformId, force },
      },
      'addPlatformToStation'
    );
  }

  async removePlatformFromStation(stationId: string, platformId: string): Promise<any> {
    return this._request(
      `/station/${encodeURIComponent(stationId)}/remove-platform`,
      {
        method: 'POST',
        body: { platform_id: platformId },
      },
      'removePlatformFromStation'
    );
  }

  /** Get a specific train. */
  async getTrain(trainId: string): Promise<any> {
    return this._request(`/trains/${encodeURIComponent(trainId)}`, {}, 'getTrain');
  }

  /** Get train lookahead data for the train monitor. */
  async getTrainLookahead(trainId: string): Promise<any> {
    return this._request(`/trains/${encodeURIComponent(trainId)}/lookahead`, {}, 'getTrainLookahead');
  }

  /**
   * Set a ghost train up as a real, driveable train.
   *
   * The ghost keeps its blocks and its controller; the body supplies the
   * identity, consist and facing the detector could never know (design_new.md
   * "Ghost trains", Adoption). The adopted train's id is usually not the one in
   * the URL, so read it back from the response.
   */
  async adoptGhostTrain(
    trainId: string,
    body: {
      new_train_id: string;
      length_cm: number;
      max_speed: number;
      direction: string;
      head_tile?: { x: number; y: number };
      cab?: string;
      car_number?: string;
      train_type?: string;
      speed_profile_id?: string;
    }
  ): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/adopt`,
      {
        method: 'POST',
        body,
      },
      'adoptGhostTrain'
    );
  }

  /**
   * Explain why a train is not moving.
   *
   * Returns `{blocking_gate, gates: [{name, ok, detail, fix}]}` with the gates
   * in the order they actually bite — `blocking_gate` names the first shut one,
   * which is the only one worth fixing.
   */
  async getWhyStopped(trainId: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/why-stopped`,
      {},
      'getWhyStopped'
    );
  }

  /**
   * Release a fatal-conflict halt, leaving every train off.
   *
   * The operator has sorted the conflict out on the layout; this reopens the
   * gate so the session carries on instead of demanding a process restart that
   * would throw the whole layout state away.
   */
  async clearFatalConflict(): Promise<any> {
    return this._request(
      '/runtime/clear-fatal',
      {
        method: 'POST',
      },
      'clearFatalConflict'
    );
  }

  // === Train Control API ===

  /** Turn train power on. */
  async trainPowerOn(trainId: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/power-on`,
      {
        method: 'POST',
      },
      'trainPowerOn'
    );
  }

  /**
   * Turn train power off.
   *
   * In v1 power-off is latched: ``power_off_cmd()`` writes
   * ``phase_latches.power_off`` (and ``phase_latches.path_cancel_request``
   * when there is a live path), and Phase 3 commits the OFF transition on
   * a later tick. The response indicates the request was accepted; callers
   * must observe the controller sibling's ``power_on === false`` (e.g. via
   * ``stateManager.getController(trainId).power_on``) on a subsequent fetch
   * before treating the train as actually OFF.
   */
  async trainPowerOff(trainId: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/power-off`,
      {
        method: 'POST',
      },
      'trainPowerOff'
    );
  }

  /** Set active cab ('A' or 'B'). */
  async trainSetCab(trainId: string, cab: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/set-cab`,
      {
        method: 'POST',
        body: { cab },
      },
      'trainSetCab'
    );
  }

  // === OnboardComputer API (New Train Control Architecture) ===

  /**
   * Get OnboardComputer state for a train.
   * This is the single source of truth for train control state.
   */
  async getOnboardState(trainId: string): Promise<any> {
    return this._request(`/trains/${encodeURIComponent(trainId)}/onboard`, {}, 'getOnboardState');
  }

  /**
   * Set throttle/brake handle position.
   * @param position - Handle position: P1-P5, N, B1-B8, EB
   */
  async setHandle(trainId: string, position: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/handle`,
      {
        method: 'POST',
        body: { position },
      },
      'setHandle'
    );
  }

  /**
   * Release the emergency brake (canonical eb.release()).
   * The handle never releases EB; this is the only way to clear the EB latch.
   * Latch-gated: fails until the train is stopped and the EB cancel committed.
   */
  async releaseEmergency(trainId: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/release-emergency`,
      { method: 'POST' },
      'releaseEmergency'
    );
  }

  /**
   * Set control mode (ATO or Manual) via OnboardComputer.
   * @param mode - 'ato' or 'manual'
   */
  async setControlMode(trainId: string, mode: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/mode`,
      {
        method: 'POST',
        body: { mode },
      },
      'setControlMode'
    );
  }

  /**
   * Set manual-mode submode for a train.
   * @param modeType - 'simulation' (handle) or 'power_percent'
   */
  async setManualModeType(trainId: string, modeType: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/manual-mode-type`,
      {
        method: 'POST',
        body: { mode_type: modeType },
      },
      'setManualModeType'
    );
  }

  /**
   * Set manual power-percent command (0..100) when manual_mode_type == power_percent.
   * @param percent - Integer 0..100
   */
  async setManualPowerPercent(trainId: string, percent: number): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/manual-power-percent`,
      {
        method: 'POST',
        body: { percent },
      },
      'setManualPowerPercent'
    );
  }

  /** Start ATO movement via OnboardComputer. */
  async atoGo(trainId: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/ato-go`,
      {
        method: 'POST',
      },
      'atoGo'
    );
  }

  /** Stop ATO movement (service stop) via OnboardComputer. */
  async atoStop(trainId: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/ato-stop`,
      {
        method: 'POST',
      },
      'atoStop'
    );
  }

  // === Layout Persistence API ===

  /** Get list of saved layouts. */
  async getLayouts(): Promise<any> {
    const result = await this._request('/layouts', {}, 'getLayouts');
    return result.layouts || [];
  }

  /** Save current layout. */
  async saveLayout(name: string, description = ''): Promise<any> {
    return this._request(
      '/layouts',
      {
        method: 'POST',
        body: { name, description },
      },
      'saveLayout'
    );
  }

  /** Load a saved layout. */
  async loadLayout(name: string): Promise<any> {
    return this._request(
      `/layouts/${encodeURIComponent(name)}/load`,
      {
        method: 'POST',
      },
      'loadLayout'
    );
  }

  /** Delete a saved layout. */
  async deleteLayout(name: string): Promise<any> {
    return this._request(
      `/layouts/${encodeURIComponent(name)}`,
      {
        method: 'DELETE',
      },
      'deleteLayout'
    );
  }

  /** Clear the entire network. */
  async clearNetwork(): Promise<any> {
    return this._request(
      '/network/clear',
      {
        method: 'POST',
      },
      'clearNetwork'
    );
  }

  /** Get network statistics. */
  async getNetworkStats(): Promise<any> {
    return this._request('/network/stats', {}, 'getNetworkStats');
  }

  // === Scale Configuration API ===

  /** Get current scale configuration. */
  async getScale(): Promise<any> {
    return this._request('/scale', {}, 'getScale');
  }

  /** Set custom scale (e.g. 160). */
  async setScale(scale: number): Promise<any> {
    return this._request(
      '/scale',
      {
        method: 'PUT',
        body: { scale },
      },
      'setScale'
    );
  }

  // === Path Dispatcher API ===

  /**
   * Create a path for a train.
   *
   * The starting block is implicit (the train's current head block); the
   * server only needs the destination and direction.
   */
  async createPath({
    train_id,
    requested_destination_block_id,
    requested_destination_direction = 'forward',
    acquisition_mode = 'all_or_nothing',
  }: {
    train_id?: string;
    requested_destination_block_id?: string;
    requested_destination_direction?: string;
    acquisition_mode?: string;
  } = {}): Promise<any> {
    return this._request(
      '/paths',
      {
        method: 'POST',
        body: {
          train_id,
          requested_destination_block_id,
          requested_destination_direction,
          acquisition_mode,
        },
      },
      'createPath'
    );
  }

  /**
   * Get list of active paths.
   * @param filters - Optional filters: { train_id, block_id, status }
   */
  async getPaths(
    filters: { train_id?: string; block_id?: string; status?: string } = {}
  ): Promise<any> {
    const params = new URLSearchParams();
    if (filters.train_id) params.append('train_id', filters.train_id);
    if (filters.block_id) params.append('block_id', filters.block_id);
    if (filters.status) params.append('status', filters.status);

    const queryString = params.toString() ? `?${params}` : '';
    const result = await this._request(`/paths${queryString}`, {}, 'getPaths');
    return result.paths || [];
  }

  /** Get details of a specific path. */
  async getPath(pathId: string): Promise<any> {
    return this._request(`/paths/${encodeURIComponent(pathId)}`, {}, 'getPath');
  }

  /**
   * Request cancellation of a path.
   *
   * In v1 the backend latches API-driven cancellations: when the path has
   * a live train, the response resolves with ``status: "queued"`` once the
   * pending_path_cancel_request latch is written. The path is not actually
   * removed and the train has not yet stopped at that point — Phase 3 of
   * the simulation tick commits the cancel after the train brakes to a
   * stop. Callers should refresh from /api/paths or /api/network on the
   * next tick rather than mutating local state synchronously.
   *
   * Paths with no assigned train still cancel synchronously and resolve
   * with a CancellationResult (success, path_id, cancellation_kind,
   * reason, released_block_ids, released_switch_ids, train_id, notes).
   */
  async cancelPath(pathId: string): Promise<any> {
    return this._request(
      `/paths/${encodeURIComponent(pathId)}`,
      {
        method: 'DELETE',
      },
      'cancelPath'
    );
  }

  /** Extend an existing path to a new destination. */
  async extendPath(
    pathId: string,
    {
      requested_destination_block_id,
      requested_destination_direction = 'forward',
      acquisition_mode = 'all_or_nothing',
    }: {
      requested_destination_block_id?: string;
      requested_destination_direction?: string;
      acquisition_mode?: string;
    } = {}
  ): Promise<any> {
    return this._request(
      `/paths/${encodeURIComponent(pathId)}/extend`,
      {
        method: 'PUT',
        body: {
          requested_destination_block_id,
          requested_destination_direction,
          acquisition_mode,
        },
      },
      'extendPath'
    );
  }

  /** Get all blocks reachable from a starting block (topological, any
   * direction, no train). Used by the route editor's feasibility highlighting. */
  async getReachableEndpoints(blockId: string, includeStations = true): Promise<any> {
    return this._request(
      `/dispatcher/reachable/${encodeURIComponent(blockId)}?include_stations=${includeStations}`,
      {},
      'getReachableEndpoints'
    );
  }

  /**
   * Span-aware reachable endpoints for a placed train: where it can actually
   * go forward out of its head or in reverse out of its tail, never routing
   * back through its own body. Used by the Quick Route tool.
   */
  async getReachableEndpointsForTrain(trainId: string, includeStations = true): Promise<any> {
    return this._request(
      `/dispatcher/reachable-for-train/${encodeURIComponent(trainId)}?include_stations=${includeStations}`,
      {},
      'getReachableEndpointsForTrain'
    );
  }

  /**
   * Get all blocks reachable from a path's end in the FORWARD direction only.
   * Used for path extension to ensure users can only extend in the direction
   * the path is already heading (no backward extension).
   */
  async getPathReachableExtensions(pathId: string, includeStations = true): Promise<any> {
    return this._request(
      `/paths/${encodeURIComponent(pathId)}/reachable-extensions?include_stations=${includeStations}`,
      {},
      'getPathReachableExtensions'
    );
  }

  // === Switch Management API ===

  /** Get every switch on the layout. */
  async getSwitches(): Promise<any> {
    return this._request('/switches', {}, 'getSwitches');
  }

  /** Get a specific switch. */
  async getSwitch(switchId: string): Promise<any> {
    return this._request(`/switches/${encodeURIComponent(switchId)}`, {}, 'getSwitch');
  }

  /** Update a switch. */
  async updateSwitch(switchId: string, updates: unknown): Promise<any> {
    return this._request(
      `/switches/${encodeURIComponent(switchId)}`,
      {
        method: 'PUT',
        body: updates,
      },
      'updateSwitch'
    );
  }

  /** Delete a switch. */
  async deleteSwitch(switchId: string): Promise<any> {
    return this._request(
      `/switches/${encodeURIComponent(switchId)}`,
      {
        method: 'DELETE',
      },
      'deleteSwitch'
    );
  }

  /**
   * Change switch state (MAIN=0, DIVERGE=1).
   * Fails with 409 Conflict if switch is locked by a path.
   */
  async setSwitchState(switchId: string, state: number): Promise<any> {
    return this._request(
      `/switches/${encodeURIComponent(switchId)}/state`,
      {
        method: 'PUT',
        body: { state },
      },
      'setSwitchState'
    );
  }

  // === Train Routes (Route Editor) ===

  /** Get all train routes. */
  async getRoutes(): Promise<any> {
    return this._request('/train-routes', {}, 'getRoutes');
  }

  /** Get a specific route. */
  async getRoute(routeId: string): Promise<any> {
    return this._request(`/train-routes/${encodeURIComponent(routeId)}`, {}, 'getRoute');
  }

  /** Create a new route. */
  async createRoute(routeData: unknown): Promise<any> {
    return this._request(
      '/train-routes',
      {
        method: 'POST',
        body: routeData,
      },
      'createRoute'
    );
  }

  /** Update a route. */
  async updateRoute(routeId: string, updates: unknown): Promise<any> {
    return this._request(
      `/train-routes/${encodeURIComponent(routeId)}`,
      {
        method: 'PUT',
        body: updates,
      },
      'updateRoute'
    );
  }

  /** Delete a route. */
  async deleteRoute(routeId: string): Promise<any> {
    return this._request(
      `/train-routes/${encodeURIComponent(routeId)}`,
      {
        method: 'DELETE',
      },
      'deleteRoute'
    );
  }

  /** Activate a route. */
  async activateRoute(routeId: string): Promise<any> {
    return this._request(
      `/train-routes/${encodeURIComponent(routeId)}/activate`,
      {
        method: 'POST',
      },
      'activateRoute'
    );
  }

  /** Deactivate a route. */
  async deactivateRoute(routeId: string): Promise<any> {
    return this._request(
      `/train-routes/${encodeURIComponent(routeId)}/deactivate`,
      {
        method: 'POST',
      },
      'deactivateRoute'
    );
  }

  /** Get route execution status. */
  async getRouteStatus(routeId: string): Promise<any> {
    return this._request(`/train-routes/${encodeURIComponent(routeId)}/status`, {}, 'getRouteStatus');
  }

  /**
   * Check viability between consecutive route-task target pairs.
   * @param pairs - Array of pair objects. Each pair is either
   *   ``{from, to, direction}`` (preferred) or the legacy ``[from, to]``
   *   list form (server treats that as direction="either").
   */
  async checkViability(pairs: unknown): Promise<any> {
    return this._request(
      '/train-routes/check-viability',
      {
        method: 'POST',
        body: { pairs },
      },
      'checkViability'
    );
  }

  /**
   * Blocks/stations reachable from a route-task target (for feasibility
   * highlighting when building waypoints).
   * @param type - 'block' | 'platform' | 'station'
   */
  async getReachableFrom(type: string, id: string): Promise<any> {
    return this._request(
      `/train-routes/reachable-from?type=${encodeURIComponent(type)}&id=${encodeURIComponent(id)}`,
      {},
      'getReachableFrom'
    );
  }

  /**
   * Read-only run history for a route (activation time + per-run task
   * completion timestamps), aggregated across all trains.
   */
  async getRouteHistory(routeId: string): Promise<any> {
    return this._request(`/train-routes/${encodeURIComponent(routeId)}/history`, {}, 'getRouteHistory');
  }

  /**
   * List every `RouteRun` across every route — live and historical.
   * Returns one summary per run (run_number, train, route, state,
   * direction, started_at, task counts, last_error).
   */
  async getRouteRuns(): Promise<any> {
    const result = await this._request('/route-runs', {}, 'getRouteRuns');
    return result?.route_runs || [];
  }

  /**
   * Get a single `RouteRun` by `run_number`, with per-task detail
   * (task_index, kind, target, resolved target, started/finished, delay
   * class, last_error).
   */
  async getRouteRun(runNumber: number): Promise<any> {
    return this._request(`/route-runs/${runNumber}`, {}, 'getRouteRun');
  }

  /**
   * Patch one or more per-run overrides on a currently RUNNING RouteRun.
   * Per design_new.md "RouteRun overrides" canonical home: accepts the
   * single-field shape `{field: value}` or the batched shape
   * `{overrides: {field: value, ...}}`.
   */
  async patchRouteRunOverrides(runNumber: number, body: unknown): Promise<any> {
    return this._request(
      `/route-runs/${runNumber}/overrides`,
      {
        method: 'PATCH',
        body,
      },
      'patchRouteRunOverrides'
    );
  }

  /**
   * Clear one per-run override on a currently RUNNING RouteRun. Restores
   * the shared-route value for that field via the effective_* helpers.
   *
   * @param field - One of `priority` / `acquisition_mode`.
   */
  async clearRouteRunOverride(runNumber: number, field: string): Promise<any> {
    return this._request(
      `/route-runs/${runNumber}/overrides/${encodeURIComponent(field)}`,
      { method: 'DELETE' },
      'clearRouteRunOverride'
    );
  }

  /**
   * Assign/unassign a train to/from a route.
   * @param routeId - Route ID or null to unassign
   * @param options.startMode - Optional per-assignment start mode
   *     ('first' | 'nearest' | 'specific'); omit to use the route default.
   * @param options.startTaskIndex - Stop index when startMode is 'specific'.
   */
  async assignRoute(
    trainId: string,
    routeId: string | null,
    options: { force?: boolean; startMode?: string; startTaskIndex?: number | null } = {}
  ): Promise<any> {
    const body: Record<string, unknown> = {
      route_id: routeId,
      force: options.force || false,
    };
    if (options.startMode !== undefined) {
      body.start_mode = options.startMode;
    }
    if (options.startTaskIndex !== undefined) {
      body.start_task_index = options.startTaskIndex;
    }
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/assign-route`,
      {
        method: 'PUT',
        body,
      },
      'assignRoute'
    );
  }

  async listTunnels(): Promise<any> {
    return this._request('/tunnels', {}, 'listTunnels');
  }

  async createTunnel(a: unknown, b: unknown): Promise<any> {
    return this._request(
      '/tunnels',
      {
        method: 'POST',
        body: { a, b },
      },
      'createTunnel'
    );
  }

  async deleteTunnel(x: number, y: number, edge: string): Promise<any> {
    return this._request(
      '/tunnels',
      {
        method: 'DELETE',
        body: { x, y, edge },
      },
      'deleteTunnel'
    );
  }

  // === Speed Profiles ===

  async getSpeedProfiles(): Promise<any[]> {
    const result = await this._request('/speed-profiles', {}, 'getSpeedProfiles');
    return result?.profiles || [];
  }

  async getSpeedProfile(profileId: string): Promise<any> {
    return this._request(
      `/speed-profiles/${encodeURIComponent(profileId)}`,
      {},
      'getSpeedProfile'
    );
  }

  async createSpeedProfile(data: unknown): Promise<any> {
    return this._request(
      '/speed-profiles',
      {
        method: 'POST',
        body: data,
      },
      'createSpeedProfile'
    );
  }

  async updateSpeedProfile(profileId: string, data: unknown): Promise<any> {
    return this._request(
      `/speed-profiles/${encodeURIComponent(profileId)}`,
      {
        method: 'PUT',
        body: data,
      },
      'updateSpeedProfile'
    );
  }

  async deleteSpeedProfile(profileId: string): Promise<any> {
    return this._request(
      `/speed-profiles/${encodeURIComponent(profileId)}`,
      {
        method: 'DELETE',
      },
      'deleteSpeedProfile'
    );
  }
}

// The single API client instance — imported as `{ api }` by the app, panels,
// and editor.
export const api = new API();
