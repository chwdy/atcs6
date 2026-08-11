"""Train roster — the operator's catalog of owned locomotives.

design_new.md "Train Roster": the `TrainRoster` is configuration data, not
runtime state — it is edited like the rest of the layout and is saved and
restored with the layout snapshot. Each `RosterEntry` records one owned
locomotive plus the defaults used to put it on the track. Placing a train may
import a `RosterEntry`: `SimulationController.add_train(...)` seeds the new
`Train`'s configuration fields and its `SpeedProfile` from the named entry
instead of from blank defaults. The entry is a placement template — the placed
`Train` is a separate runtime instance, and editing one does not change the
other. The roster stores identity, purchase record, placement defaults, and the
profile link only; cumulative usage stays on the live `Train` and its counters
(see "Maintenance counters").
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RosterEntry:
    """One owned locomotive and its placement defaults (design_new.md
    "Train Roster")."""

    entry_id: str
    name: str = ""
    # Free-text description of the physical item (manufacturer / product / box).
    model: str = ""
    # Purchase record (identity / accounting only — not used at runtime).
    purchase_date: Optional[str] = None
    purchase_price: Optional[float] = None
    # Placement defaults seeded onto the Train at add_train time.
    consist_length_cm: Optional[float] = None
    max_speed: Optional[float] = None
    min_radius_cm: Optional[float] = None
    # Maximum climbable grade percent; 0.0 means unspecified.
    max_slope: float = 0.0
    # The SpeedProfile holding this locomotive's calibration (Speed calibration
    # writes that profile, so measured curves stay tied to the physical loco).
    speed_profile_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "name": self.name,
            "model": self.model,
            "purchase_date": self.purchase_date,
            "purchase_price": self.purchase_price,
            "consist_length_cm": self.consist_length_cm,
            "max_speed": self.max_speed,
            "min_radius_cm": self.min_radius_cm,
            "max_slope": self.max_slope,
            "speed_profile_id": self.speed_profile_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RosterEntry":
        return cls(
            entry_id=str(data["entry_id"]),
            name=str(data.get("name", "")),
            model=str(data.get("model", "")),
            purchase_date=data.get("purchase_date"),
            purchase_price=data.get("purchase_price"),
            consist_length_cm=data.get("consist_length_cm"),
            max_speed=data.get("max_speed"),
            min_radius_cm=data.get("min_radius_cm"),
            max_slope=float(data.get("max_slope", 0.0) or 0.0),
            speed_profile_id=data.get("speed_profile_id"),
        )


@dataclass
class TrainRoster:
    """The catalog of `RosterEntry` records, keyed by `entry_id`."""

    entries: Dict[str, RosterEntry] = field(default_factory=dict)

    def add_entry(self, entry: RosterEntry) -> RosterEntry:
        self.entries[entry.entry_id] = entry
        return entry

    def get_entry(self, entry_id: str) -> Optional[RosterEntry]:
        return self.entries.get(entry_id)

    def remove_entry(self, entry_id: str) -> bool:
        return self.entries.pop(entry_id, None) is not None

    @staticmethod
    def match_profile_for_model(model: str, profiles) -> Optional[str]:
        """The `profile_id` measured for this model, or None.

        Profiles are matched by model (design_new.md "Train Roster"): comparing
        the two strings exactly, the profile carrying this model is the one a
        new entry links, so a second locomotive of a model starts from the curve
        the first one produced. When more than one profile carries the model the
        most recently calibrated wins, and a profile with no `calibrated_at`
        loses to any that has one — an unmeasured placeholder should never
        outrank a real measurement.

        `profiles` is any iterable of `SpeedProfile` records.
        """
        wanted = (model or "").strip()
        if not wanted:
            return None
        matches = [
            p for p in profiles if (getattr(p, "model", "") or "").strip() == wanted
        ]
        if not matches:
            return None
        # `calibrated_at` is an ISO-8601 stamp, so string order is time order;
        # "" sorts below every real stamp, which is exactly the tiebreak wanted.
        matches.sort(key=lambda p: (getattr(p, "calibrated_at", None) or ""))
        return matches[-1].profile_id

    def link_profile_by_model(self, entry: "RosterEntry", profiles) -> Optional[str]:
        """Link `entry` to the profile measured for its model, if it names none.

        The match is made once, at the moment the entry's model is written; the
        stored `speed_profile_id` is the link every later read follows, so a
        placement resolves a profile by id and never by string.
        """
        if entry.speed_profile_id:
            return entry.speed_profile_id
        matched = self.match_profile_for_model(entry.model, profiles)
        if matched:
            entry.speed_profile_id = matched
        return entry.speed_profile_id

    def clear(self) -> None:
        self.entries.clear()

    def to_dict(self) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in self.entries.values()]

    @classmethod
    def from_dict(cls, data: Any) -> "TrainRoster":
        roster = cls()
        for entry_data in data or []:
            entry = RosterEntry.from_dict(entry_data)
            roster.entries[entry.entry_id] = entry
        return roster
