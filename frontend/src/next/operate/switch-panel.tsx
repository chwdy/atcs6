/**
 * The selected switch, in the Operate screen's right panel: its position,
 * Throw, alternate arrivals, whether a train holds it, and its availability.
 *
 * Throw sends the operator's throw request (`PUT /api/switches/<id>/state`,
 * design_v2.md "Running a train", Throw a switch). A held switch is not
 * thrown (design_v2.md "Switches", A held switch is not thrown).
 */
import { api } from '../../lib/api';
import { availabilityStateToFlags, deriveAvailabilityState } from '../../lib/helpers';
import { stateManager } from '../../lib/state-manager';
import { showToast } from '../../lib/toast';
import { Segment, sendChange } from './block-panel';
import { CopyId } from './copy-id';
import { liveVersion } from './live';
import type { OperateHost } from './operate-state';

const AVAILABILITY_OPTIONS = [
  { value: 'available', label: 'Normal', title: 'Normal' },
  { value: 'manual', label: 'Manual', title: 'Manual — alternate arrivals skip it and a plain throw is refused' },
  { value: 'disabled', label: 'Disabled', title: 'Disabled — treated as missing track' },
];

/** `Switch.position`: 0 is the main route, 1 the diverging one. */
export function positionName(position: unknown): string {
  return position === 1 ? 'Diverge' : 'Main';
}

export function SwitchPanel({ host, switchId }: { host: OperateHost; switchId: string }) {
  liveVersion.value;
  const sw: any = stateManager.get('switches', switchId);
  if (!sw) return <div class="empty-state empty-state-warn">Switch {switchId} is no longer on the layout.</div>;

  const position = sw.position === 1 ? 1 : 0;
  const known = sw.position_known !== false;
  const held = !!sw.held;
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
          <span class="op-switch-position">{known ? positionName(position) : `${positionName(position)} (not known)`}</span>
          <button
            type="button"
            class="btn btn-sm"
            disabled={held || !!sw.motor_unavailable}
            title={held ? 'A train holds this switch' : `Throw to ${positionName(1 - position).toLowerCase()}`}
            onClick={() => void throwSwitch()}
          >
            <i class="ti ti-switch-horizontal" aria-hidden="true" /> Throw to {positionName(1 - position).toLowerCase()}
          </button>
        </div>
        {supportsAlternate && !sw.motor_unavailable && (
          <div class="op-row">
            <span>{alternating ? 'Alternate arrivals on' : 'Thrown by hand'}</span>
            <button type="button" class="btn btn-sm" disabled={!!sw.manual || !!sw.disabled}
              onClick={() => void sendChange('switch mode', () => api.setSwitchAlternate(switchId, !alternating), host)}>
              {alternating ? 'Turn off' : 'Alternate arrivals'}
            </button>
          </div>
        )}
        <dl class="op-fields">
          <dt>Held</dt>
          <dd>{held ? 'Yes — a train may be on it or about to cross it' : <span class="text-muted">No</span>}</dd>
        </dl>
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
