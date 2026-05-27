"""
TrackResource Base Class

Common identity surface shared by Block and Switch. TrackResource is a *data*
base class — it carries the fields every dispatcher-facing caller needs
(`resource_id`, `resource_type`, `name`, `occupied_by`, `disabled`, `manual`)
and declares no reserve/release/lock methods. Per design_new.md the manager
layer (BlockManager, SwitchManager) is the sole mutator for reservation,
occupancy, CAUTION, and lock fields; resource entities remain data containers
in v1.

`disabled` is the physical-absence flag (no operator override; physical entry
is a fatal conflict). `manual` is the virtual-disable flag (every automatic
system treats it like disabled, but an operator command carrying
`manual_override=True` bypasses the filter; physical entry into a manual
resource is ordinary clean entry). When both are true, `disabled` wins.
See the "disabled and manual availability flags" canonical home in
design_new.md (under TrackResource) for the full contract.

Subclasses (Block, Switch) keep their own @dataclass declarations and
contribute the actual fields. TrackResource itself is a regular Python class —
not a @dataclass — so it contributes no fields to the dataclass MRO and avoids
the "non-default field after default" trap on Python 3.9.
"""

from typing import Optional


class TrackResource:
    """Common identity base for Block and Switch.

    Subclasses MUST be `@dataclass`-decorated and declare these fields:
        name: str
        occupied_by: Optional[str]
        disabled: bool
        manual: bool
    Subclasses MUST also override `resource_id` and `resource_type`.
    """

    name: str
    occupied_by: Optional[str]
    disabled: bool
    manual: bool

    @property
    def resource_id(self) -> str:
        """Unique identifier (subclass projects block_id / switch_id)."""
        raise NotImplementedError

    @property
    def resource_type(self) -> str:
        """One of "block" | "switch"."""
        raise NotImplementedError
