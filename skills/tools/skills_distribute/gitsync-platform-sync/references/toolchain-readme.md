# gitsync

GitCode → GitHub / ClawHub / DB 同步工具链。

## 架构

```
gitcode master ──①──▶ gitcode github分支 ──②──▶ github remote master（身份重写）
  (Squash线性)      hop1                   ├─②'──▶ ai_shell 分支（SHA 原样）
                                           └─②''─▶ 下游仓库（身份重写，SHA 与 github 一致）
```

- **hop1**：master 新 commit 全量 cherry-pick 到 github 分支（无 `-x`，保留原作者身份）
- **hop2**：github 分支身份重写（author+committer → 固定身份）推到各目标；时间戳/内容字节级保留
- **ClawHub**：skill 增量发布，A2 方案（本地算号 + 冲突自愈）
- **DB**：按 multica_issue_id 回写平台发布状态

## 安装

```bash
pip install .
# 或开发模式
pip install -e .
```

依赖：Python 3.9+、`git`、`clawhub` CLI（仅 publish 需要）、`pymysql`（仅 db-update 需要）。

## 配置

复制 `config.yaml.example` 并按需修改。配置项：

| 段 | 说明 |
|---|---|
| `identity` | 重写后的固定身份（name/email） |
| `remotes` | 各仓库 URL（双 SSH 推荐，工具不拼接 URL） |
| `branches` | 源分支：`source_master`（master）、`source_branch`（github 分支） |
| `targets` | 同步目标列表：`rewrite_identity`（是否重写）、`match_strategy`（content/sha）、`verify_against`（跨目标校验）、**`enabled`**（v1.2.0 目标开关，false 则不参与同步/发布/DB 回写） |
| `map_file` | commit 映射文件（缓存，可 `map rebuild`） |
| `clawhub` | ClawHub 配置（base_url、organization、skills_dir、manifest、登录态 config_path、**`exclude_skills`**（v1.2.0 发布黑名单，精确名或 glob，命中不 probe/不发布/不进 manifest））；`organization` 为 clawhub 组织名，用于拼 skill 页面 url，**必须配置** |
| `db` | 数据库配置（host/user/password/database/product_dir） |

### 环境变量（v1.2.0 新增/相关）

| 变量 | 默认 | 说明 |
|---|---|---|
| `GITSYNC_CONFIG` | — | 指定 config.yaml 路径（替代 `--config`） |
| `GITSYNC_PUBLISH_WORKERS` | 3 | ClawHub skill 发布并行 worker 数（>1 且 skill 数>1 才并行；ClawHub 限流敏感，调大需谨慎） |
| `LOCK_WAIT_SEC` | 取 `TIMEOUT_SEC` | agent-run.sh 并发排队时后到者等待锁的最长秒数（超时才 exit 2） |
| `TIMEOUT_SEC` | 600 | agent-run.sh 中 gitsync run 的超时秒数 |
| `CLAWHUB_CONFIG_PATH` | 从 config.yaml 读 | clawhub CLI 登录态配置路径（data/clawhub/config.json） |

## 子命令

| 命令 | 用途 |
|---|---|
| `gitsync run --multica-issue <uuid>` | **一键**：sync → publish → db-update，失败不中断，JSON 摘要 |
| `gitsync sync` | git 多目标增量同步（含 hop1 cherry-pick + 推送） |
| `gitsync check [--json]` | 只读幂等/守卫校验 |
| `gitsync lookup <sha> [--json]` | commit 双向回溯（remote↔分支↔master） |
| `gitsync bootstrap [--dry-run]` | 首次初始化（探测先行，结构不符需显式处理） |
| `gitsync map rebuild` | 重建 map 缓存 |
| `gitsync publish` | ClawHub skill 发布（A2） |
| `gitsync db-update --multica-issue <uuid> [--target x]` | 按 multica 回写平台状态 |

## 退出码

- `0` 成功 / 已同步
- `1` 有差异（check 模式）或自校验失败
- `2` 守卫失败（外来历史/非快进无 force）、脏数据、环境错误

## 安全护栏

- 外来历史守卫：目标有非派生 commit → 拒绝（`acknowledged_commits` 白名单放行）
- 非快进守卫：需显式 `--force`（force 前自动建备份分支）
- bootstrap 配对失败绝不静默
- push 后自校验

## 并发安全（v1.2.0）

- **map/manifest 文件锁**：`mapfile.append` 与 `Manifest.save` 用 `fcntl.flock` 串行化写（多进程并发不再丢条目/覆盖）
- **agent-run.sh 排队锁**：入口 `flock -w $LOCK_WAIT`（默认取 TIMEOUT_SEC）——并发任务后到者**排队等待**而非直接失败；因 run 幂等（已同步 no-op / content_hash 命中跳过 / db-update 按各自 multica 写不同 requirement），前一个完成后后一个拿锁继续，两个任务各自成功；等待超时才 exit 2（可用 `LOCK_WAIT_SEC` 覆盖）
- **publish 并行**：各 skill 发布用 `ThreadPoolExecutor` 并行（`GITSYNC_PUBLISH_WORKERS`，默认 3）
- **db-update 并行**：各 target 的 UPDATE 用线程池并行，每线程独立 MySQL 连接（pymysql 连接非线程安全）；同一行并发由 MySQL 行锁 + 幂等重放保证（值相同，无实际冲突）
- git push 并发：hop1 有 fetch+rebase 重试 ≤5 次；hop2 重写纯函数同 SHA，并发 push 幂等

## ClawHub 发布（A2）

- 版本号由工具本地管理（每段两位数上限：`1.0.99 → 1.1.0`）
- `clawhub skill publish --version <v>` 显式发布，CLI 内置指纹比对
- 版本冲突 → 本地 bump 递增重试（自愈），绝不重传同号
- manifest 是发布状态机（含 in_review 状态），冗余备份
- 审核中/被拦版本状态仅存于 manifest（公开 API 不可见）

## DB 回写

`run` 在 db-update **之前**写产物 JSON（`latest.json`），`db-update` 读它并按 multica 更新：

```
multica_issue_id → t_skills_dev_requirement.skill_name
                 → t_skills_dev_flow 对应行 platform_<target>_status/url/time = published
```

产物 JSON 每个 skill 条目含 `url`（=github tree 链接，兼容旧字段）、`urls`（`github`/`gitcode`/`clawhub` 各自平台的链接）与 `platforms`（本轮成功同步/发布的平台）：

```json
{
  "name": "huawei-cloud-rds-smart-service",
  "url": "https://github.com/.../tree/main/storage/rds/huawei-cloud-rds-smart-service",
  "urls": {
    "github":  "https://github.com/.../tree/main/...",
    "gitcode": "https://gitcode.com/.../tree/github/...",
    "clawhub": "https://clawhub.ai/<organization>/skills/<name>"
  },
  "status": "published",
  "platforms": ["clawhub", "gitcode", "github"]
}
```

- url 分支：github 用目标 `github_master.branch`（`main`）；gitcode 用 `source_branch`（`github` 中间分支）
- `db-update` 按 `urls[target]` 写对应平台列，且只写 `platforms` 中列出的平台；旧产物（无 `platforms`）回退全写
- 一个 multica = 一个 skill（一对一）；命中多行 → 拒绝（脏数据守卫）
- `platform_huaweicloud_*` 不碰
- 幂等：UPDATE 天然可重放

## 测试

```bash
python -m pytest tests/ -q
```

集成测试使用本地临时 git 仓库 + mock，不触碰生产数据。
