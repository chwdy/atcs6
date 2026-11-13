"""
Switch Entity Module

Definition:
This module defines the Switch entity which represents a railway turnout (switch)
with configurable physical properties for each branch (MAIN and DIVERGE paths).

Design Notes:
- Each switch has separate length and radius for MAIN (position 0) and DIVERGE (position 1) paths
- Switch position is authoritative on the Switch entity (not on Tile) in v1
- Switches are auto-created when switch tracks are placed
- Default length is 0.0 cm (unconfigured) which triggers warnings but allows operation
- A switch stores no train state: whether a train holds it is read from the
  train runtime (design_v2.md "Switches").
"""

from dataclasses import dataclass
from typing import Dict, Any, Optional, Tuple, TYPE_CHECKING

from enums import SwitchState

if TYPE_CHECKING:
    from network import RailwayNetwork


@dataclass
class Switch:
    """
    Railway switch (turnout) configuration and runtime state.

    A switch is placed at a specific track tile and defines the physical
    characteristics of each path through the turnout mechanism, plus its
    commanded `position` and whether that position is known.
    """

    switch_id: str  # Unique identifier (e.g., "SW_5_10")
    x: int  # Track tile X coordinate
    y: int  # Track tile Y coordinate

    # Physical properties - PER BRANCH
    main_length_cm: float = 0.0  # Length of MAIN path (state 0)
    diverge_length_cm: float = 0.0  # Length of DIVERGE path (state 1)

    # Radii - per branch
    main_radius_cm: Optional[float] = None  # Radius of MAIN path (None = straight)
    diverge_radius_cm: Optional[float] = None  # Radius of DIVERGE path

    # Switch type metadata
    switch_type: str = "standard"  # "standard", "wye", "crossover", "slip", "scissor"
    name: str = ""  # User-friendly name
    notes: str = ""  # User notes about this switch

    # ========================================================================
    # RUNTIME STATE
    # ========================================================================
    position: SwitchState = SwitchState.MAIN
    """Authoritative physical alignment of the switch (MAIN=0, DIVERGE=1)."""

    position_known: bool = True
    """Whether the app knows where the points are (design_v2.md "Switch",
    Switch model). Activating Virtual Hardware or Real Hardware sets it False
    on every switch, because a fresh session has thrown no coil; any write
    through `apply_position_change(...)` sets it True. Runtime only, never
    saved."""

    flip_count: int = 0
    """Maintenance counter: number of times the switch actually changed
    alignment. Incremented by `apply_position_change(...)` only on a real
    change. Unlike `position`, this is persisted with the layout."""

    allow_exit_from_either_leg: bool = False
    motor_unavailable: bool = False
    disabled: bool = False
    """When True, the switch is treated as missing track / open circuit. No
    operator override bypasses this; physical entry is a fatal conflict."""

    manual: bool = False
    """When True, automatic systems treat the switch as if disabled
    (switch-state commands fail, and the runtime does not throw it), but an
    operator command carrying `manual_override=True` bypasses the filter.
    Physical entry into a manual (but not disabled) switch is ordinary clean
    entry."""

    # ========================================================================
    # TrackResource identity
    # ========================================================================

    @property
    def resource_id(self) -> str:
        return self.switch_id

    @property
    def resource_type(self) -> str:
        return "switch"

    def apply_position_change(
        self, new_position: int, manual_override: bool = False
    ) -> Tuple[bool, str]:
        """Validate and apply a position change to `self.position`.

        Pure data operation: checks availability (disabled, manual), and on
        success mutates `self.position`. Callers go through
        `SwitchManager.request_position_change`.

        Args:
            new_position: Desired switch position (0=MAIN, 1=DIVERGE).
            manual_override: When True (operator opt-in), allow position
                changes on a manual switch. The disabled filter always
                applies regardless.

        Returns:
            (success, message) tuple.
        """
        # Disabled switches cannot be targeted by manual/API position-change commands.
        if self.disabled:
            return (False, f"Switch {self.switch_id} is disabled")

        # Manual switches require an explicit operator override.
        if self.manual and not manual_override:
            return (
                False,
                f"Switch {self.switch_id} is set to manual (manual_override required)",
            )

        # Mutate authoritative position. Count a maintenance flip only when the
        # alignment actually changes; re-aligning to the position the switch
        # already holds is a no-op flip.
        if self.position != SwitchState(new_position):
            self.flip_count += 1
        self.position = SwitchState(new_position)
        self.position_known = True

        position_name = "MAIN" if new_position == 0 else "DIVERGE"
        return (True, f"Switch position changed to {position_name}")


    # ========================================================================
    # SERIALIZATION
    # ========================================================================

    def to_dict(self, network: "RailwayNetwork" = None) -> Dict[str, Any]:
        """Convert to dictionary for the snapshot.

        Includes `position` and `position_known`. Persistence does NOT use
        this path; see `to_layout_dict()` instead.
        """
        return {
            "switch_id": self.switch_id,
            "x": self.x,
            "y": self.y,
            "main_length_cm": self.main_length_cm,
            "diverge_length_cm": self.diverge_length_cm,
            "main_radius_cm": self.main_radius_cm,
            "diverge_radius_cm": self.diverge_radius_cm,
            "switch_type": self.switch_type,
            "name": self.name,
            "notes": self.notes,
            "position": int(self.position),
            "position_known": self.position_known,
            "flip_count": self.flip_count,
            "allow_exit_from_either_leg": self.allow_exit_from_either_leg,
            "motor_unavailable": self.motor_unavailable,
            "disabled": self.disabled,
            "manual": self.manual,
        }

    def to_layout_dict(self) -> Dict[str, Any]:
        """Layout-only serialization used by persistence.

        `position` is excluded — the switch loads at the dataclass default
        position (MAIN).
        """
        return {
            "switch_id": self.switch_id,
            "x": self.x,
            "y": self.y,
            "main_length_cm": self.main_length_cm,
            "diverge_length_cm": self.diverge_length_cm,
            "main_radius_cm": self.main_radius_cm,
            "diverge_radius_cm": self.diverge_radius_cm,
            "switch_type": self.switch_type,
            "name": self.name,
            "notes": self.notes,
            "allow_exit_from_either_leg": self.allow_exit_from_either_leg,
            "motor_unavailable": self.motor_unavailable,
            "disabled": self.disabled,
            "manual": self.manual,
            "flip_count": self.flip_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Switch":
        """Reconstruct from a layout snapshot.

        `position` is not loaded from disk; the default applies. Keys this
        loader does not know are skipped.
        """
        if "main_length_cm" not in data or "diverge_length_cm" not in data:
            raise ValueError(
                f"Switch.from_dict: switch '{data.get('switch_id')}' is missing "
                "required 'main_length_cm'/'diverge_length_cm'. Lengths must be "
                "supplied explicitly (0 is allowed); there is no silent default."
            )
        switch = cls(
            switch_id=data["switch_id"],
            x=data["x"],
            y=data["y"],
            main_length_cm=float(data["main_length_cm"]),
            diverge_length_cm=float(data["diverge_length_cm"]),
            main_radius_cm=data.get("main_radius_cm"),
            diverge_radius_cm=data.get("diverge_radius_cm"),
            switch_type=data.get("switch_type", "standard"),
            name=data.get("name", ""),
            notes=data.get("notes", ""),
        )
        switch.allow_exit_from_either_leg = data.get("allow_exit_from_either_leg") is True
        switch.motor_unavailable = data.get("motor_unavailable") is True
        switch.disabled = bool(data.get("disabled", False))
        switch.manual = bool(data.get("manual", False))
        switch.flip_count = int(data.get("flip_count", 0))
        return switch

    def __repr__(self) -> str:
        """String representation for debugging."""
        return (
            f"Switch({self.switch_id} at ({self.x},{self.y}): "
            f"position={self.position.name}, MAIN={self.main_length_cm}cm, "
            f"DIVERGE={self.diverge_length_cm}cm)"
        )
