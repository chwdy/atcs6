"""Scene protocol for the journey harness."""

from dataclasses import dataclass
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from network import RailwayNetwork
    from simulation import SimulationController


@dataclass(frozen=True)
class SceneFixture:
    """The bound world a Journey runs against."""

    network: "RailwayNetwork"
    sim_controller: "SimulationController"
    train_id: str


class Scene(Protocol):
    """A Scene knows how to construct the world before tick 1."""

    def build(self) -> SceneFixture: ...
