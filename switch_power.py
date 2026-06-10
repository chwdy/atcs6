"""
Switch Power Controller

Definition:
This module provides the SwitchPowerController class — the switch-side
counterpart to BlockPowerController. It is the software-to-hardware boundary
for switch *throw* output: it drives a switch's point motor to a target
alignment (MAIN=0 / DIVERGE=1). On a DC point motor the target maps to drive
polarity (one polarity throws toward MAIN, the reverse toward DIVERGE). This
throw voltage is a separate electrical output from the block ``track_voltage``
emitted by ``BlockPowerController``; the two are different wires and are never
compared to each other.

It also owns the throw continuity check (design_new.md "Power", Switch
power-controller role): after a throw, ``SwitchManager`` asks this controller
to confirm the switch physically moved by comparing the entry (common) leg's
track voltage to the leg it was thrown toward. When a switch is aligned to a
branch, that branch is electrically bonded to the entry, so the two carry the
same track voltage; the open branch is isolated.

Simulation vs real hardware: the verification logic is identical in both; only
the reading source differs. On real hardware the continuity read is a measured
value (sensor/ADC) and can disagree with the command when the switch did not
physically move. In simulation the reading is derived from the commanded
``Switch.position`` itself, so it is always as-expected and a simulated throw
always confirms (happy hardware). The failed-throw path is reached only on real
hardware, or in a test that supplies a stub output interface whose read returns
an unexpected voltage.

The module includes:
- SwitchOutputInterface: Abstract base for hardware throw output
- SimulatedSwitchOutputInterface: In-memory implementation for simulation
- SwitchPowerController: Throw driver + continuity verification
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from block_power import SimulatedOutputInterface

if TYPE_CHECKING:
    from network import RailwayNetwork
    from switch import Switch

# Two track voltages count as the same electrical node when within this
# tolerance (volts). Track voltage spans roughly 0..12V.
CONTINUITY_TOLERANCE_V = 0.5
# A block counts as an energized source when its commanded voltage magnitude
# exceeds this (volts).
ENERGIZED_EPS_V = 0.05


class SwitchOutputInterface(ABC):
    """Abstract interface for hardware switch-throw output.

    Implementations drive the point motor, whether to simulated storage or
    real hardware (an H-bridge over GPIO, etc.). The target alignment maps to
    drive polarity: 0 = MAIN, 1 = DIVERGE.
    """

    @abstractmethod
    def set_throw(self, switch_id: str, target_state: int) -> bool:
        """Drive the switch toward ``target_state`` (0=MAIN, 1=DIVERGE).

        Returns True if the command was issued, False otherwise.
        """

    @abstractmethod
    def get_last_throw(self, switch_id: str) -> Optional[int]:
        """Return the last target_state commanded for ``switch_id``, or None."""

    @abstractmethod
    def enter_safe_state(self) -> None:
        """Drive the safe state: every switch point motor de-energized.

        Per design_new.md:2235 the runtime issues this when it stops driving
        outputs (simulation stop or process shutdown). The simulated interface
        drops its commanded drive; real hardware de-energizes the point motors.
        """


class SimulatedSwitchOutputInterface(SwitchOutputInterface):
    """Simulated throw output — records the last commanded target per switch.

    Used for simulation mode when no hardware is connected. There is no
    separate notion of a "realized" alignment here: simulation models happy
    hardware, so the recorded command is also what the switch reaches.
    """

    def __init__(self) -> None:
        self._throws: Dict[str, int] = {}

    def set_throw(self, switch_id: str, target_state: int) -> bool:
        self._throws[switch_id] = 0 if target_state == 0 else 1
        return True

    def get_last_throw(self, switch_id: str) -> Optional[int]:
        return self._throws.get(switch_id)

    def clear(self) -> None:
        self._throws.clear()

    def enter_safe_state(self) -> None:
        """Safe state: drop every commanded throw (motors de-energized). The
        switch's commanded `Switch.position` remains the alignment authority;
        this only stops asserting drive (design_new.md:2235)."""
        self._throws.clear()


@dataclass
class SwitchThrowVerification:
    """Outcome of a throw continuity check.

    - ``confirmed``: the entry and the thrown-toward branch read the same
      track voltage, so the switch is bonded as commanded.
    - ``skipped``: the check could not be made (no single common entry leg,
      missing geometry, no power controller, or an indeterminate
      energization) — accepted by the caller, no verdict.
    - ``reason``: free-form diagnostic string for traceability.
    """

    confirmed: bool
    skipped: bool
    reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        """True when the throw should be accepted (confirmed OR skipped)."""
        return self.confirmed or self.skipped


@dataclass
class SwitchPowerController:
    """Drives switch point motors and verifies the throw landed.

    Mirrors ``BlockPowerController``'s software-to-hardware role on the switch
    side. It owns no routing, lock, or availability policy — it drives the
    motor and reports the throw result. ``SwitchManager`` invokes it; see
    design_new.md "Power" (Switch power-controller role) and "SwitchManager".

    Attributes:
        network: Reference to the railway network (switch registry, block
            registry, tile geometry, and the block ``power_controller`` used
            for the continuity reads).
        output_interface: Interface for the actual throw output.
    """

    network: Optional["RailwayNetwork"] = None
    output_interface: SwitchOutputInterface = field(
        default_factory=SimulatedSwitchOutputInterface
    )

    def set_network(self, network: "RailwayNetwork") -> None:
        """Set the network reference."""
        self.network = network

    # ------------------------------------------------------------------
    # Throw output (software -> hardware)
    # ------------------------------------------------------------------

    def throw(self, switch_id: str, target_state: int) -> bool:
        """Drive ``switch_id``'s point motor to ``target_state`` via the interface."""
        return self.output_interface.set_throw(
            switch_id, 0 if target_state == 0 else 1
        )

    def enter_safe_state(self) -> None:
        """Put the switch output hardware into its safe state — every point motor
        de-energized (design_new.md:2235). Issued by the runtime when it stops
        driving outputs; delegated to the output interface."""
        self.output_interface.enter_safe_state()

    # ------------------------------------------------------------------
    # Throw continuity verification
    # ------------------------------------------------------------------

    def verify_throw(
        self, switch: "Switch", target_state: int
    ) -> SwitchThrowVerification:
        """Confirm ``switch`` reached ``target_state`` by track-voltage continuity.

        Compares the entry (common) leg's track voltage to the thrown-toward
        branch's track voltage; they match only when the switch is aligned to
        bond them. See design_new.md "Power", Switch power-controller role.
        """
        if self.network is None:
            return SwitchThrowVerification(False, True, "no network")
        if getattr(self.network, "power_controller", None) is None:
            return SwitchThrowVerification(False, True, "no block power controller")

        ports = self._derive_ports(switch)
        if ports is None:
            return SwitchThrowVerification(
                False, True, "no single entry leg (crossing not verifiable)"
            )
        entry_block, branch_by_state = ports
        target_branch = branch_by_state.get(0 if target_state == 0 else 1)
        if entry_block is None or target_branch is None:
            return SwitchThrowVerification(
                False, True, "entry or target branch is not a block"
            )

        # Single-energized rule: the check is conclusive only when exactly one
        # of the two compared blocks is a commanded source and the other is
        # unpowered, so the only path for the entry voltage to reach the
        # branch is through the switch. The source (commanded) voltage is the
        # same on simulation and real hardware.
        v_entry_src = self._source_voltage(entry_block)
        v_branch_src = self._source_voltage(target_branch)
        energized = [
            v for v in (v_entry_src, v_branch_src) if abs(v) > ENERGIZED_EPS_V
        ]
        if len(energized) != 1:
            return SwitchThrowVerification(
                False, True, "indeterminate (not exactly one side energized)"
            )

        # Continuity reads: derived from commanded position in simulation,
        # measured (sensor/ADC) on real hardware.
        c_entry = self._continuity_voltage(entry_block)
        c_branch = self._continuity_voltage(target_branch)
        if abs(c_entry - c_branch) <= CONTINUITY_TOLERANCE_V:
            return SwitchThrowVerification(True, False, None)
        return SwitchThrowVerification(
            False,
            False,
            (
                f"continuity mismatch: entry {entry_block}={c_entry:.2f}V vs "
                f"branch {target_branch}={c_branch:.2f}V"
            ),
        )

    # ------------------------------------------------------------------
    # Geometry: entry + branch blocks for a switch
    # ------------------------------------------------------------------

    def _derive_ports(
        self, switch: "Switch"
    ) -> Optional[Tuple[Optional[str], Dict[int, Optional[str]]]]:
        """Resolve (entry_block_id, {state: branch_block_id}) from geometry.

        Returns ``None`` when the switch tile has no single common entry leg
        (a crossing carrying multiple independent through-routes, e.g. a
        scissors or double crossover), which is not verifiable.
        """
        seg = self.network.get_track(switch.x, switch.y)
        if seg is None:
            return None
        switch_points = seg.switch_points
        if len(switch_points) != 1:
            # No single common entry leg (or not a switch) -> not verifiable.
            return None
        entry_edge = next(iter(switch_points.keys()))

        branch_edge_by_state: Dict[int, str] = {}
        for exit_edge, required_state in seg.enumerate_traversals(entry_edge):
            if required_state in (0, 1) and required_state not in branch_edge_by_state:
                branch_edge_by_state[required_state] = exit_edge

        entry_block = self._block_across(switch.x, switch.y, entry_edge)
        branch_by_state: Dict[int, Optional[str]] = {
            state: self._block_across(switch.x, switch.y, edge)
            for state, edge in branch_edge_by_state.items()
        }
        return (entry_block, branch_by_state)

    def _block_across(self, x: int, y: int, edge: str) -> Optional[str]:
        """Block ID of the neighbour tile across ``edge`` from ``(x, y)``."""
        neighbor = self.network.get_neighbor(x, y, edge)
        if neighbor is None:
            return None
        return self.network.tile_to_block_id(neighbor.x, neighbor.y)

    # ------------------------------------------------------------------
    # Voltage reads
    # ------------------------------------------------------------------

    def _source_voltage(self, block_id: str) -> float:
        """Commanded per-block track voltage (the value last driven onto it).

        Same on simulation and real hardware — it is the controller's own
        output decision, stored on the block by ``BlockPowerController``.
        """
        block = self.network.blocks.get(block_id)
        return float(getattr(block, "current_voltage", 0.0)) if block else 0.0

    def _continuity_voltage(self, block_id: str) -> float:
        """Continuity-aware track-voltage read for ``block_id``.

        Real hardware: the measured value (continuity is physical). Simulation:
        derived from the commanded ``Switch.position`` bonds, so it always
        reflects the expected aligned state.
        """
        pc = self.network.power_controller
        is_sim = pc is not None and isinstance(
            pc.output_interface, SimulatedOutputInterface
        )
        if not is_sim:
            return pc.get_block_voltage(block_id)
        return self._sim_node_voltage(block_id)

    def _sim_node_voltage(self, block_id: str) -> float:
        """Simulated node voltage: the single energized source on ``block_id``'s
        connected group of blocks bonded through aligned switches."""
        adjacency: Dict[str, Set[str]] = {}
        for block_a, block_b in self._aligned_bonds():
            adjacency.setdefault(block_a, set()).add(block_b)
            adjacency.setdefault(block_b, set()).add(block_a)

        seen: Set[str] = set()
        frontier: List[str] = [block_id]
        node_voltage = 0.0
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            source = self._source_voltage(current)
            if abs(source) > ENERGIZED_EPS_V:
                node_voltage = source
            for neighbor in adjacency.get(current, ()):
                if neighbor not in seen:
                    frontier.append(neighbor)
        return node_voltage

    def _aligned_bonds(self) -> List[Tuple[str, str]]:
        """Block pairs bonded right now: each switch's entry leg to the branch
        its commanded ``Switch.position`` selects."""
        bonds: List[Tuple[str, str]] = []
        for switch in self.network.switches.values():
            ports = self._derive_ports(switch)
            if ports is None:
                continue
            entry_block, branch_by_state = ports
            active_branch = branch_by_state.get(int(switch.position))
            if entry_block and active_branch:
                bonds.append((entry_block, active_branch))
        return bonds
