/**
 * The selected switch, in the Operate screen's right panel
 * (docs/ui-layout-rewrite-plan.md §5): its position, Throw, and its locks.
 *
 * Throw sends the operational switch command, the same as today's switch
 * panel and the map's right-click "Throw" in Operate (`setSwitchState`;
 * design_new.md "Canonical `Switch.position` mutation workflow").
 *
 * design_new.md has no operator command that locks a switch: a switch is
 * locked by the paths that reserve it (`Switch.locks`) and held by the train
 * on it (`train_protection_claims`). So the Lock section shows who holds it,
 * and the operator's own say over the switch is its availability flags
 * (Normal / Manual / Disabled), which `manual` makes automatic routing skip.
 */
import { api } from '../../lib/api';
import { availabilityStateToFlags, deriveAvailabilityState } from '../../lib/helpers';
import { stateManager } from '../../lib/state-manager';
import { showToast } from '../../lib/toast';
import { AVAILABILITY_OPTIONS, Segment, sendChange } from './block-panel';
import { liveVersion } from './live';
import type { OperateHost } from './operate-state';
import { CopyId } from './train-panel';

/** `Switch.position`: 0 is the main route, 1 the diverging one. */
export function positionName(position: unknown): string {
  return position === 1 ? 'Diverge' : 'Main';
}

export function SwitchPanel({ host, switchId }: { host: OperateHost; switchId: string }) {
  liveVersion.value;
  const sw: any = stateManager.get('switches', switchId);
  if (!sw) return <div class="empty-state empty-state-warn">Switch {switchId} is no longer on the layout.</div>;

  const position = sw.position === 1 ? 1 : 0;
  const locks: any[] = Array.isArray(sw.locks) ? sw.locks : [];
  const claims: any[] = Array.isArray(sw.train_protection_claims) ? sw.train_protection_claims : [];
  const held = !!sw.held || locks.length > 0 || claims.length > 0;
  const supportsAlternate = Object.prototype.hasOwnProperty.call(sw, 'alternate_next');
  const alternating = sw.alternate_next != null;
  const name = String(sw.name || '').trim();

  const throwSwitch = async () => {
    const next = position === 0 ? 1 : 0;
    try {
      await api.setSwitchState(switchId, next);
      showToast(`Switch ${switchId} → ${next === 1 ? 'diverge' : 'main'} requested`, 'info', 2500);
    } catch (err: any) {
      showToast(`Cannot throw switch: ${err?.message ?? err}`, 'error', 5000);
    }
    await host.refreshNetwork();
  };
  const trainLink = (id: string) => (
    <button type="button" class="rr-flat-btn font-mono" onClick={() => host.selectTrain(id)}>
      {id}
    </button>
  );

  return (
    <div class="op-entity-panel">
      <div class="status-header">
        <div class="status-header-row">
          <div class="id-copy-group">
            <div class="status-header-id font-mono" title={switchId}>
              {name || switchId}
            </div>
            <CopyId id={switchId} label="Switch ID" />
          </div>
          <span class={`chip chip-state ${position === 1 ? 'chip-warn' : 'chip-info'}`}>
            {positionName(position).toUpperCase()}
          </span>
        </div>
        <div class="status-header-ident">
          <span class="status-header-role">Switch</span>
          <span class="status-sep">·</span>
          <span class="font-mono">
            ({sw.x}, {sw.y})
          </span>
        </div>
      </div>

      <div class="control-section">
        <div class="control-section-header">Position</div>
        <div class="op-row">
          <span class="op-switch-position">{positionName(position)}</span>
          <button
            type="button"
            class="btn btn-sm"
            disabled={held || !!sw.motor_unavailable}
            title={locks.length > 0 ? 'A path has locked this switch' : `Throw to ${positionName(1 - position).toLowerCase()}`}
            onClick={() => void throwSwitch()}
          >
            <i class="ti ti-switch-horizontal" aria-hidden="true" /> Throw to {positionName(1 - position).toLowerCase()}
          </button>
        </div>
        {supportsAlternate && !sw.motor_unavailable && (
          <div class="op-row">
            <span>{alternating ? `Alternate · next ${sw.alternate_next === 1 ? 'siding' : 'main'}` : 'Manual'}</span>
            <button type="button" class="btn btn-sm" disabled={!!sw.manual || !!sw.disabled}
              onClick={() => void sendChange('switch mode', () => api.setSwitchAlternate(switchId, !alternating), host)}>
              {alternating ? 'Use manual' : 'Alternate arrivals'}
            </button>
          </div>
        )}
        <dl class="op-fields">
          <dt>Occupied by</dt>
          <dd>{sw.occupied_by ? trainLink(sw.occupied_by) : <span class="text-muted">No train</span>}</dd>
        </dl>
      </div>

      <div class="control-section">
        <div class="control-section-header">Lock</div>
        {!held && <div class="text-muted text-xs">Not locked — no path or train holds this switch.</div>}
        {locks.map((lock, i) => (
          <div class="protection-row" key={`l${i}`}>
            <div class="protection-row-main">
              {lock.train_id ? trainLink(lock.train_id) : <span>—</span>}
              <span class="chip chip-state chip-accent">
                {lock.claim_role === 'flank' ? 'Flank' : 'Path'} · {positionName(Number(lock.locked_state))}
              </span>
            </div>
            {lock.path_id && (
              <div class="protection-row-sub font-mono" title={lock.path_id}>
                {lock.path_id}
              </div>
            )}
          </div>
        ))}
        {/* Each claim is [train_id, source_switch_id, locked_state]. */}
        {claims.map(([trainId, sourceId, lockedState], i) => (
          <div class="protection-row" key={`c${i}`}>
            <div class="protection-row-main">
              {trainLink(String(trainId))}
              <span class="chip chip-state chip-accent">Train · {positionName(Number(lockedState))}</span>
            </div>
            <div class="protection-row-sub">
              <span class="text-muted">from</span> <span class="font-mono">{String(sourceId)}</span>
            </div>
          </div>
        ))}
      </div>

      <div class="control-section">
        <div class="control-section-header">Operation</div>
        <Segment
          label="Availability"
          options={AVAILABILITY_OPTIONS}
          value={deriveAvailabilityState(sw)}
          onPick={(v) =>
            void sendChange('availability', () => api.updateSwitch(switchId, availabilityStateToFlags(v)), host)
          }
        />
      </div>
    </div>
  );
}
