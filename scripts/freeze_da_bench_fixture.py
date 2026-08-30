"""Freeze the checked DA benchmark subset from a user-supplied source snapshot."""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
from hashlib import sha256
from pathlib import Path
from typing import Any

SOURCE_FILES = {
    "ene": (
        "000000000000/000000000000_02.ene.log",
        "reac/000000000000/000000000000_02.ene.log.gz",
    ),
    "diene": (
        "000000403256/000000403256_03.diene.log",
        "reac/000000403256/000000403256_03.diene.log.gz",
    ),
    "transition_state": (
        "000000000000_000000403256/00/ts/000000000000_000000403256_00_conf_01_ts.43b3faa8fcc9.log",
        "complete_set/000000000000_000000403256/00/ts/"
        "000000000000_000000403256_00_conf_01_ts.43b3faa8fcc9.log.gz",
    ),
    "product": (
        "000000000000_000000403256/00/prod/000000000000_000000403256_00_00.prod.log",
        "complete_set/000000000000_000000403256/00/prod/"
        "000000000000_000000403256_00_00.prod.log.gz",
    ),
}

METADATA_FILES = {
    "000000000000/000000000000.json": "reac/000000000000/000000000000.json",
    "000000403256/000000403256.json": "reac/000000403256/000000403256.json",
    "000000000000_000000403256/00/000000000000_000000403256_00.json": (
        "complete_set/000000000000_000000403256/00/000000000000_000000403256_00.json"
    ),
}

LOG_EXPECTATIONS = {
    "ene": (5, 6, 2, "C2H4", "[H][C]([H])=[C]([H])[H]", 0),
    "diene": (
        7,
        15,
        2,
        "C6H6O2S",
        "[H][c]1[s][c]([H])[c]2[c]1[O][C]([H])([H])[C]([H])([H])[O]2",
        0,
    ),
    "transition_state": (
        23,
        21,
        3,
        "C8H10O2S",
        "[H][C]([H])=[C]([H])[H].[H][c]1[s][c]([H])[c]2[c]1[O][C]([H])([H])[C]([H])([H])[O]2",
        1,
    ),
    "product": (
        10,
        21,
        2,
        "C8H10O2S",
        "[H][C]1([H])[O][C]2=[C]([O][C]1([H])[H])[C@@]1([H])[S][C@]2([H])[C]([H])([H])[C]1([H])[H]",
        0,
    ),
}

EXPECTED_MOLOP_TOTALS = {
    "frames": 45,
    "segments": 9,
    "arrays": 227,
    "thermochemistry": 4,
    "energies": 45,
    "energy_observations": 49,
    "vibrations": 4,
    "optimizations": 40,
    "statuses": 40,
    "molecular_orbitals": 14,
    "population_results": 14,
    "population_series": 18,
    "polarizabilities": 14,
    "array_assignments": 106,
    "running_times": 45,
}

EXPECTED_ARRAY_COUNTS = {
    "forces": 40,
    "hessian": 4,
    "vibrational_frequencies": 4,
    "reduced_masses": 4,
    "vibrational_force_constants": 4,
    "ir_intensities": 4,
    "normal_modes": 4,
    "moments_of_inertia": 4,
    "rotational_temperatures": 4,
    "rotational_constants": 45,
    "vibrational_temperatures": 4,
    "orbital_alpha_energies": 14,
    "atomic_population": 18,
    "polarizability_tensor": 4,
    "dipole": 14,
    "quadrupole": 14,
    "traceless_quadrupole": 14,
    "octapole": 14,
    "hexadecapole": 14,
}

MAPPED_REACTION_SMILES = (
    "[C:1](=[C:2]([H:5])[H:6])([H:3])[H:4]."
    "[c:7]1([H:16])[s:8][c:9]([H:17])[c:10]2[c:11]1[O:12]"
    "[C:13]([H:18])([H:19])[C:14]([H:20])([H:21])[O:15]2>>"
    "[C:1]1([H:3])([H:4])[C:2]([H:5])([H:6])[C@@:7]2([H:16])"
    "[S:8][C@:9]1([H:17])[C:10]1=[C:11]2[O:12][C:13]([H:18])"
    "([H:19])[C:14]([H:20])([H:21])[O:15]1"
)

# Atom maps are ordered by each source log's original Cartesian atom order.
# Geometry uses canonical atom order, so the stored source-to-Geometry permutation
# converts these lists into the mapped-reaction topology order.
SOURCE_ATOM_MAP_NUMBERS = {
    "ene": [1, 2, 3, 4, 5, 6],
    "diene": list(range(7, 22)),
    "transition_state": list(range(1, 22)),
    "product": [1, 2, 4, 3, 6, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 19, 18, 21, 20],
}

FIXTURE_README = """# DA benchmark minimal fixture

该 fixture 是从用户提供的 `.tmp` DA-bench 快照冻结出的自包含测试子集。测试和开发
seed 直接使用仓库内的压缩日志与元数据，不依赖原始目录继续存在，也不要求用户预先整理
上传文件。

固定反应为：

```text
C=C + c1scc2c1OCCO2 -> C1COC2=C(O1)[C@@H]1CC[C@H]2S1
```

四个 Gaussian 日志分别是 ene、diene、TS `conf_01` 和 `product_00`。`conf_01` 具有
一个虚频，MolOP 可以推断反应端点；其 `file_frame_index=22` 同时是 terminal/converged
帧，因此可以作为 Reaction-Geometry-Frame 绑定来源。优化中间帧仍作为计算事实保存，
但不得绑定到反应节点。

`manifest.json` 固定以下内容：

- 每个原始日志和 deterministic gzip (`mtime=0`) 的 SHA-256；
- 三个 DA-bench 元数据 JSON 的字节大小和 SHA-256；
- mapped reaction、参与物 atom map、路径节点和 frame selector；
- MolOP 解析总量与各类科学数组数量。

当前基线为 9 个 segment、45 个 frame 和 227 个 `ScientificArray`。其中包括 45 个
`FrameEnergyResult`、49 个 `EnergyObservation`、40 个
`GeometryOptimizationResult`、40 个 `CalculationStatusResult`，以及各 4 个
`VibrationResult` 和 `ThermochemistryResult`。完整分项以 `manifest.json` 为准。

如需从同一 `.tmp` 快照重新冻结：

```bash
uv run python scripts/freeze_da_bench_fixture.py \\
  --source-root .tmp \\
  --output-root tests/fixtures/da_bench_minimal
```

生成器使用固定压缩参数和稳定 JSON 顺序；重新生成后目录应无差异。普通测试会先验证
压缩和解压后的 hash，再将日志交给 MolOP。

CI 在 seed 前还会执行 `make validate-da-bench-fixture`，独立验证 schema version、所有日志的
压缩/解压后大小与 SHA-256，以及所有元数据文件的大小与 SHA-256。

该 fixture 只证明 Gaussian 多 Link1、dump-first 解析、ReactionPath 声明和数据库往返。
ORCA 跨软件同几何复用由独立的 Gaussian/ORCA fixture 覆盖，不能由本 fixture 的结果
代替。
"""


def _digest(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def _selector(segment_index: int, file_frame_index: int) -> dict[str, int]:
    return {
        "segment_index": segment_index,
        "frame_index": 0,
        "file_frame_index": file_frame_index,
    }


def _workflow() -> dict[str, Any]:
    selectors = {
        "ene": _selector(1, 4),
        "diene": _selector(1, 6),
        "transition_state": _selector(2, 22),
        "product": _selector(1, 9),
    }
    participants = [
        {
            "side": "reactant",
            "participant_index": 0,
            "role": "dienophile",
            "log_role": "ene",
            "source_atom_map_numbers": SOURCE_ATOM_MAP_NUMBERS["ene"],
        },
        {
            "side": "reactant",
            "participant_index": 1,
            "role": "diene",
            "log_role": "diene",
            "source_atom_map_numbers": SOURCE_ATOM_MAP_NUMBERS["diene"],
        },
        {
            "side": "product",
            "participant_index": 0,
            "role": "product",
            "log_role": "product",
            "source_atom_map_numbers": SOURCE_ATOM_MAP_NUMBERS["product"],
        },
    ]
    component_specs = (
        ("reactants", "reactant", "reactant:0", 0, "ene", "reactant", 0),
        ("reactants", "reactant", "reactant:1", 1, "diene", "reactant", 1),
        (
            "transition-state",
            "transition_state",
            "transition-state",
            0,
            "transition_state",
            None,
            None,
        ),
        ("product", "product", "product:0", 0, "product", "product", 0),
    )
    nodes: dict[str, dict[str, Any]] = {}
    for (
        node_key,
        node_role,
        component_key,
        component_index,
        log_role,
        side,
        index,
    ) in component_specs:
        node = nodes.setdefault(
            node_key,
            {
                "node_key": node_key,
                "node_index": len(nodes),
                "role": node_role,
                "components": [],
            },
        )
        component: dict[str, Any] = {
            "component_key": component_key,
            "component_index": component_index,
            "log_role": log_role,
            "geometry_authority": selectors[log_role],
            "thermochemistry_source": selectors[log_role],
            "source_atom_map_numbers": SOURCE_ATOM_MAP_NUMBERS[log_role],
        }
        if side is not None and index is not None:
            component["participant_side"] = side
            component["participant_index"] = index
        node["components"].append(component)
    return {
        "manifest_key": "da-bench:000000000000_000000403256:00",
        "reaction_key": "000000000000_000000403256_00",
        "path_key": "conf-01-product-00",
        "mapped_reaction_smiles": [MAPPED_REACTION_SMILES],
        "participants": participants,
        "nodes": list(nodes.values()),
        "edge": {
            "edge_key": "cycloaddition-step",
            "edge_kind": "elementary_step",
            "source_node_key": "reactants",
            "target_node_key": "product",
            "transition_state_node_key": "transition-state",
        },
    }


def freeze_fixture(source_root: Path, output_root: Path) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    logs: list[dict[str, Any]] = []
    for role, (source_relative, output_relative) in SOURCE_FILES.items():
        source_path = source_root / source_relative
        payload = source_path.read_bytes()
        compressed = gzip.compress(payload, compresslevel=9, mtime=0)
        output_path = output_root / output_relative
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(compressed)
        frame_count, atom_count, normal_count, formula, topology, imaginary_count = (
            LOG_EXPECTATIONS[role]
        )
        entry = {
            "role": role,
            "relative_path": output_relative,
            "source_size_bytes": len(payload),
            "source_sha256": _digest(payload),
            "gzip_sha256": _digest(compressed),
            "frame_count": frame_count,
            "atom_count": atom_count,
            "normal_termination_count": normal_count,
            "final_formula": formula,
            "final_topology_smiles": topology,
            "final_imaginary_frequency_count": imaginary_count,
        }
        if role == "transition_state":
            entry["reaction_frame_file_index"] = 22
            entry["positive_frequency_mode_count"] = 56
        logs.append(entry)

    metadata_files = []
    for source_relative, output_relative in METADATA_FILES.items():
        source_path = source_root / source_relative
        output_path = output_root / output_relative
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, output_path)
        metadata_files.append(
            {
                "relative_path": output_relative,
                "size_bytes": output_path.stat().st_size,
                "sha256": _digest(output_path.read_bytes()),
            }
        )

    manifest = {
        "schema_version": "da-bench-fixture-v3",
        "source": "user-supplied .tmp snapshot frozen on 2026-08-12",
        "reaction": {
            "ene_id": "000000000000",
            "diene_id": "000000403256",
            "prod_id": "00",
            "reaction_smiles": ("C=C.c1scc2c1OCCO2>>C1COC2=C(O1)[C@@H]1CC[C@H]2S1"),
        },
        "workflow": _workflow(),
        "logs": logs,
        "metadata_files": metadata_files,
        "expected_molop_totals": EXPECTED_MOLOP_TOTALS,
        "expected_array_counts": EXPECTED_ARRAY_COUNTS,
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    (output_root / "README.md").write_text(FIXTURE_README, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(".tmp"))
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("tests/fixtures/da_bench_minimal"),
    )
    args = parser.parse_args()
    freeze_fixture(args.source_root.resolve(), args.output_root.resolve())


if __name__ == "__main__":
    main()
