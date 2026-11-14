/**
 * The selected switch, in the Operate screen's right panel: its position,
 * Throw, alternate arrivals, each train's assigned leg, whether a train holds
 * it, and its availability.
 *
 * Throw sends the operator's throw request (`PUT /api/switches/<id>/state`,
 * design_v2.md "Running a train", Throw a switch). A held switch is not
 * thrown (design_v2.md "Switches", A held switch is not thrown).
 */
import { api } from '../../lib/api';
import { availabilityStateToFlags, deriveAvailabilityState, MANUAL_SWITCH_HELP } from '../../lib/helpers';
import { stateManager } from '../../lib/state-manager';
import { showToast } from '../../lib/toast';
import { Segment, sendChange } from './block-panel';
import { CopyId } from './copy-id';
import { liveVersion, runningTrains } from './live';
import type { OperateHost } from './operate-state';

// `Switch.manual` (design_v2.md "Switches", A switch marked manual).
const AVAILABILITY_OPTIONS = [
  { value: 'available', label: 'Normal', title: 'Normal — trains throw this switch when they need it' },
  { value: 'manual', label: 'Manual', title: MANUAL_SWITCH_HELP },
];

// A train's assigned leg at this switch (design_v2.md "Switches", Assigned
// leg).
const LEG_OPTIONS = [
  { value: 'none', label: 'As set', title: 'This train follows the switch as it is set' },
  { value: 'main', label: 'Main', title: 'Send this train out of the main leg every time it comes to this switch' },
  { value: 'diverge', label: 'Diverge', title: 'Send this train out of the diverge leg every time it comes to this switch' },
];

const LEG_REFUSALS: Record<string, string> = {
  not_live: 'the session is not Live',
  train_lost: 'this train is lost',
  unknown_train: 'this train is no longer on the track',
  unknown_switch: 'this switch is no longer on the layout',
  motor_unavailable: 'this switch has no working motor',
  switch_manual: 'this switch is marked manual',
};

export function legValue(leg: number | undefined): string {
  return leg === 0 ? 'main' : leg === 1 ? 'diverge' : 'none';
}

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
  const trains = runningTrains().filter((train) => !train.lost);

  const assignLeg = (trainId: string, value: string) =>
    sendChange(
      'assigned leg',
      async () => {
        const leg = value === 'main' ? 0 : value === 'diverge' ? 1 : null;
        const reply = await api.setTrainAssignedLeg(trainId, switchId, leg);
        return reply.success ? null : { error: LEG_REFUSALS[reply.reason ?? ''] ?? reply.reason ?? 'refused' };
      },
      host,
    );

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
            <button type="button" class="btn btn-sm" disabled={!!sw.manual}
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

      {!sw.motor_unavailable && !sw.manual && trains.length > 0 && (
        <div class="control-section">
          <div class="control-section-header">Assigned leg</div>
          {trains.map((train) => (
            <Segment
              key={train.train_id}
              label={train.name}
              options={LEG_OPTIONS}
              value={legValue(train.assigned_legs?.[switchId])}
              onPick={(v) => void assignLeg(train.train_id, v)}
            />
          ))}
          <div class="text-xxs text-muted">
            A train with an assigned leg is sent out of that leg every time it comes to this switch. Throwing the
            switch by hand ends them.
          </div>
        </div>
      )}

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
        <div class="text-xxs text-muted">{MANUAL_SWITCH_HELP}</div>
      </div>
    </div>
  );
}
