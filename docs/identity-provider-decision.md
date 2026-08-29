# 身份提供方决策记录

> English edition: [Identity-provider decision record](en/identity-provider-decision.md). This is a dated decision record.

建立日期：2026-08-16

## 决策

本轮保留 Keycloak，不切换到服务端 npm 身份服务。Keycloak 仍只用于 OIDC 身份生命周期，
FastAPI 继续负责 `ExternalIdentity` 映射、项目权限、审计和本地 opaque Session。该决定不
意味着 Keycloak 可以直接暴露到公网：当前 Compose 服务只绑定 loopback，生产部署仍要求同源
HTTPS 反向代理、Secure Cookie 和经过验证的 OIDC issuer。

复查日期：2026-11-16。只有在独立身份服务完成迁移、恢复、密钥轮换、反向代理和浏览器回归后，
才重新评估切换。

## 候选 PoC

候选包在 2026-08-16 重新读取了 npm registry 的版本：

| 候选 | 版本 | 结果 |
| --- | --- | --- |
| `better-auth` | `1.6.29` | 包可安装；需要独立账户/Session 数据库、邮件和管理交互；不是现有 Keycloak realm 的无迁移替代品 |
| `@better-auth/oauth-provider` | `1.6.29` | `oauthProvider`、`oidcServerMetadata` 和 discovery endpoint 导出可导入；仍需 Better Auth 应用和账户生命周期集成 |
| `oidc-provider` | `9.11.3` | Node 22 disposable provider 返回 discovery、JWKS、logout 和 `S256` PKCE；需要自建登录/同意页、账户 adapter、邮件、MFA 和管理边界 |

临时 tarball SHA-256 为：

```text
better-auth-1.6.29.tgz                 1c437056eb7fbe4329865661219711401ae61be0bf58e8bbcb505f4d31df27a4
better-auth-oauth-provider-1.6.29.tgz  179a3652777fbbd536392066a72785bf19bb41a085c9067d268fb72b72fe7d28
oidc-provider-9.11.3.tgz               ad192ca940380e598d124e149c2a8f78c6e66ef5d26f06ef4c4aa5d4b5ed9575
```

`oidc-provider` 的 disposable smoke 使用 Node `v22.23.2`，因为 `9.11.3` 明确拒绝当前
Node 20 runtime；结果为 HTTP 200，并包含 `/auth`、`/token`、`/jwks`、`/session/end` 和
`code_challenge_methods_supported: ["S256"]`。该 smoke 只证明协议核心，不证明生产身份服务。
它还明确警告默认 in-memory adapter、开发 signing keys 和 dev interactions，正是本项目不能
直接采用的部分。

## 现有 Keycloak 基线和恢复演练

- 镜像为 Keycloak `26.3.2`，Compose 使用明确版本 tag；生产发布时应记录实际拉取的
  digest 和镜像签名验证结果。
- 本地稳定运行观察到约 `595 MiB` 容器内存；这是开发 H2/`start-dev` 基线，不是生产容量承诺。
- 在 Keycloak 停止后执行 `kc.sh export --dir=/backup --realm=tricycle --users=realm_file`，
  生成 `tricycle-realm.json`，大小 `78,380` bytes，包含 realm `tricycle`、7 个 clients、
  1 个 user 和 3 个 realm roles。
- 将该文件挂载到全新 Keycloak 26.3.2 容器和全新 volume，启动 `--import-realm`，再用
  `kcadm.sh get clients -r tricycle` 与 `get users -r tricycle` 验证恢复结果为 7 个 clients、
  1 个 user（`developer@localhost`）。验证后删除了临时容器和 volume；原开发服务已重新启动。

可复现的本地流程如下。备份目录必须是权限为 `700` 的临时目录，不能提交 Git，也不能写入日志：

```bash
backup_dir="$(mktemp -d /tmp/tricycle-keycloak-backup.XXXXXX)"
chmod 700 "$backup_dir"
docker compose stop keycloak
docker compose run --rm --no-deps -v "$backup_dir:/backup" keycloak \
  export --dir=/backup --realm=tricycle --users=realm_file
docker compose start keycloak
# 将 $backup_dir 挂载到全新 Keycloak volume，使用 --import-realm，
# 再通过 kcadm.sh 分别查询 clients 和 users；完成后删除临时 volume。
```

该演练是本地开发恢复证据，不是生产灾备认证；生产还必须备份身份数据库、realm export、
签名密钥和受控配置，并在隔离环境演练恢复。

## 迁移和回滚边界

本轮没有创建新 issuer，也没有修改生产或开发 `ExternalIdentity` 行。现有映射仍以唯一的
`issuer + subject` 为身份键；未来切换必须先生成逐用户映射表，把新 subject 显式绑定到已有
`UserAccount.id`，禁止按邮箱静默合并。bootstrap administrator、暂停用户、普通用户和项目
邀请必须分别演练首次登录和已有账户路径。

切换失败时的回滚顺序是：停止新 identity service 流量，恢复反向代理到 Keycloak，恢复旧
issuer/client/JWKS 配置，恢复旧 realm export 或 volume，最后保留已写入的
`ExternalIdentity` 映射并逐项核对；不回滚项目角色、科学事实或业务数据库 migration。由于本轮
没有部署新 issuer，这只是已审阅的配置级回滚流程，不冒充生产切换演练。

现阶段保留 Keycloak 的理由是：协议核心 PoC 不能覆盖账户暂停、管理员边界、MFA、找回、邮箱
验证、邀请和现有 identity 映射；把这些能力搬到 npm 服务会引入新的安全自研面。资源占用虽高，
但现有 OIDC、realm 导入、恢复路径和项目映射已经有可验证基线。
