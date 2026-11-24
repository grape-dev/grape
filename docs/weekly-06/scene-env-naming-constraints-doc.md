# 项目场景自定义环境变量（custom_fields）命名约束说明

> 状态：文档（信息登记，2026-10-12）｜分支：weekly-06-lyz
> 范围：Grape 项目场景「高级选项 → custom_fields」注入 agent 的自定义环境变量，其**命名规则、保留变量、密钥/范围语义、校验错误**，统一说明。
> 代码与校验唯一权威：`src/scene_custom_env.py`（供 `scenes.py` 保存/读取与 `event_pipeline._inject_scene_env` 注入端复用）。

---

## 1. 核心规则（三条红线）

| 规则 | 约束 | 违反示例 → 报错 |
|------|------|------|
| ① 非空 | 变量名不能为空 | 空 →「自定义环境变量名不能为空」 |
| ② 全大写 | 符合 `[A-Z][A-Z0-9_]*`（首字符大写字母） | `my_api_key` / `MyKey` →「需使用大写字母（如 MY_API_KEY）」 |
| ③ 不与系统保留冲突 | 不能以 `GRAPE_` 开头；不能等于保留集合中的名字 | `GRAPE_APP` / `ISSUE_NUMBER` →「与系统保留环境变量冲突，请更换名称」 |

> 同时满足三条才算合法；任意一条不满足即被**拒绝注入**（scenes.py 保存端报错、event_pipeline 注入端兜底跳过）。

---

## 2. 系统保留变量（自定义时禁用）

### 2.1 前缀兜底：`GRAPE_*`（任意以 `GRAPE_` 开头的变量名均保留）

> 系统用 `GRAPE_` 前缀统一承载服务端注入的平台变量，自定义场景变量**不得占用该命名空间**。

### 2.2 显式保留集合（`scene_custom_env._RESERVED_ENV_NAMES`）

```
PATH HOME USER USERNAME SHELL LANG LANGUAGE LC_ALL LC_CTYPE PWD OLDPWD
HOSTNAME HOST TERM TMPDIR TMP TEMP EDITOR VISUAL SHLVL PS1 OSTYPE MACHTYPE
LOGNAME MAIL WINDIR SYSTEMROOT COMSPEC NUMBER_OF_PROCESSORS PROCESSOR_ARCHITECTURE
REPO_FULL_NAME ACCOUNT BOT_USERNAME ISSUE_NUMBER PR_NUMBER GIT_ASKPASS
GRAPE_SERVER_URL GRAPE_WORK_BRANCH GRAPE_SCENE_ID GRAPE_SESSION_TOKEN GRAPE_CLI_MODE
BOT_GITCODE_TOKEN USER_ID CONVERSATION_ID
```

> 附注（系统完整注入集，含最近新增）：`GRAPE_ISSUE_ID`（需求卡片主键，awaiting-input 等用）、`REPO_OWNER / REPO_NAME`（`REPO_FULL_NAME` 拆分）、`GRAPE_LINKED_REPOS / GRAPE_LINKED_WORKTREES`、`GRAPE_CLI_MODE`、`BOT_GITHUB_TOKEN` 等——**凡平台注入的均为保留**，一律不可自定义覆盖。

---

## 3. 命名约定驱动的密钥语义

- **自动密钥识别后缀**（大小写不敏感）：`_KEY` `_SK` `_SECRET` `_TOKEN` `_PASSWORD` `_PWD` `_CREDENTIALS`
  - 例：`HUAWEI_AK` → 自动判定 `is_secret=true`，页面掩码、注入不落明文日志。
- **三态覆盖**：`is_secret` 显式 `True/False` 优先；未显式 → 按上述后缀自动识别。
- 语义：命名约定 = 默认安全；显式标记 = 强控（可强制非密钥或强制密钥）。

---

## 4. 注入范围（scope）

- 取值枚举：`all` / `vod` / `issue` / `coding` / `qa` / `sre`
- 归一化（`normalize_scope`）：
  - 空 / `[]` / `[""]` → `["all"]`；
  - 旧单值字符串兼容（`"sre"` → `["sre"]`）；
  - 去重、非法项剔除；含 `all` 收敛为 `["all"]`（全域，不再并列其它）；
  - 排除后为空 → `["all"]`。
- 语义：变量只注入 scope 命中的 agent（如 `["sre"]` 只在 SRE 任务注入）。

---

## 5. 预置参数（preset）

- `preset=True`：场景声明的**预置参数**，`name / scope / is_secret` 由场景声明锁定，前端/用户只读不可改（`value` 可放默认值 `default_value`）。

---

## 6. 校验来源与注入端行为

| 层 | 行为 |
|----|------|
| `scenes.py` 保存/读取 | `custom_env_error(name)` 校验；非法名报错，不落库 |
| `routes/autopilot.py` 保存（Issue #4066） | 定时任务 create/update 走 `custom_env_error(name)`；冲突/非法名一次性 400 汇总回显（`invalid_names`），不落库 |
| `event_pipeline._inject_scene_env` | 注入 agent env 时兜底校验（`is_upper_name` / `is_reserved_name`）；非法项跳过注入；平台已注入的键优先，不被 custom_fields 覆盖 |
| `scheduler._execute_autopilot_agent` | 定时任务注入同一套校验；一次执行输出**单条** info 汇总（`injected/skipped/names`），不再逐条 WARNING |
| 技能侧 | 直接 `os.environ.get('MY_API_KEY')` 读取（变量名原样注入） |

> `GRAPE_` 前缀默认保留，但 `src/kernel/scene_custom_env.py::_PLATFORM_MANAGED_ENV` 白名单里的
> **三精确名**（`GRAPE_ANALYSIS_MODE` / `GRAPE_ORG_ID` / `GRAPE_MANAGED_BY`）允许自定义注入
> （Issue #4066 收窄保留语义）；其余 `GRAPE_*`（`GRAPE_SESSION_TOKEN` / `GRAPE_API_TOKEN` /
> `GRAPE_WORK_BRANCH` / `GRAPE_SERVER_URL` / `GRAPE_SCENE_ID` 等）仍不可用。

---

## 7. 常见问题

1. **为什么必须全大写**：确保与系统（`GRAPE_*` 及 POSIX 常规）语义边界清晰、跨 shell 无歧义。
2. **想要 `GRAPE_*` 名字怎么办**：默认不可（`GRAPE_` 为平台保留命名空间），仅白名单三精确名例外，请优先用业务名（如 `CLOUD_AK`）。
3. **口令变量命名**：建议带 `_TOKEN/_KEY/_PASSWORD` 后缀即可自动掩码；或显式 `is_secret=true`。
4. **只想注入给某 agent**：scope 选该 agent（如 `qa`）；缺省=全部。
5. **改已有字段语义**：改名/改组以 `normalize_custom_field` 为准（读取容错 `key`→`name`，写入以 `name`）。

---

## 8. 落档

- 本文档为**规则说明**（对齐 `src/scene_custom_env.py` 与 `event_pipeline._inject_scene_env`）；后续规则变更需同步本文件与两处消费端。
- 平台注入变量全表另见 `help/usage/agent-environment-variables.md`。