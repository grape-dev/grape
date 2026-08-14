# Grape 文件存储 OBS 化改造 —— 方案设计文档

> 文档编号：GRP-DESIGN-20260813-01
> 版本：v1.0
> 状态：正式设计（评审稿 obs-storage-reform-design.md 已并入本文档，历史见 git）
> 日期：2026-08-13
> 需求依据：`obs-storage-requirements.md`（GRP-REQ-20260813-01）
> 关联：`agent-cluster-design.md`（集群化）、`worker-server-delivery-design.md`（安装包下发）、
>       `skill-delivery-remote.md`（技能远程交付）、`multi-issue-isolation-design.md`（worktree）

---

## 1. 概述

### 1.1 背景

Grape 当前所有业务文件均落本地磁盘（`skills/`、`WORKSPACE_DIR`、`data/cache/`、
`configs/`）。在服务多集群部署与分布式客户端（多 Worker / admin / WebUI）诉求下，
本地磁盘模型产生共享缺失、分发成本、清理不彻底三类问题（详见需求文档 §1）。

### 1.2 设计目标

1. **统一存储抽象**：`StorageBackend`（local / obs 双实现），配置切换，单机零影响。
2. **上传 OBS 化**：运行期文件写入华为云 OBS，多集群读写同一份数据。
3. **下载临时 URL 化**：服务端签发 OBS 临时签名 URL，客户端直连 OBS，不走服务端带宽。
4. **分布式客户端一致**：Worker / admin / WebUI 三类客户端拿到一致文件视图。

### 1.3 范围结论（详见需求文档 §3）

- 纳入：技能库（custom/repo）、worker 安装包、project-info 产物、共享知识、
  event.json（可选）、一次性生成物、清理联动、存量迁移、admin 技能链路。
- 排除：git mirror/worktree、codegraph 索引、任务物化目录、会话目录、DB、日志、
  官方技能、静态资源/密钥配置/代码。

---

## 2. 总体架构

```
客户端 / Worker / admin
      │  ①上传（服务端中转，POST 现有 API，校验后转存）
      ▼
┌─────────────────┐      StorageBackend 抽象      ┌──────────────┐
│  Grape 主服务    │ ──────── save / read ───────▶ │    OBS       │
│  (FastAPI)      │ ◀─────── 临时URL 签发 ──────── │  bucket(私有) │
└─────────────────┘                               └──────────────┘
      │ ②下载：返回 {url, expires_in}（或 302 Location）
      ▼
客户端直接 GET 签名 URL ────────────────────────▶ OBS
```

核心原则：

- **上传**：现有 API 形态不变，服务端校验（大小/类型/zip slip）通过后转存 OBS；
  AK/SK 只存在于服务端，不下发前端。
- **下载**：接口返回临时 URL（或 302），客户端直连 OBS；有效期可配、短时生效。
- **抽象收口**：所有落盘点经 `StorageBackend`，新增文件型数据默认走同一通道。
- **数据流方向**：文件正体在 OBS（运行期数据），本地为缓存/工作副本；
  部署物正体在代码仓库（官方技能等），不进 OBS。

### 2.1 SDK 选型

| 端 | SDK | 说明 |
|----|-----|------|
| 主服务（Python） | `esdk-obs-python` | `ObsClient(ak, sk, server=endpoint)`；putObject / createTemporarySignature / deleteObjects |
| admin（Java） | 不引入（默认方案 B1） | 复用主服务 /api/skills/*；若评审选 B2 则 `esdk-obs-java` |
| 测试 | MinIO（S3 兼容）或真实测试桶 | OBS 兼容 S3 协议，MinIO 可本地模拟 |

---

## 3. 详细设计

### 3.1 存储抽象接口（Python 端）

```python
class StorageBackend:
    def save_bytes(self, key: str, data: bytes) -> str: ...      # 返回对象 key
    def save_stream(self, key: str, stream) -> str: ...
    def read_bytes(self, key: str) -> bytes | None: ...          # 服务端内部读取
    def read_file(self, key: str, local_path: str) -> bool: ...  # 拉取到本地缓存
    def exists(self, key: str) -> bool: ...
    def list_keys(self, prefix: str) -> list[str]: ...           # 前缀枚举（迁移/复制/清理用）
    def delete_prefix(self, prefix: str) -> int: ...             # 前缀批量删
    def temporary_url(self, key: str, expires: int = 300) -> str: ...  # 临时签名 GET URL
    def backend_name(self) -> str: ...                           # "local" | "obs"

def get_storage() -> StorageBackend:  # 单例；OBS_ENABLED=false → LocalStorage
```

实现要点：

- `LocalStorage`：`key` 映射为本地根目录（`OBS_LOCAL_CACHE` 或原路径）下的相对路径，
  temporary_url 返回 `/local-files/{key}` 需经鉴权的内部端点（或直接 FileResponse），
  保证 `OBS_ENABLED=false` 时下载行为兼容。
- `ObsStorage`：esdk-obs-python；`temporary_url` 调 `createTemporarySignature(
  method="GET", bucketName, objectKey, expires=expires)`；写入用 `putObject` 流式；
  删除用 `listObjects(prefix)` + `deleteObjects` 批量。
- 并发写同 key：业务层已由 DB 唯一约束防同名冲突（skill_name 唯一），
  OBS 覆盖写以最后一次为准，不设计跨节点同 key 并发写。

### 3.2 对象键（Object Key）规划

统一前缀隔离环境（`grape/prod`、`grape/test`），配置项 `OBS_PREFIX`：

```
{OBS_PREFIX}/skills/custom/{user_id}/{skill_name}/SKILL.md
{OBS_PREFIX}/skills/custom/{user_id}/{skill_name}/references/...
{OBS_PREFIX}/skills/repo/{repo_key}/{skill_name}/...
{OBS_PREFIX}/skills/template/skill-template.zip
{OBS_PREFIX}/worker-package/grape-worker.tar.gz
{OBS_PREFIX}/worker-package/VERSION
{OBS_PREFIX}/worker-package/SHA256SUMS
{OBS_PREFIX}/project-info/{owner}/{repo}/project-graph.md
{OBS_PREFIX}/project-info/{owner}/{repo}/project-capabilities.md
{OBS_PREFIX}/project-info/{owner}/{repo}/.metadata.json
{OBS_PREFIX}/knowledge/{owner}/{repo}/{scope}/knowledge.md
{OBS_PREFIX}/event-archive/{owner}/{repo}/issues/{n}/event.json
```

安全约束：`user_id / repo_key / skill_name / owner / repo` 动态片段入库前过白名单
正则 `^[\w.\-]+$`（沿用 repos.py 清理逻辑的既有校验，扩展到全部拼 key 处），防对象键注入。

### 3.3 配置

Python（`src/config.py` + 环境变量，AK/SK 仅环境变量）：

```python
OBS_ENABLED     = os.environ.get("OBS_ENABLED", "false").lower() == "true"
OBS_ENDPOINT    = os.environ.get("OBS_ENDPOINT", "https://obs.cn-north-4.myhuaweicloud.com")
OBS_BUCKET      = os.environ.get("OBS_BUCKET", "")
OBS_ACCESS_KEY  = os.environ.get("OBS_ACCESS_KEY", "")   # gitignored
OBS_SECRET_KEY  = os.environ.get("OBS_SECRET_KEY", "")   # gitignored
OBS_PREFIX      = os.environ.get("OBS_PREFIX", "grape/prod")
OBS_URL_EXPIRES = int(os.environ.get("OBS_URL_EXPIRES", "300"))
OBS_LOCAL_CACHE = os.environ.get("OBS_LOCAL_CACHE", "")  # 默认 WORKSPACE_DIR 同级 cache 目录
```

admin：`application.yml` 增加 `app.obs.*`（仅 B2 方案需要；B1 方案 admin 无 OBS 配置）。

### 3.4 主服务技能链路（需求 FR2/FR3）

**上传** `POST /api/skills/upload`（改动最小化）：
1. 现有校验全部保留（10MB、ZIP 魔数、zip slip、front matter）；
2. 临时目录解压逻辑不变 → 逐文件 `storage.save_stream()`
   写入 `skills/{scope}/{owner}/{skill_name}/...` → 清理本地 tmp；
3. 事务策略：DB 记录先行，OBS 上传失败则 `delete_prefix` 回滚 + 返回 502，
   不产生半成品。

**详情** `GET /api/skills/{name}`：正文读取改 `storage.read_bytes()`，调用方无感。

**转换** `POST /api/skills/convert`：`shutil.copytree` 改
`list_keys(前缀) + read_bytes + save_bytes` 逐对象复制（对象数少，可接受）；
原目录保留（复制语义不变）。

**模板下载** `GET /api/skills/template`：打包 zip → 上传
`skills/template/skill-template.zip` → 返回 `{url, expires_in}`；无鉴权端点保留
（token 经 query 传递，与现状一致），URL 有效期默认 300s。

### 3.5 任务下发链路联动（重要，worker 零改动）

`runtime/service.py:_pack_skill_dir` 目前读本地 skills/ 打包。改造为：
- **方案 a（推荐）**：本地缓存目录（`OBS_LOCAL_CACHE/skills/`）兜底 —— 打包前
  确保缓存（miss 则 `read_file` 从 OBS 拉取），打包逻辑与 worker 侧完全不动；
- 方案 b：`_pack_skill_dir` 直接走 `read_bytes` 逐文件读 OBS。

选 a：高频路径命中本地缓存，OBS 请求量最低，且 `OBS_ENABLED=false` 时
缓存即原目录，天然兼容。project-info / knowledge 打包同理。

### 3.6 admin 技能链路（需求 FR9，评审点 B）

- **方案 B1（默认）**：admin 技能上传/详情/提升改为调用主服务
  `/api/skills/upload|detail|convert`（admin 与主服务同网）；SkillService.java 中
  `skillsDir` 本地文件 IO（uploadSkill / getSkillContent / promoteToOfficial /
  getSkillTemplate / copyRecursively / deleteRecursively）整体退役，
  DB 读写逻辑保留（共享 MySQL 已一致）。
- 方案 B2：admin 独立引入 esdk-obs-java 双写。解耦但两套实现、两份密钥、行为漂移风险。
- **推荐 B1**：一套存储逻辑、admin 不引 OBS SDK、主服务升级即生效。

### 3.7 worker 安装包（需求 FR4）

1. `build_package()` 构建后：`storage.save_stream('worker-package/…')` + 上传 VERSION；
   `package_version()` 与 OBS 上 VERSION 一致则跳过上传（复用已有对象）；
2. `GET /api/runtime/install/package` 返回 `{url, expires_in: 1800, sha256}`；
   对老 install 脚本保留 302（Location=临时 URL）；
3. `scripts/install-worker.sh` 改造：取 URL → `curl -fSL` 立即下载 → 校验 sha256；
   **失败重试必须重新取 URL**（不缓存过期 URL）；
4. 本地 `data/cache/worker-package/` 保留为 OBS 故障回退源（FileResponse 兜底）。

### 3.8 project-info / knowledge / event.json 双写（需求 FR5/FR6）

- **写入（write-through）**：`builder.py` 产物、`knowledge.py` 文件、`event_archive.py`
  的 event.json 本地写完后，异步线程上传 OBS（失败仅 log.warning，不阻塞；
  events 表/DB 仍是可靠底账）。
- **读取**：服务端内部与任务打包优先本地缓存，miss 回源 OBS 并落缓存；
  Worker 侧保持任务快照下发（快照已由服务端从共享存储读全），worker 零改动。
- **清理**：`repos.py:_cleanup_repo_data` 追加
  `storage.delete_prefix()` 删除 skills/repo/{key}/、project-info/{owner}/{repo}/、
  knowledge/{owner}/{repo}/、event-archive/{owner}/{repo}/。

### 3.9 存量迁移（需求 FR8）

`scripts/migrate_to_obs.py`：
1. 枚举本地 `skills/custom/`、`skills/repo/`、`data/cache/worker-package/`、
   `WORKSPACE_DIR/{owner}/{repo}/project-info/`（产物文件）等纳入范围目录；
2. 按 §3.2 key 规则上传，逐对象 sha256 校验；
3. `--dry-run` 只列清单；`--delete-after` 校验通过后删除本地；
4. 迁移窗口期：读本地优先 + OBS 回源，双写期数据不撕裂，迁移完成后以 OBS 为正体。

### 3.10 临时 URL 生命周期

```
请求下载 → 鉴权（登录态/rta_ 凭据）→ storage.temporary_url(key, expires)
        → 返回 {url, expires_in} → 客户端立即发起 GET
        → 过期 → OBS 拒绝 → 客户端重新请求（重新签发）
```

- 有效期默认 300s（技能/知识/模板），安装包 1800s，运维可配（OBS_URL_EXPIRES）。
- 签名 URL 无法单独吊销 → 有效期短 + 必要时 bucket policy 前缀封禁。
- URL 不含 AK/SK，仅签名参数；日志不记录完整 URL（记录 key + 过期时间）。

---

## 4. 安全设计

| 项 | 方案 |
|----|------|
| Bucket 权限 | 私有（private ACL），控制台/策略层禁止匿名访问 |
| 密钥 | AK/SK 仅服务端环境变量 / k8s secret；不入代码、不入日志 |
| 临时 URL | 仅签发 GET；短有效期（300s/1800s）；获取仍需现有鉴权 |
| 对象键注入 | 动态片段全过 `^[\w.\-]+$` 白名单后拼 key |
| 上传校验 | 服务端维持：大小上限、ZIP 魔数、zip slip 检查，通过后转存 |
| 传输 | 生产建议 OBS 内网 endpoint（同 VPC），公网 HTTPS 兜底 |

---

## 5. 兼容性与回退

1. **配置开关**：`OBS_ENABLED=false` 全链路行为与现状一致（LocalStorage 兜底
   FileResponse）；灰度 = 测试桶 → 生产桶。
2. **故障降级**：
   - 上传类：OBS 失败 → 502 + 回滚删除，不产生半成品；
   - 下载类：本地缓存命中 → FileResponse 回退；未命中 → 503 + 告警；
   - 双写件读取失败 → 告警 + 回退本地目录。
3. **老数据**：迁移脚本执行前本地为正体；执行后 OBS 为正体、本地为缓存。
4. **Worker 兼容**：任务快照格式不变；install 脚本 302 兼容；worker 不引 OBS SDK。

---

## 6. 测试方案

| 层 | 内容 |
|----|------|
| 单测（Python） | StorageBackend 契约（local/obs 同套用例）；key 白名单；临时 URL 参数；迁移 --dry-run |
| 集成 | MinIO 模拟 / 真实测试桶：上传→下载往返、10MB 边界、BadZipFile/zip slip 拒绝、前缀删除、URL 过期 |
| 回归 | `pytest tests/ -q` 全量；skills 上传用例改 mock backend |
| 端到端 | 双 Worker + 主服务（OBS_ENABLED=true）：A 机上传技能 → B 机任务可用；安装包 sha256 校验 |
| 安全 | AK/SK 不出现在日志/响应；临时 URL 无密钥；公共读返回 403 |

测试约定：沿用 conftest（DEVOPS_DB_BACKEND=sqlite）；OBS 单测用 mock，
真实 OBS 用例打 `OBS_E2E=1` 门控。

---

## 7. 实施计划

| 里程碑 | 内容 | 工期 |
|--------|------|------|
| M0 | esdk-obs-python 依赖 + StorageBackend 抽象 + Local 实现 + 配置 | 0.5d |
| M1 | 技能上传/详情/转换/模板改 OBS（skills.py） | 1d |
| M2 | admin 技能链路对接主服务 API（B1） | 0.5~1d |
| M3 | worker 安装包：构建上传 + 临时 URL + install 脚本改造 | 0.5d |
| M4 | project-info/knowledge/event.json 双写 + 清理联动 | 1d |
| M5 | 迁移脚本 + 全量回归 + 灰度切换 | 0.5~1d |

---

## 8. 风险与对策

| 风险 | 对策 |
|------|------|
| 临时 URL 过期导致下载失败 | 有效期可配；客户端失败重取，不缓存旧 URL |
| OBS 请求费用 | 本地缓存 + 打包走缓存；官方技能留本地；双写异步化 |
| OBS 故障 | 上传宁失败不半成品；下载缓存回退；开关一键回滚 local |
| 对象键注入 | 全部动态片段白名单正则 |
| 迁移窗口数据不一致 | 读本地优先+OBS 回源；sha256 校验；双写期后切读 |
| admin 与主服务实现漂移 | 默认 B1 消除；B2 需评审明确 |
| 依赖兼容 | esdk-obs-python 与 py3.10+ 在 M0 验证；仅主服务安装，worker 不引入 |

---

## 9. 决策点（评审确认后固化）

| # | 决策点 | 默认建议 |
|---|--------|---------|
| A | 技能库范围：custom/repo 上 OBS + 官方留本地 | A1（推荐） |
| B | admin 接入：复用主服务 API / Java 独立接 OBS | B1（推荐） |
| C | project-info/knowledge/event.json 本期纳入 | 纳入（event.json 低优先） |
| D | 配置形态：环境变量 | 环境变量（推荐） |
| E | 存量数据迁移脚本 | 提供（推荐） |
| F | 临时 URL 有效期：技能/知识 300s、安装包 1800s | 默认值（可配） |

---

## 10. 附录：文件场景全景盘点（多集群 + 分布式客户端视角）

### 10.1 类别 A：部署物（随代码发布，无需 OBS）

| 场景 | 路径 | 读写方 | 说明 |
|------|------|--------|------|
| 可执行代码 | `src/`、`admin/`、`requirements*.txt` | 部署 | worker 安装包构建源 |
| 前端静态资源 | `src/api/static/` | WebUI 读 | FastAPI 挂载本地目录 |
| 配置（非密钥） | `configs/*.json` | 主服务/admin 读 | 随代码同步 |
| 配置（密钥类） | `global-bot-token.json`、`db-backend.json`、`.env` | 主服务读 | gitignored，部署注入，不进 OBS |
| 官方技能库 | `skills/{official}/*`、`skills/_template/` | 主服务/admin/worker（经打包） | 部署物，保持本地（决策点 A1） |
| 示例/脚本 | `examples/`、`scripts/install-worker.sh` | 部署 | 随代码 |

### 10.2 类别 B：运行时共享数据（OBS 改造面）

| # | 场景 | 路径 | 写方 | 读方 | 多集群问题 | 改造 |
|---|------|------|------|------|-----------|------|
| B1 | 用户/仓库技能 | `skills/custom/{uid}/`、`skills/repo/{key}/` | 主服务上传/转换、admin 上传/提升 | 主服务打包、admin、WebUI、Worker | A 机传 B 机不可见；admin 已现降级分支 | ✅ §3.4/3.5/3.6 |
| B2 | worker 安装包 | `data/cache/worker-package/` | 主服务构建 | 所有 Worker | 各节点自建或传输 | ✅ §3.7 |
| B3 | project-info 产物 | `WORKSPACE_DIR/{owner}/{repo}/project-info/` | 主服务 builder | 主服务打包、Worker | 换节点需重新生成/传输 | ✅ §3.8 |
| B4 | 共享知识 | `WORKSPACE_DIR/{owner}/{repo}/**/knowledge.md` | 主服务 knowledge.py | 主服务打包、Worker | 同上 | ✅ §3.8 |
| B5 | 事件档案 | `WORKSPACE_DIR/{owner}/{repo}/issues/{n}/event.json` | 主服务 event_archive.py | 主服务/任务续接、WebUI | 任务换节点读不到 | ✅ 可选低优先 |
| B6 | 一次性生成物 | 技能模板 zip、导出包 | 主服务 | WebUI/Worker | 无共享诉求 | ✅ 临时 URL 分发 |

### 10.3 类别 C：节点本地工作区（不上 OBS）

| 场景 | 路径 | 理由 |
|------|------|------|
| git bare mirror | `WORKSPACE_DIR/{owner}/{repo}/codebase/` | git 随机读写+原子性，OBS 不适用；保持每机懒建立 |
| Issue worktree | `WORKSPACE_DIR/{owner}/{repo}/{agent}/issues/{n}/` | 任务 cwd 每机派生，换机重建 |
| codegraph 索引 | `project-info/codegraph-repo/.codegraph/` | 本地生成、体量大、单点跑 |
| 任务物化目录 | cwd 下 `.skills/` `.project-info/` `.knowledge/` | Worker 本地落地，随快照重建 |
| 会话目录 | `WORKSPACE_DIR/vod/` `feishu/` `history/` `onboarding-agent/` | 高频追加小文件；会话亲和兜底 |

### 10.4 类别 D：状态/日志（非文件改造面）

| 场景 | 路径 | 结论 |
|------|------|------|
| 业务库 | `data/sessions.db`（默认 SQLite） | 多集群切 MySQL RDS（mysql_compat 已有）；SQLite 仅单机/测试 |
| 日志 | `data/devops-bot.log`、`data/logs/` | 每节点本地；集中采集另行 |
| 本地构建缓存 | `data/cache/` | 安装包见 B2；其余保持本地 |

> 需求文档（GRP-REQ-20260813-01）与本设计文档为唯一权威依据；
> 评审稿 obs-storage-reform-design.md 内容已全部并入，历史见 git。
