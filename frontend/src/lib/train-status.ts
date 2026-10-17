/**
 * Collapse the two-field controller status into one display token.
 *
 * The overall status is `controller.train_status` (off / manual / ato / eb). An
 * ATO train additionally carries its ATO-motion detail in the read-model
 * `controller.ato_status` (ready / running / held / dwelling), so an ATO train
 * displays that detail while every other train displays its overall status.
 * Mirrors design_new.md "Controller state machine" and "ATO status". Replaces
 * the removed single `controller.state`.
 */
export function trainDisplayStatus(controller: any): string {
  const trainStatus = controller?.train_status || 'off';
  if (trainStatus === 'ato') return controller?.ato_status || 'ready';
  return trainStatus;
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
