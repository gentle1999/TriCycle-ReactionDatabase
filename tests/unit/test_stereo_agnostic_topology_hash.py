from rdkit import Chem

from tricycle_reaction_db.ingestion.normalization import (
    normalize_topology,
    stereo_agnostic_graph_hash,
)


def test_stereo_agnostic_topology_hash_ignores_stereo_but_keeps_graph_identity() -> None:
    cis = Chem.MolFromSmiles("C/C=C/C")
    trans = Chem.MolFromSmiles("C/C=C\\C")
    different_order = Chem.MolFromSmiles("CC#CC")
    assert cis is not None
    assert trans is not None
    assert different_order is not None

    cis_record = normalize_topology(
        cis,
        add_hydrogens=True,
        reconstruction_method="tests/hash",
        reconstruction_version="1",
    )
    trans_record = normalize_topology(
        trans,
        add_hydrogens=True,
        reconstruction_method="tests/hash",
        reconstruction_version="1",
    )
    different_order_record = normalize_topology(
        different_order,
        add_hydrogens=True,
        reconstruction_method="tests/hash",
        reconstruction_version="1",
    )

    assert cis_record.topology.graph_hash != trans_record.topology.graph_hash
    assert (
        cis_record.topology.stereo_agnostic_graph_hash
        == trans_record.topology.stereo_agnostic_graph_hash
    )
    assert (
        cis_record.topology.stereo_agnostic_graph_hash
        != different_order_record.topology.stereo_agnostic_graph_hash
    )


def test_stereo_agnostic_topology_hash_retains_isotope_identity() -> None:
    deuterium_tritium = Chem.MolFromSmiles("[2H][3H]")
    deuterium_deuterium = Chem.MolFromSmiles("[2H][2H]")
    assert deuterium_tritium is not None
    assert deuterium_deuterium is not None

    assert stereo_agnostic_graph_hash(deuterium_tritium) != stereo_agnostic_graph_hash(
        deuterium_deuterium
    )
