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
