# 提交日志：拓扑匹配与 profile 刷新优化

- 日期：2026-09-20
- 提交主题：限制昂贵的分子图匹配，稳定分子序列化，并补充真实规模导入回归。

## 背景

真实项目中的 profile 刷新仍可能在准备来源时读取大量拓扑和 MOL 数据，并触发昂贵的 RDKit 全图匹配。上传、重解析和局部化学投影也需要更可靠的帧保留、立体化学和分子序列化语义。此前的性能样本规模不足以代表持续并行导入，因此需要把极限样本和性能基准纳入 CI。

## 主要变更

1. **以拓扑 hash 和 DAG 缩小候选范围**：增加版本化的无立体拓扑 hash，并通过迁移 `0057_stereo_agnostic_hash` 为既有拓扑回填和建立索引。Hash 忽略立体信息但保留同位素；DAG 查找限定在相关连通分量内，避免无关的全项目扫描。
2. **在昂贵匹配前做数据库资格筛选**：profile 来源先按 endpoint 的 DAG 范围、hash、Geometry 收敛状态、热化学和虚频资格过滤；只有确实可能贡献 profile 的候选才读取 MOL。旧数据或缺失 hash 才走有界的 RDKit 兼容性回退，多候选歧义保持未解析，不猜测绑定。
3. **为大分子图匹配增加保护并复用证据**：大图匹配可在可终止的子进程中执行并受超时、原子数和结果数限制；已知原子映射、DAG witness 和会话内缓存可证明匹配时不再重复全图搜索。DAG 持久化、回填和 profile 刷新使用一致的候选语义。
4. **修正安全序列化和立体投影**：规范化时不修改 MolOP 提供的源分子；Round-trip 验证区分结构与自由基状态，映射拓扑 SMILES 在移除立体信息时仍保留同位素。清理残留方向键标志，避免已移除的 E/Z 信息在再次规范化时被重新推断；反应 endpoint 与 TS 的 E/Z 不再错误互相约束。
5. **降低 profile 刷新的锁占用和冗余计算**：按 32 个 reaction 分块处理。昂贵来源计算不再持有 `MappedReaction` 写锁；profile 替换、可见性刷新和 generation/job 收尾拆成较短事务，并在刷新期间有新证据到达时保留后续任务。
6. **加固上传与重解析路径**：解析超时随输入文件大小增长；保持部分成功文件中可用帧的持久化；重解析清理旧结果后重新建立 ORM 状态，避免清理操作使待写记录被过期或异步懒加载丢失。
7. **补充前端和真实规模测试**：文件批量下载增加进度呈现；加入 256 文件真实语料、极端文件和 SDF endpoint 样本、阶段化 RustFS 导入回归以及 JSON/Markdown 性能报告生成，并将全进程池基准加入 CI。
8. **更新运维说明**：同步中英文上传时序、性能配置、拓扑/profile 设计说明及 MolOP 超时和图匹配参数示例。

## 验证结果

- `uv run --frozen ruff check src tests migrations scripts`、`ruff format --check`、`mypy src scripts`、`pyright src scripts`：全部通过。
- `uv run --frozen pytest`：578 passed，135 项按需启用的集成测试跳过。
- `TRICYCLE_RUN_DATABASE_TESTS=1 uv run --frozen pytest -m "integration and not rustfs"`：118 passed，2 个 Redis 用例按标记跳过。
- `TRICYCLE_RUN_DATABASE_TESTS=1 TRICYCLE_RUN_RUSTFS_TESTS=1 uv run --frozen pytest -m rustfs`：16 passed。
- `TRICYCLE_RUN_REDIS_TESTS=1 uv run --frozen pytest -m redis`：2 passed。本机 Docker Hub 无法拉取 Redis 镜像，使用 Redis 协议兼容的 Valkey 7.2.4 运行。
- `npm --prefix frontend run build` 及 `NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1 npm --prefix frontend run test:e2e`：构建成功，Playwright 58/58 passed。
- `uv run --frozen python scripts/validate_da_bench_fixture.py`：通过。
- `alembic upgrade head` 后执行 `alembic check`：无 schema 差异；开发 bootstrap 连续运行两次结果一致。
- `python3 scripts/validate_caddy_runtime.py` 及 Caddyfile `caddy validate`：通过，运行时探测覆盖 17 条代理路径。
- 真实数据进程池基准命令：

  ```sh
  uv run --frozen python scripts/benchmark_real_world_ingestion.py \
    --fixture-dir <expanded-real-world-batch-256> \
    --fixture-dir tests/fixtures/real_world_extremes \
    --n-jobs -1 --output <report.json> --markdown-output <report.md>
  ```

  结果：263/263 文件成功，10,098/10,098 帧物化，650 个 segment，无失败 inference 或解析诊断。32 个 worker、128 并发文件，墙钟 70.261 秒，约 143.721 帧/秒和 8.173 uncompressed MiB/秒。

性能基准只计本地文件准备之后的 MolOP 解析和帧物化，不包含 RustFS 上传、PostgreSQL 持久化或 profile 刷新；该吞吐不能等同于端到端上传吞吐。CI 会上传 JSON/Markdown 基准报告。本机报告已生成在 `/tmp`，未纳入版本库。

## 未执行项与提示

- 本机无法拉取 Docker Hub Redis 镜像，Redis 用例使用 Valkey 7.2.4；部署 CI 仍使用其独立 Redis 服务。
- 当前 macOS 环境未安装 `promtool` 和 `systemd-analyze`，因此 Prometheus rules 与 systemd unit 的平台校验未执行。
- Ruff、测试和前端构建均通过；输出包含已有 SQLModel/Starlette 弃用告警，前端构建另有大 chunk 提示。
