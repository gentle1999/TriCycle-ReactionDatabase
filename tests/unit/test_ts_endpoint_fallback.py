from types import SimpleNamespace

import numpy as np
import pytest
from molop.unit import atom_ureg
from rdkit import Chem

from tricycle_reaction_db.application.services import artifact_molop_inference as inference
from tricycle_reaction_db.application.services.ts_endpoint_fallback import endpoint_provenance
from tricycle_reaction_db.core.config import Settings


def frame():
    center = np.array([[-0.4, 0.0, 0.0], [0.4, 0.0, 0.0]])
    mode = np.array([[-0.1, 0.0, 0.0], [0.1, 0.0, 0.0]])
    return SimpleNamespace(
        atoms=[1, 1],
        charge=0,
        multiplicity=1,
        coords=center * atom_ureg.angstrom,
        vibrations=[SimpleNamespace(vibration_mode=mode * atom_ureg.angstrom)],
    )


def endpoint(source, direction):
    graph = Chem.RWMol()
    graph.AddAtom(Chem.Atom(1))
    graph.AddAtom(Chem.Atom(1))
    graph.AddBond(0, 1, Chem.BondType.SINGLE)
    conformer = Chem.Conformer(2)
    conformer.Set3D(True)
    for index, xyz in enumerate(
        source.coords.m - direction * 1.4 * source.vibrations[0].vibration_mode.m
    ):
        conformer.SetAtomPosition(index, xyz)
    graph.AddConformer(conformer)
    return graph.GetMol()


def test_compatibility_is_disabled_by_default():
    assert Settings(_env_file=None).ts_endpoint_openbabel_fallback is False


def test_unverified_charge_exception_does_not_relax_strict_reactions(monkeypatch):
    from tricycle_reaction_db.application.services import reactions
    from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide as Side

    composition = [{"atomic_number": 1, "isotope": 0, "count": 1}]
    participants = [
        SimpleNamespace(
            side=side,
            stoichiometric_coefficient=1,
            topology=SimpleNamespace(
                formal_charge=charge, formula=SimpleNamespace(composition=composition)
            ),
        )
        for side, charge in [(Side.REACTANT, -1), (Side.PRODUCT, 0)]
    ]
    reaction = SimpleNamespace(participants=participants, reaction_hash="test")
    monkeypatch.setattr(reactions, "reaction_hash_for_participants", lambda _: "test")
    with pytest.raises(ValueError, match="conserve formal charge"):
        reactions.validate_logical_reaction(reaction)
    reactions.validate_logical_reaction(reaction, allow_unverified_charge=True)
    participants[1].topology.formula = SimpleNamespace(composition=[])
    with pytest.raises(ValueError, match="conserve elements"):
        reactions.validate_logical_reaction(reaction, allow_unverified_charge=True)


def test_disabled_mode_keeps_strict_failure(monkeypatch):
    source = frame()

    def fail(**kwargs):
        raise ValueError("strict endpoint unavailable")

    source.possible_pre_post_ts = fail
    monkeypatch.setattr(
        inference, "openbabel_endpoint", lambda *a, **k: pytest.fail("fallback ran")
    )
    with pytest.raises(ValueError, match="strict endpoint unavailable"):
        inference.signed_ts_endpoints(source, 0)


@pytest.mark.parametrize("missing", [set(), {-1}, {1}, {-1, 1}])
def test_fallback_is_per_side_and_exactly_ratio_one(monkeypatch, missing):
    source = frame()

    def strict(_source, direction):
        if direction in missing:
            raise ValueError("no legal candidates")
        return endpoint(source, direction)

    monkeypatch.setattr(inference, "strict_side_endpoint", strict)
    negative, positive, nr, pr = inference.signed_ts_endpoints(
        source,
        0,
        allow_openbabel_fallback=True,
    )
    for direction, graph, ratio in [(-1, negative, nr), (1, positive, pr)]:
        expected_ratio = 1.0 if direction in missing else 1.4
        assert ratio == pytest.approx(expected_ratio)
        np.testing.assert_allclose(
            graph.GetConformer().GetPositions(),
            source.coords.m - direction * expected_ratio * source.vibrations[0].vibration_mode.m,
        )
        evidence = endpoint_provenance(graph)
        assert evidence["strict_validation_passed"] == (direction not in missing)
        if direction in missing:
            assert evidence["validation_status"] == "unverified"
            assert evidence["mode_ratio"] == 1.0
            assert evidence["fallback_reason"] == "no legal candidates"
            restored = Chem.Mol(graph.ToBinary(Chem.PropertyPickleOptions.AllProps))
            assert endpoint_provenance(restored) == evidence


def test_bad_source_mode_is_not_converted_to_compatibility_success(monkeypatch):
    source = frame()
    source.vibrations[0].vibration_mode.m[0, 0] = float("nan")
    monkeypatch.setattr(
        inference, "openbabel_endpoint", lambda *a, **k: pytest.fail("fallback ran")
    )
    with pytest.raises(ValueError, match="imaginary mode"):
        inference.signed_ts_endpoints(source, 0, allow_openbabel_fallback=True)


def test_failed_openbabel_reconstruction_still_fails(monkeypatch):
    def fail_strict(*args):
        raise ValueError("no candidates")

    def fail_openbabel(*args, **kwargs):
        raise RuntimeError("Open Babel failed")

    monkeypatch.setattr(inference, "strict_side_endpoint", fail_strict)
    monkeypatch.setattr(inference, "openbabel_endpoint", fail_openbabel)
    with pytest.raises(RuntimeError, match="Open Babel failed"):
        inference.signed_ts_endpoints(frame(), 0, allow_openbabel_fallback=True)
