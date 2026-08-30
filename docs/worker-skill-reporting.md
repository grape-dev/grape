# Worker Engine 本地 Skill 详情披露需求文档

## 1. 背景与现状

### 1.1 问题

Worker 机器上各 engine（opencode/codearts/hermes）在本地物理存放了大量 skill 文件，分布在多个目录中。这些 skill 是 engine 自带的或用户在 worker 本地安装的，Web 端完全不可见，用户无法感知每个 engine 自带了哪些 skill。

### 1.2 需求

Worker 注册/心跳时，扫描各 engine 本地物理存在的 skill 列表，与 engine 绑定上报。Web 端每个 engine 可点击打开详情页，展示 engine 详细信息 + 自带的全部 skill 列表。

### 1.3 关键设计原则

1. **skill 与 engine 绑定**（每个 agent_runtimes 行），不是 worker 级
2. **不做 diff，全量展示** — Web 端已有的 skill 不会有人特地安装到本地，无需过滤
3. **engine 详情页** — 每个 engine 卡片可点击打开详情，展示 engine 信息 + skill 列表

### 1.4 Worker 上 skill 物理分布（实测）

| 目录 | engine 归属 | 说明 |
|------|------------|------|
| `~/.hermes/skills/` | hermes | Hermes 引擎自带 skills（~96 个，按 category 分目录） |
| `~/.config/opencode/skills/` | opencode | opencode 用户级 skills |
| `~/.codeartsdoer/cli-data/system/skills/` | codearts | codearts 内置系统 skills |
| `~/grape/skills/` | 共用（worker 安装包） | grape 官方 skills（安装包自带，~16 个） |
| `~/VOD-Developer-skills/skills/` | 用户自建 | 开发者自行创建的 skill（通过环境变量配置） |

不扫描：`grape-workspaces/*/.skills/`（task 执行时临时物化的，随 worktree 清理，不是持久 skill）。

## 2. 目标

Worker 注册和心跳时上报各 engine 本地 skill 列表（全量，绑定到 engine）。Web 端每个 engine 卡片可点击打开详情页，展示 engine 详细信息 + 自带的全部 skill。

## 3. 改动逻辑

### 3.1 Worker 端 — 本地 skill 扫描 — agent.py

#### 新增函数：`_scan_local_skills() -> list[dict]`

扫描各 engine 的 skill 目录，返回 per-engine 的 skill 列表。每个含 SKILL.md 的目录视为一个 skill。

```python
def _scan_local_skills() -> list[dict]:
    """扫描 worker 上各 engine 本地物理存在的 skill，返回 per-engine skill 列表。

    每个 engine 扫描其标准 skill 目录 + 共用目录。
    共用目录（~/grape/skills 等）的 skill 合并到每个 engine 下。
    """
    import os as _os

    home = _os.path.expanduser("~")

    # per-engine 专属 skill 目录
    engine_dirs = {
        "hermes": [_os.path.join(home, ".hermes", "skills")],
        "opencode": [_os.path.join(home, ".config", "opencode", "skills")],
        "codearts": [_os.path.join(home, ".codeartsdoer", "cli-data", "system", "skills")],
    }

    # 共用 skill 目录（所有 engine 都可访问）
    common_dirs = [_os.path.join(home, "grape", "skills")]
    extra = _os.environ.get("RUNTIME_EXTRA_SKILL_DIRS", "")
    if extra:
        for d in extra.split(":"):
            d = d.strip()
            if d and _os.path.isdir(d):
                common_dirs.append(d)

    def _scan_dir(base_dir):
        """扫描目录下含 SKILL.md 的子目录，返回 skill_name 列表。
        支持一级和二级目录结构。"""
        skills = []
        if not _os.path.isdir(base_dir):
            return skills
        for entry in _os.listdir(base_dir):
            entry_path = _os.path.join(base_dir, entry)
            if not _os.path.isdir(entry_path):
                continue
            if _os.path.isfile(_os.path.join(entry_path, "SKILL.md")):
                skills.append(entry)
            else:
                for sub in _os.listdir(entry_path):
                    sub_path = _os.path.join(entry_path, sub)
                    if _os.path.isdir(sub_path) and _os.path.isfile(_os.path.join(sub_path, "SKILL.md")):
                        skills.append(sub)
        return skills

    # 扫描共用目录
    common_skills = set()
    for d in common_dirs:
        common_skills.update(_scan_dir(d))

    # 扫描 per-engine 目录 + 合并共用
    result = []
    for engine, dirs in engine_dirs.items():
        engine_skills = set()
        for d in dirs:
            engine_skills.update(_scan_dir(d))
        engine_skills |= common_skills
        if engine_skills:
            result.append({"engine": engine, "skills": sorted(engine_skills)})

    return result
```

#### register 调用改动

```python
# main() 中
_local_skills = _scan_local_skills()
ids, probe_url = await client.register(installed, device_name=device, local_skills=_local_skills)
```

#### heartbeat 调用改动

```python
# _run_loop 中，每 4 次心跳（60s）附带一次 local_skills
if heartbeat_count % 4 == 0:
    _ls = _scan_local_skills()
    await client.heartbeat(runtime_ids, local_skills=_ls)
else:
    await client.heartbeat(runtime_ids)
```

#### _reregister 改动

```python
async def _reregister(client, installed, device_name) -> list[int] | None:
    await client.reset_session()
    _ls = _scan_local_skills()
    ids, _probe = await client.register(installed, device_name=device_name, local_skills=_ls)
    ...
```

### 3.2 Client 端 — payload 扩展 — client.py

#### `register()` 增加 `local_skills` 可选参数

```python
async def register(self, runtimes: list[dict], device_name: str = "",
                   cli_version: str = "", local_skills: list[dict] | None = None) -> tuple[list[int], str]:
    payload = {"device_name": device_name, "cli_version": cli_version, "runtimes": runtimes}
    if local_skills is not None:
        payload["local_skills"] = local_skills
    data = await self._request("POST", "/api/runtime/register", payload)
    ...
```

#### `heartbeat()` 增加 `local_skills` 可选参数

```python
async def heartbeat(self, runtime_ids: list[int], local_skills: list[dict] | None = None) -> bool:
    payload = {"runtime_ids": runtime_ids}
    if local_skills is not None:
        payload["local_skills"] = local_skills
    result = await self._request("POST", "/api/runtime/heartbeat", payload, retries=2)
    return result is not None
```

### 3.3 服务端 — DB schema — db_setup.py

#### `agent_runtimes` 表增加 `local_skills TEXT DEFAULT '[]'`

存储与该 engine 绑定的全量 skill 列表（JSON 数组）。

DDL 增加：
```sql
local_skills TEXT DEFAULT '[]'
```

迁移：
```python
_ensure_column(c, "agent_runtimes", "local_skills", "TEXT", "'[]'")
```

### 3.4 服务端 — register/heartbeat 接收 — runtime.py

#### `RuntimeRegBody` 增加字段

```python
class RuntimeRegBody(BaseModel):
    device_name: str = ""
    cli_version: str = ""
    runtimes: list[dict] = []
    local_skills: list[dict] = []   # 新增：per-engine 本地 skill 列表
```

#### `HeartbeatBody` 增加字段

```python
class HeartbeatBody(BaseModel):
    runtime_ids: list[int] = []
    local_skills: list[dict] = []   # 新增
```

#### `runtime_register()` 写入逻辑

worker 上报的 `local_skills` 格式：
```json
[
  {"engine": "hermes", "skills": ["ascii-art", "arxiv", "comfyui", ...]},
  {"engine": "opencode", "skills": ["officecli", "vod", "qa", ...]},
  {"engine": "codearts", "skills": ["bug-fix-verification", "codebase-structure", ...]}
]
```

`local_skills` 中 engine 字段是类型名（hermes/opencode/codearts），与 `agent_runtimes.name` 一致，直接按 name 匹配写入。

```python
# 构建 engine name → skills JSON 映射
skills_by_name = {}
for ls in body.local_skills or []:
    eng = (ls.get("engine") or "").strip().lower()
    if eng:
        skills_by_name[eng] = json.dumps(ls.get("skills") or [], ensure_ascii=False)

# 在 upsert 循环中，每个 rt 按 name 匹配
for rt in body.runtimes or []:
    name = (rt.get("name") or "").strip().lower()
    local_skills_json = skills_by_name.get(name, "[]")
    # INSERT 和 UPDATE 都加 local_skills
    c.execute("UPDATE agent_runtimes SET version=?, healthy=?, ..., local_skills=? WHERE id=?",
              (..., local_skills_json, rid))
    c.execute("INSERT INTO agent_runtimes (..., local_skills, ...) VALUES (..., ?, ...)",
              (..., local_skills_json, ...))
```

#### `runtime_heartbeat()` 写入逻辑

```python
if body.local_skills:
    skills_by_name = {}
    for ls in body.local_skills:
        eng = (ls.get("engine") or "").strip().lower()
        if eng:
            skills_by_name[eng] = json.dumps(ls.get("skills") or [], ensure_ascii=False)
    if skills_by_name:
        c.execute(f"SELECT id, name FROM agent_runtimes WHERE id IN ({ph}) AND credential_id=?",
                  body.runtime_ids + [credential_id])
        for rid, rname in c.fetchall():
            if rname.lower() in skills_by_name:
                c.execute("UPDATE agent_runtimes SET local_skills=? WHERE id=?",
                          (skills_by_name[rname.lower()], rid))
```

### 3.5 服务端 — API 返回 — settings.py

#### `get_agent_runtimes()` 增加 local_skills 字段

查询增加 `local_skills` 列，返回时直接透传（不做 diff，全量返回）：

```python
# devices 查询增加 local_skills 列
c.execute("SELECT id, name, version, healthy, installed, status, last_seen_at, local_skills FROM agent_runtimes WHERE credential_id=?", (cid,))

# runtimes 构建增加 local_skills
runtimes = [
    {"id": x[0], "name": x[1], "version": x[2], "healthy": bool(x[3]),
     "installed": bool(x[4]),
     "instance_name": instance_name(x[1], cred.get("device_name", ""), cid),
     "health": derive_runtime_health(x[5], x[6], now),
     "local_skills": json.loads(x[7] or "[]")}   # 全量返回
    for x in c.fetchall()
]
```

Engines 视图的 visible_runtimes 也同步增加 `local_skills` 字段（从 user_db.get_agent_runtimes 的返回中取，需确认其 SQL 选了该列并补选）。

本机直连引擎（cid=0）的 devices.append 也同步增加 `local_skills`。

### 3.6 前端 — engine 详情页 + Workers 页展示 — runtimes.js

#### Engines 视图：每个 engine 卡片可点击打开详情

`renderEngines()` 中每个 engine 卡片增加点击事件，打开详情弹窗：

```javascript
// engine 卡片整体可点击
html += `<div style="...;cursor:pointer" onclick="openEngineDetail(${rt.id})">...</div>`;
```

#### Workers 视图：承载引擎列表中每个 engine 可点击

`renderWorkers()` 中承载引擎列表的每个 engine 可点击：

```javascript
// 每个 engine 渲染为可点击链接
const clis = (d.runtimes || []).map(rt =>
    `<span style="cursor:pointer;color:#235ec7;text-decoration:underline" onclick="openEngineDetail(${rt.id})">${rt.name} v${rt.version || ''}${rt.healthy ? ' ✓' : ' ✗'}</span>`
).join(' · ');
```

#### 新增函数：`openEngineDetail(runtimeId)`

打开 engine 详情弹窗，展示 engine 详细信息 + skill 列表：

```javascript
function openEngineDetail(runtimeId) {
    // 从 __runtimeData 中查找对应 runtime
    const allRuntimes = (__runtimeData && __runtimeData.runtimes) || [];
    const rt = allRuntimes.find(r => r.id === runtimeId);
    if (!rt) return;

    const skills = rt.local_skills || [];
    const skillBadges = skills.length
        ? skills.map(s => `<span style="display:inline-block;font-size:12px;background:#e0e7ff;color:#3730a3;padding:3px 10px;border-radius:999px;margin:3px 4px 3px 0">${s}</span>`).join('')
        : '<span style="color:var(--t3);font-size:12px">无本地 skill</span>';

    const html = `<div id="engineDetailOverlay" style="position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center" onclick="closeEngineDetail(event)">
        <div style="background:var(--c);border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.2);max-width:640px;width:90%;max-height:80vh;overflow:auto;padding:24px" onclick="event.stopPropagation()">
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
                ${_healthBadge(rt.health)}
                <span style="font-weight:700;font-size:18px">${rt.instance_name || rt.name}</span>
                ${rt.is_local ? '<span style="font-size:11px;font-weight:600;color:#0e7490;background:#cffafe;border-radius:999px;padding:1px 8px">本机直连</span>' : ''}
                <button class="bt bt-o" style="margin-left:auto" onclick="closeEngineDetail()">关闭</button>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:13px;color:var(--t2);margin-bottom:20px">
                <div><span style="color:var(--t3);display:inline-block;min-width:70px">引擎类型</span> ${rt.name}</div>
                <div><span style="color:var(--t3);display:inline-block;min-width:70px">版本</span> v${rt.version || '—'}</div>
                <div><span style="color:var(--t3);display:inline-block;min-width:70px">健康状态</span> ${rt.healthy ? '✓ 健康' : '✗ 异常'}</div>
                <div><span style="color:var(--t3);display:inline-block;min-width:70px">Worker</span> ${rt.worker_name || (rt.is_local ? '本机' : '—')}</div>
                <div><span style="color:var(--t3);display:inline-block;min-width:70px">IP</span> ${rt.worker_ip || '—'}</div>
                <div><span style="color:var(--t3);display:inline-block;min-width:70px">Scope</span> ${_scopeLabel(rt.scope)}</div>
                <div><span style="color:var(--t3);display:inline-block;min-width:70px">Owner</span> ${rt.owner || '—'}</div>
                <div><span style="color:var(--t3);display:inline-block;min-width:70px">已安装</span> ${rt.installed ? '是' : '否'}</div>
            </div>
            <div style="border-top:1px solid var(--b);padding-top:16px">
                <div style="font-weight:600;font-size:14px;margin-bottom:10px">本地 Skill 列表 <span style="font-weight:400;color:var(--t3);font-size:12px">（${skills.length} 个）</span></div>
                <div style="display:flex;flex-wrap:wrap">${skillBadges}</div>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML('beforeend', html);
}

function closeEngineDetail(event) {
    if (event && event.target.id !== 'engineDetailOverlay') return;
    const el = document.getElementById('engineDetailOverlay');
    if (el) el.remove();
}
```

详情页内容：
- Engine 基本信息：类型、版本、健康状态、Worker、IP、Scope、Owner、是否已安装
- 本地 Skill 列表：全量展示，badge 样式，标注总数

## 4. 改动文件清单

| # | 文件 | 改动类型 | 改动内容 |
|---|------|---------|---------|
| 1 | src/runtime/agent.py | 新增函数 + 改调用 | 新增 `_scan_local_skills()`；`main()` register 传 local_skills；`_run_loop` heartbeat 每 60s 传 local_skills；`_reregister` 传 local_skills |
| 2 | src/runtime/client.py | 签名扩展 | `register()` 和 `heartbeat()` 增加 `local_skills` 可选参数 |
| 3 | src/api/routes/runtime.py | 新增字段 + 逻辑 | `RuntimeRegBody`/`HeartbeatBody` 增加 `local_skills`；register/heartbeat 按 engine name 匹配写入 DB |
| 4 | src/api/db_setup.py | DDL + 迁移 | `agent_runtimes` 增加 `local_skills TEXT DEFAULT '[]'` 列 |
| 5 | src/api/routes/settings.py | 查询扩展 | `get_agent_runtimes` 选 local_skills 列；runtimes 构建增加 `local_skills` 字段（全量透传） |
| 6 | src/api/static/js/pages/runtimes.js | 详情页 + 可点击 | Engines 视图 engine 卡片可点击打开详情；Workers 视图引擎列表可点击；新增 `openEngineDetail()` / `closeEngineDetail()` 函数 |

## 5. 改动后完整数据流

### 5.1 Skill 扫描流

```
Worker 启动 / 每 60s 心跳
  → _scan_local_skills()
      → 扫描 ~/.hermes/skills/（hermes 专属）
      → 扫描 ~/.config/opencode/skills/（opencode 专属）
      → 扫描 ~/.codeartsdoer/cli-data/system/skills/（codearts 专属）
      → 扫描 ~/grape/skills/（共用，合并到每个 engine）
      → 扫描 RUNTIME_EXTRA_SKILL_DIRS 配置的额外目录（共用）
      → 每个含 SKILL.md 的目录 = 一个 skill
      → 返回 [{engine:"hermes", skills:[...]}, {engine:"opencode", skills:[...]}, ...]
```

### 5.2 上报流

```
register / heartbeat
  → payload 携带 local_skills（per-engine 全量 skill 列表）
  → 服务端按 engine name 匹配 agent_runtimes 行
  → 写入 agent_runtimes.local_skills（JSON 数组，与 engine 绑定）
```

### 5.3 Web 展示流

```
用户打开 Workers / Engines 页
  → GET /settings/agent-runtimes
  → get_agent_runtimes()
      → 查 agent_runtimes 表（含 local_skills 列）
      → 返回 runtimes[].local_skills（全量，不做 diff）
  → 前端渲染
      → Engines 视图：每个 engine 卡片可点击
      → Workers 视图：承载引擎列表中每个 engine 可点击
      → 点击 → openEngineDetail(runtimeId)
      → 弹窗展示 engine 信息 + skill 列表（全量 badge）
```

### 5.4 完整示例

```
Worker vod-worker (127.0.0.1) 注册时上报：
  local_skills = [
    {engine: "hermes", skills: ["ascii-art","arxiv","comfyui",...,"vod","qa"]},     // ~96 个
    {engine: "opencode", skills: ["officecli","vod","qa","implementation",...]},     // 含共用
    {engine: "codearts", skills: ["bug-fix-verification","codebase-structure",...]}  // ~10 个
  ]

服务端写入 DB：
  agent_runtimes 行 (name=hermes) → local_skills = '["ascii-art","arxiv",...]'
  agent_runtimes 行 (name=opencode) → local_skills = '["officecli","vod",...]'
  agent_runtimes 行 (name=codearts) → local_skills = '["bug-fix-verification",...]'

Web Engines 页：
  [hermes-worker-ecs-grape-de-0002]  ● 在线  v...
    worker: vod-worker  |  owner: —  |  scope: 全局
    → 点击卡片打开详情弹窗：
        引擎类型: hermes    版本: v...
        健康状态: ✓ 健康     Worker: vod-worker
        IP: 127.0.0.1   Scope: 全局
        ────────────────────────
        本地 Skill 列表（96 个）
        ascii-art  arxiv  comfyui  ...  vod  qa
```

## 6. 测试用例设计

### 6.1 Worker 端单元测试 — test_scan_local_skills.py

| 用例 ID | 用例名 | 前置条件 | 操作 | 预期结果 |
|---------|--------|---------|------|---------|
| T001 | 扫描 hermes skills 目录 | `~/.hermes/skills/` 有 creative/ascii-art/SKILL.md 等 | 调用 `_scan_local_skills()` | result 含 `{engine:"hermes", skills:[...,"ascii-art",...]}` |
| T002 | 扫描 opencode skills 目录 | `~/.config/opencode/skills/officecli/SKILL.md` 存在 | 调用 `_scan_local_skills()` | result 含 `{engine:"opencode", skills:["officecli",...]}` |
| T003 | 扫描 codearts skills 目录 | `~/.codeartsdoer/cli-data/system/skills/` 有 10 个 SKILL.md | 调用 `_scan_local_skills()` | result 含 `{engine:"codearts", skills:[10 个]}` |
| T004 | 共用目录合并到所有 engine | `~/grape/skills/` 有 vod/qa 等 | 调用 `_scan_local_skills()` | 每个 engine 的 skills 都包含 vod/qa |
| T005 | 二级目录扫描 | `.hermes/skills/creative/ascii-art/SKILL.md`（category/skill 两层） | 调用 `_scan_local_skills()` | skills 含 "ascii-art"（取 skill 名） |
| T006 | 目录不存在时跳过 | `~/.config/opencode/skills/` 不存在 | 调用 `_scan_local_skills()` | result 不含 opencode 条目（如果也没有共用 skill）；不报错 |
| T007 | 无 SKILL.md 的目录不扫描 | 某目录下有子目录但无 SKILL.md | 调用 `_scan_local_skills()` | 该子目录不计入 skills |
| T008 | RUNTIME_EXTRA_SKILL_DIRS 环境变量 | 设 `RUNTIME_EXTRA_SKILL_DIRS=/root/VOD-Developer-skills/skills` | 调用 `_scan_local_skills()` | VOD-Developer-skills 下的 skill 出现在所有 engine 的 skills 中 |
| T009 | 跳过 grape-workspaces 临时目录 | `~/grape-workspaces/*/.skills/` 有物化 skill | 调用 `_scan_local_skills()` | 不扫描 grape-workspaces；result 不含这些临时 skill |
| T010 | skill 去重 | 同一 skill 名在多个目录出现 | 调用 `_scan_local_skills()` | skills 列表中该 skill 只出现一次 |

### 6.2 Client 端单元测试 — test_runtime_client_local_skills.py

| 用例 ID | 用例名 | 前置条件 | 操作 | 预期结果 |
|---------|--------|---------|------|---------|
| T020 | register 携带 local_skills | mock HTTP server | `await client.register(runtimes, device_name="dev", local_skills=[{"engine":"opencode","skills":["officecli"]}])` | 请求 body 含 `local_skills` 字段 |
| T021 | register 不带 local_skills（向后兼容） | mock HTTP server | `await client.register(runtimes, device_name="dev")` | 请求 body 不含 `local_skills` 字段 |
| T022 | heartbeat 携带 local_skills | mock HTTP server | `await client.heartbeat([1,2], local_skills=[...])` | 请求 body 含 `local_skills` 字段 |
| T023 | heartbeat 不带 local_skills（向后兼容） | mock HTTP server | `await client.heartbeat([1,2])` | 请求 body 不含 `local_skills` 字段 |

### 6.3 服务端 API 测试 — test_runtime_routes_local_skills.py

| 用例 ID | 用例名 | 前置条件 | 操作 | 预期结果 |
|---------|--------|---------|------|---------|
| T030 | register 写入 local_skills（按 engine 匹配） | 有效凭据，DB 已建表 | POST /api/runtime/register，body 含 `local_skills:[{engine:"hermes",skills:["ascii-art","vod"]}]`，runtimes 含 name=hermes | DB hermes 行 local_skills = `'["ascii-art","vod"]'` |
| T031 | register 多 engine 分别写入 | 同一 worker 注册 3 个 engine，local_skills 含 3 个 engine 条目 | POST /api/runtime/register | 每个 runtime 行 local_skills 对应正确 engine |
| T032 | register 不带 local_skills | 有效凭据 | POST /api/runtime/register，body 不含 local_skills | DB local_skills = `'[]'`；注册正常 |
| T033 | heartbeat 更新 local_skills | 已注册的 runtime | POST /api/runtime/heartbeat，含 local_skills | DB local_skills 被更新 |
| T034 | heartbeat 不带 local_skills | 同上 | POST /api/runtime/heartbeat，不含 local_skills | DB local_skills 不变 |
| T035 | GET 返回 local_skills（全量） | DB hermes 行 local_skills = `'["ascii-art","vod","qa"]'` | GET /settings/agent-runtimes | runtimes[].local_skills = `["ascii-art","vod","qa"]`（全量，不过滤） |
| T036 | GET 返回空 local_skills | DB local_skills = `'[]'` | GET /settings/agent-runtimes | runtimes[].local_skills = `[]` |

### 6.4 DB 迁移测试

| 用例 ID | 用例名 | 前置条件 | 操作 | 预期结果 |
|---------|--------|---------|------|---------|
| T040 | 全新库建表含 local_skills | 空 DB | 运行 db_setup | agent_runtimes 表含 local_skills 列 |
| T041 | 老库迁移加列 | agent_runtimes 无 local_skills 列 | 运行 db_setup | local_skills 列被添加；已有行默认 `'[]'` |

### 6.5 前端手动验证

| 用例 ID | 用例名 | 前置条件 | 操作 | 预期结果 |
|---------|--------|---------|------|---------|
| T050 | Engines 视图 engine 卡片可点击 | 有已注册 engine | 打开 Engines 页，点击 engine 卡片 | 弹出 engine 详情弹窗 |
| T051 | Workers 视图引擎可点击 | 有已注册 worker，有承载引擎 | 打开 Workers 页，点击承载引擎列表中的 engine 名 | 弹出 engine 详情弹窗 |
| T052 | 详情弹窗展示 engine 信息 | engine 有版本/健康/worker/IP 等信息 | 打开详情弹窗 | 展示引擎类型、版本、健康状态、Worker、IP、Scope、Owner、是否已安装 |
| T053 | 详情弹窗展示 skill 列表 | engine 的 local_skills 有 96 个 | 打开详情弹窗 | 展示"本地 Skill 列表（96 个）"，每个 skill 为 badge |
| T054 | 详情弹窗无 skill 时展示空态 | engine 的 local_skills 为空 | 打开详情弹窗 | 展示"本地 Skill 列表（0 个）" + "无本地 skill" 提示 |
| T055 | 关闭弹窗 | 弹窗已打开 | 点击关闭按钮 / 点击遮罩 | 弹窗关闭 |
| T056 | skill 列表刷新 | Worker 心跳上报了新 skill | 等 60s 后刷新页面，重新打开详情 | 新 skill 出现在列表中 |

### 6.6 端到端集成测试

| 用例 ID | 用例名 | 步骤 | 预期结果 |
|---------|--------|------|---------|
| T060 | 完整链路 | 1. Worker 注册（含 local_skills）<br>2. GET /settings/agent-runtimes<br>3. 检查 local_skills | local_skills = worker 上报的全量 skill 列表 |
| T061 | 心跳刷新 | 1. Worker 注册<br>2. 在 worker 上新建一个 skill 目录（含 SKILL.md）<br>3. 等 60s（下次心跳）<br>4. 刷新 Web | 新 skill 出现在 engine 详情中 |
| T062 | 多 Worker 各 engine 独立 | 1. Worker A 的 hermes 有 skill-x<br>2. Worker B 的 hermes 没有 skill-x<br>3. 分别打开 A 和 B 的 hermes 详情 | A 的 hermes 显示 skill-x，B 的 hermes 不显示 |
| T063 | 重注册后数据保留 | 1. Worker 已有 skill 数据<br>2. 触发重注册<br>3. 刷新 Web | local_skills 不丢失 |

## 7. 向后兼容性

| 场景 | 兼容方案 |
|------|---------|
| 老 Worker（不携带 local_skills） | 服务端 `local_skills` 默认空 list；DB 写 `'[]'`；详情弹窗显示"无本地 skill"；不影响注册/心跳 |
| 老服务端收到新 Worker | Pydantic BaseModel 默认忽略未知字段；local_skills 不影响注册 |
| DB 迁移 | `_ensure_column` 幂等加列，已有行默认 `'[]'` |
| 前端老版本 | `rt.local_skills` 为 undefined 时详情弹窗显示空列表 |
| skill 目录不存在 | `_scan_local_skills()` 跳过不存在的目录，返回对应 engine 无 skill |

## 8. 边界与风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| hermes skills 数量多（~96） | 详情弹窗 skill 列表较长 | badge 自动换行；弹窗内容区 max-height 80vh 可滚动 |
| 用户自建 skill 目录路径不固定 | 扫描不到 | 提供 `RUNTIME_EXTRA_SKILL_DIRS` 环境变量配置；默认扫描标准目录 |
| 二级目录扫描误判 | category 目录下无 SKILL.md 的空目录 | 只认含 SKILL.md 的目录为 skill |
| skill_name 大小写不一致 | 列表中有重复 | 扫描时保留原始名称；展示时不去重（不同目录可能有同名不同内容的 skill） |
| heartbeat 频率 | 每 60s 扫描一次增加 IO | find 操作在 ~120 个文件级别，耗时 < 10ms，可忽略 |
| 共用 skill 挂到每个 engine | 3 个 engine 都有 vod/qa 等 | 符合预期——这些共用 skill 每个 engine 确实都能访问 |
