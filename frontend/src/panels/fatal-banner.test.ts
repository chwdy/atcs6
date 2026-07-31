import { describe, it, expect } from 'vitest';
import { describeFatalConflict, resolveConflictTiles, runtimeNoun } from './fatal-banner';

describe('runtimeNoun', () => {
  it('calls a session with real detectors an Operation, not a Simulation', () => {
    expect(runtimeNoun({ occupancy_is_real: true })).toBe('Operation');
  });

  it('calls a model-derived session a Simulation', () => {
    expect(runtimeNoun({ occupancy_is_real: false })).toBe('Simulation');
  });

  it('falls back to Simulation when the payload says nothing', () => {
    expect(runtimeNoun(null)).toBe('Simulation');
    expect(runtimeNoun({})).toBe('Simulation');
  });
});

describe('describeFatalConflict', () => {
  it('names both trains and the block for an occupancy conflict', () => {
    const text = describeFatalConflict({
      resource_type: 'block',
      resource_id: 'BLK_1',
      incoming_train_id: 'T1',
      prior_owner_train_id: 'GHOST_1',
      conflict_kind: 'occupied_by_other_train',
    });
    expect(text).toBe('Train T1 tried to enter block BLK_1, but train GHOST_1 already occupies it.');
  });
});

describe('resolveConflictTiles', () => {
  it('finds every tile of the conflicting block', () => {
    const network = {
      tracks: [
        { x: 0, y: 0, block_section_id: 'BLK_0' },
        { x: 1, y: 0, block_section_id: 'BLK_1' },
        { x: 2, y: 0, block_section_id: 'BLK_1' },
      ],
    };
    expect(resolveConflictTiles(network, { resource_id: 'BLK_1' })).toEqual([
      [1, 0],
      [2, 0],
    ]);
  });
});
