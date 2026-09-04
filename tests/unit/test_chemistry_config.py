from tricycle_reaction_db.application.services.geometry_energy import (
    GEOMETRY_ENERGY_POLICY_VERSION as service_geometry_energy_policy_version,
)
from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics import (
    MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION as service_thermodynamics_policy_version,
)
from tricycle_reaction_db.application.services.reaction_stereo_projection import (
    INVERSION_LABILE_RULES as service_inversion_labile_rules,
)
from tricycle_reaction_db.application.services.reaction_stereo_projection import (
    INVERSION_STEREO_PROJECTION_POLICY_VERSION as service_inversion_policy_version,
)
from tricycle_reaction_db.application.services.reaction_stereo_projection import (
    InversionLabileRule as service_inversion_labile_rule_type,
)
from tricycle_reaction_db.application.services.topology_abstraction import (
    STEREO_ABSTRACTION_POLICY_VERSION as service_stereo_abstraction_policy_version,
)
from tricycle_reaction_db.core.chemistry_config import (
    GEOMETRY_ENERGY_POLICY_VERSION,
    INVERSION_LABILE_RULES,
    INVERSION_STEREO_PROJECTION_POLICY_VERSION,
    LOGICAL_PARTICIPANT_CONCRETE_MATCH_POLICY_VERSION,
    LOGICAL_PARTICIPANT_CONCRETE_MATCH_SCHEMA_VERSION,
    MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION,
    STEREO_ABSTRACTION_MATCH_SCHEMA_VERSION,
    STEREO_ABSTRACTION_POLICY_VERSION,
    InversionLabileRule,
)


def test_chemistry_policy_values_have_one_source_of_truth() -> None:
    assert service_geometry_energy_policy_version is GEOMETRY_ENERGY_POLICY_VERSION
    assert service_thermodynamics_policy_version is MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION
    assert service_stereo_abstraction_policy_version is STEREO_ABSTRACTION_POLICY_VERSION
    assert service_inversion_policy_version is INVERSION_STEREO_PROJECTION_POLICY_VERSION
    assert service_inversion_labile_rules is INVERSION_LABILE_RULES
    assert service_inversion_labile_rule_type is InversionLabileRule
    assert LOGICAL_PARTICIPANT_CONCRETE_MATCH_SCHEMA_VERSION == (
        LOGICAL_PARTICIPANT_CONCRETE_MATCH_POLICY_VERSION
    )
    assert STEREO_ABSTRACTION_MATCH_SCHEMA_VERSION == "topology-stereo-abstraction-match-v1"


def test_inversion_rule_registry_is_immutable_and_explicit() -> None:
    assert isinstance(INVERSION_LABILE_RULES, tuple)
    assert tuple(rule.rule_id for rule in INVERSION_LABILE_RULES) == ("neutral-trivalent-nitrogen",)
    assert tuple(rule.atom_smarts for rule in INVERSION_LABILE_RULES) == ("[N;X3;v3;+0]",)
