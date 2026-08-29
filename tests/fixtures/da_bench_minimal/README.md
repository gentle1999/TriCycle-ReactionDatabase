# DA benchmark minimal fixture

[中文](README.md) | [English](README.en.md)

该 fixture 是从用户提供的 `.tmp` DA-bench 快照冻结出的自包含测试子集。测试和开发
seed 直接使用仓库内的压缩日志与元数据，不依赖原始目录继续存在，也不要求用户预先整理
上传文件。

固定反应为：

```text
C=C + c1scc2c1OCCO2 -> C1COC2=C(O1)[C@@H]1CC[C@H]2S1
```

四个 Gaussian 日志分别是 ene、diene、TS `conf_01` 和 `product_00`。`conf_01` 具有
一个虚频，MolOP 可以推断反应端点；其 `file_frame_index=22` 同时是 terminal/converged
帧，并提供 Gibbs 自由能，因此可以作为 Reaction-Geometry-Frame 绑定来源。优化中间帧仍
作为计算事实保存，但不得绑定到反应节点。

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
uv run python scripts/freeze_da_bench_fixture.py \
  --source-root .tmp \
  --output-root tests/fixtures/da_bench_minimal
```

生成器使用固定压缩参数和稳定 JSON 顺序；重新生成后目录应无差异。普通测试会先验证
压缩和解压后的 hash，再将日志交给 MolOP。

CI 在 seed 前还会执行 `make validate-da-bench-fixture`，独立验证 schema version、所有日志的
压缩/解压后大小与 SHA-256，以及所有元数据文件的大小与 SHA-256。

该 fixture 只证明 Gaussian 多 Link1、dump-first 解析、ReactionPath 声明和数据库往返。
ORCA 跨软件同几何复用由独立的 Gaussian/ORCA fixture 覆盖，不能由本 fixture 的结果
代替。
