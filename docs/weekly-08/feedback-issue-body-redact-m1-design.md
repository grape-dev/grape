# 修复 M-1：反馈→Issue 转换在公开仓库发布前对 fail_summary/description 做 IP/路径/凭据脱敏 [weekly-08]

> 状态：**已实施（TDD 先红后绿 ✅）**｜目标分支：weekly-08（部署分支 weekly-08-lyz）｜关联：PR #680 架构守护审查 M-1
> **敏感信息声明**：本文不含真实 IP、域名、账号、仓库名或 token（一律 `<xxx>` 占位）。

---

## 〇、问题现象与影响

- `src/api/feedback_issue_converter.py::_build_issue_body()`（PR #680 新增）把反馈行转成 Issue 正文，
  仅做**长度截断**（`[:2000]`），未对 IP/绝对路径/凭据做脱敏。
- `fail_summary` 来源（`src/api/routes/feedback.py:193`）：`task_result_text`（`agent_tasks.result_text` 即 Agent
  输出文本）或回退 `events.result`，截断 500 存库。Agent 失败/报错输出天然可能带内网 IP、绝对路径、错误消息中的凭据片段。
- `_resolve_target` 对 `skill.scope=='official'` 路由到**公开仓库**（`GRAPE_SKILLS_REPO`，全局 bot token 身份建 Issue），
  其余路由到反馈所属仓库。公开仓库 Issue 一经创建即对外可见。
- 现实场景：Agent 执行失败 result_text 含内网地址（如 `Connection refused to <intranet_ip>:<port>`）、
  绝对路径（如 `<deploy_root>/.env`）、凭据串 → 反馈落库 → reconcile worker 转换 → 公开 Issue 正文泄露部署拓扑。

**与既有文档承诺的差距**：`docs/weekly-08/feedback-openapi-expose-design.md` §四.9 写明「描述进入 Issue 前做
长度/空行/URL 消毒」，§五也声明「文档中不出现内网 IP / 绝对路径 / 部署拓扑细节」——但实现只覆盖了长度，
**IP/路径/凭据三亲面未覆盖**。M-1 即此实现缺口。

---

## 一、目标语义

1. **任何进入 Issue 正文的文本**（`description` + `fail_summary`）在组装前必须经过脱敏，且脱敏在
   **转换器一处**完成（`_build_issue_body` 内），不分散到调用点。
2. 脱敏覆盖三类敏感面：
   - **凭据**（token / secret / password / api key / URL 含密码等）→ `<redacted>`
   - **IP 地址**（含 IPv4 / 内网段 / 端口）→ `<redacted>`
   - **绝对路径**（Unix `/xxx/yyy`、Windows `C:\xxx`）→ `<redacted>`（保留低信息量的相对路径/文件名语义）
3. **不破坏可读性**：脱敏后 Issue 正文仍能看出失败原因（保留报错上下文，仅替换敏感子串），
   且不误伤正常业务文案（如用户自定义 description 里的普通字段名）。
4. 幂等：脱敏函数对已脱敏文本再次执行不改变结果（可复跑/可测试）。
5. **写侧两层兜底**：脱敏先在 `submit_feedback` 落库前对 `fail_summary` 做一次（防库内明文扩散），
   再在 `_build_issue_body` 出转变换时再做一次（防存量明文）。两层都幂等。

---

## 二、方案设计

### 2.1 新增脱敏模块 `src/observability/redact_issue_body.py`（或并入既有 logs.redact）

**复用而非重造**：既有 `src/observability/logs.py::redact()` 已覆盖 token/URL 凭据面（正则：
`grape_/gcw_/rta_/tk_/ghp_/glpat-` 前缀、`key=value`、`scheme://user:pass@host`、`authorization:`），
且 `src/api/knowledge.py` 已有 `_load_credential_filter()` 可从 `configs/credential-filter.json` 读敏感子串。

设计一个**纯函数**（无 IO、无 DB）：

```python
# src/observability/redact_issue_body.py
def redact_issue_text(text: str, max_len: int = 2000) -> str
    # 1) 复用 logs.redact 的凭据/URL 打码（token、key=value、URL 含密码）
    # 2) 补充 IP / 内网段 / 端口 正则
    # 3) 补充绝对路径正则（Unix /home/xxx、/root/xxx；Windows [A-Z]:\...）
    # 4) 长度截断（保持原 max_len 语义）
    # 返回脱敏后的文本（幂等）
```

正则明细（put in module constants）：

| 类别 | 匹配 | 替换 |
|------|------|------|
| 凭据/URL | 复用 `logs._TOKEN_RES[0..3]` | 现行为（`***`） |
| 敏感前缀 token | `\b(grape_|gcw_|rta_|tk_|ghp_|glpat-)[A-Za-z0-9_\-]+` | `<redacted>` |
| IPv4（含端口） | `\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?::\d+)?\b` | `<redacted>` |
| 内网段兜底（私网 CIDR 命中但上面已全量 IP 替换，双保险） | `(?:10\.|172\.(?:1[6-9]\|2\d\|3[01])\.|192\.168\.)` | `<redacted>` |
| Unix 绝对路径 | `(?:/root\|/home\|/etc\|/opt\|/var\|/srv\|/data\|/tmp\|/app\|/workspace\|/config)\S*` | `<redacted>` |
| Windows 绝对路径 | `[A-Za-z]:\\[^\s"']+` | `<redacted>` |

> 说明：绝对路径用**常见部署根目录白名单**前缀，而非任意 `/` 开头——避免误伤正常文案中的
> `1/2`、URL 已脱敏剩余部分、CJK 标点等。`<redacted>` 用全角尖括号占位与仓库既有文档规范一致。

### 2.2 转换器接入点（核心，一处生效）

`src/api/feedback_issue_converter.py::_build_issue_body()`：

```python
from src.observability.redact_issue_body import redact_issue_text

desc = (row.get("description") or "").strip()
if desc:
    lines.append(redact_issue_text(desc, 2000))
summary = (row.get("fail_summary") or "").strip()
if summary:
    lines += ["", "## 失败摘要", "```", redact_issue_text(summary, 2000), "```"]
```

### 2.3 写侧摄入前兜底（防明文扩散到 DB）

`src/api/routes/feedback.py::submit_feedback` 落库前：

```python
fail_summary = redact_issue_text((ctx or {}).get("fail_summary") or "", 500)
# 注意顺序：db 列上限 500；先脱敏后截断，长度语义不变
```

> 双保险原因：`fail_summary` 是要进**公开 Issue**的字段，落库前先打码可减少"明文在库"的面；
> `_build_issue_body` 再打码一次覆盖存量明文。`description`/`title` 由用户/agent 提交，不做写侧预打码
> （保留用户原始展示），仅出转变换时打码。

### 2.4 不改动项

- `src/observability/logs.py` 保持原样（其 `redact` 被本模块复用，不反向引入循环依赖）。
- `user_feedback` 表结构、`feedback` 响应模型、`_resolve_target` 路由逻辑均不动。
- `GRAPE_SKILLS_REPO` 是否公开属运营配置，不在本 PR 判定范围。

---

## 三、模块改动

| 模块 | 改动 |
|------|------|
| `src/observability/redact_issue_body.py`（新增） | `redact_issue_text()` 纯函数 + 三个正则族 + 复用 `logs.redact` |
| `src/api/feedback_issue_converter.py` | `_build_issue_body` 对 `description`/`fail_summary` 调用 `redact_issue_text` |
| `src/api/routes/feedback.py` | `submit_feedback` 落库前对 `fail_summary` 调 `redact_issue_text`（防明文入库） |
| `tests/test_redact_issue_body.py`（新增） | 见 §六 TDD（先红后绿） |

---

## 四、关键实现要点

1. `redact_issue_text` 必须**无 IO/无 DB/无 env 依赖**（纯函数），便于单测与在 converter（异步 worker 线程）中调用。
2. IP 正则与 `logs.redact` 分工：`logs.redact` 管凭据/URL；新增模块管 IP/路径。**模块内部先复用再补充**，
   避免两处正则漂移。
3. 幂等验证可用固定例：`redact_issue_text(redact_issue_text(x)) == redact_issue_text(x)` 作为不变量单测。
4. 长度语义：先脱敏后截断（`[:max_len]`），保持与现状 `[:2000]`/`[:500]` 一致；已脱敏文本可能更短，无副作用。
5. 兼容 GitCode/GitHub 双 provider：脱敏在**服务端**做，与目标平台无关；GitHub 的 `https://...` URL 里
   `user:pass@host` 由 `logs.redact[2]` 覆盖。

---

## 五、影响面 / 风险

| 项 | 说明 |
|----|------|
| 公开仓库泄露 | **根治**：fail_summary/description 出转变换前打码，内网 IP/路径/凭据不再进公开 Issue |
| 库内明文 | 写侧预打码减少扩散面；存量明文由转换器二次打码兜住 |
| 可读性 | 仅替换敏感子串，保留报错上下文；绝对路径用部署根白名单避免误伤业务文案 |
| 误伤风险 | 全量 IP 替换可能把正常文案里的版本号/编号（如 `1.2.3` 非 IP）算入——正则限定为完整 IPv4 形态，
  并用「IP 替换先于路径」顺序避免路径里的 IP 段被二次处理；测试用例覆盖正常文案不误伤 |
| 回归范围 | `tests/test_feedback_*`、`tests/test_issue_451_placeholder_comment.py`、原子回归套件中
  `feedback_issue_converter` 相关原子 |

---

## 六、测试设计（TDD，先红后绿）

落地文件 `tests/test_redact_issue_body.py`：

| ID | 场景 | 断言 |
|----|------|------|
| R1 | `fail_summary` 含内网 IPv4 + 端口（如 `<intranet_ip>:<port>`） | 正文中该串被替换为 `<redacted>` |
| R2 | `fail_summary` 含绝对路径（如 `<deploy_root>/configs/...` / Windows `C:\...`） | 路径被替换为 `<redacted>` |
| R3 | `fail_summary` 含 `password=xxx` / `https://<user>:<pass>@<host>` | 凭据部分被替换（`key=***` / `:****@`） |
| R4 | 脱敏幂等：`redact_issue_text(redact_issue_text(x)) == redact_issue_text(x)` | 成立 |
| R5 | 普通文案（无 IP/路径/凭据） | 原文不变（不误伤），长度截断仍生效 |
| R6 | `_build_issue_body` 集成：构造带内网 IP 的 fail_summary 行 | 返回正文不含原始 IP，含 `<redacted>` |
| R7 | `submit_feedback` 写侧：入参 fail_summary 含内网 IP | 落库值已打码（防明文入库） |
| R8 | 版本号/编号（`1.2.3` 非 IP）、CJK 文案 | 不被误替换 |

red 阶段：先写以上断言（R1/R2/R3/R6/R7 目前应 fail——现状仅长度截断），
green 阶段：实现 §二改动，断言全部 pass；
全量：`pytest tests/test_feedback_*.py tests/test_issue_comment_resume_ledger.py` 保持 green。

---

## References

- PR #680 架构守护审查 M-1（fail_summary 公开仓库泄露风险，Medium）
- `docs/weekly-08/feedback-openapi-expose-design.md` §四.9 / §五（"长度/空行/URL 消毒"承诺 vs 实现缺口）
- `src/api/feedback_issue_converter.py:194-213` `_build_issue_body`
- `src/api/routes/feedback.py:189-193,320,344-358`（fail_summary 来源与落库）
- `src/observability/logs.py:40-56` `redact()`（凭据/URL 打码复用源）
- `src/api/knowledge.py:770-799` `_load_credential_filter`（`configs/credential-filter.json` 读取参考）
- `configs/credential-filter.json`（敏感子串白名单）