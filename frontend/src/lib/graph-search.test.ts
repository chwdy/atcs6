import { describe, it, expect } from 'vitest';
import { graphSearch, type SearchStep } from './graph-search';

// A tiny weighted directed graph over string nodes, so the engine is tested
// independent of any railway specifics.
type Adj = Record<string, Array<{ to: string; cost: number }>>;
const neighborsOf =
  (adj: Adj) =>
  (n: string): SearchStep<string>[] =>
    (adj[n] ?? []).map((e) => ({ node: e.to, cost: e.cost }));
const id = (n: string) => n;

describe('graphSearch engine', () => {
  it('Dijkstra: picks the cheapest path, not the fewest hops', () => {
    const adj: Adj = {
      A: [
        { to: 'X', cost: 10 }, // 1 hop, expensive
        { to: 'Y', cost: 1 },
      ],
      Y: [{ to: 'X', cost: 1 }], // 2 hops, cheap (total 2)
    };
    const { path } = graphSearch({ seeds: ['A'], key: id, neighbors: neighborsOf(adj), goal: (n) => n === 'X' });
    expect(path).toEqual(['A', 'Y', 'X']);
  });

  it('BFS-equivalence: uniform cost + no heuristic → fewest-hops path', () => {
    const adj: Adj = {
      A: [
        { to: 'B', cost: 1 },
        { to: 'E', cost: 1 },
      ],
      B: [{ to: 'C', cost: 1 }],
      C: [{ to: 'D', cost: 1 }],
      E: [{ to: 'D', cost: 1 }], // A-E-D (2 hops) beats A-B-C-D (3 hops)
    };
    const { path } = graphSearch({ seeds: ['A'], key: id, neighbors: neighborsOf(adj), goal: (n) => n === 'D' });
    expect(path).toEqual(['A', 'E', 'D']);
  });

  it('A*: a consistent heuristic yields the same optimal path as h=0', () => {
    const adj: Adj = {
      A: [
        { to: 'B', cost: 1 },
        { to: 'E', cost: 1 },
      ],
      B: [{ to: 'C', cost: 1 }],
      C: [{ to: 'D', cost: 1 }],
      E: [{ to: 'D', cost: 1 }],
    };
    const H: Record<string, number> = { A: 2, B: 2, C: 1, D: 0, E: 1 }; // consistent
    const withH = graphSearch({
      seeds: ['A'],
      key: id,
      neighbors: neighborsOf(adj),
      heuristic: (n) => H[n] ?? 0,
      goal: (n) => n === 'D',
    });
    expect(withH.path).toEqual(['A', 'E', 'D']);
  });

  it('seed is the goal: returns a single-node path', () => {
    const { path } = graphSearch({ seeds: ['A'], key: id, neighbors: () => [], goal: (n) => n === 'A' });
    expect(path).toEqual(['A']);
  });

  it('no path: unreachable goal → null path, visited = explored set', () => {
    const adj: Adj = { A: [{ to: 'B', cost: 1 }] };
    const { path, visited } = graphSearch({
      seeds: ['A'],
      key: id,
      neighbors: neighborsOf(adj),
      goal: (n) => n === 'Z',
    });
    expect(path).toBeNull();
    expect([...visited.keys()].sort()).toEqual(['A', 'B']);
  });

  it('collect-all (no goal): visited is every reachable node; unreachable excluded', () => {
    const adj: Adj = {
      A: [{ to: 'B', cost: 1 }],
      B: [{ to: 'C', cost: 1 }],
      // Z exists conceptually but nothing points to it → never reached
    };
    const { path, visited } = graphSearch({ seeds: ['A'], key: id, neighbors: neighborsOf(adj) });
    expect(path).toBeNull(); // no goal → flood
    expect([...visited.keys()].sort()).toEqual(['A', 'B', 'C']);
  });

  it('multiple seeds flood from all starts', () => {
    const adj: Adj = {
      A: [{ to: 'B', cost: 1 }],
      C: [{ to: 'D', cost: 1 }],
    };
    const { visited } = graphSearch({ seeds: ['A', 'C'], key: id, neighbors: neighborsOf(adj) });
    expect([...visited.keys()].sort()).toEqual(['A', 'B', 'C', 'D']);
  });

  it('maxExpansions caps the search', () => {
    const adj: Adj = {
      A: [{ to: 'B', cost: 1 }],
      B: [{ to: 'C', cost: 1 }],
      C: [{ to: 'D', cost: 1 }],
      D: [{ to: 'E', cost: 1 }],
    };
    const { path, visited } = graphSearch({
      seeds: ['A'],
      key: id,
      neighbors: neighborsOf(adj),
      goal: (n) => n === 'E',
      maxExpansions: 2,
    });
    expect(path).toBeNull(); // E is 4 hops away, cap stops at 2 settled
    expect(visited.size).toBe(2);
  });

  it('relaxation: a later-discovered cheaper route wins', () => {
    // A->B costs 10 directly; A->C->B costs 1+1=2. B must settle via C.
    const adj: Adj = {
      A: [
        { to: 'B', cost: 10 },
        { to: 'C', cost: 1 },
      ],
      C: [{ to: 'B', cost: 1 }],
      B: [{ to: 'G', cost: 1 }],
    };
    const { path } = graphSearch({ seeds: ['A'], key: id, neighbors: neighborsOf(adj), goal: (n) => n === 'G' });
    expect(path).toEqual(['A', 'C', 'B', 'G']);
  });
});
