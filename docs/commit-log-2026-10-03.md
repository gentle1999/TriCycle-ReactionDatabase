# 提交日志：前后端依赖安全更新

- 日期：2026-10-03
- 基线提交：`25800c7`
- 范围：依赖版本约束、前后端锁文件及本日志；不修改业务逻辑。

## 背景

CI 安全审计发现前端 `brace-expansion` 的拒绝服务漏洞，以及 Python 运行时 PyJWT、urllib3 的已知漏洞。修复通过升级依赖完成，没有增加漏洞忽略规则或降低审计门槛。

## 变更

| 依赖 | 原锁定版本 | 新锁定版本 | 变更位置 |
| --- | --- | --- | --- |
| brace-expansion | 2.1.4 | 2.1.7 | `frontend/package-lock.json` |
| PyJWT | 2.13.0 | 2.15.0 | `pyproject.toml`、`uv.lock` |
| urllib3 | 2.7.0 | 2.8.0 | `pyproject.toml`、`uv.lock` |

- `brace-expansion` 由 `vue-tsc → @vue/language-core → minimatch` 间接引入，保持 2.x 主版本，仅更新该锁定依赖；新包下载地址使用官方 npm registry。
- PyJWT 的直接依赖约束从 `>=2.10,<3` 提高为 `>=2.15,<3`，避免重新解析依赖时选回受影响版本。
- urllib3 由 botocore 间接引入，在 `[tool.uv]` 增加 `constraint-dependencies = ["urllib3>=2.8,<3"]`，保持间接依赖关系并限制最低安全版本。
- Python 锁文件仅更新这两个包、相应元数据及约束，没有进行全量依赖升级。

## 已完成验证

以下结果来自提交前的修复验证，本次整理日志时未重复运行测试：

| 检查 | 结果 |
| --- | --- |
| `npm --prefix frontend audit --audit-level=high --registry=https://registry.npmjs.org` | `found 0 vulnerabilities`，退出码 0 |
| 冻结导出运行时依赖后执行 pip-audit 2.10.1 | `No known vulnerabilities found`，退出码 0 |
| `npm --prefix frontend run check` | 前端类型检查和构建通过 |
| `uv lock --check` | 通过 |
| `git diff --check` | 通过 |

Python 审计使用与 CI 一致的命令：

```bash
uv export --frozen --no-dev --no-emit-project --no-hashes \
  --format requirements-txt --output-file /tmp/tricycle-runtime-requirements.txt
uvx --from pip-audit==2.10.1 pip-audit --disable-pip --no-deps \
  -r /tmp/tricycle-runtime-requirements.txt
```

审计结果反映执行时的漏洞数据库。pip-audit 的无哈希提示及前端构建的 bundle 大小提示仍存在，但检查均成功退出。本轮未重新运行后端单元/集成测试或 Playwright；此前重构的测试结果不能替代本次升级后的完整回归。

## 提交与部署边界

本次提交仅包含上述三个依赖文件和本日志。不包含临时审计输出、凭据或构建产物；不执行生产部署、数据库迁移或远端推送。
