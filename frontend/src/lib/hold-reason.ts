/**
 * Plain-language text for a stopped train's hold reason.
 *
 * The server assembles `hold_reason` in `operator_status_by_train_id` as
 * `{code, question, detail}`: the first row of design_new.md "Hold reason"
 * whose condition holds, or null while the train moves or when nothing holds
 * it. Every operator-facing explanation of a stopped train reads it, so this
 * file only turns a code into words; it never decides why a train is held.
 */

export interface HoldReason {
  code: string;
  question: number;
  detail: string | null;
}

const HALTED: Record<string, string> = {
  layout_emergency_cut_out: 'Emergency stop has cut every train out. Press Reset to release them.',
  hardware_failure:
    'A hardware failure has halted the session. Repair the device, then activate the environment again.',
  runtime_failure:
    'A fatal track conflict has halted the session. Sort it out on the layout, then clear the conflict.',
  not_activated: 'The environment is not activated. Activate it in Hardware setup to go live.',
};

const MESSAGES: Record<string, (detail: string | null) => string> = {
  halted: (detail) => HALTED[detail ?? ''] ?? 'The session is halted.',
  setup: () => 'The session is in Setup. Activate the environment to run trains.',
  paused: () => 'The session is paused. Press Play.',
  unidentified: () => 'This is an unidentified train. The app cannot drive it.',
  detection_lost: () => 'The detectors have lost this train. It accepts no command until it is detected again.',
  powered_off: () => 'The train is powered off.',
  emergency_cut_out: () => 'The emergency cut-out is latched. Release it once the train is safe.',
  emergency_brake: () => 'The emergency brake is latched. Release it once the train has stopped.',
  calibrating: (detail) => `A ${detail ?? ''} calibration run is driving the train.`.replace('  ', ' '),
  ato_not_engaged: () => 'ATO is selected but not started. Press Go or Start route.',
  pending_action: (detail) => `Waiting: ${detail ?? 'a command is finishing'}.`,
  no_route: () => 'No route is assigned.',
  dwelling: () => 'Dwelling at a station.',
  // The detail is the `origin` code of the train's latest retained error,
  // e.g. `no_viable_path`; it reads as words once the underscores go.
  no_reservation: (detail) =>
    detail ? `No reservation yet (${detail.replace(/_/g, ' ')}).` : 'No reservation yet.',
  reservation_end: (detail) =>
    detail ? `Stopped at the end of its reservation (${detail}).` : 'Stopped at the end of its reservation.',
  atc_limit: (detail) => (detail ? `The ATC limit is 0 (${detail}).` : 'The ATC limit is 0.'),
  driver_idle: () => "The driver's input commands no power.",
};

/** The sentence for a hold reason, or null when there is none. */
export function holdReasonText(hold: HoldReason | null | undefined): string | null {
  if (!hold || !hold.code) return null;
  const message = MESSAGES[hold.code];
  return message ? message(hold.detail ?? null) : `Held: ${hold.code}.`;
}
