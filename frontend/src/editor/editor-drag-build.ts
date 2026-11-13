// editor-drag-build.ts — Drag-to-build track preview + commit.
//
// Lifted out of the Editor god-class (behavior unchanged). Owns the
// click-drag track-laying flow: following an existing drag path, recomputing
// the live preview path/tracks, and committing the dragged span into real
// track (with segment prompts). The mutable drag state
// (previewPath, previewTracks, dragStart, dragEnd, forced*Segment, etc.)
// stays declared on the Editor; this handler reaches it via `this.editor`.
//
// Owned by the Editor (`new DragBuildHandler(this)`).

import { showToast } from '../lib/toast';
import { showConfirmModal } from '../lib/helpers';
import {
  pathToTracks,
  validateSegmentPath,
  traceNodeKey,
  type TraceNode,
} from '../lib/pathfinders';
import { graphSearch } from '../lib/graph-search';
import type { DragBuildHost } from './types';

export class DragBuildHandler {
  editor: DragBuildHost;

  constructor(editor: DragBuildHost) {
    this.editor = editor;
  }

  updatePreviewPath(): void {
    if (!this.editor.dragStart || !this.editor.dragEnd) return;

    const dragToolSet = [
      'track', 'segment', 'extend-segment', 'remove-segment-tile',
    ];
    if (dragToolSet.includes(this.editor.selectedTool)) {
      const isTrackTool = this.editor.selectedTool === 'track';
      // The track tool LAYS new track, so it routes with the placement A* (it may
      // cross empty tiles). Every other drag tool operates ON existing track, so
      // it follows existing connectivity and never proposes an empty tile.
      const foundPath = isTrackTool
        ? this.editor.pathfinder.findPath(
            this.editor.dragStart.x,
            this.editor.dragStart.y,
            this.editor.dragEnd.x,
            this.editor.dragEnd.y,
            this.editor.cachedNetwork ? this.editor.cachedNetwork.tracks : [],
            {
              avoidConnect: this.editor.avoidConnectMode,
              forcedStartSegment: this.editor.forcedStartSegment ?? undefined,
              forcedEndSegment: this.editor.forcedEndSegment ?? undefined,
            }
          )
        : this._followExistingDragPath();

      if (foundPath) {
        this.editor.previewPath = foundPath;
        this.editor.previewTracks = isTrackTool
          ? pathToTracks(this.editor.previewPath, this.editor.cachedNetwork, {
              avoidConnect: this.editor.avoidConnectMode,
              forcedStartSegment: this.editor.forcedStartSegment,
              forcedEndSegment: this.editor.forcedEndSegment,
            })
          : null;
        this.editor.previewInvalid = false;
      } else {
        // Any drag tool with no legal route: show the red attempt-line (an
        // off-track gap, a switch/crossing a block can't cross, or a forced
        // segment that can't turn) and leave previewPath
        // null so commitDragBuild lays nothing — never freeze on a stale shape.
        this.editor.previewPath = null;
        this.editor.previewTracks = null;
        this.editor.previewInvalid = true;
      }
    } else {
      // Single placement for other tools
      this.editor.previewPath = [this.editor.dragEnd];
      this.editor.previewTracks = null;
    }

    // Push the preview overlay to the renderer and request a redraw. The
    // renderer paints it in _drawLive, so it survives the rAF-coalesced flush.
    // (Drawing it synchronously here would be wiped when the flush clears the
    // live canvas — see renderer setBuildPreview / _drawLive.)
    if (this.editor.cachedNetwork) {
      if (this.editor.previewInvalid && this.editor.dragStart && this.editor.dragEnd) {
        this.editor.renderer.setBuildPreview({ kind: 'invalid', path: [this.editor.dragStart, this.editor.dragEnd] });
      } else if (this.editor.previewTracks && this.editor.previewTracks.length > 0) {
        this.editor.renderer.setBuildPreview({ kind: 'tracks', tracks: this.editor.previewTracks });
      } else if (
        this.editor.selectedTool === 'remove-segment-tile' &&
        this.editor.previewPath
      ) {
        const tracks = this.editor.cachedNetwork?.tracks || [];
        const trackKeys = new Set(tracks.map((t: any) => `${t.x},${t.y}`));
        const filtered = this.editor.previewPath.filter((p: any) => trackKeys.has(`${p.x},${p.y}`));
        this.editor.renderer.setBuildPreview({ kind: 'eraser', path: filtered });
      } else if (['segment', 'extend-segment'].includes(this.editor.selectedTool) && this.editor.previewPath) {
        this.editor.renderer.setBuildPreview({ kind: 'segment', path: this.editor.previewPath });
      } else if (this.editor.previewPath) {
        this.editor.renderer.setBuildPreview({ kind: 'path', path: this.editor.previewPath });
      } else {
        this.editor.renderer.setBuildPreview(null);
      }
      this.editor.renderer.requestRedraw();
    }
  }

  async commitDragBuild(): Promise<void> {
    // Build tools require an actual drag and bail when there is no path.
    if (!this.editor.previewPath || this.editor.previewPath.length === 0) return;

    try {
      if (this.editor.selectedTool === 'track') {
        const tracks = pathToTracks(this.editor.previewPath, this.editor.cachedNetwork, {
          avoidConnect: this.editor.avoidConnectMode,
          forcedStartSegment: this.editor.forcedStartSegment,
          forcedEndSegment: this.editor.forcedEndSegment,
        });
        await this.editor.api.addTracksBulk(tracks);
      } else if (this.editor.selectedTool === 'segment') {
        if (!this.editor.previewPath || this.editor.previewPath.length === 0) return;

        const tracks = this.editor.cachedNetwork?.tracks || [];
        const validation = validateSegmentPath(this.editor.previewPath, tracks, 'create');

        if (!validation.valid) {
          const errorMsg = 'Cannot create segment:\n' + validation.errors.join('\n');
          showToast(errorMsg, 'error', 5000);
          console.error('[Editor] Segment creation validation failed:', validation.errors);
          return;
        }

        if (validation.warnings.length > 0) {
          const proceed = await showConfirmModal({
            title: 'Proceed with Warnings',
            message: 'Segment creation reported warnings.',
            detail: `${validation.warnings.join('\n')}\n\nProceed anyway?`,
            confirmText: 'Proceed',
            confirmVariant: 'primary',
          });
          if (!proceed) return;
        }

        const result = await this.editor.showSegmentModal({
          mode: 'create',
          trackCount: this.editor.previewPath.length,
          anchorTiles: this.editor.previewPath.map((p: any) => ({ x: p.x, y: p.y })),
        });

        if (!result) return; // Cancelled

        try {
          const segmentOptions: Record<string, unknown> = {};
          if (result.maxSpeed) segmentOptions.speed_limit_kmh = result.maxSpeed;
          if (result.minRadius != null) segmentOptions.min_radius_cm = result.minRadius;
          if (result.slope != null) segmentOptions.grade_percent = result.slope;

          // POST /api/segments creates the segment holding these tiles in one
          // call, so there is no separate "add tracks" round trip.
          const positions = this.editor.previewPath.map((p: any) => [p.x, p.y]);
          await this.editor.api.createSegment(
            result.segmentId,
            positions,
            result.name || '',
            segmentOptions
          );

          await this.editor.refreshSegmentsList();
        } catch (err: any) {
          console.error('[Editor] Failed to create segment:', err);
          showToast('Failed to create segment: ' + err.message, 'error', 5000);
          return;
        }
      } else if (this.editor.selectedTool === 'extend-segment') {
        if (!this.editor.previewPath || this.editor.previewPath.length === 0) return;

        const tracks = this.editor.cachedNetwork?.tracks || [];
        const validation = validateSegmentPath(this.editor.previewPath, tracks, 'extend');

        if (!validation.valid) {
          const errorMsg = 'Cannot extend segment:\n' + validation.errors.join('\n');
          showToast(errorMsg, 'error', 5000);
          console.error('[Editor] Segment extension validation failed:', validation.errors);
          return;
        }

        const trackMap = new Map<string, any>();
        tracks.forEach((t: any) => trackMap.set(`${t.x},${t.y}`, t));

        const targetSegmentId = validation.segmentsInPath.length > 0 ? validation.segmentsInPath[0] : null;
        let newTileCount = this.editor.previewPath.length;
        if (targetSegmentId) {
          newTileCount = this.editor.previewPath.filter((pos: any) => {
            const track = trackMap.get(`${pos.x},${pos.y}`);
            return !track || track.segment_id !== targetSegmentId;
          }).length;
        }

        const result = await this.editor.showSegmentModal({
          mode: 'extend',
          trackCount: newTileCount,
          existingSegments: validation.segmentsInPath,
        });

        if (!result) return; // Cancelled

        try {
          const positions = this.editor.previewPath.map((p: any) => [p.x, p.y]);
          const hasOtherSegments =
            validation.segmentsInPath.length > 1 ||
            (validation.segmentsInPath.length === 1 && validation.segmentsInPath[0] !== result.segmentId);
          await this.editor.api.extendSegment(result.segmentId, positions, hasOtherSegments);

          await this.editor.refreshSegmentsList();
        } catch (err: any) {
          console.error('[Editor] Failed to extend segment:', err);
          showToast('Failed to extend segment: ' + err.message, 'error', 5000);
          return;
        }
      } else if (this.editor.selectedTool === 'remove-segment-tile') {
        if (!this.editor.previewPath || this.editor.previewPath.length === 0) return;

        const tracks = this.editor.cachedNetwork?.tracks || [];

        const segmentGroups = new Map<string, any[]>();
        for (const pos of this.editor.previewPath) {
          const track = tracks.find((t: any) => t.x === pos.x && t.y === pos.y);
          if (track && track.segment_id) {
            if (!segmentGroups.has(track.segment_id)) {
              segmentGroups.set(track.segment_id, []);
            }
            segmentGroups.get(track.segment_id)!.push([pos.x, pos.y]);
          }
        }

        if (segmentGroups.size === 0) {
          showToast('No segment tiles in selected path', 'error', 3000);
          return;
        }

        let totalSuccess = 0;
        const totalErrors = [];

        for (const [segmentId, positions] of segmentGroups.entries()) {
          try {
            const result = await this.editor.api.removeSegmentTiles(segmentId, positions);
            totalSuccess += result.removed || 0;
            if (result.errors && result.errors.length > 0) {
              totalErrors.push(...result.errors);
            }
          } catch (err: any) {
            totalErrors.push(`Segment ${segmentId}: ${err.message}`);
          }
        }

        if (totalSuccess > 0) {
          showToast(`Removed ${totalSuccess} segment tile(s)`, 'success');
        }
        if (totalErrors.length > 0) {
          showToast(`Errors:\n${totalErrors.slice(0, 3).join('\n')}`, 'error', 5000);
        }
      }

      await this.editor.refreshAndSelect(null); // Refresh without selecting specific element
    } catch (err) {
      console.error('[Editor] Failed to commit build:', err);
    }
  }

  // Path for a segment/eraser drag: these operate ON existing track, so
  // they follow existing connectivity (never an empty tile) via graphSearch.
  // Blocks additionally cannot contain switches or crossings (see
  // validateSegmentTile), so the segment tools stop at them — a drag that can only
  // be completed by crossing one finds no path, which surfaces as the red line.
  private _followExistingDragPath(): Array<{ x: number; y: number }> | null {
    const byKey = this.editor._tracksByKey();
    const s = this.editor.dragStart;
    const g = this.editor.dragEnd;
    if (!byKey.has(`${s.x},${s.y}`) || !byKey.has(`${g.x},${g.y}`)) return null;

    const isBlock = this.editor.selectedTool === 'segment' || this.editor.selectedTool === 'extend-segment';
    const { path } = graphSearch<TraceNode>({
      seeds: [{ x: s.x, y: s.y, entry: '' }],
      key: traceNodeKey,
      neighbors: this.editor.pathfinder.followExistingNeighbors(
        byKey,
        isBlock ? { stopAtSwitches: true, stopAtCrossings: true } : {}
      ),
      goal: (n) => n.x === g.x && n.y === g.y,
    });
    return path ? path.map((n) => ({ x: n.x, y: n.y })) : null;
  }
}
