import pytest

from adi_engines.mdp import ActionSpec, Economics
from adi_engines.synthetic import AVG_BALANCE_BN


@pytest.fixture
def econ():
    """Invented economics sized to the demo sample (VND bn per account per month)."""
    return Economics(
        balance=dict(AVG_BALANCE_BN),
        monthly_yield=0.0158,
        fee_per_account=0.0001,
        cof_rate=0.0039,
        base_collection_cost={"B1": 0.0005, "B2": 0.0007, "B3": 0.0009, "B4": 0.0011},
        ecl_rate={"B0": 0.01, "B1": 0.10, "B2": 0.30, "B3": 0.50, "B4": 0.70, "PP": 0.0, "WO": 0.0},
        lgd=0.65,
        discount=0.99,
    )


@pytest.fixture
def collections():
    """Assumed effect: moves 15% of B1/B2 forward-roll mass to cure. An assumption, not a measurement."""
    return ActionSpec("intensify_collections", ("B1", "B2"), {"B1": 0.15, "B2": 0.15},
                      cost_per_account=0.0003, capacity_per_month=45)
