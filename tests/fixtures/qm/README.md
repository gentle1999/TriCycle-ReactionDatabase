# QM parser fixtures

`minimal_orca_water_sp.orcaout` is a reduced ORCA 6.1.1 single-point output derived from the
field structure of a real ORCA output supplied for ingestion testing. It retains only the parser
contract evidence needed for a deterministic water calculation: banner/version, printed input,
Angstrom coordinates, SCF convergence, final energy, termination, and runtime.

The fixture is intentionally independent of filenames and directory layout. Tests pass it to
MolOP as `unstructured-upload.bin` and require content probing to identify `orcaout`.
