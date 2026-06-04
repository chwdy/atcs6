// Octilinear easing: the track-construction pathfinder must never lay two
// adjacent tiles that meet at a 90° on-screen corner ("square corner"). Every
// direction change has to ease through a 45° NS/EW diagonal. This is the
// across-tiles companion to isSquareCornerSwitch (constants.ts), which only
// rejects the same shape within a single tile.
import { describe, it, expect } from 'vitest';
import { ConstructionPathfinder, pathToTracks, pathsToTrackBits, type Point } from './pathfinders';

// On the 45°-rotated render grid each laid segment draws as one screen line:
// NW/SE => 0° (horizontal), NS => 45° (╱), NE/SW => 90° (vertical), EW => 135°
// (╲). Computed from the REAL pathToTracks output, so this is ground truth.
function tileAngle(bits: number): number | null {
  if (bits & (8 | 16)) return 0;
  if (bits & 1) return 45;
  if (bits & (4 | 32)) return 90;
  if (bits & 2) return 135;
  return null;
}

function squareCorners(path: Point[] | null, existing: any[], opts: any = {}): number {
  if (!path) return 0;
  const tracks = pathToTracks(path, { tracks: existing }, opts);
  const byKey = new Map<string, number>();
  for (const t of tracks) byKey.set(`${t.x},${t.y}`, pathsToTrackBits(t.directed_paths));
  const angles = path
    .map((p) => tileAngle(byKey.get(`${p.x},${p.y}`) ?? 0))
    .filter((a): a is number => a != null);
  let n = 0;
  for (let i = 1; i < angles.length; i++) {
    let d = Math.abs(angles[i] - angles[i - 1]);
    if (d > 90) d = 180 - d;
    if (d === 90) n++;
  }
  return n;
}

// Sweep every target tile in a radius and report corner / no-path totals.
function sweep(existing: any[], opts: any = {}) {
  const pf = new ConstructionPathfinder();
  let corners = 0;
  let noPath = 0;
  let total = 0;
  for (let ex = -9; ex <= 9; ex++) {
    for (let ey = -9; ey <= 9; ey++) {
      if (ex === 0 && ey === 0) continue;
      total++;
      const path = pf.findPath(0, 0, ex, ey, existing, opts);
      if (!path || path.length < 2) {
        noPath++;
        continue;
      }
      corners += squareCorners(path, existing, opts);
    }
  }
  return { corners, noPath, total };
}

describe('track-construction octilinear easing', () => {
  it('free space never lays a 90° corner and always reaches the target', () => {
    const r = sweep([]);
    expect(r.corners).toBe(0);
    expect(r.noPath).toBe(0);
  });

  it('drawing off an existing horizontal track eases at 45° (the screenshot bug)', () => {
    // NW|SE renders horizontally on screen; before the fix this produced a 90°
    // corner for ~45% of targets (162/360).
    const horizontal = [{ x: 0, y: 0, type: 'STRAIGHT', track_bits: 8 | 16 }];
    const r = sweep(horizontal);
    expect(r.corners).toBe(0);
    expect(r.noPath).toBe(0);
  });

  it('drawing off an existing vertical track eases at 45°', () => {
    const vertical = [{ x: 0, y: 0, type: 'STRAIGHT', track_bits: 4 }]; // NE
    const r = sweep(vertical);
    expect(r.corners).toBe(0);
    expect(r.noPath).toBe(0);
  });

  it('forced start segments never force a 90° corner', () => {
    for (const seg of ['NS', 'EW', 'NE', 'NW', 'SE', 'SW']) {
      const r = sweep([], { forcedStartSegment: seg });
      expect(r.corners, `forcedStart=${seg}`).toBe(0);
    }
  });

  // A forced END segment is an explicit pin: unlike the start (which can always
  // ease cleanly), some end orientations cannot be reached without a 90° meet.
  // The pin overrides corner-easing there, so the end tile always lays exactly
  // the forced segment — it is never silently dropped or re-eased into a
  // different shape.
  const SEG_BIT: Record<string, number> = { NS: 1, EW: 2, NE: 4, NW: 8, SE: 16, SW: 32 };

  it('forced end segments are honored — the end tile lays exactly the forced segment', () => {
    const pf = new ConstructionPathfinder();
    for (const [seg, bit] of Object.entries(SEG_BIT)) {
      for (let ex = -9; ex <= 9; ex++) {
        for (let ey = -9; ey <= 9; ey++) {
          if (ex === 0 && ey === 0) continue;
          const path = pf.findPath(0, 0, ex, ey, [], { forcedEndSegment: seg });
          if (!path || path.length < 2) continue;
          const tracks = pathToTracks(path, { tracks: [] }, { forcedEndSegment: seg });
          const endTrack = tracks.find((t) => t.x === ex && t.y === ey);
          const endBits = endTrack ? pathsToTrackBits(endTrack.directed_paths) : 0;
          expect(endBits, `forcedEnd=${seg} -> (${ex},${ey})`).toBe(bit);
        }
      }
    }
  });

  it('a forced end that the easing could not reach now places directly (no silent drop)', () => {
    const pf = new ConstructionPathfinder();
    // forceEnd=SW on a NE-diagonal drag used to return null: the SW end (90°
    // vertical) could not be approached without a square corner, so the pin was
    // silently dropped and the preview fell back to the last valid path. The
    // pin now overrides the corner rule and the build reaches the target.
    const path = pf.findPath(0, 0, 5, 5, [], { forcedEndSegment: 'SW' });
    expect(path).not.toBeNull();
    expect(path![path!.length - 1]).toEqual({ x: 5, y: 5 });
  });

  it('avoid-connect (crossover) mode stays corner-free', () => {
    const horizontal = [{ x: 0, y: 0, type: 'STRAIGHT', track_bits: 8 | 16 }];
    const r = sweep(horizontal, { avoidConnect: true });
    expect(r.corners).toBe(0);
  });

  it('still draws direct straight runs (no needless detours)', () => {
    const pf = new ConstructionPathfinder();
    // Pure east and a pure diagonal should be minimal-length straight runs.
    expect(pf.findPath(0, 0, 6, 0, [], {})!.length).toBe(7);
    expect(pf.findPath(0, 0, 0, 6, [], {})!.length).toBe(7);
    // A 4x4 build off a horizontal track reaches the target without ballooning.
    const horizontal = [{ x: 0, y: 0, type: 'STRAIGHT', track_bits: 8 | 16 }];
    const eased = pf.findPath(0, 0, 4, -4, horizontal, {})!;
    expect(eased[eased.length - 1]).toEqual({ x: 4, y: -4 });
    expect(eased.length).toBeLessThanOrEqual(11);
    expect(squareCorners(eased, horizontal)).toBe(0);
  });
});
