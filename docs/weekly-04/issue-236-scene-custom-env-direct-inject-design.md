# Issue #236 设计方案：优化场景自定义环境变量机制

> 状态：设计方案（基于 Issue #236 + Spec，可开发性 ready）
> 分支：weekly-04-lyz
> 关联：Issue #230（原需求，已关闭）、#236（活跃开发流）、#219（场景 env 统一注入机制）

---

## 1. 背景与目标

### 1.1 问题现状

当前场景自定义环境变量采用**间接映射**机制。用户配置的 `custom_fields`（`[{key, value}]`）在注入时被转换为：

- `GRAPE_CF_1..N`：按序号注入的变量值
- `GRAPE_CF_MAP`：`{"GRAPE_CF_1": "key名", ...}` 编号→key 名映射

技能要拿到自定义变量必须**两步操作**：

```
1. printenv GRAPE_CF_MAP            # 拿到 {"GRAPE_CF_1":"MY_KEY"}
2. printenv GRAPE_CF_1              # 再按编号取值
```

问题：

1. **两步获取**：使用复杂，技能开发、调试成本高。
2. **编号与变量名脱耦**：`GRAPE_CF_N` 的编号无业务语义，维护困难。
3. **文档误导**：`help/usage/agent-environment-variables.md` 甚至要求技能「eval $(printenv GRAPE_CF_MAP | python3 ...)」才能用。

### 1.2 涉及链路（现状代码定位）

从 Issue 正文与源码核对，涉及模块比 Spec 初版更准确定位：

| 层 | 文件 | 位置 | 现状 |
|----|------|------|------|
| 存储/校验 | `src/api/routes/scenes.py` | L1693-1795 | `AdvancedOptionsRequest.custom_fields` 为 `[{key,value}]`；保存时只校验 key 非空、不重复 |
| env 注入 | `src/api/event_pipeline.py` | L2059-2096 `_inject_scene_env` | 读 custom_fields，生成 `GRAPE_CF_1..N` + `GRAPE_CF_MAP` |
| prompt 指引 | `src/prompt_builder.py` | L148-215 `_build_scene_advanced_options` | prompt 指引 agent「printenv GRAPE_CF_MAP」两步读取 |
| 前端-编辑 | `src/api/static/js/pages/dashboard.js` | L811-825 `_addCustomFieldInline` / L845-857 保存 / L1624-1648 回填 | 录入字段为「key」+「value」 |
| 前端-应用 | `src/api/static/js/pages/scene-market.js` | L730-744、L811-844、L848+ | 场景市场应用/覆盖应用时同样用 key/value 录入，回填按 `f.key` |
| 测试 | `tests/test_scene_env.py` | L66-103 | 断言 `GRAPE_CF_1` / `GRAPE_CF_MAP` |

（注：Spec 提到的 `src/runtime/service.py` / `agent.py` 实际不直接处理 custom_fields —— env_extra 在 `_enqueue_pipeline_task`/`_execute_direct_task` 组装时已完整注入，worker 端仅透传；本方案改 `event_pipeline._inject_scene_env` 即可覆盖 worker 与 direct 两条链路。）

### 1.3 目标（Spec v2 口径）

- 技能可通过 `os.environ.get('VAR_NAME')` **一步直接获取**自定义变量值。
- 注入前**防冲突校验**：自定义变量名与系统保留变量（`GRAPE_*`、`PATH`、`HOME`、`USER`、`SHELL`、`LANG` 等）冲突时拒绝注入并明确提示。
- **命名规范统一**：变量名要求大写格式，非大写时报错提示。
- **旧机制完全移除**：`GRAPE_CF_MAP` / `GRAPE_CF_N` 注入逻辑删除，不再生成（无需兼容过渡）。
- 前端适配新的 name+value 直接录入 + 大写校验。
- 所有现有测试通过，并新增冲突校验/大写校验/直接注入测试。

---

## 2. 方案概述

### 2.1 数据格式：`{key, value}` → `{name, value}`

`custom_fields` 每项字段名从 `key` 改为 `name`：

```json
// 旧
{"custom_fields": [{"key": "MY_API_KEY", "value": "xxx"}]}
// 新
{"custom_fields": [{"name": "MY_API_KEY", "value": "xxx"}]}
```

`name` 即环境变量名，注入时**直接以 name 作为键**写入 env。

**值存储位置**：`custom_fields` 整体以 JSON 明文存于 `devops_pipeline_scene.advanced_options` 列（`scenes.py` 保存写入 / `event_pipeline._inject_scene_env` 读取同名表）。存入对象含 `{name, value, is_secret, scope}`（`is_secret`/`scope` 随该项一起序列化入库，与 name/value 同级，无独立列；读取时原样取回，用于回显示/按需注入）。本次**值不加密**（Spec v2 Out of Scope 明确排除）。值仅注入 agent 任务 env，不落 prompt。

### 2.5 注入范围 scope（评审 2026-09-24：新增）

> 需求：每条自定义变量增加 `scope` 字段，**按 agent 类型过滤注入**，避免变量注入到不相关 agent。默认 `["all"]`（注入所有 agent），可选与场景 node 一致的 agent：`vod`/`issue`/`coding`/`qa`/`sre`。
>
> 评审迭代（2026-09-24）：**scope 应为数组，支持多选**（如同时注入 coding+qa）。

- **字段**：`custom_fields[]` 项增加 `"scope": ["all"]`（agent 名小写数组）。
- **枚举**：`all` + `vod`/`issue`/`coding`/`qa`/`sre`（与场景 node 一致）。
- **存储**：随项序列化入库，默认 `["all"]`。
- **归一化**（后端保存时）：
  - 空 / 缺省 / `[]` / `[""]` → `["all"]`；
  - 旧单值 string（`"sre"` / `"all"`）→ 兼容包装为 `["sre"]` / `["all"]`（读取侧以数组为准）；
  - 数组内去重、非法值剔除并归一化（若剔除后为空 → `["all"]`）。
- **注入过滤**（`_inject_scene_env`）：
  - `_inject_scene_env` 增加 `agent_type: str = ""` 参数；
  - 对每个字段：`"all" in scope or agent_type in scope` 才注入，否则跳过（`log.debug` 级，不告警噪音）；
  - `agent_type` 为空时（调用方未传/通用注入）视为全覆盖注入（保持现状行为）。
- **调用点**（两处都有 agent_type 可用）：
  - `event_pipeline._enqueue_pipeline_task`（worker 模式）L1962；
  - `event_pipeline._execute_agent_inner`（direct 模式）L2414。
- **校验**：保存时 scope 数组内每项 ∈ 枚举；整项非法 → 400。
- **前端**：每行新增 scope **多选**（`all` + 5 个 agent 的 checkbox/多选下拉）；未选默认 `["all"]`。

```json
{"name": "HUAWEI_AK", "value": "xxx", "is_secret": true, "scope": ["sre"]}              // 仅 SRE 注入
{"name": "CICD_ENV",   "value": "xxx", "is_secret": false, "scope": ["coding", "qa"]}  // 仅 coding+qa 注入
{"name": "DEPLOY_URL", "value": "xxx", "is_secret": false, "scope": ["all"]}            // 全 agent 注入
```

### 2.2 校验规则（前端录入时 + 后端保存时，双端一致）

**大写校验**：
- `name` 必须与 `name.upper()` 相等（`[A-Z][A-Z0-9_]*`），否则报错「自定义环境变量名需使用大写，如 MY_API_KEY」。

**冲突校验（系统保留变量）**：
- `name` 以 `GRAPE_` 开头 → 拒绝。
- `name` 命中系统保留变量集合 → 拒绝。
- 保留集合（常量 `_RESERVED_ENV_NAMES`）：
  ```
  PATH HOME USER USERNAME SHELL LANG LANGUAGE LC_ALL LC_CTYPE PWD OLDPWD
  HOSTNAME HOST TERM TMPDIR TMP TEMP EDITOR VISUAL SHLVL PS1 OSTYPE MACHTYPE
  LOGNAME MAIL WINDIR SYSTEMROOT COMSPEC NUMBER_OF_PROCESSORS PROCESSOR_ARCHITECTURE
  REPO_FULL_NAME ACCOUNT BOT_USERNAME ISSUE_NUMBER PR_NUMBER GIT_ASKPASS
  GRAPE_SERVER_URL GRAPE_WORK_BRANCH GRAPE_SCENE_ID GRAPE_SESSION_TOKEN GRAPE_CLI_MODE
  ```
  （动态以 `GRAPE_` 前缀规则兜底；剩余显式列表覆盖常见系统/平台注入变量。）

**重复校验**：沿用现有 name 不重复逻辑。

**前端录入即校验**（评审结论 2026-09-24）：前端在**录入/失焦时**就实时检查大写与保留冲突（保留变量常量表与后端 `_RESERVED_ENV_NAMES` 保持一致），冲突项给红色提示并阻止保存；后端保存时**再兜底校验一次**（防绕过前端的直接 API 调用）。双端规则一致：先大写、再 `GRAPE_` 前缀、再保留集合。

### 2.3 防冲突兜底（运行时）

保存期已拦截大部分冲突；`_inject_scene_env` 注入前仍做**二次冲突校验**（兜底存量/旁路数据）：冲突项跳过不注入并 `log.warning`，其余项正常注入。避免破坏 agent 运行环境（如自定义 `PATH` 覆盖系统 PATH）。

### 2.4 密钥类型变量（secret）处理

> 评审问题：环境变量值可能是密钥（AK/SK/token），如何处理？2026-09-24。

**现状约束**：系统现有全部凭据（GitCode/GitHub token、华为云 AK/SK、IM app_secret）均为**明文存储**（`users`/`user_credentials` 表），无加密先例。前端统一 `xxx_exists` 布尔 + 掩码回显（保存时 `'********'` 视为未修改）。Spec v2 明确「值加密存储」为 Out of Scope。

**secret 判断规则**（评审 2026-09-24，最终版）：**命名约定自动识别 + 显式覆盖（三态）**：

- **自动识别**：name 匹配约定后缀 → 默认 `is_secret=true`。约定后缀（大小写不敏感、精确匹配结尾）：`_KEY`、`_SECRET`、`_SK`、`_TOKEN`、`_PASSWORD`、`_CREDENTIALS`、`_PWD`。
- **显式覆盖**：前端通过**眼睛按钮**切换（password=密钥 / text=明文），保存时以 value 输入框 `type` 判定 `is_secret`；显式 `true/false` 优先于后缀识别（三态语义：传 undefined 才按后缀）。
- 判定时机：**前端录入行时**即按后缀自动识别；**后端保存时**同一三态规则兜底（防绕过前端的直接 API 调用）。
- 特例：华为云 AK/SK 预设行（`HUAWEICLOUD_SDK_AK/SK`）由场景市场**强制标记** secret（name 只读）。

```json
{"name": "DEPLOY_SECRET", "value": "xxx", "is_secret": true}   // _SECRET 后缀 → secret
{"name": "MY_API_KEY",    "value": "xxx", "is_secret": false}  // 显式 false → 明文（覆盖 _KEY 后缀）
```

- **本期采纳（A）：密钥标记 + 前端按 is_secret 显隐，存储仍明文**
  - `custom_fields` 项按上述三态规则附加 `"is_secret"`；
  - `get_advanced_options` 返回明文 `value` + `is_secret`（前端按 is_secret 默认隐藏 + 眼睛可查看），与 settings 页华为云 AK/SK 明文回显惯例一致；
  - `_inject_scene_env` 注入时直读明文 `value`（落库值即明文原值）。
  - 代价：DB 仍可读明文，仅降低页面默认扩散风险。
- **后续增强（B，拉大改造面，建议单独 Issue）：真加密存储**
  - 对称加密（AES-GCM/Fernet，密钥派生自服务端 `SECRET_KEY`）加密落库，读取回显 only exists、注入时解密；`requirements.txt` 已有 `python-jose[cryptography]` 可复用 crypto 基础。
  - 涉及密钥管理/轮换/迁移存量，超出本 Issue 验收范围。

本期按 A 实施，B 记为后续可选（不阻塞本 Issue）。

---

## 3. 各模块改动

### 3.1 存储/校验层 `src/api/routes/scenes.py`

- `AdvancedOptionsRequest.custom_fields` 字段定义不变（仍为 list），语义为 `[{name, value, is_secret?, scope?}]`。
- `save_advanced_options`：
  - 校验 `name` 大写 + 保留冲突 + 重复（见 2.2），任一不通过 400；
  - 校验/归一化 `scope` 数组：每项 ∈ `{"all","vod","issue","coding","qa","sre"}`，整数组合并去重；空/缺省→`["all"]`；含非法项剔除后仍空→`["all"]`；全部非法→400；
  - 存储字段 `{"name", "value", "is_secret": bool, "scope": ["all"]}`。
- `get_advanced_options`：原样返回 `{name, value, is_secret, scope}`（明文 value，前端按 is_secret 决定显隐）。

### 3.2 env 注入层 `src/api/event_pipeline.py`

- `_inject_scene_env(env_extra, repo_key, issue_id, agent_type="")`：
  - 增加 agent_type 过滤：`scope=="all" or scope==agent_type` 才注入，否则跳过；
  - agent_type 为空（未传）→ 全部注入（兼容现状）。

```python
fields = [f for f in (opts.get("custom_fields", []) or []) if f.get("name")]
for f in fields:
    name = (f.get("name") or "").strip()
    if not name:
        continue
    if not _valid_custom_env_name(name):      # 大写 + 保留冲突检查（兜底）
        log.warning(f"scene custom env '{name}' rejected: reserved or not uppercase")
        continue
    scope = f.get("scope") or []
    if isinstance(scope, str):                # 兼容旧单值
        scope = [scope]
    if scope and "all" not in scope and agent_type and agent_type not in scope:
        continue                              # scope 多选过滤
    env_extra[name] = str(f.get("value", ""))
```

- 两处调用点传入 agent_type：
  - `_enqueue_pipeline_task` L1962 → `_inject_scene_env(env_extra, repo_key, issue_id, agent_type)`
  - `_execute_agent_inner` L2414 → 同上
- **删除** `GRAPE_CF_1..N` 与 `GRAPE_CF_MAP` 生成逻辑。
- 抽公共工具 `_valid_custom_env_name(name)`（大写 + 前缀 `GRAPE_` + 保留集合）。

### 3.3 prompt 指引层 `src/prompt_builder.py`

> 评审结论（2026-09-24）：**不新增任何自定义变量 prompt 指引**，自定义变量的使用方式统一在技能 SKILL.md 中写清楚。

- L197 `fields = [f for f in (...) if f.get("name")]`（读取 name 判断是否输出指引，行为与现状一致）。
- 移除「GRCPE_CF_MAP + 两步读取」的指引描述（旧机制指引清理，**列变量名提示同样不新增**）。
- docstring L151 同步更新（不再描述 GRAPE_CF_N）。
- 技能侧：需要读取自定义变量的技能在自身 SKILL.md 中补充说明（如 `printenv MY_API_KEY` / `os.environ.get('MY_API_KEY')`），不属于本方案代码范围。

### 3.4 前端 `dashboard.js`

- L811/L1537 两处 `_addCustomFieldInline(name, value, isSecret, scope)`：`placeholder="name"`；每行增加 scope **多选**（`all` + `vod/issue/coding/qa/sre` 的 checkbox/多选下拉），默认 `["all"]`，回传 scope 数组。
- 录入即校验（L811-825 `_addCustomFieldInline` 输入事件 + L854 `_saveAdvancedOptionsInline`、L1591-1601 场景创建）：`name` 非大写或命中前端 `_RESERVED_ENV_NAMES`/`GRAPE_` 前缀时实时标红并阻止保存；提交字段 `{name, value, is_secret, scope}`。
- secret：value 显隐由 `is_secret`（眼睛切换）决定，保存时按输入框 `type=password` 判定 is_secret。
- 回填 `window._addCustomFieldInline(f.name, f.value, f.is_secret, f.scope)`。

### 3.5 前端 `scene-market.js`

- 华为云 AK/SK 预设行 `HUAWEICLOUD_SDK_AK/SK` 保持 name 只读，scope 默认 `["all"]`。
- `_applyAddCustomField(name, value, readOnlyKey, isSecret, scope)` 增加 scope 多选（只读/华为云场景可隐藏）。
- 回填 `f.name/f.value/f.is_secret/f.scope`。
- 应用确认 `_cfCollectFields` 提交 `{name, value, is_secret, scope}`。

### 3.6 测试 `tests/test_scene_env.py`

- 更新 `test_injects_scene_id_and_custom_fields_from_issue_scene`：断言 `env["ENV"]=="prod"`（直接注入）。
- 删除 `GRAPE_CF_1`/`GRAPE_CF_MAP` 断言。
- 新增用例：
  - 直接注入：`{name: MY_API_KEY, value: xxx}` → `env_extra["MY_API_KEY"]=="xxx"`；
  - 冲突拒绝：name=`PATH` / `GRAPE_X` → 不注入 + 其余项正常注入；
  - 大写校验：name=`my_key`（后端保存层）→ 400/报错；
  - secret：get 返回明文 value + is_secret，注入用明文；
  - scope 过滤：`{scope:["sre"]}` + `agent_type="coding"` → 不注入；`agent_type="sre"` → 注入；`{scope:["coding","qa"]}` + `agent_type="qa"` → 注入；
  - scope 默认：`{scope:["all"]}`（或缺省/空数组）→ 任意 agent_type 注入；
  - scope 兼容：旧单值字符串 `"sre"` 等价 `["sre"]`；
  - 显式 is_secret=false 覆盖后缀识别（`_KEY` 后缀保持明文）。

### 3.7 文档同步

- `help/usage/agent-environment-variables.md`：删 `GRAPE_CF_1..N`/`GRAPE_CF_MAP` 行，改描述「自定义变量以 name 直接注入 + scope 多选过滤」。
- `docs/weekly-02/scene-advanced-options-design.md`、`help/api/06-scenes.md`、`docs/openapi/06-scenes.md`：`custom_fields` 结构示例/说明 `key`→`name` + `scope` 数组。

---

## 4. 验收对照（Spec v2 + 2026-09-24 scope 增补）

| 验收项 | 方案覆盖 |
|--------|---------|
| 技能可通过 `os.environ.get('VAR_NAME')` 直接获取 | ✅ 3.2 直接注入 |
| 冲突时拒绝注入并明确错误提示 | ✅ 3.1 保存拦截 + 3.2 运行时兜底跳过 |
| 非大写报错提示 | ✅ 3.1 + 3.4 前端 |
| 旧机制 `GRAPE_CF_MAP`/`GRAPE_CF_N` 完全移除 | ✅ 3.2 删除生成逻辑 + 3.7 文档清理 |
| 前端 name+value 直接录入 + 大写校验 | ✅ 3.4 / 3.5 |
| 现有测试通过 + 新增测试 | ✅ 3.6 |
| 密钥类型变量：回显明文（前端按 is_secret 隐藏） | ✅ 2.4-A（is_secret + 眼睛切换；加密为后续 B） |
| 变量按 agent 类型 scope 过滤注入（默认 all，支持多选数组） | ✅ 2.5 + 3.2 + 3.4/3.5 + 3.6 |

---

## 5. 影响评估

- **存量数据**：已存 `{key,value}` 的场景，读取时 `custom_fields` 中无 `name`，注入层过滤后不注入（静默跳过）。因本次为测试环境小范围使用，且 Issue 明确「无需兼容过渡」，不引入迁移；如需保留可后续补一次性字段迁移。
- **华为云场景**：`HUAWEICLOUD_SDK_AK/SK` 已大写且非保留名，直接兼容。
- **会话/任务前后兼容**：env_extra 是任务创建时快照，改动不影响历史任务。

---

## 6. 测试命令

```bash
python -m pytest tests/test_scene_env.py -v
python -m pytest tests/test_prompt_builder.py tests/test_runtime_agent.py -q
```