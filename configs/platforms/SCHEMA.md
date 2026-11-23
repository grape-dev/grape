# configs/platforms 卡片 schema —— 凭据存储契约（F4/D106 定稿）

本目录每平台一个 JSON 卡（能力聚合点，键集=PlatformCard 字段，未知键整卡拒绝）。
本文钉死**仓库凭据存储**的显式契约，供加新平台卡时对照。

## 目录口径（开源审查 §1.4-1-10 / §3-5 登记）

本目录是**代码级默认平台卡**（feature 核心数据，不含任何凭据），不适用
「configs 只留 *.example」约定——卡是运行必需的默认数据而非环境配置；
部署侧覆写走 `meta.vcs_providers_config`（运行时配置），与默认卡分离。

## token_storage_field / webhook_token_field

| 取值 | 语义 |
|---|---|
| 显式 `""`（键存在） | **无物理列**。该平台仓库凭据唯一落点=`grape_t_repo_credentials` 单表，`db_setup._ensure_platform_storage_columns` 不会为该平台建 managed_repos 列 |
| 键缺席 | 回退派生列名 `bot_<provider>_token` / `<provider>_webhook_token`（gitcode/github 存量列语义，物理列+历史回落保留） |

- 显式 `""` 与键缺席是**两个语义**（构建层 `_explicit_or_default`）；`null`/缺键≠空串。
- F4 第一批（2026-11-11）：gitee、gitlab 置空（终态）；gitcode、github 保持派生列（列+历史回落，归后续批次）。

## grape_t_repo_credentials 单表契约

- **每仓库（repo_key）每 cred_type 至多一行**（表级 `UNIQUE(repo_key, cred_type)` 钉死；cred_type 值域 `{'bot','webhook'}`，平台由 repo_key 前缀承载）。
- **不加 `cred_slot`**：同仓同种类多凭据（如多 bot）是未来需求，出现时走单表轻迁移（加列/换键），当前卡 schema 与写路径零预留。
- 写路径唯一权威入口：`add_managed_repo`（归一逻辑键 `bot`/`webhook` 或存量列名别名）/ `_update_repo_bot_token` / admin `config_store` 轮换 / `db_setup` 回填迁移——新写入点须走 D90 契约评审。
- 写 rc 成功后**同一事务**置空 managed_repos 对应平台 token 列（渐进收敛，见 D106 设计分片；SQL 精确限定 `UPDATE managed_repos`——meta 表存在同名全局键 `gitcode_webhook_token`，严禁误伤）。

## 一实例一卡（Issue #3580）

自建 GitLab 等需要独立 host/凭据的实例**每实例一张卡**，卡文件名即 provider：

- 卡名仅 `[A-Za-z0-9_-]`、长度 1..64，显式拒绝 `@`（不得把账号语法混入前缀）；
- `host` 为裸主机名（可带合法端口），`api_base` / `download_base` 必须 https，
  且不得包含内嵌凭据、query、fragment、`..` 路径段；
- `ca_bundle` = 自建实例 TLS CA 的 PEM **绝对路径**（空=系统 CA），仅由 git 传输
  注入 `http.sslCAInfo`；改卡即生效，不引入环境变量；
- `repo_key` = `<卡名>:<group>/<sub>/<repo>`，`parse_repo_key` 原样保留多级路径；
- GitLab 实例卡声明 `dialect="gitlab"`、`api_auth="private_token"`、
  `webhook_dialect="gitlab_v4"`、`webhook_verify="token_compare"`、`clone_auth="oauth2"`。
