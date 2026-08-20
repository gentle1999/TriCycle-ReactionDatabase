# QM parser fixtures

`minimal_orca_water_sp.orcaout` is a reduced ORCA 6.1.1 single-point output derived from the
field structure of a real ORCA output supplied for ingestion testing. It retains only the parser
contract evidence needed for a deterministic water calculation: banner/version, printed input,
Angstrom coordinates, SCF convergence, final energy, termination, and runtime.

`minimal_gaussian_water_sp.log` is the corresponding reduced Gaussian 16 A.03 single-point
contract fixture with the same source-order water geometry and HF/STO-3G protocol. Together the
two fixtures verify that calculation software and protocol do not participate in Geometry identity.

The fixtures are intentionally independent of filenames and directory layout. Tests pass them to
MolOP as `unstructured-upload.bin` and require content probing to identify `g16log` or `orcaout`.
