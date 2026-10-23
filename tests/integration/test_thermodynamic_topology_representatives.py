import hashlib
import os
from uuid import UUID, uuid4

import pytest
from rdkit import Chem
from sqlalchemy import create_engine
from sqlmodel import Session, select
from test_domain_query_filters import _create_domain_sample

from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics import (
    MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION,
)
from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    refresh_mapped_reaction_thermodynamics,
)
from tricycle_reaction_db.application.services.reaction_topology_representatives import (
    prefer_observed_participant_topologies,
)
from tricycle_reaction_db.application.services.thermodynamic_profile_recovery import (
    recover_profile_refresh_dependencies,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    LogicalParticipantConcreteTopology,
    LogicalReactionParticipant,
    MappedReactionParticipant,
    MolecularTopology,
)
from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide as Side

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1", reason="database tests disabled"
    ),
]


def test_no_frame_participant_reuses_observed_equivalent_without_mutating_topologies(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("canonical correspondence only")

    for method in ("GetSubstructMatch", "GetSubstructMatches", "HasSubstructMatch"):
        monkeypatch.setattr(Chem.Mol, method, forbidden)
    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                sample = _create_domain_sample(session)
                observed, mapped, logical = sample[5], sample[6], sample[7]
                session.refresh(observed)
                stale = MolecularTopology(
                    **observed.model_dump(exclude={"id", "created_at", "morgan_bfp"})
                )
                stale.id = uuid4()
                stale.graph_hash = hashlib.sha256(str(stale.id).encode()).hexdigest()
                session.add(stale)
                session.flush()
                slot = session.exec(
                    select(LogicalReactionParticipant).where(
                        LogicalReactionParticipant.logical_reaction_id == logical.id,
                        LogicalReactionParticipant.side == Side.REACTANT,
                    )
                ).one()
                participant = MappedReactionParticipant(
                    id=uuid4(),
                    mapped_reaction_id=mapped.id,
                    logical_reaction_participant_id=slot.id,
                    concrete_topology_id=stale.id,
                    side=Side.REACTANT,
                    template_index=0,
                    atom_map_numbers=[1, 2],
                    mapped_smiles="[H:1][H:2]",
                )
                session.add_all(
                    [
                        participant,
                        LogicalParticipantConcreteTopology(
                            logical_reaction_participant_id=slot.id,
                            concrete_topology_id=observed.id,
                        ),
                    ]
                )
                session.flush()
                old_hash, observed_hash = stale.graph_hash, observed.graph_hash
                mapped.thermodynamic_profile_policy_version = (
                    MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION
                )
                mapped.thermodynamic_profile_materialized_generation = (
                    mapped.thermodynamic_profile_generation
                )
                session.add(mapped)
                session.flush()
                cursor = UUID(int=mapped.id.int - 1)
                # No notification/dirty generation is present: the audit must
                # still find a stale canonical reference and durably queue it.
                audit = recover_profile_refresh_dependencies(session, after_id=cursor, batch_size=1)
                assert (audit.topology_replacements, audit.enqueued) == (1, 1)
                assert participant.concrete_topology_id == stale.id
                assert (
                    recover_profile_refresh_dependencies(
                        session, after_id=cursor, batch_size=1
                    ).enqueued
                    == 0
                )
                refresh_mapped_reaction_thermodynamics(session, mapped)
                assert participant.concrete_topology_id == observed.id
                assert (
                    recover_profile_refresh_dependencies(
                        session, after_id=cursor, batch_size=1
                    ).enqueued
                    == 0
                )
                assert participant.concrete_topology_id == observed.id
                assert participant.mapped_smiles == "[H:1][H:2]"
                assert prefer_observed_participant_topologies(session, [mapped.id]) == ()
                assert session.get(MolecularTopology, stale.id).graph_hash == old_hash
                assert session.get(MolecularTopology, observed.id).graph_hash == observed_hash
        finally:
            transaction.rollback()
    engine.dispose()


@pytest.mark.parametrize("missing", ["generation", "policy"])
def test_audit_recovers_missing_queue_and_does_not_retry_healthy_incomplete_profiles(missing):
    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                sample = _create_domain_sample(session)
                mapped = sample[6]
                mapped.thermodynamic_profile_policy_version = (
                    MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION
                )
                mapped.thermodynamic_profile_generation = 7
                mapped.thermodynamic_profile_materialized_generation = 7
                session.add(mapped)
                session.flush()
                cursor = UUID(int=mapped.id.int - 1)
                assert (
                    recover_profile_refresh_dependencies(
                        session, after_id=cursor, batch_size=1
                    ).enqueued
                    == 0
                )
                if missing == "generation":
                    mapped.thermodynamic_profile_generation = 8
                else:
                    mapped.thermodynamic_profile_policy_version = "legacy"
                session.add(mapped)
                session.flush()
                assert (
                    recover_profile_refresh_dependencies(
                        session, after_id=cursor, batch_size=1
                    ).enqueued
                    == 1
                )
                generation = mapped.thermodynamic_profile_generation
                assert (
                    recover_profile_refresh_dependencies(
                        session, after_id=cursor, batch_size=1
                    ).enqueued
                    == 0
                )
                assert mapped.thermodynamic_profile_generation == generation
        finally:
            transaction.rollback()
    engine.dispose()
