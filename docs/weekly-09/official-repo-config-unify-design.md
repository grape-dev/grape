# 官方反馈路由：官方技能反馈进官方技能仓（可开关）、非官方技能反馈进所属仓库 [weekly-09]

> 状态：**已实施（TDD 先红后绿 ✅）**｜目标分支：weekly-09（部署参考 weekly-09-lyz）
> **敏感信息声明**：本文不含真实 IP、域名、账号、仓库名或 token（一律 `<xxx>` 占位）。
> ⚠️ **占位语义**：本文中 `<owner>` 指**项目唯一固定组织**（官方仓 owner，写死、非变量、不存在其它值）；
> `_OFFICIAL_SKILLS_REPO` 即 `"<owner>/grape-skills"`（官方技能仓）的具名常量——**直接硬编码，无 env 覆盖地址**。
> **路由模型**：非官方技能反馈（repo/user scope）→ **自己托管的所属仓库**；官方技能反馈
> （official scope）→ **官方技能仓**，由 `GRAPE_FEEDBACK_OFFICIAL_ISSUE`（默认允许 `1`）控制是否提交给
> 官方技能仓 Issue。

---

## 〇、问题现象与影响

- 生产（Env-2）`grape feedback submit` 提交的 `source=agent`、`skill_scope=official` 反馈成功落库
  （`user_feedback` id=1），但 **`issue_url` 恒空 / `converted_at` 恒 null** —— 官方反馈
  **从未被转换成官方技能仓 Issue**，被 reconcile worker 静默 skipped。
- 根因：`feedback_issue_converter.py` 的 `GRAPE_SKILLS_REPO` **无默认值**（缺省空）→
  `_official_skills_target` 返回 None → `_resolve_target` skipped。而同一系统的
  **场景/技能同步模块默认值早已是官方技能仓**——两套逻辑指向同一官方仓，却一套有默认一套没有。
- 同时缺少「官方技能反馈是否提交官方技能仓」的控制开关（企业私有部署可能不想把内部技能使用问题
  发到公开官方技能仓）。
- 相关官方仓**环境变量命名混乱、来源不统一**（见 §一盘点），给部署与排障带来跳坑风险。

---

## 一、现状盘点：官方仓相关配置到底有几个

| Env 变量 | 所在模块 | 用途 | 当前默认 | 平台 |
|----------|----------|------|----------|------|
| `GRAPESKILLS_REPO_URL` | `src/storage/scene_sync.py:36` | 官方**场景**同步源 | `https://gitcode.com/<owner>/grape-skills`（兜底，owner 固定项目组织） | gitcode/github 双支持 |
| `GRAPESKILLS_REPO_BRANCH` | `src/storage/scene_sync.py:37` | 场景同步分支 | `main`（兜底） | — |
| `GRAPE_SKILLS_REPO`（本 PR 移除） | `src/api/feedback_issue_converter.py:20` | 官方**反馈→Issue**目标仓 | **空 ⚠️（无默认 → 改硬编码）** | 依赖 `GRAPE_SKILLS_PROVIDER` |
| `GRAPE_FEEDBACK_OFFICIAL_ISSUE`（本 PR 新增） | `src/api/feedback_issue_converter.py` | 是否向**官方技能仓**提交官方技能 / CLI / 平台反馈 Issue | `1`（默认允许） | — |
| `GRAPE_SKILLS_PROVIDER` | `src/api/feedback_issue_converter.py:22` | 反馈 Issue 建在哪个平台 | `gitcode` | gitcode/github 双支持 |
| `GRAPE_CLI_REPO` | `src/worker_package.py:19` | worker 打包时 **grape CLI 源码仓** | `https://gitcode.com/<owner>/grape-cli.git` | — |

> `GRAPE_CLI_REPO` 是 **grape-cli 独立项目源码仓**，与 `grape-skills`（技能/场景/官方反馈目标）是
> **两个不同的仓库**；本文不处理 CLI 仓，仅标记其存在避免误并。

**核心不一致**：
1. 同一官方技能仓在 `scene_sync` 有兜底默认、在 `feedback_issue_converter` 却依赖 env 且无默认。
2. `GRAPESKILLS_*` vs `GRAPE_SKILLS_*` **两套前缀命名并存**，含义相近（条同步 vs 反馈建仓）。
3. 官方仓**地址是稳定的工程事实**（`<owner>/grape-skills`），owner 唯一固定、无其它值，却被切成
   "环境变量覆盖 + JSON 配置回退"多层——**过度参数化，且正是本次"空默认静默丢"事故的温床**。

---

## 二、目标语义

1. **非官方技能反馈 → 所属仓库（保持现状）**：`skill_scope` 为 `repo` / `user` 的技能类反馈，一律建到
   **自己托管的所属仓库**（`repo_key`），与现状语义一致，不做变更。
2. **官方技能 / CLI / 平台反馈 → 官方技能仓（受开关控制）**：
   - `skill_scope='official'` 的**官方技能**反馈；
   - **CLI / 平台类**反馈（`feedback_kind` 标识为 CLI / 平台组件，如 `cli`、`platform`）——
     无论挂在哪，都是**项目自身组件**，一律进官方技能仓 `<owner>/grape-skills`。
   两者均先解析官方技能仓目标，再由 `GRAPE_FEEDBACK_OFFICIAL_ISSUE`（默认允许 `1`）决定是否提交。
3. **官方技能仓地址唯一固定、硬编码**：`_OFFICIAL_SKILLS_REPO = "<owner>/grape-skills"`，owner 为
   项目唯一固定组织、**不存在其它值**；不经 env/JSON 覆盖地址（与官方仓"工程事实不变"一致），
   彻底消除"空默认→静默丢"的可能。
4. **平台不写死**：`GRAPE_SKILLS_PROVIDER` 保持可配（gitcode/github），默认 gitcode；
   provider 决定用哪套**全局 bot token**（DB meta + 文件 fallback），地址与 token 解耦。
5. **token 缺位显式可见**：官方目标已解析但全局 bot token 缺失时，转换器记明确 WARNING 并走
   `skipped`（现状已有）；但**不再有空地址静默 skipped** 的路径。
6. **开关关闭时不建官方 Issue，不丢反馈**：`GRAPE_FEEDBACK_OFFICIAL_ISSUE=0` 时，官方/CLI/平台
   类反馈走 `skipped`（保留在 `user_feedback` 表内，管理员可见），返回统计中被计数，后续可改配重跑。
7. **不扩展发送侧目标仓矩阵**：不新增 `GRAPE_CLI_REPO` / `GRAPE_REPO` 作为反馈目标；CLI/平台类反馈
   复用官方技能仓通道（项目自身组件统一由官方仓承接）。

---

## 三、方案设计

### 3.1 官方技能仓地址硬编码（核心，必做）

owner **不是变量、不存在其它值**：官方技能仓的 owner 就是项目唯一固定组织（与 `scene_sync`、
技能同步、worker 打包引用的官方仓同源），直接写死为常量；`<repo>` 恒为 `grape-skills`。
**不保留任何 env / JSON 覆盖**（官方仓位置是工程事实，fork/镜像场景不在本 PR 支持范围）。

在 `src/api/feedback_issue_converter.py`：

```python
# 官方技能仓库（owner 唯一固定、无其它值；无 env 覆盖——反馈目标恒为官方技能仓）
_OFFICIAL_SKILLS_REPO = "<owner>/grape-skills"

def _official_skills_target():
    """恒返回官方技能仓目标（(provider, owner, repo, token)）。"""
    repo = _OFFICIAL_SKILLS_REPO
    if "/" not in repo:
        return None
    owner, rname = repo.split("/", 1)
    if not owner or not rname:
        return None
    mod = _provider_module(GRAPE_SKILLS_PROVIDER)
    try:
        if GRAPE_SKILLS_PROVIDER == "github":
            from src.user.db import get_global_github_bot_token
            token = get_global_github_bot_token()
        else:
            from src.user.db import get_global_gitcode_bot_token
            token = get_global_gitcode_bot_token()
    except Exception:
        token = ""
    return (GRAPE_SKILLS_PROVIDER, owner, rname, token)
```

**官方提交开关**（与地址硬编码正交，默认允许）：

```python
# 是否向官方技能仓提交 official 技能反馈 Issue；默认允许（1），私有部署可设 0 关闭
GRAPE_FEEDBACK_OFFICIAL_ISSUE = os.environ.get("GRAPE_FEEDBACK_OFFICIAL_ISSUE", "1").strip()

def _feedback_official_issue_enabled() -> bool:
    """默认允许向官方技能仓提交 official 反馈；显式 '0'/'false'/'no' 视为关闭。"""
    return GRAPE_FEEDBACK_OFFICIAL_ISSUE.lower() not in ("0", "false", "no")
```

- 该开关**不改目标地址**（`_OFFICIAL_SKILLS_REPO` 恒官方技能仓），只控制「official 技能反馈是否
  提交到官方技能仓」；关闭时该反馈走 `skipped`（保留表内，管理员可见，不丢）。

- 删除 `GRAPE_SKILLS_REPO = os.environ.get(...)` 全局变量及其 env 读取；模块内仅 `_OFFICIAL_SKILLS_REPO`
  常量。
- 保留 `GRAPE_SKILLS_PROVIDER` env（gitcode/github，决定 token 与 URL 基址）与官方提交开关
  `GRAPE_FEEDBACK_OFFICIAL_ISSUE`（默认允许），owner/repo 不随其变化。

### 3.2 路由：所属仓 / 官方技能仓（CLI、平台、官方技能）受开关（核心，必做）

`feedback_issue_converter._resolve_target` 按归属分类三判定：

```python
# 项目自身组件反馈标识：CLI / 平台（进官方技能仓）
_OFFICIAL_COMPONENT_KINDS = {"cli", "platform"}

def _resolve_target(row):
    """- 非官方技能（repo/user 技能类）→ 所属仓库（现状）；
    - 官方技能 / CLI / 平台（项目自身组件）→ 官方技能仓（受 GRAPE_FEEDBACK_OFFICIAL_ISSUE，
      默认允许）。关闭时 skipped（不丢，表内保留）。"""
    scope = (row.get("skill_scope") or "").lower()
    kind = (row.get("feedback_kind") or "").lower()
    if scope != "official" and kind not in _OFFICIAL_COMPONENT_KINDS:
        # 非官方技能（repo/user）→ 自己托管的所属仓库
        provider, owner, repo = _repo_target(row.get("repo_key") or "")
        token = _repo_bot_token(row.get("repo_key") or "")
        return (provider, owner, repo, token)
    if not _feedback_official_issue_enabled():
        # 私有部署关闭官方提交：skipped（返回空目标 → convert 走 skipped，表内保留，不丢）
        log.info(f"feedback→issue: official/CLI/platform feedback skipped "
                 f"(GRAPE_FEEDBACK_OFFICIAL_ISSUE=0) fid={row.get('id')}")
        return ("", "", "", "")
    target = _official_skills_target()
    if target is None:
        return ("", "", "", "")
    return target
```

- **非官方技能（`repo`/`user`）反馈回所属仓库**——互联网托管仓库是私有资产，其技能类反馈
  不进公开官方仓（保留现状语义）。
- **官方技能（`skill_scope='official'`）反馈受开关控制**；关闭时走 `skipped`，`user_feedback` 行保留
  （`issue_url=''`），管理员可见、改配后可重跑。
- **CLI / 平台类反馈**（`feedback_kind` ∈ `cli` / `platform`）是**项目自身组件**，一律进官方技能仓
  （与官方技能同通道）；同样受 `GRAPE_FEEDBACK_OFFICIAL_ISSUE` 控制。
- `feedback_kind` 现有取值是透传字符串，本文要求 Converter 对 `cli` / `platform` 两个枚举值识别；
  其余值不影响判定（non-official 技能类默认走所属仓）。
- 幂等组（task_id, skill_name, title）与回写 `issue_url` 逻辑不变。
- 幂等组（task_id, skill_name, title）与回写 `issue_url` 逻辑不变。

### 3.3 provider 与 token 就绪校验（加固）

- 保留 `GRAPE_SKILLS_PROVIDER`（gitcode/github），不改默认语义。
- `_official_skills_target` 增加**显式日志**：解析出目标但 `token` 为空时 log WARNING
  （"official target resolved but no global bot token for provider=<p>"），
  与现有 `no bot token for fid=` 判断不重复但更早暴露配置缺失。
- 不把 provider/token 写进代码（token 安全红缐）。

### 3.4 命名收敛（可选，低风险）

- 内部读取 prefer `GRAPE_SKILLS_REPO_URL`（规范前缀），`scene_sync` 的 `GRAPESKILLS_*` 保留为
  兼容别名（读旧名兜底），文档标注推荐名。可放 follow-up，避免本 PR 膨胀。

---

## 四、模块改动

| 模块 | 改动 |
|------|------|
| `src/api/feedback_issue_converter.py` | ①`GRAPE_SKILLS_REPO` env 读取移除，改硬编码常量 `_OFFICIAL_SKILLS_REPO`；②新增官方提交开关 `GRAPE_FEEDBACK_OFFICIAL_ISSUE`（默认允许 `1`）+ `_feedback_official_issue_enabled()`；③`_official_skills_target` 读常量（不再 read env）；④`_resolve_target` 新增归属分类：repo/user 技能→所属仓；official 技能 / CLI / platform（`feedback_kind`）→ 官方技能仓，开关关闭时 skipped；⑤目标解析成功但 token 空时显式 WARNING |
| `tests/test_feedback_issue_converter.py`（扩充） | 见 §七 TDD 新增用例 |
| （不改）`src/storage/scene_sync.py` | 保持现有默认/配置回退不动 |
| （不改）`src/storage/skill_sync.py`、`src/worker_package.py` | 非本次范围（`GRAPE_CLI_REPO` 保持） |

---

## 五、关键实现要点

1. 官方技能仓地址**纯常量硬编码**：`_OFFICIAL_SKILLS_REPO = "<owner>/grape-skills"`，无 env 读取、
   无 URL 解析、无运行时变化；owner/repo 均唯一固定。
2. 解析逻辑在 `_official_skills_target` 内一次性拆 `owner` / `repo`（模块级仅存常量，不重复 parse）。
3. 移除以 `GRAPE_SKILLS_REPO` 为名的环境变量读取；保留 `GRAPE_SKILLS_PROVIDER`（gitcode/github，
   决定 token 与 URL 基址，owner/repo 不随其变化）与新增官方提交开关 `GRAPE_FEEDBACK_OFFICIAL_ISSUE`
   （默认 `1` 允许；`0` 关闭）。
4. `_resolve_target` 按归属分类：repo/user 技能类 → 所属仓（现状保持）；official 技能 / CLI /
   platform（`feedback_kind` ∈ `{"cli","platform"}`）→ 官方技能仓（受开关控制）。开关关闭时
   该类反馈走 skipped（表内保留、不丢）。需在部署文档/发行说明声明该行为变化。

---

## 六、影响面 / 风险

| 项 | 说明 |
|----|------|
| 官方/CLI/平台反馈落官方仓 | **根治**：无默认→硬编码 + 开关后，官方技能、CLI、平台类反馈可被转换为官方仓 Issue（前提全局 bot token 就绪） |
| CLI/平台归属 | 由发送侧"无处去"转为**恒定官方技能仓**（官方技能、CLI、platform 同通道）——**不再丢** |
| 私有仓技能反馈语义 | repo/user 技能类反馈**仍留所属仓库**（现状语义，私有 bug 不进公开仓，无行为变化） |
| 官方开关关闭 | `GRAPE_FEEDBACK_OFFICIAL_ISSUE=0`：官方/CLI/平台反馈走 skipped（表内保留、不丢、管理员可见） |
| token 缺失仍 skipped | 不变的语义：目标可解析但无 token → 明确 WARNING + skipped，不静默 |
| 兼容性 | 删 `GRAPE_SKILLS_REPO` env 后，部署不再有该配置项；迁移成本：需移除存量环境里已设的 `GRAPE_SKILLS_REPO`（如有） |
| 平台支持 | provider 未动；gitcode/github 双支持保留 |
| 回归范围 | `tests/test_feedback_issue_converter.py`、`tests/test_redact_issue_body.py`、feedback 相关套件 |

---

## 七、测试设计（TDD，先红后绿）

扩充 `tests/test_feedback_issue_converter.py`：

| ID | 场景 | 断言（red 阶段现状 → 应 fail） |
|----|------|----------------------------------|
| C1 | `skill_scope='official'` 反馈（无任何 env） | `_official_skills_target()` 返回基于 `_OFFICIAL_SKILLS_REPO` 的官方仓三元组（不再 None）——red：现返回 None |
| C2 | 设 `GRAPE_SKILLS_REPO=<owner>/<repo>`（应被忽略） | 目标恒为 `_OFFICIAL_SKILLS_REPO` 常量（env 不再生效）——red：现读 env |
| C3 | 设 `GRAPE_SKILLS_REPO=https://gitcode.com/<owner>/<repo>`（URL 形态，应被忽略） | 目标仍为 `_OFFICIAL_SKILLS_REPO` 常量（不再解析 env URL） |
| C4 | `_OFFICIAL_SKILLS_REPO` 常量值 | 恒等于 `"<owner>/grape-skills"`；`owner/repo` 拆分为非空 |
| C5 | `convert_pending_feedback` 集成：official 反馈 + mock token | 不再 skipped（`target resolve failed` 消失），走建 Issue 分支 |
| C6 | 目标解析成功但全局 bot token 空 | 有明确 WARNING 日志、result.skipped 计数+1（不静默） |
| C7 | 回归：`GRAPE_SKILLS_PROVIDER=github` 分支 | `get_global_github_bot_token` 被调（平台解耦保留） |
| C8 | `skill_scope='repo'` 技能反馈（默认开关开） | `_resolve_target` **恒所属仓库**（`_repo_target`），不进官方仓 |
| C9 | CLI 缺陷反馈（`feedback_kind='cli'`，skill 非官方） | 一律官方技能仓（`_OFFICIAL_SKILLS_REPO`），正文含 `<redacted>` 脱敏 |
| C10 | 平台类反馈（`feedback_kind='platform'`） | 一律官方技能仓 |
| C11 | 开关关闭：`GRAPE_FEEDBACK_OFFICIAL_ISSUE=0`，official / CLI / platform 反馈 | `_resolve_target` 返回空目标 → skipped（表内保留、不丢），有 `GRAPE_FEEDBACK_OFFICIAL_ISSUE=0` 日志 |
| C12 | 开关关闭：`GRAPE_FEEDBACK_OFFICIAL_ISSUE=0`，`skill_scope='repo'` | 仍所属仓库（不受官方开关影响） |
| C13 | 开关默认：`GRAPE_FEEDBACK_OFFICIAL_ISSUE` 未设 | `_feedback_official_issue_enabled()` 返回 True（默认允许） |

red 阶段：先写 C1/C2/C5 等断言（现状 C1=target None、C2=读 env、C5=skipped），跑 → fail；
green 阶段：实现 §三改动（硬编码常量 + 收口路由），断言 pass；全量 feedback 套件保持 green。

实施记录（weekly-09）：
- `src/api/feedback_issue_converter.py`：`_OFFICIAL_SKILLS_REPO` 硬编码 `grape-dev/grape-skills`
  （owner 固定、无 env），移 `GRAPE_SKILLS_REPO` env；新增 `_OFFICIAL_COMPONENT_KINDS={cli,platform}`、
  `GRAPE_FEEDBACK_OFFICIAL_ISSUE`（默认 `1`）+ `_feedback_official_issue_enabled()`；`_resolve_target`
  三分支（repo/user→所属仓；official/CLI/platform→官方技能仓，开关关时 skipped）；`_fetch_pending`
  透出 `feedback_kind`。
- 测试：`tests/test_issue_official_issue_switch.py` 8 用例（C1/C4/C8/C9/C10/C11/C12/C13）全 pass；
  既有 `test_feedback_issue_converter.py` 7 项、feedback 开放/状态/台账/脱敏 110 项回归全绿。

---

## References

- `docs/weekly-08/feedback-openapi-expose-design.md` §四.5/§五（官方反馈→Issue 设计）
- `src/api/feedback_issue_converter.py:20-53,180-191`（`GRAPE_SKILLS_REPO` env（待移除）/`_official_skills_target`/`_resolve_target`）
- `src/storage/scene_sync.py:30-86`（`_load_config` 默认 + `_OWNER_REPO` 既有事实源）
- `src/storage/skill_sync.py:441`、`src/worker_package.py:19`（`SKILL_SYNC_INTERVAL`/`GRAPE_CLI_REPO` 存量）
- `src/user/db.py:1974-1996`（`get_global_gitcode_bot_token`/`get_global_github_bot_token`：DB meta + 文件 fallback）