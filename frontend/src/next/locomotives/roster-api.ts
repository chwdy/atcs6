/**
 * The Train Roster endpoints (routes/roster.py). `POST /api/roster` creates
 * an entry, or replaces the one with the same `entry_id`; when the entry
 * names no speed profile the server links it by model (design_new.md "Train
 * Roster", Profiles are matched by model).
 */
import { api } from '../../lib/api';
import type { RosterEntry } from './model';

export async function saveRosterEntry(body: Record<string, unknown>): Promise<RosterEntry> {
  const reply = await api._request('/roster', { method: 'POST', body }, 'saveRosterEntry');
  return reply.entry as RosterEntry;
}

export async function deleteRosterEntry(entryId: string): Promise<void> {
  await api._request(`/roster/${encodeURIComponent(entryId)}`, { method: 'DELETE' }, 'deleteRosterEntry');
}
