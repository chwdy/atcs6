import { describe, it, expect } from 'vitest';

import { resolveActiveGroup, type ActiveChainCandidate } from './tab-group';

// The chain is ordered most-recently-interacted first. A candidate only has to
// answer isPinned(), so these are plain objects — no TabGroup, no DOM (this
// project's vitest has no jsdom; see window-manager.test.ts for the same
// constraint).
function group(name: string, pinned = false): ActiveChainCandidate & { name: string } {
  return { name, isPinned: () => pinned };
}

describe('resolveActiveGroup — which group new tabs land in', () => {
  it('picks the most recently interacted group', () => {
    const recent = group('recent');
    expect(resolveActiveGroup([recent, group('older'), group('oldest')])).toBe(recent);
  });

  it('skips a pinned group and falls through to the one behind it', () => {
    const behind = group('behind');
    expect(resolveActiveGroup([group('pinned', true), behind])).toBe(behind);
  });

  it('skips a whole run of pinned groups', () => {
    const survivor = group('survivor');
    const chain = [group('p1', true), group('p2', true), group('p3', true), survivor];
    expect(resolveActiveGroup(chain)).toBe(survivor);
  });

  it('returns null when every group is pinned — the chain is exhausted', () => {
    // The caller reads this as "same as having no window at all" and spawns a
    // new group rather than dropping the panel on a pinned one.
    expect(resolveActiveGroup([group('p1', true), group('p2', true)])).toBeNull();
  });

  it('returns null for an empty chain', () => {
    expect(resolveActiveGroup([])).toBeNull();
  });

  it('does not treat a pinned group as active even when it is the most recent', () => {
    // Interacting with a pinned group (scrolling it, clicking inside it) must
    // not make it the target — that is the whole point of pinning.
    const unpinned = group('unpinned');
    expect(resolveActiveGroup([group('justClicked', true), unpinned])).toBe(unpinned);
  });
});
