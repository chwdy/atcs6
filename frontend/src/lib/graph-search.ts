// graph-search.ts — one best-first graph search for every editor search.
//
// Every editor pathfinding task (lay track, trace existing track, show
// reachable tiles, select a block run) is the same loop over a graph whose
// nodes are "(tile, edge/direction)". They differ only in five knobs:
//
//   seeds      — where the search starts
//   key        — node identity (for the visited/dedup set)
//   neighbors  — the legal moves from a node (the per-tool RULES live here)
//   heuristic  — distance-to-go estimate (return 0 → behaves like BFS/Dijkstra)
//   goal       — when to stop (omit → collect every reachable node, a flood)
//
// This file is the loop. The knobs are supplied by callers (the "plugs" in
// pathfinders.ts: placementNeighbors for laying track, followExistingNeighbors
// for traversing existing track). Nodes are opaque to the engine: each node
// carries its own `parent` link, so plugs that need ancestry (no-90° corner,
// no self-intersection) can walk it, while the engine only ever calls `key`,
// `neighbors`, `heuristic`, and `goal`.
//
// IMPORTANT: the search settles each node on first pop and never re-opens it,
// so the heuristic must be CONSISTENT (monotone) for `path` to be optimal. A
// zero heuristic is trivially consistent; the construction heuristic (Chebyshev
// scaled by the diagonal step cost) is too.

export interface SearchStep<N> {
  /** A successor node. Plugs set `successor.parent = currentNode` when relevant. */
  node: N;
  /** Non-negative cost of the move from the current node to `node`. */
  cost: number;
}

export interface GraphSearchOptions<N> {
  /** Starting nodes, each settled at cost 0. */
  seeds: N[];
  /** Stable string identity of a node (the dedup/visited key). */
  key: (node: N) => string;
  /** Legal moves from a node, each with its step cost. */
  neighbors: (node: N) => SearchStep<N>[];
  /** Distance-to-go estimate; default 0 (→ Dijkstra/BFS). Must be consistent. */
  heuristic?: (node: N) => number;
  /** Stop predicate. Omit to run to exhaustion and collect every reachable node. */
  goal?: (node: N) => boolean;
  /** Safety cap on settled nodes (replaces ad-hoc 500/1000 limits). Default: no cap. */
  maxExpansions?: number;
}

export interface GraphSearchResult<N> {
  /** Seed→goal node chain (inclusive), or null if the goal was never reached. */
  path: N[] | null;
  /** Every settled (closed) node, keyed by `key` — the reachable set for floods. */
  visited: Map<string, N>;
}

interface OpenEntry {
  key: string;
  f: number;
}

/**
 * Best-first graph search. With `heuristic` omitted (or 0) and uniform step
 * cost it is BFS; with non-uniform cost it is Dijkstra; with a consistent
 * heuristic it is A*. See the file header for the node/knob contract.
 *
 * Relaxation is IN PLACE: an open node reached more cheaply has its entry's
 * priority lowered and its node replaced (a still-open node has not been
 * expanded yet, so its successors are generated only after the final, cheapest
 * relaxation). No stale duplicates are left in the open set. This keeps the
 * open-set evolution deterministic — the same nodes in the same order — which
 * is what lets a caller's path be stable down to its tie-breaking.
 */
export function graphSearch<N>(opts: GraphSearchOptions<N>): GraphSearchResult<N> {
  const h = opts.heuristic ?? (() => 0);
  const cap = opts.maxExpansions ?? Infinity;

  const gScore = new Map<string, number>();
  const cameFrom = new Map<string, N>();
  const nodeByKey = new Map<string, N>();
  const openByKey = new Map<string, OpenEntry>();
  const closed = new Map<string, N>();
  // Open set as a plain array with linear extract-min + swap-remove. openByKey
  // gives O(1) relax lookup so a cheaper route updates the existing entry's
  // priority in place rather than pushing a duplicate.
  const open: OpenEntry[] = [];

  const relax = (nk: string, ng: number, parent: N | null, node: N) => {
    gScore.set(nk, ng);
    if (parent !== null) cameFrom.set(nk, parent);
    nodeByKey.set(nk, node);
    const f = ng + h(node);
    const entry = openByKey.get(nk);
    if (entry) {
      entry.f = f;
    } else {
      const e = { key: nk, f };
      openByKey.set(nk, e);
      open.push(e);
    }
  };

  for (const seed of opts.seeds) {
    const k = opts.key(seed);
    if (gScore.has(k)) continue; // duplicate seed
    relax(k, 0, null, seed);
  }

  while (open.length > 0) {
    let minIdx = 0;
    for (let i = 1; i < open.length; i++) {
      if (open[i].f < open[minIdx].f) minIdx = i;
    }
    const current = open[minIdx];
    open[minIdx] = open[open.length - 1];
    open.pop();
    openByKey.delete(current.key);

    const node = nodeByKey.get(current.key)!;
    closed.set(current.key, node);

    if (opts.goal && opts.goal(node)) {
      return { path: reconstruct(cameFrom, opts.key, node), visited: closed };
    }

    if (closed.size >= cap) break;

    const gCur = gScore.get(current.key)!;
    for (const step of opts.neighbors(node)) {
      const nk = opts.key(step.node);
      if (closed.has(nk)) continue;
      const ng = gCur + step.cost;
      if (ng < (gScore.get(nk) ?? Infinity)) {
        relax(nk, ng, node, step.node);
      }
    }
  }

  return { path: null, visited: closed };
}

function reconstruct<N>(cameFrom: Map<string, N>, key: (n: N) => string, end: N): N[] {
  const out: N[] = [];
  const seen = new Set<string>();
  let cur: N | undefined = end;
  while (cur !== undefined) {
    const k = key(cur);
    if (seen.has(k)) break; // defensive against a malformed parent cycle
    seen.add(k);
    out.unshift(cur);
    cur = cameFrom.get(k);
  }
  return out;
}
