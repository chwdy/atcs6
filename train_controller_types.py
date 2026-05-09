"""
Shared dataclasses for `TrainController` and the helper modules
(`LookaheadBuilder`, `ATCCalculator`, `ATONavigator`) extracted out of
`train_controller.py`.

These types live in their own module so the helpers can import them
without inducing a circular import on `train_controller`. They are
re-exported from `train_controller` for backwards compatibility with
existing call-sites.
"""

from dataclasses import dataclass
from typing import Optional

from enums import SignalState


@dataclass
class LookaheadPoint:
    """One point in the lookahead representing block information ahead.

    Used by both ATC (for safety speed calculation) and ATO (for navigation).
    Signal state is included for display purposes but does not affect ATC
    speed calculation (this is a CBTC-like block-based system).
    """

    distance_cm: float  # Distance from train head to start of this block
    speed_limit_kmh: float  # Block speed limit (or 0 if path end)
    block_id: str
    signal_state: Optional[SignalState] = None
    station_id: Optional[str] = None
    is_path_end: bool = False


@dataclass
class ATOInfo:
    """ATO navigation information derived from the train's assigned path.

    Contains path/destination details and determines which cab should be
    active based on path direction.
    """

    path_id: Optional[str] = None
    destination_block_id: Optional[str] = None
    destination_station_name: Optional[str] = None
    path_heading: str = "a"  # "a" or "b" — which end the path heads toward
    required_cab: str = "A"  # "A" or "B" based on path_heading
