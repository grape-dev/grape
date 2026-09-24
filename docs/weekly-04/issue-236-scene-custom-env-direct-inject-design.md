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

**值存储位置**：`custom_fields` 整体以 JSON 明文存于 `devops_pipeline_scene.advanced_options` 列（`scenes.py` 保存写入 / `event_pipeline._inject_scene_env` 读取同名表）。存入对象含 `{name, value, is_secret}`（`is_secret` 随该项一起序列化入库，与 name/value 同级，无独立列；读取时原样取回，用于回显示/掩码）。本次**值不加密**（Spec v2 Out of Scope 明确排除）。值仅注入 agent 任务 env，不落 prompt。

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

**现状约束**：系统现有全部凭据（GitCode/GitHub token、华为云 AK/SK、IM app_secret）均为**明文存储**（`users`/`user_credentials` 表），无加密先例。前端统一 `xxx_exists` 布尔 + `********` 掩码回显，保存时 `'********'` 视为未修改（`im_channel.js:173`）。Spec v2 明确「值加密存储」为 Out of Scope。

**secret 判断规则**（评审 2026-09-24）：采用「**命名约定自动识别 + 手动开关覆盖**」，不依赖用户每次手动标记：

- **自动识别**：name 匹配约定后缀 → 默认标记 `is_secret=true`。约定后缀（大小写不敏感、精确匹配结尾）：`_KEY`、`_SECRET`、`_SK`、`_TOKEN`、`_PASSWORD`、`_CREDENTIALS`、`_PWD`。
- **手动覆盖**：前端每行提供「密钥」勾选，用户可手动勾选/取消（如 `MY_PASSWORD` 实际非密钥、或 `DEPLOY_TOKEN` 需要隐藏）。勾选状态优先级高于自动识别。
- 判定时机：**前端录入行时**即计算 `is_secret` 预览并回显 `********`；后端保存时按同一规则兜底（防绕过前端）。规则放在双端共享，避免不一致。
- 特例：华为云 AK/SK 预设行（`HUAWEICLOUD_SDK_AK/SK`）由场景市场**强制标记** secret（key 只读、不回显明文），不依赖自动识别。

```json
// 自动识别示例
{"name": "DEPLOY_SECRET", "value": "xxx", "is_secret": true}   // _SECRET 后缀 → secret
{"name": "DEPLOY_URL",    "value": "xxx", "is_secret": false}   // 无约定后缀 → 普通
```

- **本期采纳（A）：密钥类型标记 + 掩码回显，存储仍明文**
  - `custom_fields` 项按上述规则附加 `"is_secret": true`。
  - `get_advanced_options`：secret 项返回 `value_exist=true`，**不回显明文值**（`value` 置空/返回 `"********"`）。
  - 前端：secret 项 value 输入框为 password 类型 + 涂眼切换（现有控件已具备）；有值时回显 `********`，保存时 `********` 视为未修改、沿用旧值。
  - `_inject_scene_env`：注入 agent env 时解密/直读明文 `value`（落库值即明文原值，读库即得）。
  - 代价：DB 仍可读明文，仅降低页面/接口回显扩散风险。
- **后续增强（B，拉大改造面，建议单独 Issue）：真加密存储**
  - 对称加密（AES-GCM/Fernet，密钥派生自服务端 `SECRET_KEY`）加密落库，读取回显 only exists、注入时解密；`requirements.txt` 已有 `python-jose[cryptography]` 可复用 crypto 基础。
  - 涉及密钥管理/轮换/迁移存量，超出本 Issue 验收范围。

本期按 A 实施，B 记为后续可选（不阻塞本 Issue）。

---

## 3. 各模块改动

### 3.1 存储/校验层 `src/api/routes/scenes.py`

- `AdvancedOptionsRequest.custom_fields` 字段定义不变（仍为 list），但语义改为 `[{name, value, is_secret?}]`。
- `save_advanced_options`（L1759-1775）：
  - 将 `f.get("key")` 改为 `f.get("name")`；
  - 新增大写校验 + 冲突校验（见 2.2），任一不通过返回 400 + 明确 message；
  - 存储字段 `{"name": ..., "value": ..., "is_secret": bool}`（is_secret 默认 false）。
- `get_advanced_options`（L1727-1731）：secret 项返回 `{"name", "value_exist": true, "is_secret": true}`（value 不回显明文）。

### 3.2 env 注入层 `src/api/event_pipeline.py`

重写 `_inject_scene_env`（L2084-2093）：

```python
fields = [f for f in (opts.get("custom_fields", []) or []) if f.get("name")]
for f in fields:
    name = (f.get("name") or "").strip()
    if not name:
        continue
    if not _valid_custom_env_name(name):      # 大写 + 保留冲突检查（兜底）
        log.warning(f"scene custom env '{name}' rejected: reserved or not uppercase")
        continue
    env_extra[name] = str(f.get("value", ""))
```

- **删除** `GRAPE_CF_1..N` 与 `GRAPE_CF_MAP` 生成逻辑。
- 抽公共工具 `_valid_custom_env_name(name)`（大写 + 前缀 `GRAPE_` + 保留集合）。

### 3.3 prompt 指引层 `src/prompt_builder.py`

> 评审结论（2026-09-24）：**不新增任何自定义变量 prompt 指引**，自定义变量的使用方式统一在技能 SKILL.md 中写清楚。

- L197 `fields = [f for f in (...) if f.get("name")]`（读取 name 判断是否输出指引，行为与现状一致）。
- L207 移除「GRCPE_CF_MAP + 两步读取」的指引描述（旧机制指引清理，**列变量名提示同样不新增**）。
- docstring L151 同步更新（不再描述 GRAPE_CF_N）。
- 技能侧：需要读取自定义变量的技能在自身 SKILL.md 中补充说明（如 `printenv MY_API_KEY` / `os.environ.get('MY_API_KEY')`），不属于本方案代码范围。

### 3.4 前端 `dashboard.js`

- L820 `_addCustomFieldInline(key, value)` → 参数名改 `name`，`placeholder="name"`，并把回填 key 改为 name。
- 录入即校验（L811-825 `_addCustomFieldInline` 输入事件 + L845-857 `_saveAdvancedOptionsInline`、L1591-1601 场景创建）：`name` 非大写或命中前端 `_RESERVED_ENV_NAMES`/`GRAPE_` 前缀时实时标红并阻止保存；提交字段 `{name, value, is_secret}`。
- secret 项：value 回显 `********`（`is_secret` 时），保存时 `********` 视为未修改沿用旧值；password/明文切换沿用现有涂眼控件。
- 回填 L1646 `window._addCustomFieldInline(f.key, f.value)` → `f.name`（secret 项传 `f.value_exist ? '********' : ''`）。

### 3.5 前端 `scene-market.js`

- 华为云 AK/SK 预设行 L736/L738/L741 已是大写 name（`HUAWEICLOUD_SDK_AK`），直接兼容；key 只读逻辑保留（`is_secret=true`，页面不回显明文）。
- 回填 L814-827 `f.key` → `f.name`。
- 应用确认 L836-841 提交 `{name, value, is_secret}`（secret 沿用 `********` 语义）。

### 3.6 测试 `tests/test_scene_env.py`

- 更新 `test_injects_scene_id_and_custom_fields_from_issue_scene`：断言 `env["env"]=="prod"`（直接注入）。
- 删除 `GRAPE_CF_1`/`GRAPE_CF_MAP` 断言。
- 新增用例：
  - 直接注入：`{name: MY_API_KEY, value: xxx}` → `env_extra["MY_API_KEY"]=="xxx"`；
  - 冲突拒绝：name=`PATH` / `GRAPE_X` → 不注入 + 其余项正常注入；
  - 大写校验：name=`my_key`（后端保存层）→ 400/报错；
  - secret 项（A 档）：`get_advanced_options` 不返回明文 value（仅 `value_exist`）、`_inject_scene_env` 仍注入明文值。

### 3.7 文档同步

- `help/usage/agent-environment-variables.md`：删 `GRAPE_CF_1..N`/`GRAPE_CF_MAP` 行，改描述「自定义变量以 name 直接注入」。
- `docs/weekly-02/scene-advanced-options-design.md`、`help/api/06-scenes.md`、`docs/openapi/06-scenes.md`：`custom_fields` 结构示例/说明 `key`→`name`。

---

## 4. 验收对照（Spec v2）

| 验收项 | 方案覆盖 |
|--------|---------|
| 技能可通过 `os.environ.get('VAR_NAME')` 直接获取 | ✅ 3.2 直接注入 |
| 冲突时拒绝注入并明确错误提示 | ✅ 3.1 保存拦截 + 3.2 运行时兜底跳过 |
| 非大写报错提示 | ✅ 3.1 + 3.4 前端 |
| 旧机制 `GRAPE_CF_MAP`/`GRAPE_CF_N` 完全移除 | ✅ 3.2 删除生成逻辑 + 3.7 文档清理 |
| 前端 name+value 直接录入 + 大写校验 | ✅ 3.4 / 3.5 |
| 现有测试通过 + 新增测试 | ✅ 3.6 |
| 密钥类型变量：不回显明文 + 注入可用 | ✅ 2.4-A（is_secret + 掩码；加密为后续 B） |

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