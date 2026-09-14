# Prompt 架构优化方案

> 基于对 `src/prompt_builder.py` 全量代码审查发现的问题，提出优化方案并已全部实施。
> 优先级按影响范围和实施风险排列。

---

## 问题一：`build_prompt` 废弃别名仍在使用 ✅ 已实施

### 现状

`prompt_builder.py` L946:
```python
build_prompt = build_devops_agent_prompt  # deprecated: use build_devops_agent_prompt
```

`event_pipeline.py` 3 处调用仍用旧名（L191/L1355/L1764）:
```python
prompt = build_prompt(evt, [], "", [skill_name] if skill_name else [agent_type], ...)
```

### 风险

有人清理 prompt_builder.py 删别名 → DevOps 流水线直接挂掉，且不会有编译期错误（Python 动态特性）。

### 实施

将 event_pipeline.py 中 3 处 `build_prompt` 调用改为 `build_devops_agent_prompt`，然后删除别名。

**改动文件**: `src/api/event_pipeline.py`（3 处）、`src/prompt_builder.py`（删 L946 别名行）

**验证**: py_compile 通过 + 120 passed, 0 new failures

---

## 问题二：VOD 场景列表注入位置耦合在项目知识基线内部 ✅ 已实施

### 现状

`_inject_vod_scenes()` 在 `_build_project_info_section` 内部被调用（L78 无基线路径 + L98 有基线路径），不是独立步骤。

两个分支行为不一致：
- 有基线：先输出 project-graph/capabilities → 再注入 VOD 场景表格
- 无基线：输出降级提示 → 注入 VOD 场景表格（但 VOD 场景数据来源 `get_scenes(repo_key)` 与项目知识无关）

### 风险

- 项目知识加载异常（DB 锁超时、文件不存在）时 try/except 吞掉异常返回空，VOD 场景列表也会跟着消失
- 改项目知识格式时不小心影响 VOD 场景注入

### 实施

1. 将 `_inject_vod_scenes(lines, agent_type, repo_key)` 重构为 `_build_vod_scenes_section(agent_type, repo_key) -> str`，返回字符串而非操作 lines 列表
2. 从 `_build_project_info_section` 中移除两处 `_inject_vod_scenes` 调用（L78/L98）
3. 在 `build_devops_agent_prompt` 中项目知识之后独立调用：

```python
parts.append(_build_project_info_section(repo_key, agent_type))

# VOD 场景列表（独立于项目知识基线，避免项目知识异常时场景列表消失）
_vod_scenes = _build_vod_scenes_section(agent_type, repo_key)
if _vod_scenes:
    parts.append(_vod_scenes)
```

**改动文件**: `src/prompt_builder.py`

**验证**: 120 passed, 0 new failures

---

## 问题三：Prompt 行为约束散落在硬编码字符串中 ✅ 已实施

### 现状

`build_devops_agent_prompt` L611-662 包含约 50 行硬编码字符串：
- JSON 契约（actions/conclusion/artifacts/...）
- 任务结束规则
- 内部思考 vs 对外结论分离规则
- 评论内容边界（[角色名 Agent] 前缀）
- 发布前自检

这些规则与 prompt 其他部分（A2A 协议、角色定义）混在同一个函数里，没有结构化分离。

### 风险

- 改 JSON 契约字段时不小心碰到评论边界规则
- 无法单独测试行为约束的正确性
- 新增 agent_type 时不知道哪些约束需要保留

### 实施

提取为独立函数 `_build_output_contract()`，返回行为约束段落的完整字符串：

```python
def _build_output_contract() -> str:
    """处理记录输出要求 + 行为约束（所有 DevOps agent 共用，见 agent-runtime-event-full-design.md §8）。

    包含：JSON 契约、任务结束规则、内部思考 vs 对外结论分离、评论内容边界、发布前自检。
    """
    return """
## 处理记录输出要求
...
## 任务结束
...
## 内部思考 vs 对外结论
...
"""
```

在 `build_devops_agent_prompt` 中:
```python
parts.append(_build_output_contract())
```

**改动文件**: `src/prompt_builder.py`（函数定义 + 调用替换）

**验证**: 运行时 `len(_build_output_contract()) == 1404` 字符，内容完整

---

## 问题四：A2A 协议每次从文件读取 ✅ 已实施

### 现状

`build_devops_agent_prompt` 每次构建 prompt 都 `open(configs/agent-description.json)` 读文件:

```python
agents_file = os.path.join(..., "configs", "agent-description.json")
if os.path.isfile(agents_file):
    with open(agents_file, encoding="utf-8") as f:
        agent_registry = json.load(f)
```

### 风险

- 高并发时文件 IO 争用（虽然概率低）
- 文件被编辑中途读到不完整 JSON → `json.load` 异常被 except 吞掉 → A2A 协议段缺失 → Agent 不知道怎么交办

### 实施

模块级缓存 + mtime 检查:

```python
_agent_registry_cache: dict = {}
_agent_registry_mtime: float = 0.0

def _load_agent_registry() -> dict:
    """加载 A2A 协议配置（mtime 缓存，文件未变时不重复读盘）。"""
    global _agent_registry_cache, _agent_registry_mtime
    agents_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs", "agent-description.json")
    if not os.path.isfile(agents_file):
        return {}
    try:
        mtime = os.path.getmtime(agents_file)
        if _agent_registry_cache and mtime == _agent_registry_mtime:
            return _agent_registry_cache
        with open(agents_file, encoding="utf-8") as f:
            _agent_registry_cache = json.load(f)
            _agent_registry_mtime = mtime
            return _agent_registry_cache
    except Exception:
        return _agent_registry_cache or {}
```

在 `build_devops_agent_prompt` 中替换为:
```python
agent_registry = _load_agent_registry()
```

**改动文件**: `src/prompt_builder.py`（缓存函数 + 调用替换）

**验证**: 运行时 `_load_agent_registry()` 返回 `{'vod', 'issue', 'coding', 'qa', 'sre'}` 5 个 agent

---

## 问题五：Chat Agent general prompt 缺少 `Assistant:` 结尾标记的显式管理 ✅ 已实施

### 现状

`build_general_chat_prompt` L774:
```python
parts.append(f"\nUser{repo_note}: {message}\nAssistant:")
```

`Assistant:` 标记直接拼在用户消息后面，没有显式注释说明这是 LLM 生成触发 token。

### 风险

有人重构 prompt 末尾格式时去掉 `Assistant:` → 模型可能生成续写而非对话回复。

### 实施

提取为模块级常量 + 注释:

```python
_CHAT_PROMPT_SUFFIX = "\nAssistant:"  # 触发 LLM 生成回复的关键 token，不可省略
```

在 `build_general_chat_prompt` 末尾:
```python
parts.append(f"\nUser{repo_note}: {message}{_CHAT_PROMPT_SUFFIX}")
```

**改动文件**: `src/prompt_builder.py`

---

## 问题六：`_build_history_file_guide` 已废弃但仍被调用 ✅ 已实施

### 现状

`_build_history_file_guide` (L50) 已废弃，永远返回空字符串，但 `build_devops_agent_prompt` L479 仍调用它:

```python
parts.append(_build_history_file_guide(repo_key, account, runtime_id))
```

### 风险

无害但增加阅读理解成本——维护者以为这里有历史文件引导逻辑。

### 实施

删除 L479 调用 + 删除 L50-54 函数定义。

**改动文件**: `src/prompt_builder.py`

---

## 问题七：场景高级选项 SQL 查询未走缓存 ✅ 已实施

### 现状

`_build_scene_advanced_options` (L167) 每次构建 coding/qa/sre 的 prompt 都执行 2 次 SQL:
1. `SELECT dev_scene FROM managed_repos WHERE repo_key=?`
2. `SELECT advanced_options FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=?`

### 风险

高频 webhook 场景下（多个 Issue 同时触发 coding agent），DB 锁争用。

### 实施

加内存缓存（TTL 60s），同一 repo_key 的 advanced_options 60 秒内不重复查 DB:

```python
_advanced_options_cache: dict = {}  # repo_key -> (options_str, timestamp)

def _build_scene_advanced_options(repo_key: str) -> str:
    import time as _time
    now = _time.time()
    cached = _advanced_options_cache.get(repo_key)
    if cached and now - cached[1] < 60:
        return cached[0]
    # ... 原 SQL 查询逻辑 ...
    result = "\n".join(lines) if len(lines) > 1 else ""
    _advanced_options_cache[repo_key] = (result, now)
    return result
```

**改动文件**: `src/prompt_builder.py`

---

## 实施总结

| 优先级 | 问题 | 状态 | 改动文件 | 影响 |
|--------|------|------|----------|------|
| P0 | 废弃别名仍在使用 | ✅ 已实施 | event_pipeline.py(3处) + prompt_builder.py | 别名删除不再导致流水线挂 |
| P0 | `_build_history_file_guide` 废弃调用 | ✅ 已实施 | prompt_builder.py | 消除阅读理解成本 |
| P1 | VOD 场景注入耦合 | ✅ 已实施 | prompt_builder.py | 项目知识异常不再影响 VOD 场景 |
| P1 | Chat prompt `Assistant:` 标记 | ✅ 已实施 | prompt_builder.py | 防止重构时误删触发 token |
| P2 | 行为约束散落在硬编码中 | ✅ 已实施 | prompt_builder.py | 结构化分离，可独立测试 |
| P2 | A2A 协议每次读文件 | ✅ 已实施 | prompt_builder.py | mtime 缓存减少文件 IO |
| P3 | 场景高级选项SQL无缓存 | ✅ 已实施 | prompt_builder.py | 60s TTL 缓存减少 DB 查询 |

### 验证结果

- py_compile: prompt_builder.py OK + event_pipeline.py OK
- pytest: 120 passed, 2 failed（均为 weekly-02 pre-existing 失败，与本次优化无关）
- 服务重启: health 200, 3 引擎 online
- 运行时验证: 所有新函数 import 成功，_build_output_contract() 返回 1404 字符，_load_agent_registry() 返回 5 个 agent

### 改动文件清单

| 文件 | 改动内容 |
|------|----------|
| `src/api/event_pipeline.py` | 3 处 `build_prompt` → `build_devops_agent_prompt`，恢复被 replace_all 误删的 evt 赋值行 |
| `src/prompt_builder.py` | 删除 `build_prompt` 别名 + 删除 `_build_history_file_guide` 废弃函数 + VOD 场景注入解耦（`_inject_vod_scenes` → `_build_vod_scenes_section`）+ 提取 `_build_output_contract()` + 新增 `_load_agent_registry()` mtime 缓存 + 新增 `_CHAT_PROMPT_SUFFIX` 常量 + `_build_scene_advanced_options` 加 60s TTL 缓存 |
