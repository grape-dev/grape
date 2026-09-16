# Grape API 文档标准化与最佳实践优化方案

> 目标：让 `help/api/` 目录成为**每一接口都有详细且一致介绍**的可信 API 参考——以 `/openapi.json` 为单一事实源，
> 收敛当前「文档与契约漂移、格式三套并存、覆盖不全、细节缺失」的现状。
> 现状分析见 §一；本方案为**设计（待实施）**。
> 分支：待建；状态：设计草案；日期：2026-09-16

---

## 一、现状分析（已核对 help/api 全 13 篇 + /openapi.json 全量 171 paths）

### 1.1 文档与契约（openapi.json）双向漂移

**openapi.json 落后于文档**（契约缺新端点）：

| 缺口 | 文档有、契约无 |
|------|----------------|
| issues | 全部 `/api/issues/{issue_id}/*` 操作端点（start/status/confirm-done/cancel/resume/pipeline/clarifications/clarify/attachments）+ `/api/issues/mine` |
| tasks | `POST /api/tasks/notify`、`GET /api/tasks/current` |
| events | `POST /api/events/{seq}/flow/reset`（标注 internal，可豁免） |

**文档落后于契约**（契约有、文档未覆盖的 endpoint 前缀与端点）：

| 前缀 | 契约 paths | 文档覆盖 | 缺 |
|------|:---:|:---:|-----|
| settings | 34 | 0 | 全量（凭据/API Key/通知/OAuth/runtimes/模式/标签） |
| runtime | 17 | 0 | 全量（列/恢复/健康/批次/tags/凭据） |
| devstage | 8 | 0 | 全量 |
| project-templates | 4 | 0 | 全量 |
| auth | 2 | 0 | profile / logout |
| dashboard/pipeline/search/feishu | 1-3 | 0 | 全量 |
| scenes | 25 | ~16 | `GET/POST /api/scenes`、market CRUD、branches、check-name、icon-upload、advanced-options |
| knowledge | 15 | 2（content/context） | entry/CRUD/refs/archive/stats |
| repos | 17 | 14 | `scene-recommend-config` GET/PUT |
| events | 12 | 10 | `DELETE /api/events/{seq}/chat`、`POST /api/events/{seq}/chat/submit` |
| skills | 13 | 12 | `GET /api/skills`（列表） |

**契约还残留已下线端点**（文档已标注迁移，契约未清理）：`/api/tasks/{task_id}/*` 全部 10 个（已迁 `/api/issues/{issue_id}/...`）；
`POST/DELETE /api/tasks/attachments/pre-upload*` 等。

> 根因：`/openapi.json` 是生成物但未随路由演进及时重生成；markdown 靠人工写、无校验，两套各写各的。

### 1.2 格式三套并存（同一目录内不统一）

| 样式 | 使用文件 | 特征 |
|------|----------|------|
| `\| 项 \| 值 \|` 表格 | 01-tasks / 11-issues / 07-knowledge | 每端点一张表：scope/Path/Query/出参/返回分列 |
| `\| 出参 \| \| 返回 \|` 表格 | 02-repos / 03-agents / 04-chat / 05-skills / 06-scenes / 08-events / 09-task-logs | 每端点重复出参/返回两行 |
| 混合 | 10-git-repos / 12-a2a | 自定义「入参表 + JSON 示例 + 错误表」 |

同样的 scope 说明以几乎相同的长句在各端点重复几十次（如 02-repos 里
`> scope（x-token-scope）：**user_only**（\`grape_\` API Key / JWT；\`tk_\` → 403）` 出现 14 次），
篇幅冗余且易写漏（05-skills 的 `GET /api/skills/search`、08-events 的
`GET /api/events/chat/task/{task_id}/status` 和 `/stream` 均已漏写 scope / 返回行）。

### 1.3 细节缺失（多数端点不满足「详细一致」）

| # | 缺口 | 例证 |
|---|------|------|
| D1 | **缺响应 JSON 完整示例** | 13 篇中仅 git-repo/issues/a2a/tasks 部分端点有真实响应示例；skills/scenes/events/knowledge 多数只列字段名 |
| D2 | **缺请求示例（curl / CLI）** | 全目录无任何可复制请求样例（GitCode 官方文档每个端点都有） |
| D3 | **入参表列不齐** | 04-chat `PUT /config`、`POST /submit` 只写「出参/返回」无请求体字段；agents PUT 表无「必填」列 |
| D4 | **scope 行缺失或过简** | 05-skills search（无）、06-scenes 正文缺 `GET /api/scenes`（intro 引用了却无端点半）、08-events status/stream（无出参/返回）、02-repos bot-token/deploy-url GET（细粒度 scope 注丢失） |
| D5 | **字段类型/格式不精确** | `task_id` 在 notify 返回 `5`（int）、complete 返回 `"t_xxx"`（string），前缀语义未说明；`repo_key` 在 04-chat 写 `"owner/repo"`、在 git-repo 内为 `gitcode:owner/repo`，两处口径未统一表述 |
| D6 | **无页码/时间/ID 全局约定** | 分页参数名混用 `page/limit`（events、chat messages）与 `per_page`（git-repo）；`created_at` 无 ISO8601/UTC 说明；`{issue_id}`=内部 id vs `{number}`=平台编号的规矩只写在本篇内 |
| D7 | **`{{SERVER_URL}}` 从未正式定义** | 00-auth 无 Base URL / 环境变量节；10-git-repos 直接使用，README 未收录 |
| D8 | **无 SSE 协议规范** | chat/events/task-logs 多处提到 SSE 流但无统一事件格式约定（done/terminate/error 事件）

### 1.4 一致性与可维护性

- scope 全表只在 README，端点文档应引用而非重复长句（现状二者并存，已出现漏写）。
- 错误码契约已收敛（README「状态码约定」+ `错误响应统一格式`，见 `error-code-standard-design.md`），
  但逐端点错误表仅在 10-git-repos / 11-issues 落地，其余文档仍是叙述性「返回 200；400；404」。
- 无自动化校验——`tests/test_openapi.py` 只验契约端点集合，不验 markdown 与契约是否一致。

---

## 二、方案：单一事实源 + 统一模板 + 全量补齐 + 自动化校验

### 2.1 原则

1. **`/openapi.json` 是唯一权威**：所有对外端点必须存在于契约（含 `x-token-scope`、`summary`、`description`、
   响应 schema）；markdown 是从契约衍生的**人类可读镜像**，不得出现契约没有的对外端点，反之亦然
   （内部端点如 `events/{seq}/flow/reset`、webhook 回调可列入例外白名单）。
   > CLI `--help` 文案已取自契约 summary/description（`issue-45-openapi-api-design.md` §3.5），统一契约即统一 CLI。
2. **一个端点一套模板**：所有端点共用同一 markdown 块结构（见 §2.3），消灭 1.2 的三种写法。
3. **全局约定只在 README/00-auth 写一遍**，端点文档引用；杜绝逐端点重复长句。
4. **DRY 错误表**：README 是唯一错误码字典，端点只列「本端点特有」错误（如 409 冲突、502 上游），通用 400/401/403/404 用一行引用。

### 2.2 单一事实源落地方案

| 动作 | 内容 |
|------|------|
| R1 | 重生成 `/openapi.json`（触发 custom_openapi），纳入：issues 全量操作端点、`tasks/notify` + `tasks/current`；**剔除已下线**的 `/api/tasks/{task_id}/*`、attachments/pre-upload 残留 |
| R2 | `test_openapi.OPENAPI_JSON`/`EXPECTED_PUBLIC` 同步更新，回归契约集合 |
| R3 | 新增一致性测试 `tests/test_api_docs_sync.py`：解析各 `help/api/*.md` 的 `METHOD /path` 标题，
     与 openapi paths 双向比对；例外白名单放 `_DOC_EXEMPT`（flow/reset、webhook、`{{SERVER_URL}}` 变量说明等） |
| R4 | 新增校验脚本 `scripts/check_api_docs.py`（可选，配合 CI）：lint 每个端点块是否含 scope/入参/出参示例/错误表四要素，缺则报 | 

> R3/R4 是防回归的闸门——没有它们，§二/三的任何补齐都会在下次人工维护时再次漂移。

### 2.3 统一端点模板（每个端点块）

```markdown
## {METHOD} /api/xxx/{param} — {一句话描述}

> scope：`{user_only | session_only | dual}`（全表见 README「Token 作用域」，不在本篇重复）
> 调用方/适用：{谁调用、什么场景}

**入参**

| 位置 | 参数 | 类型 | 必填 | 说明 |
|------|------|------|:---:|------|
| Path | `{owner}` | string | ✅ | 仓库属主 |
| Query | `page` | int | 否 | 页码（默认 1，约定见 README「分页」） |
| Header | `Content-Type` | string | 有 body 时 | `application/json` |

**请求体（{BodyName}）与示例**

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `title` | string | ✅ | … |

```json
{ 真实可运行的请求 JSON 示例 }
```

**响应（200）**

```json
{ 完整响应对象示例，短字段可省略用 "..." }
```

{ 复杂对象补字段表：如 ResponseItem 字段说明 }

**错误**（通用 400/401/403/404 见 README，以下仅列本端点特有）

| 码 | 场景 |
|----|------|
| 409 | 同标题已存在 |
| 502 | 上游 GitCode 创建失败 |

**示例**

```bash
curl -X POST '{{SERVER_URL}}/api/issues/owner/repo' \
  -H 'Authorization: Bearer <grape_xxx>' -H 'Content-Type: application/json' \
  -d '{"title":"...","description":"..."}'
# grape CLI（若契约 operation_id 已映射）
grape issues create --owner owner --repo repo --title ...
```
```

> 模板要素 = **方法+一句话描述 / scope / 入参表 / 请求体表+示例 / 响应示例 / 本端点错误表 / 可执行示例**——
> 七个要素齐全才算「详细且一致」。列表类端点补充：分页出参形态（`{items, total}` 或数组）与默认 `per_page`。

### 2.4 全局约定集中（README + 00-auth 增补）

在 README 增补以下「全局约定」节（各端点只引用、不重复）：

| 约定 | 内容 |
|------|------|
| Base URL | `{{SERVER_URL}}` 定义：环境变量 `GRAPE_SERVER_URL`；默认 `https://<host>`；所有路径前缀 `/api/...` |
| 版本 | 当前无版本号（`/api/...`）；未来破坏性变更引入 `/api/v1/...`，旧版本 410 Gone，迁移期至少保留一个版本 |
| 分页 | 统一 `page`（默认 1）+ `per_page`（默认 20，最大 100）；`limit` 仅保留给 stream/历史类游标接口 |
| 时间格式 | 一律 ISO 8601 UTC（`2026-09-16T08:00:00Z`），文档各端点不再枚举 |
| ID 前缀口径 | `t_` = agent 执行实例、`tk_` = 会话 token、`grape_` = API Key、`seq` = 事件序号、`{issue_id}`=内部 id vs `{number}`=平台编号；`repo_key` 统一表达式 `[gitcode:|github:]owner/repo`（对外入参可省略前缀由服务端按仓库判定） |
| SSE | 事件格式统一 `data: {…}\n\n`；终止事件固定 `data: {"done": true}`，异常 `data: {"error": {"code","message"}}`；`Accept: text/event-stream` |
| 频率限制 | 429 与重试建议（先 1s 退避，最大 3 次） |
| 认证三通道 + 优先级 | 已存在，保留并固化为唯一表述 |

### 2.5 全量补齐（覆盖缺口）清单

按优先级分四批：

| 批 | 内容 | 说明 |
|----|------|------|
| **P0 补契约** | 重生成 openapi.json（§2.2 R1）+ test_openapi 同步 | 先让契约成为可信源 |
| **P1 补文档** | settings(34) / runtime(17) / devstage(8) / auth(2) / project-templates(4) /
  dashboard/pipeline/search/feishu(1-3) | 9 个前缀全量补齐（openapi-full-coverage 已将它们纳入契约，文档跟进） |
| **P2 补端点** | scenes（market CRUD/branches/check-name/icon-upload/advanced-options/`GET,POST /api/scenes`）、
  knowledge（entry/CRUD/refs/archive/stats/list/detail）、repos `scene-recommend-config`、
  events（`DELETE /chat`、`/chat/submit`）、skills `GET /api/skills`、tasks notify/current | 已覆盖前缀内缺漏端点 |
| **P3 一致性翻修** | 全部端点套用 §2.3 模板：补 scope 缺失行（skills search、events status/stream 等）、
  补请求/响应示例（skills/scenes/events/knowledge/chat）、统一 `| 项 | 值 |` 与 `| 出参 |` 两种表格为模板、
  重复 scope 长句改为一行引用、逐端点错误表按 §2.4 DRY 收敛、`repo_key`/时间/分页口径对齐 README | 详见 §三 检查清单 |
| **P4 自动化** | `tests/test_api_docs_sync.py` + `scripts/check_api_docs.py`（R3/R4） | 防回归 |

### 2.6 文档目录结构调整（建议）

```
help/api/
├── README.md          # 全局约定（Base URL/版本/分页/时间/ID 口径/SSE/限流）+ 状态码 + 错误格式 + scope 表 + 目录
├── 00-auth.md         # 认证三通道 + 优先级 + 获取 API Key + 通用响应约定
├── 01-tasks.md … 12-a2a.md   # 各资源端点，统一模板
├── 13-settings.md     # P1 新增
├── 14-runtime.md      # P1 新增
├── 15-devstage.md     # P1 新增
├── 16-misc.md         # P1 新增（dashboard/pipeline/search/feishu/project-templates/auth）
└── openapi.json       # 权威契约（重生成）
```

> 目录 13-16 可按需并入对应资源文档；关键点不是份数，而是「每端点一份、格式一致、契约同步」。

---

## 三、P3 一致性检查清单（逐端点人工/脚本核对）

1. 每个端点标题格式：`## {METHOD} /path — {一句话中文描述}`（当前 08-events 等部分无「— 描述」）。
2. 每个端点有且仅有一行 scope（缺失：05-skills search、06-scenes 正文 `GET /api/scenes`、08-events
   chat/task/status、chat/task/stream）。
3. 每个 body 接口有请求体字段表 + JSON 示例（缺失：04-chat PUT config / POST submit、06-scenes PUT、
   08-events chat）。
4. 每个端点有响应示例或明确出参结构（缺失：03-agents 部分、05-skills 多数、06-scenes 多数、08-events
   status/stream、09-task-logs stream）。
5. 每个端点有「本端点特有错误」表（缺失：03-agents、04-chat、05-skills、06-scenes）。
6. 重复 scope 长句 → 一行 `> scope：dual`（全表引用 README），全目录一次性收敛（约 60 处）。
7. 时间字段说明 / `repo_key` 口径 / 分页参数名统一（`per_page` 优于 `limit`，stream 除外）。
8. `{{SERVER_URL}}` 首次使用处链到 README「Base URL」。
9. 错误码与 README 字典一致：不再出现 `{"message":"401 Unauthorized"}` 旧写法。

---

## 四、实施步骤与状态追踪

| 阶段 | 动作 | 产出 | 状态 |
|------|------|------|:---:|
| P0 | 重生成 openapi.json + test_openapi 同步 | 契约可信源 | ✅ 已完成（221 ops / 176 paths，与 EXPECTED_PUBLIC 精确一致） |
| P1 | 补齐 9 个未覆盖前缀文档 | settings(13)/runtime(14)/devstage(15)/auth-misc(16) 四篇 | ✅ 已完成 |
| P2 | 补齐已覆盖前缀内缺漏端点 | scenes/knowledge/repos/events/skills/git-repo 补全 | ✅ 已完成 |
| P3 | 全目录套统一模板 + §三检查清单 | 现有文档补齐 scope/response/query 细节；README 增「全局约定」 | ✅ 已完成（结构性修复） |
| P4 | 一致性测试 + lint 脚本 + CI | `tests/test_api_docs_sync.py`（3 例）+ `scripts/check_api_docs.py` + `scripts/regenerate_openapi.py` | ✅ 已完成 |

> 2026-09-16 实施记录：
> - P0：`scripts/regenerate_openapi.py`（复用 `custom_openapi(app)` + `write_openapi_contract`，DB 走临时 sqlite）
>   落盘 `help/api/openapi.json` + `src/api/static/openapi.json`；契约 221 ops / 176 paths 与 `EXPECTED_PUBLIC` 零漂移。
> - P1/P2：新增 13-settings(34)/14-runtime(17)/15-devstage(8)/16-auth-and-misc(auth+6 类)；补齐 scenes(11)/knowledge(13)/
>   repos scene-recommend-config(2)/events(2)/git-repo(3，含同名根目录与凭据工作区)/skills(list+agent-stats)。
> - P3：按模板补全 scope 行、Query/Path/Body 表、出参结构与错误/返回；README 收拢「全局约定」（Base URL/版本/分页/时间/ID 口径/repo_key/SSE/限流）；
>   移除已下线端点文档 `POST /api/skills/sync-official`、`GET/DELETE /api/git-repo/{owner}/{repo}/issues/{number}/labels`。
> - P4：`src/help_docs_sync.py`（tracked，解析/归一化/compare 共享逻辑）+ `scripts/check_api_docs.py`（薄 CLI 入口）
>   路径参数名归一化 + query 剥离 + `GET/POST` 联合标题解析 + `DOC_EXEMPT` 白名单
>   （tasks/notify、agents/types、skills/template、events flow/reset）；sync 测试与契约/文档零缺口。
> - 门户同步：docs/openapi 镜像同步（除有意分叉的 10-git-repos.md）；test_help_directory/test_help_page_generation 文件数断言动态化。

---

## 五、验证

1. `tests/test_api_docs_sync.py`：markdown 端点集合 == openapi paths 集合（白名单除外），双向零漂移。
2. `scripts/check_api_docs.py` 全目录 0 报错（模板七要素齐全）。
3. `tests/test_openapi.py` 回归绿。
4. 抽样：任一外部读者按文档 curl 请求能直接跑通（示例非杜撰，字段与契约一致）。
5. 人工抽查 P3 检查清单 9 项全覆盖。

---

## 六、影响面与风险

| 项 | 影响 |
|----|------|
| 文档 | help/api 13 篇全量改版 + 新增 4-5 篇；README 增全局约定节 |
| 代码 | `/openapi.json` 重生成 + 下线残留路由清理确认；新增 `tests/test_api_docs_sync.py`、`scripts/check_api_docs.py` |
| CLI | 契约增补 → 新命令解锁（settings/scenes 市场/runtime/knowledge 写），与 openapi-full-coverage 收益一致 |
| 测试 | test_openapi 补录/清理；新增 sync 测试 |
| 风险 | 「契约大而文档全」工作量集中在 P1/P2；缓解：按资源分批、样板先行、脚本兜底，避免一次性大 diff |
| 不动点 | 不改 `x-token-scope` 语义、不改任何 HTTP 行为/状态码、不改字段名（纯文档 + 契约重生成） |

---

## 附：与既有设计的衔接

- `error-code-standard-design.md`（P2 deps 已做英文化）：§2.5 逐端点错误表模板即本方案的「本端点特有错误」
  一节，直接复用其模板与「错误码字典见 README」原则。
- `openapi-full-coverage-design.md`：已将 162 处 `get_current_user` → `require_no_session_token`，
  settings/runtime/devstage 等前缀已进契约——本方案 P1 补文档正是承接其产出的**未完成文档侧**。
- `openapi-x-token-scope-design.md` / `token-scope-classification-design.md`：scope 三档语义不变，
  本方案只规范其**表述方式**（一行引用 + README 全表）。