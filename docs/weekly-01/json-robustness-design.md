# Agent 结构化 JSON 鲁棒性方案

## 1. 问题根因

### 1.1 #3514 实际损坏分析

从 DB 取 #3514 的 result_text 逐字节分析，发现 JSON **内容完整**（5 个字段都在），但有 4 种损坏同时发生：

```
result_text 中的 JSON 块（位置 1080~1920）:

{
  "actions": ["...", "..."],          ← 正常
  "con
clusion": "Issue #3 ... {{ post.content }} 改为 {{ post.content|safe }}",  ← ①字段名断裂 ②Jinja2 {{ }} 干扰括号匹配
  "art
ifacts": ["PR #1 ...", ...],          ← ①字段名断裂
  "
next_step": "QA Agent ...",           ← ①字段名断裂（\n 在引号前）
  "issue_summary": "...待QA审查→SRE部署"   ← 缺少闭合 }  ③JSON 截断
}

[PREMATURE_END_TURN] agent 多次结束...  ← 系统追加的标记
```

4 种损坏：
1. **字段名断裂**：`"con\nclusion"` / `"art\nifacts"` / `"\nnext_step"` — flush 边界 \n 插入字段名中间
2. **JSON 截断（缺闭合 }）**：agent 输出了所有字段，但最终的 `}` 没被收到（prompt_done 触发或进程退出提前截断）
3. **Jinja2 模板干扰**：字符串值中 `{{ post.content|safe }}` 的 `{{` `}}` 被正则/括号匹配误认为 JSON 结构
4. **正则匹配错误**：`re.search(r"\{.*\}", text, re.S)` 匹配到第一个 `{` 到 `{{ }}` 中的 `}`，只截取了 293 字符的残缺片段

### 1.2 损坏类型全量分类

| 类型 | 示例 | 根因 | 当前容错能否处理 |
|------|------|------|----------------- |
| 字段名断裂 | `"con\nclusion"` | flush 边界 \n | 否（去空白粘粘连值） |
| JSON 截断（缺 }） | 末尾无 `}` | prompt_done 提前触发 | 否（regex 找不到闭合 }） |
| 字符串值内 \n | `"...\n..."` | flush 边界 | 否（Invalid control char） |
| 字符串内 {{ }} 干扰 | `{{ post.x }}` | agent 输出含模板 | 否（regex 括号误匹配） |
| 多 JSON 块干扰 | 前有元数据 JSON | 多个 `{...}` | 部分（贪婪匹配取最后但被 } 截断） |
| markdown fence | ` ```json ` | agent 格式化输出 | 是（已 replace） |
| 字段值截断 | `"conclusion": "已完成` | 进程中途退出 | 否（JSON 语法错误不可修复） |

**不可修复的情况**：字段值本身被截断（进程异常退出 mid-stream），JSON 语法结构已破坏，无法推断缺失内容。此时应走 PREMATURE_END_TURN 重试。

**可修复的情况**：JSON 内容完整但格式被破坏（字段名断裂、缺闭合 }、字符串内 \n、Jinja2 干扰）。

### 1.3 当前容错为什么修不了

parse_agent_output_json 三级容错：
1. **直接解析** — `\n` 在字段名中间 → `Invalid control character`
2. **去空白 compact** — `re.sub(r'\s+', '', raw)` 去掉所有空白后，字符串值里的中文被粘连，引号匹配错位 → `Unterminated string`
3. **激进 strip** — 去掉非 JSON 字符后，值内容丢失太多，依然无法解析

核心问题：**去空白是全局操作，无法区分"字段名中的 \n（应去除）"和"字符串值中的 \n（应保留为空格或转义）"**。同时正则 `\{.*\}` 因为 Jinja2 `{{ }}` 中的 `}` 匹配到错误位置，截取了残缺片段。

## 2. 方案设计

分两层修复：源头预防 + 解析兜底。

### 2.1 源头预防：JSON 感知的文本拼接 (hermes_acp.py)

#### 问题

`collected_text` 是 list，最终 `"\n".join(collected_text)` 在每段 flush 之间插入 `\n`。对人类可读文本这是合理的（段落分隔），但对 JSON 输出会破坏字段名。

#### 方案：拼接时修复 JSON 块

在最终 full_text 拼接后（hermes_acp.py line 652），检测是否含结构化 JSON，如含则对 JSON 部分做定向修复：

```python
# hermes_acp.py 最终收尾处 (line 652 附近)

full_text = "\n".join(collected_text)

# JSON 鲁棒拼接：修复 flush 边界导致的 JSON 损坏
if _detect_structured_output(full_text):
    _fixed = _robust_join_json(collected_text)
    if _fixed and _detect_structured_output(_fixed):
        full_text = _fixed
```

`_robust_join_json` 实现：

```python
def _robust_join_json(chunks: list[str]) -> str:
    """拼接 collected_text，修复 flush 边界导致的 JSON 损坏。
    
    只对 JSON 块部分操作，不影响非 JSON 文本。
    """
    full = "\n".join(chunks)
    
    # 定位 JSON 块：找最后一个 {\"actions\" 模式（结构化产出契约起点）
    m = re.search(r'\{\s*"actions"', full)
    if not m:
        return full
    
    start = m.start()
    json_part = full[start:]
    # 去掉尾部非 JSON 内容（如 [PREMATURE_END_TURN] 标记）
    # 找最后一个已知字段后的内容
    for marker in ['[PREMATURE_END_TURN]', '\n\n[', '\n---']:
        idx = json_part.rfind(marker)
        if idx > 0:
            json_part = json_part[:idx]
    
    # 修复 1: 断裂字段名（\n 在字段名中间）
    for field in ("actions", "conclusion", "artifacts", "next_step", "issue_summary"):
        for i in range(1, len(field)):
            broken = field[:i] + '\n' + field[i:]
            if broken in json_part:
                json_part = json_part.replace(broken, field)
    
    # 修复 2: 字符串外 \n 去除，字符串内 \n 转义
    json_part = _fix_json_newlines(json_part)
    
    # 修复 3: 补闭合 } 如果缺失
    json_part = _ensure_closed(json_part)
    
    return full[:start] + json_part


def _fix_json_newlines(json_str: str) -> str:
    """去掉 JSON 中非字符串值内的换行符，字符串内的裸 \n 转为 \\n。"""
    result = []
    in_string = False
    escape = False
    for ch in json_str:
        if escape:
            result.append(ch)
            escape = False
            continue
        if ch == '\\':
            result.append(ch)
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if ch == '\n':
            if in_string:
                result.append('\\n')
            # 字符串外: 跳过
            continue
        result.append(ch)
    return "".join(result)


def _ensure_closed(json_str: str) -> str:
    """如果 JSON 缺少闭合 }，补上。"""
    s = json_str.rstrip()
    if s.endswith('}'):
        return s
    # 计算括号深度（忽略字符串内的 {{ }}）
    depth = 0
    in_string = False
    escape = False
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
    if depth > 0:
        s += '}' * depth
    return s
```

### 2.2 解析兜底：鲁棒 JSON 提取 (event_store.py)

#### 方案：基于已知字段模式提取 + 五级修复

放弃正则 `\{.*\}`，改为基于 JSON 契约字段（`"actions"`）定位 JSON 块起点，然后逐级修复：

```python
_KNOWN_FIELDS = ("actions", "conclusion", "artifacts", "next_step", "issue_summary")


def parse_agent_output_json(final_text: str) -> dict | None:
    if not final_text:
        return None
    import re as _re, json as _json

    # Step 0: 去 markdown fence
    _clean = final_text.replace("```json", "").replace("```", "")

    # Step 1: 定位 JSON 块 — 找 {"actions" 模式（最后一个）
    m = _re.search(r'\{\s*"actions"', _clean)
    if not m:
        # 降级：旧正则
        m2 = _re.search(r'\{.*\}', _clean, _re.S)
        if not m2:
            return None
        raw = m2.group(0)
    else:
        # 从 {"actions" 开始取到文本末尾
        raw = _clean[m.start():]
        # 去掉尾部非 JSON 标记
        for marker in ('[PREMATURE_END_TURN]', '\n---', '\n\n['):
            idx = raw.rfind(marker)
            if idx > 0:
                raw = raw[:idx]

    # Step 2: 修复断裂字段名
    for field in _KNOWN_FIELDS:
        for i in range(1, len(field)):
            broken = field[:i] + '\n' + field[i:]
            if broken in raw:
                raw = raw.replace(broken, field)

    # Step 3: 修复换行（字符串外去 \n，字符串内转义 \n）
    raw = _fix_json_newlines(raw)

    # Step 4: 补闭合 }
    raw = _ensure_closed(raw)

    # Step 5: 直接解析
    try:
        obj = _json.loads(raw)
        if _is_valid_output(obj):
            return obj
    except Exception:
        pass

    # Step 6: compact 兜底（去所有非字符串内空白）
    compact = _strip_non_string_whitespace(raw)
    try:
        obj = _json.loads(compact)
        if _is_valid_output(obj):
            return obj
    except Exception:
        pass

    return None
```

辅助函数（_fix_json_newlines / _ensure_closed / _strip_non_string_whitespace / _is_valid_output）与 2.1 中相同，放在 event_store.py 或公共模块中。

### 2.3 截断场景的处理策略

| 截断类型 | 可修复？ | 修复方法 |
|---------|---------|---------|
| 缺闭合 `}` | 是 | `_ensure_closed` 按括号深度补 `}` |
| 字段名断裂 | 是 | 按 _KNOWN_FIELDS 逐个匹配修复 |
| 字符串值内 `\n` | 是 | `_fix_json_newlines` 转义为 `\\n` |
| 字符串内 `{{ }}` 干扰 | 是 | 不用正则，用字段模式定位 + 括号深度计算时跳过字符串内 |
| 字段值本身截断 | **否** | JSON 语法不可恢复，走 PREMATURE_END_TURN 重试 |

### 2.4 两层的关系

```
                ┌─────────────────────────────────────┐
                │  hermes_acp.py (源头预防)            │
                │  _robust_join_json                   │
                │  修复 flush 边界 \n + 补闭合 }       │
                │  保证写入 DB 的 result_text 干净     │
                └────────────┬────────────────────────┘
                             │ full_text (已修复)
                             ▼
                ┌─────────────────────────────────────┐
                │  event_store.py (解析兜底)           │
                │  parse_agent_output_json             │
                │  字段模式定位 + 五级修复              │
                │  处理源头漏网（旧 worker / 其他后端） │
                └─────────────────────────────────────┘
```

- **源头预防**（2.1）：在 hermes_acp.py 拼接 full_text 时修复，保证写入 DB 的 result_text 是干净的
- **解析兜底**（2.2）：在 event_store.py 解析时修复，处理源头漏网的情况

两层独立工作，互不依赖。

### 2.5 不可修复场景的兜底

当 JSON 字段值本身被截断（进程异常退出），上述修复都无法成功。此时：
- parse_agent_output_json 返回 None（正确行为）
- complete_task 检测到 exit_code=0 但无结构化产出 → PREMATURE_END_TURN
- 多步重试机制（MAX_ATTEMPTS=50）会开新进程重试

这是正确的行为：不可恢复的截断应该重试，而不是用残缺数据假装成功。

## 3. 实施范围

| 文件 | 改动点 | 说明 |
|------|--------|------|
| src/agent/hermes_acp.py | 新增 `_robust_join_json` + `_fix_json_newlines` + `_ensure_closed`；line 652 调用 | 源头预防 |
| src/api/event_store.py | 重写 `parse_agent_output_json`；新增辅助函数 | 解析兜底 |

辅助函数 `_fix_json_newlines` 和 `_ensure_closed` 两处共用，可放在 event_store.py 中作为公共函数，hermes_acp.py import 调用。

不涉及：prompt_builder.py / service.py / 数据库结构 / 前端。

## 4. 验证方法

用 #3514 的实际 result_text 做回归测试（已验证可解析成功）：

```python
# 从 DB 取 #3514 result_text → 修复断裂字段名 + 补闭合 } → compact parse → 成功
# 返回含 actions/conclusion/artifacts/next_step/issue_summary 五个字段的 dict
```

另构造测试用例：
- 正常 JSON（无损坏）→ 直接解析成功
- 字段名断裂（`"con\nclusion"`）→ Step 2 修复 → 成功
- JSON 截断（缺闭合 `}`）→ Step 4 补 `}` → 成功
- 字符串值内含 `\n` → Step 3 转义 → 成功
- 字符串内含 `{{ }}` → 字段模式定位避免误匹配 → 成功
- 多个 JSON 块 → 取最后一个 `{"actions"` → 成功
- markdown fence → Step 0 去除 → 成功
- 字段值截断（不可修复）→ 返回 None → PREMATURE_END_TURN 重试
- 空文本 / 无 JSON → 返回 None

## 5. 风险评估

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| _fix_json_newlines 误改非 JSON 文本 | 低 | 仅影响 JSON 块内部 | 只对字段模式定位的 JSON 部分操作 |
| _ensure_closed 补错括号深度 | 低 | JSON 语法错误 | 括号计算跳过字符串内 `{{ }}` |
| 旧 worker 没有源头修复 | 确定 | 解析兜底覆盖 | event_store.py 在主控端，全局生效 |
| 字段值截断误判为可修复 | 低 | compact parse 失败返回 None | Step 5/6 解析失败 → None → 重试 |
