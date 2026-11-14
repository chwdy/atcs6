// setup-tool-refusals.ts — the plain words for the two refusals a Hardware
// Setup tool can get because of the session's one device handle
// (design_v2.md "Hardware interface", A tool that would take owned outputs is
// refused; "Setup tools", A device that does not open). The server names the
// refusal in the reply's `reason`; `api._request` turns it into the error
// message every Hardware Setup tool already shows.

const REFUSALS: Record<string, string> = {
  outputs_owned: 'Another tool is using the outputs — close it first',
  no_device: 'No controller connected',
};

/** The message to show for a refusal reply, or null when `body` carries no
 *  reason this module knows. */
export function setupToolRefusalMessage(body: unknown): string | null {
  if (!body || typeof body !== 'object') return null;
  const reason = (body as { reason?: unknown }).reason;
  if (typeof reason !== 'string') return null;
  return REFUSALS[reason] ?? null;
}
