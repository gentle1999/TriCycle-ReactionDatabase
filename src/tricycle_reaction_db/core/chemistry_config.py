"""Code-owned chemistry policies and persisted identity versions.

These values are deliberately separate from :class:`Settings`: they describe
how chemical identities and relations are derived, rather than deployment
environment.  Changing one changes the meaning of persisted data and therefore
requires an explicit re-import or migration; they must not be silently
overridden by an environment variable.
"""

from dataclasses import dataclass
from typing import Final

# Formula, topology, geometry, and calculation-protocol identity contracts.
CALCULATION_PROTOCOL_VERSION: Final[str] = "calculation-protocol-v1"
FORMULA_COMPOSITION_VERSION: Final[str] = "formula-composition-v1"
TOPOLOGY_IDENTITY_VERSION: Final[str] = "topology-identity-v1"
TOPOLOGY_SOURCE_ORDER_STEREO_IDENTITY_VERSION: Final[str] = (
    "topology-source-order-stereo-identity-v1"
)
TOPOLOGY_DERIVATION_VERSION: Final[str] = "topology-derivation-v1"
GEOMETRY_CANONICALIZATION_VERSION: Final[str] = "geometry-internal-coordinates-v1"


# Directed topology abstraction and concrete reaction membership contracts.
STEREO_ABSTRACTION_POLICY_VERSION: Final[str] = "topology-stereo-abstraction-v1"
STEREO_ABSTRACTION_RECONSTRUCTION_METHOD: Final[str] = "topology/stereo-abstraction"
STEREO_ABSTRACTION_MATCH_SCHEMA_VERSION: Final[str] = "topology-stereo-abstraction-match-v1"
LOGICAL_PARTICIPANT_CONCRETE_MATCH_POLICY_VERSION: Final[str] = (
    "logical-participant-concrete-match-v1"
)
LOGICAL_PARTICIPANT_CONCRETE_MATCH_SCHEMA_VERSION: Final[str] = (
    LOGICAL_PARTICIPANT_CONCRETE_MATCH_POLICY_VERSION
)


# Geometry/reaction and energy projection contracts.
GEOMETRY_MATCH_POLICY_VERSION: Final[str] = "geometry-internal-coordinate-match-v4"
REACTION_GEOMETRY_LINK_METHOD: Final[str] = "topology-identity"
REACTION_GEOMETRY_LINK_POLICY_VERSION: Final[str] = "reaction-geometry-link-v1"
GEOMETRY_ENERGY_POLICY_VERSION: Final[str] = "geometry-energy-view-v1"
MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION: Final[str] = "mapped-reaction-thermodynamics-v1"


@dataclass(frozen=True, slots=True)
class InversionLabileRule:
    """One extensible atom pattern whose strict stereo is hidden logically."""

    rule_id: str
    atom_smarts: str


INVERSION_STEREO_PROJECTION_POLICY_VERSION: Final[str] = "reaction-inversion-stereo-projection-v1"

# Keep this registry intentionally explicit.  Adding a new chemically
# reversible centre must be a reviewed rule, rather than an accidental global
# ``useChirality=False`` match.
INVERSION_LABILE_RULES: Final[tuple[InversionLabileRule, ...]] = (
    InversionLabileRule(rule_id="neutral-trivalent-nitrogen", atom_smarts="[N;X3;v3;+0]"),
)


__all__ = [
    "CALCULATION_PROTOCOL_VERSION",
    "FORMULA_COMPOSITION_VERSION",
    "TOPOLOGY_IDENTITY_VERSION",
    "TOPOLOGY_SOURCE_ORDER_STEREO_IDENTITY_VERSION",
    "TOPOLOGY_DERIVATION_VERSION",
    "GEOMETRY_CANONICALIZATION_VERSION",
    "STEREO_ABSTRACTION_POLICY_VERSION",
    "STEREO_ABSTRACTION_RECONSTRUCTION_METHOD",
    "STEREO_ABSTRACTION_MATCH_SCHEMA_VERSION",
    "LOGICAL_PARTICIPANT_CONCRETE_MATCH_POLICY_VERSION",
    "LOGICAL_PARTICIPANT_CONCRETE_MATCH_SCHEMA_VERSION",
    "GEOMETRY_MATCH_POLICY_VERSION",
    "REACTION_GEOMETRY_LINK_METHOD",
    "REACTION_GEOMETRY_LINK_POLICY_VERSION",
    "GEOMETRY_ENERGY_POLICY_VERSION",
    "MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION",
    "INVERSION_STEREO_PROJECTION_POLICY_VERSION",
    "INVERSION_LABILE_RULES",
    "InversionLabileRule",
]
