# DA Benchmark Minimal Fixture

[中文](README.md) | [English](README.en.md)

This fixture is a self-contained frozen subset of the user-provided `.tmp`
DA-bench snapshot. Tests and development seed use the compressed logs and
metadata committed here; they neither depend on the original source directory
nor require pre-organized uploads.

The fixed reaction is:

```text
C=C + c1scc2c1OCCO2 -> C1COC2=C(O1)[C@@H]1CC[C@H]2S1
```

The four Gaussian logs are ene, diene, TS `conf_01`, and `product_00`.
`conf_01` has one imaginary frequency, allowing MolOP endpoint inference. Its
`file_frame_index=22` is terminal/converged and supplies Gibbs free energy, so
it can provide a Reaction-Geometry-Frame binding. Optimization intermediates are
still persisted as calculation facts but are not reaction-node bindings.

`manifest.json` pins every raw log and deterministic gzip (`mtime=0`) SHA-256,
the DA-bench metadata sizes/hashes, mapped reaction and atom-map data, path-node
and frame selection data, and expected MolOP/scientific-array totals. The
baseline is 9 segments, 45 frames, and 227 `ScientificArray` rows; use
`manifest.json` for the complete breakdown.

Re-freeze from the same `.tmp` snapshot only when deliberately updating the
fixture:

```bash
uv run python scripts/freeze_da_bench_fixture.py \
  --source-root .tmp \
  --output-root tests/fixtures/da_bench_minimal
```

The generator uses deterministic compression and stable JSON order. Tests verify
compressed and decompressed hashes before sending logs to MolOP. CI also runs
`make validate-da-bench-fixture` before seed.

This fixture validates Gaussian multi-Link1, dump-first parsing, ReactionPath
declaration, and database round trips. Separate Gaussian/ORCA fixtures cover
cross-software reuse and must not be replaced by this result.
