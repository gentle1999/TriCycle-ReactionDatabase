from types import SimpleNamespace

from tricycle_reaction_db.application.services.queries import (
    _canonical_reactant_product_changed,
)
from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide


def _participant(side: LogicalReactionParticipantSide, index: int, coefficient: int = 1):
    return SimpleNamespace(
        side=side,
        participant_index=index,
        stoichiometric_coefficient=coefficient,
    )


def _topology(smiles: str | None):
    return SimpleNamespace(canonical_isomeric_smiles=smiles)


def test_canonical_reactant_product_change_is_order_independent():
    rows = [
        (_participant(LogicalReactionParticipantSide.REACTANT, 0), _topology("CC")),
        (_participant(LogicalReactionParticipantSide.REACTANT, 1), _topology("C=C")),
        (_participant(LogicalReactionParticipantSide.PRODUCT, 0), _topology("C=C")),
        (_participant(LogicalReactionParticipantSide.PRODUCT, 1), _topology("CC")),
    ]

    assert _canonical_reactant_product_changed(rows) is False


def test_canonical_reactant_product_change_includes_stoichiometry():
    rows = [
        (_participant(LogicalReactionParticipantSide.REACTANT, 0), _topology("CC")),
        (_participant(LogicalReactionParticipantSide.PRODUCT, 0, coefficient=2), _topology("CC")),
    ]

    assert _canonical_reactant_product_changed(rows) is True


def test_canonical_reactant_product_change_is_unknown_without_complete_topology_sides():
    rows = [
        (_participant(LogicalReactionParticipantSide.REACTANT, 0), _topology("CC")),
    ]

    assert _canonical_reactant_product_changed(rows) is None
