from uuid import uuid4

from tricycle_reaction_db.application.services.geometry_energy import (
    protocol_dominates,
)
from tricycle_reaction_db.db.models import CalculationProtocol


def _protocol(
    *,
    method_family: str,
    method: str,
    functional: str | None,
    basis_set: str,
    dispersion_model: str | None = None,
) -> CalculationProtocol:
    return CalculationProtocol(
        id=uuid4(),
        protocol_hash=uuid4().hex + uuid4().hex,
        qm_software="orca",
        qm_software_version="test",
        method_family=method_family,
        method=method,
        functional=functional,
        basis_set=basis_set,
        dispersion_model=dispersion_model,
    )


def test_wb97m_v_tzvpp_dominates_b3lyp_d3_svp() -> None:
    high = _protocol(
        method_family="DFT",
        method="DFT",
        functional="wB97M-V",
        basis_set="def2-TZVPP",
    )
    low = _protocol(
        method_family="DFT",
        method="B3LYP",
        functional="B3LYP-GD3BJ",
        basis_set="def2svp",
        dispersion_model="GD3BJ",
    )
    assert protocol_dominates(high, low)
    assert not protocol_dominates(low, high)


def test_method_and_basis_crossing_is_not_automatically_ordered() -> None:
    larger_basis = _protocol(
        method_family="DFT",
        method="DFT",
        functional="B3LYP",
        basis_set="def2-TZVPP",
    )
    better_functional = _protocol(
        method_family="DFT",
        method="DFT",
        functional="wB97M-V",
        basis_set="def2-SVP",
    )
    assert not protocol_dominates(larger_basis, better_functional)
    assert not protocol_dominates(better_functional, larger_basis)
