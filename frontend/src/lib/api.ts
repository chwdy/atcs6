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
import type { VirtualFaultRequest } from './virtual-hardware-faults';

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
// the running requests it also sends.
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

  /** With `layoutVersion`, the reply leaves the layout out while it still
   *  matches (see lib/network-poll.ts). Without it, the full network. */
  async getNetwork(layoutVersion?: string): Promise<any> {
    const query = layoutVersion ? `?layout_version=${encodeURIComponent(layoutVersion)}` : '';
    return this._request(`/network${query}`, {}, 'getNetwork');
  }

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

  async _trainRequest(path: string, method: string, body?: unknown): Promise<{ success: boolean; reason: string | null }> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    try {
      const data = await response.json();
      return { success: !!data.success, reason: data.reason ?? data.error ?? null };
    } catch {
      return { success: false, reason: `HTTP ${response.status}` };
    }
  }

  setTrainKnob(trainId: string, volts: number) {
    return this._trainRequest(`/trains/${encodeURIComponent(trainId)}/knob`, 'PUT', { volts });
  }

  stopRunningTrain(trainId: string) {
    return this._trainRequest(`/trains/${encodeURIComponent(trainId)}/stop`, 'POST');
  }

  reverseRunningTrain(trainId: string) {
    return this._trainRequest(`/trains/${encodeURIComponent(trainId)}/reverse`, 'POST');
  }

  removeRunningTrain(trainId: string) {
    return this._trainRequest(`/trains/${encodeURIComponent(trainId)}`, 'DELETE');
  }

  stopAllTrains() {
    return this._trainRequest('/trains/stop-all', 'POST');
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
   * Read the saved Hardware Setup (design_v2.md "Hardware setup"):
   * `{ config: {controller, chips, block_track, block_occupancy,
   * switch_output, ...} }`. Each chip is enriched with its derived role and
   * port count.
   */
  async getHardwareConfig(): Promise<any> {
    return this._request('/hardware/config', {}, 'getHardwareConfig');
  }

  async getOperationEnvironment(): Promise<any> {
    return this._request('/operation/environment', {}, 'getOperationEnvironment');
  }

  async selectOperationEnvironment(selected: string): Promise<any> {
    return this._request(
      '/operation/environment',
      { method: 'PUT', body: { selected } },
      'selectOperationEnvironment'
    );
  }

  async getOperationReadiness(): Promise<any> {
    return this._request('/operation/readiness', {}, 'getOperationReadiness');
  }

  async activateOperationEnvironment(): Promise<any> {
    return this._request(
      '/operation/activate',
      { method: 'POST', body: {} },
      'activateOperationEnvironment'
    );
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
   * On a PWM output the pulse is held long enough to read on a multimeter (the
   * server's PORT_TEST_PULSE_S, currently 1 s), and `pulseS` overrides that
   * width. On a '595 bit the pulse is the switch-coil width (10 ms), and the
   * server refuses a `pulseS` above its coil limit (200 ms), because the bit may
   * be wired to a coil. The server echoes back the width used.
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

  /** Take the controller for the occupancy bench: deactivate the environment,
   * hand the device over, and build the bench's own track + detector interfaces
   * on it. `closeOccupancyBench` puts both back. */
  async openOccupancyBench(): Promise<any> {
    return this._request('/hardware/bench/open', { method: 'POST' }, 'openOccupancyBench');
  }

  /** De-energize, release the controller, and reactivate the environment the
   * bench took. Answers `{reactivated, issues}`: false with a populated `issues`
   * is a rebuild that was blocked, and the only place the operator can hear it —
   * the panel is closing and the header will simply read inactive. */
  async closeOccupancyBench(): Promise<any> {
    return this._request('/hardware/bench/close', { method: 'POST' }, 'closeOccupancyBench');
  }

  /** Check one block's wiring on the open bench: read every block at rest,
   * pulse this one at `current_sense_min_drive_v`, and report whether its
   * detectors followed the pulse and whether any other block's did. */
  async checkOccupancyBenchBlock(blockId: string): Promise<any> {
    return this._request(
      '/hardware/bench/check-block',
      { method: 'POST', body: { block_id: blockId } },
      'checkOccupancyBenchBlock'
    );
  }

  /** Both occupancy detectors for every bound block, plus what the bench is
   * driving. Answers `{open: false}` rather than erroring when no bench holds
   * the device, so the panel can poll one endpoint either way. */
  async getOccupancyBenchState(options?: RequestOptions): Promise<any> {
    return this._request('/hardware/bench/state', options ?? {}, 'getOccupancyBenchState');
  }

  /** Start / stop the bench drive. `flipped` REPLACES the stored set — send the
   * whole set every time, never a delta. */
  async setOccupancyBenchDrive(
    running: boolean,
    percent: number,
    flipped: string[]
  ): Promise<any> {
    return this._request(
      '/hardware/bench/drive',
      { method: 'POST', body: { running, percent, flipped } as any },
      'setOccupancyBenchDrive'
    );
  }

  /** Judge every bench current channel against one threshold in mA, or null to
   * put each block back on its own configured value. Reads only — the rails are
   * not re-commanded, and the hardware configuration is not written. */
  async setOccupancyBenchThreshold(thresholdMa: number | null): Promise<any> {
    return this._request(
      '/hardware/bench/threshold',
      { method: 'POST', body: { threshold_ma: thresholdMa } as any },
      'setOccupancyBenchThreshold'
    );
  }

  /** Start every bench current channel's running peak over. The bench already
   * resets them whenever the drive changes; this is for a change on the layout
   * — a loco lifted off, a block rewired — that the server cannot see. */
  async resetOccupancyBenchPeaks(): Promise<any> {
    return this._request(
      '/hardware/bench/peaks/reset',
      { method: 'POST' },
      'resetOccupancyBenchPeaks'
    );
  }

  /** Read the operator-facing event log — the one NotificationCenter stream
   * (design_v2.md "Event log"). `query` is the already-encoded filter string
   * built by the viewer (level / source / operation / entity / free text /
   * id watermark); see GET /api/event-log for the full parameter set.
   *
   * A read only: it acknowledges nothing and empties nothing. */
  async getEventLog(query = '', options?: RequestOptions): Promise<any> {
    const suffix = query ? `?${query}` : '';
    return this._request(`/event-log${suffix}`, options ?? {}, 'getEventLog');
  }

  /** Set (or clear, when chipId is null) one port binding, keyed by logical id.
   * `extra` carries the current map's per-channel calibration (baseline, threshold and
   * driven threshold mA). */
  async updateHardwareBinding(
    map: string,
    logicalId: string,
    chipId: string | null,
    port: number | null,
    extra?: {
      baseline_ma?: number | null;
      threshold_ma?: number | null;
      driven_threshold_ma?: number | null;
    }
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

  /** Set (or clear, when blockId is null) the block whose feed powers one
   * switch's rails. A switch has no track channel of its own — its rails are
   * wired to the block at its main entrance and carry that block's voltage. */
  async updateSwitchTrackSource(switchId: string, blockId: string | null): Promise<any> {
    return this._request(
      '/hardware/binding',
      {
        method: 'PUT',
        body: {
          map: 'switch_track_source',
          logical_id: switchId,
          block_id: blockId,
        },
      },
      'updateSwitchTrackSource'
    );
  }

  /** Enter Setup for the selected hardware environment (design_v2.md
   *  "Setup tools", Entering Setup). From Live it runs the layout emergency cut-out
   *  first; only Activate leaves Setup. */
  async enterSetup(): Promise<any> {
    return this._request('/operation/setup', { method: 'POST', body: {} }, 'enterSetup');
  }

  /** Open a held commissioning session on the controller (for live input /
   *  sweep). Accepted only in Setup. */
  async commissionOpen(config: any): Promise<any> {
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
   * an operating throw. Pass `pulseS` only for a bit no switch binding names
   * (the output sweep does, so an LED on the pin visibly lights). The server
   * refuses any width above its coil limit (200 ms). */
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
    return this._request(
      '/hardware/commission/close',
      { method: 'POST', body: {} },
      'commissionClose'
    );
  }

  /**
   * Develop mode (fake hardware) input-poke surface. These act on the LIVE
   * session's fake device, so the real occupancy pipeline reads the injected
   * state back on its next tick. Available only when the running session booted
   * on the fake device (`session.develop_active`); otherwise they answer 409.
   */

  /** Read the fake device's per-block / per-switch state for the dev panel. */
  async getHwDebugState(): Promise<any> {
    return this._request('/virtual-hardware', {}, 'getVirtualHardwareState');
  }

  /** Set a block's fake '165 occupancy bit. */
  async setHwDebugOccupancy(blockId: string, occupied: boolean): Promise<any> {
    return this._request(
      '/virtual-hardware/input/occupancy',
      { method: 'PUT', body: { block_id: blockId, occupied } },
      'setHwDebugOccupancy'
    );
  }

  /** Set a block's fake current-sense reading (mA); decides occupancy only
   * while the block is driven. */
  async setHwDebugCurrent(blockId: string, currentMa: number): Promise<any> {
    return this._request(
      '/virtual-hardware/input/current',
      { method: 'PUT', body: { block_id: blockId, current_ma: currentMa } },
      'setHwDebugCurrent'
    );
  }

  /** Put a virtual object on a block's detectors, or lift it off — every source
   *  the block is bound to, written together. This is what "place a train here"
   *  calls; the two raw setters above are for making the sources disagree. */
  async setHwDebugPresence(blockId: string, present: boolean): Promise<any> {
    return this._request(
      '/virtual-hardware/input/presence',
      { method: 'PUT', body: { block_id: blockId, present } },
      'setHwDebugPresence'
    );
  }

  /** Inject or clear one operator fault below the real HAL. `request` is a
   *  VirtualFaultRequest `{action, target_kind, target_id, operation?, fault?}`
   *  — build it with `injectFaultBody` / `clearFaultBody`. */
  async setVirtualHardwareFault(request: VirtualFaultRequest): Promise<any> {
    return this._request(
      '/virtual-hardware/fault',
      { method: 'PUT', body: request },
      'setVirtualHardwareFault'
    );
  }

  async clearVirtualHardwareFaults(): Promise<any> {
    return this._request(
      '/virtual-hardware/faults/clear',
      { method: 'POST', body: {} },
      'clearVirtualHardwareFaults'
    );
  }

  async getVirtualRigScenario(): Promise<any> {
    return this._request(
      '/virtual-hardware/scenario',
      {},
      'getVirtualRigScenario'
    );
  }

  async commandVirtualRigScenario(action: string, payload: any = {}): Promise<any> {
    return this._request(
      '/virtual-hardware/scenario',
      { method: 'POST', body: { action, ...payload } },
      'commandVirtualRigScenario'
    );
  }

  // === Segment Geometry (the geometry axis: /api/segments) ===
  //
  // A `Segment` owns geometry only — ordered tiles, length, min radius, grade
  // and speed limit. Occupancy / power / grouping live on its
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
   */
  async extendSegment(
    segmentId: string,
    positions: unknown,
    forceOverwrite = false
  ): Promise<any> {
    return this._request(
      `/segments/${encodeURIComponent(segmentId)}/extend`,
      {
        method: 'POST',
        body: { positions, force_overwrite: forceOverwrite },
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
  // A `Block` groups one or more segments and owns occupancy,
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

  // === Layouts ===

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

  // === Switches ===

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

  /** Turn alternate arrivals on or off for a switch (design_v2.md "Running a
   *  train", Alternate arrivals). */
  setSwitchAlternate(switchId: string, enabled: boolean) {
    return this._request(
      `/switches/${encodeURIComponent(switchId)}/alternate`,
      { method: 'PUT', body: { enabled } },
      'setSwitchAlternate',
    );
  }

  /** Throw a switch: MAIN=0, DIVERGE=1. Refused while it is held. */
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

  // === Tunnels ===

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
}

// The single API client instance — imported as `{ api }` by the app, panels,
// and editor.
export const api = new API();
