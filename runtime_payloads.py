"""
Runtime Payloads — shared records for cross-component runtime contracts.

- `ErrorRecord`: the diagnostic error carried in one runtime event.
- `HardwareFailure`: why a hardware-backed session halted.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class ErrorRecord:
    """The single diagnostic-error object (design_new.md "Design Direction",
    Errors and diagnostic classification).

    It carries exactly two fields, always written whole — both present, or the
    whole record is `None`:

    - ``text``: the human-readable failure detail.
    - ``origin``: a free-form label naming what produced the error (``"eb"``,
      ``"out_of_sequence"``, ``"switch_intrusion"``, ``"operator_cancelled"``,
      ``"planning_failed"``, ``"no_viable_path"``). Diagnostic only: no runtime
      branch reads it, and writers may use labels beyond the ones the document
      names. It is the field a reader groups errors by when ``operation`` alone
      is too coarse.

    It carries no timestamp and no severity of its own: a record is never
    stored on an entity, it rides in the ``error`` field of one runtime event,
    and that event owns the time (``event_time``) and the level (``kind``).
    """

    text: str
    origin: str

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "origin": self.origin}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ErrorRecord":
        return cls(text=str(data["text"]), origin=str(data.get("origin", "")))


@dataclass(frozen=True)
class HardwareFailure:
    """A hardware-backed session lost the device contract after activation.

    Unlike a physical-entry conflict, this halt cannot be cleared in place. The
    operator must make the outputs safe, repair or replace the failed hardware,
    and activate a newly validated session.
    """

    component: str
    operation: str
    message: str
    resource_id: Optional[str] = None
