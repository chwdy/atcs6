"""Smoke tests for `signal_calculator.SignalCalculator`.

End-to-end signal-state behaviour is exercised by `tests/test_signals.py`
through the `RailwayNetwork.update_signals_from_blocks()` forwarder. This
file just covers the construction path and a couple of lookup contracts so
the module's public surface has direct test coverage.
"""

from network import RailwayNetwork
from signal_calculator import SignalCalculator


def test_signal_calculator_constructs_with_network():
    network = RailwayNetwork()
    calc = SignalCalculator(network)
    # The class must hold a back-ref keyed `_n` for the forwarders to work.
    assert calc._n is network


def test_reachable_blocks_returns_empty_for_unknown_signal():
    network = RailwayNetwork()
    calc = SignalCalculator(network)
    assert calc.reachable_blocks("does-not-exist") == []


def test_update_all_with_no_signals_is_noop():
    network = RailwayNetwork()
    calc = SignalCalculator(network)
    # Should not raise even with an empty signal registry.
    calc.update_all()


def test_network_lazy_inits_signal_calculator():
    """`RailwayNetwork._get_signal_calculator()` lazy-creates the helper."""
    network = RailwayNetwork()
    assert network._signal_calculator is None
    calc = network._get_signal_calculator()
    assert calc is not None
    assert isinstance(calc, SignalCalculator)
    # Subsequent calls return the same instance.
    assert network._get_signal_calculator() is calc
