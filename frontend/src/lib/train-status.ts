/**
 * Collapse the train's status into one display token.
 *
 * The overall status is `controller.train_status` (off / manual / ato / eb /
 * calibrating). An ATO train additionally carries its ATO-motion detail in the
 * read model's `ato_status` (ready / running / held / dwelling), served in
 * `operator_status_by_train_id`, so an ATO train displays that detail while
 * every other train displays its overall status. Mirrors design_new.md
 * "Controller state machine" and "ATO status".
 */
export function trainDisplayStatus(controller: any, operatorStatus?: any): string {
  const trainStatus = controller?.train_status || 'off';
  // `ato_status` values are upper case (`READY`, `RUNNING`, ...); display
  // tokens are lower case like `train_status`.
  if (trainStatus === 'ato') return String(operatorStatus?.ato_status || 'ready').toLowerCase();
  return trainStatus;
}

/**
 * Whether ATO is driving the train: it reads `train_status == ato` and its
 * `ato_status` is `RUNNING`, `HELD` or `DWELLING` — engaged with a running
 * route run (design_new.md "ATO status"). Engaged alone is not enough: a train
 * whose one-off route has finished stays engaged but reads `READY`.
 */
export function atoIsDriving(controller: any, operatorStatus?: any): boolean {
  if ((controller?.train_status || 'off') !== 'ato') return false;
  const atoStatus = String(operatorStatus?.ato_status || '').toUpperCase();
  return atoStatus === 'RUNNING' || atoStatus === 'HELD' || atoStatus === 'DWELLING';
}

/**
 * Why ATO Go cannot be pressed right now, or null when it can.
 *
 * Mirrors design_new.md "ATO go": the server accepts Go only while the train
 * reads `train_status == ato` and has a route assigned. `assignedRouteId` is
 * the train's entry in the snapshot's `assigned_routes_by_train_id` (the route
 * executor owns it; the controller payload does not carry it). Go is also
 * pointless while ATO is already engaged.
 */
export function atoGoUnavailableReason(controller: any, assignedRouteId: unknown): string | null {
  if (!(controller?.power_on ?? false)) return 'Power is off';
  if ((controller?.train_status || 'off') !== 'ato') return 'Select ATO first';
  if (controller?.engaged ?? false) return 'ATO is already running';
  if (!assignedRouteId) return 'Assign a route first';
  return null;
}

/**
 * Why Start route cannot be pressed right now, or null when it can.
 *
 * Mirrors design_new.md "Start route": the server accepts it only when the
 * train is stopped, has no EB latched, has no calibration run open, has no
 * power-off pending, and has a route assigned. It powers an OFF train up,
 * selects ATO, and engages it, so it is also pointless on an ATO train that is
 * already engaged. `operatorStatus` is the train's entry in
 * `operator_status_by_train_id`; its pending actions say whether a power-off
 * is waiting.
 */
export function startRouteUnavailableReason(
  controller: any,
  assignedRouteId: unknown,
  operatorStatus?: any
): string | null {
  const trainStatus = controller?.train_status || 'off';
  if (!(controller?.is_stopped ?? true)) return 'Stop the train first';
  if (trainStatus === 'eb') return 'Release the emergency brake first';
  if (trainStatus === 'calibrating') return 'A calibration run is driving the train';
  const pending = operatorStatus?.pending_actions || [];
  if (pending.some((p: any) => p?.latch === 'power_off')) return 'A power-off is pending';
  if (!assignedRouteId) return 'Assign a route first';
  if (trainStatus === 'ato' && (controller?.engaged ?? false)) return 'The route is already running';
  return null;
}
