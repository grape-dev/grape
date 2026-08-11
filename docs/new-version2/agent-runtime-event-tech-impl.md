# Agent Runtime + Event 档案 技术实现文档

> 版本：v1.0
> 日期：2026-08-05
> 刷新：2026-08-08（基于 main@13709ee 代码结构校准）
> 状态：待评审
> 前置：`agent-runtime-event-design.md`（设计稿）与 `agent-runtime-event-full-design.md`（完整设计）
> 说明：本文为技术实现细节，含关键代码结构、协议报文、迁移 SQL、测试策略。按里程碑 M0→M4 组织。

---

## 〇.1 现状基线核对（2026-08-08，基于 main 代码实测）

> 详见 `agent-runtime-design.md` §0。本文档涉及的关键实现基线：
> - **前端目录**：所有 `static/` 路径实际为 **`src/api/static/`**（`index.html` / `js/app.js` / `js/pages/*.js`）。
> - **VCS 适配层**：`VcsBackend` / `GitCodeBackend` / `GitHubBackend`（`src/vcs/`），**不迁移**；`GitLabBackend` 无实现文件。
> - **`stream_agent`**（`executor.py:106`）现 yield 字符串事件：`step_start/text/reasoning/tool_use/step_finish/error/other/raw/done`，其中 `tool_use` 为 `"Tool: {tool} {input}"` 格式化字符串（M1 改造点）。
> - **调用方**：`event_pipeline._execute_agent`（`event_pipeline.py:323`）、`chat.py`（`send` `:125/:133`，SSE `send/stream` `:182`）、`webhook.py` `POST /api/a2a`（`:68`）。
> - **`events` 表**含 `backend` TEXT（`db_setup.py:20`）；**`chat_agent_session`** 含 `backend` TEXT + `backend_id` INTEGER（`session_manager.py:79/94`）。
> - **`requirements.txt` 无 `requests`**：§3.1 `RuntimeClient` 若用 `requests.post` 需先 `pip install requests` 并加入 requirements.txt，或改用已依赖的 `aiohttp`（推荐，避免新依赖）。
> - **Python 3.14.3**：SQLite ≥3.25，`RENAME COLUMN` 可直接用。

---

## 〇.2 技术栈与约束

| 项 | 说明 |
|----|------|
| 语言/框架 | Python 3.14.3 + FastAPI + SQLite + subprocess |
| 存储 | SQLite（`data/sessions.db`，`DEVOPS_DB_PATH` 可覆盖供测试隔离）；event.json 落 `data/workspaces/` 文件系统 |
| Agent 执行 | 复用 `stream_agent` 的 NDJSON 流式逻辑（改名 `stream_runtime`，保留 `stream_agent` 兼容别名） |
| 传输 | 纯 HTTP（RuntimeAgent 仅外连，无入站端口） |
| 并发 | 服务端 FastAPI 异步；任务队列 SQLite + 每 repo 一把锁 |
| 编码 | 所有文件读写 `encoding='utf-8'`，严禁 PowerShell 写文件（见部署文档教训） |
| HTTP 客户端 | ⚠️ `requirements.txt` 现**无 `requests`**：`RuntimeClient` 用 `requests` 需先加依赖，或用已依赖的 `aiohttp` |

---

## M0：术语重构（backend → runtime）

### 0.1 模块迁移

```
src/agent/            →   src/runtime/
  base.py             →   base.py（AgentBackend → AgentRuntime）
  executor.py         →   registry.py + executor.py
  opencode.py         →   opencode.py
  codearts.py         →   codearts.py
```

### 0.2 关键代码：兼容别名

```python
# src/runtime/executor.py
def stream_agent(*args, **kwargs):
    """兼容别名：调用方渐进迁移到 stream_runtime"""
    yield from stream_runtime(*args, **kwargs)
```

### 0.3 DB 迁移（`db_setup.py` 幂等区新增）

```python
# 1) backend_agents → agent_runtimes
if "backend_agents" in tables and "agent_runtimes" not in tables:
    c.execute("ALTER TABLE backend_agents RENAME TO agent_runtimes")
    _ensure_column(c, "agent_runtimes", "credential_id", "INTEGER")
    _ensure_column(c, "agent_runtimes", "provider", "TEXT DEFAULT ''")
    _ensure_column(c, "agent_runtimes", "status", "TEXT DEFAULT 'offline'")
    _ensure_column(c, "agent_runtimes", "last_seen_at", "TEXT DEFAULT NULL")
elif "backend_agents" in tables and "agent_runtimes" in tables:
    c.execute("DROP TABLE backend_agents")

# 2) 引用列改名（SQLite 3.25+ RENAME COLUMN）
for tbl, cols in {
    "devops_agent_session": [("backend_id", "runtime_id"), ("backend_session_id", "runtime_session_id")],
    "agent_type": [("default_backend_id", "default_runtime_id")],
    "chat_agent_session": [("backend_id", "runtime_id"), ("backend", "runtime")],  # backend TEXT 一并更名
    "chat_conversations": [("backend_session_id", "runtime_session_id")],
    "vod_conversations": [("backend_session_id", "runtime_session_id")],
    "events": [("backend", "runtime")],  # events.backend TEXT（db_setup.py:20）
}.items():
    for old, new in cols:
        _rename_column_if_exists(c, tbl, old, new)
```

> 一次性迁移（不做双写）；`_rename_column_if_exists` 需先 `PRAGMA table_info` 检查旧列存在。

### 0.4 验收

`python -m pytest tests/ -v` 全绿（`test_github_normalize.py` / `ui_*` 中 VCS 相关不改）。注意 `tests/test_api_routes.py::TestSettings::test_backend_agents` 断言响应含 `"backends"` 键，改名后需同步为 `"runtimes"`。

---

## M1：控制平面 + 事件档案模块

### 1.1 新表建表 SQL（`db_setup.py`）

`runtime_credentials` / `agent_runtimes` / `agent_tasks` 见完整设计 §四。建表放 `_create_tables()`，迁移与建表分离。

> ⚠️ `agent_tasks` 需在完整设计基础上追加两列（服务端资源向远端交付，见 `skill-delivery-remote.md`）：
> ```sql
> ALTER TABLE agent_tasks ADD COLUMN skill_files TEXT DEFAULT '{}';  -- {skill_name: {rel_path: content}}
> ALTER TABLE agent_tasks ADD COLUMN server_url  TEXT DEFAULT '';     -- A2A 外呼地址，worker 替换 {{SERVER_URL}}
> ```
> 技能目录打包、占位符替换、凭据随任务均在 enqueue（`service.enqueue_task`）侧完成，worker 端仅落地 + 替换 + 执行（详见 §2.4 追加实现节）。

### 1.2 `src/api/event_archive.py`（事件档案读写）

```python
"""event.json 事件档案读写模块"""
import json, os, threading, tempfile
from src.config import WORKSPACE_DIR, log

_repo_locks: dict[str, threading.Lock] = {}
_lock_guard = threading.Lock()

def _lock_for(repo_key: str) -> threading.Lock:
    with _lock_guard:
        if repo_key not in _repo_locks:
            _repo_locks[repo_key] = threading.Lock()
        return _repo_locks[repo_key]

def get_event_json_path(repo_key: str, issue_num) -> str:
    return os.path.join(WORKSPACE_DIR, repo_key, "issues", str(issue_num), "event.json")

def _atomic_write(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _load(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _redact(text, tokens):
    for t in tokens or []:
        if t: text = text.replace(t, "[REDACTED]")
    return text

def init_event_json(repo_key, seq, evt, agent_type, runtime="", skill="", tokens=None):
    """触发时建/追加 pipeline 段。runtime 参数 = 执行该段的 runtime 名（原 backend 概念，M0 后统一术语）"""
    issue_num = _extract_num(evt.get("issue_id", ""))
    if not issue_num:
        return  # #0 无关联 Issue 不建档案
    path = get_event_json_path(repo_key, issue_num)
    with _lock_for(repo_key):
        data = _load(path) or {
            "issue": {"number": issue_num, "title": evt.get("title", ""),
                      "status": "open", "created_at": evt.get("created_at", "")},
            "status": "running", "created_at": _now(), "updated_at": _now(),
            "pipeline": [],
            "summary": f"Issue #{issue_num} 处理中，尚无结论",
        }
        seg = {
            "seq": seq, "agent": agent_type, "event_type": evt.get("type", ""),
            "runtime": runtime or "", "skill": skill or "",
            "status": "running", "exit_code": None, "duration": None,
            "trigger": {"actor": evt.get("actor", ""), "branch": evt.get("branch", ""),
                        "raw_type": evt.get("_raw_type", "")},
            "input": {"issue_id": f"#{issue_num}", "summary": evt.get("summary", "")},
            "output": {"actions": [], "conclusion": "", "artifacts": [], "next_step": "", "issue_summary": ""},
            "a2a_to": None, "logs": [], "started_at": _now(), "completed_at": "",
        }
        data["pipeline"].append(seg)
        data["updated_at"] = _now()
        _atomic_write(path, data)
    log.info(f"Archive init: repo={repo_key}, issue=#{issue_num}, seq={seq}")

def append_event_archive_log(repo_key, seq, evt_type, data, tokens=None):
    """执行中增量写 logs（全量、脱敏）"""
    issue_num, path = _locate_by_seq(repo_key, seq)
    if not issue_num or not path: return
    with _lock_for(repo_key):
        arch = _load(path)
        if not arch: return
        for seg in arch["pipeline"]:
            if seg["seq"] == seq:
                entry = {"time": _now(), "type": evt_type,
                         "msg": _redact(_trunc_safe(data), tokens)}
                seg["logs"].append(entry)
        arch["updated_at"] = _now()
        _atomic_write(path, arch)

def finalize_event_archive(repo_key, seq, status, exit_code=None, duration=None,
                           output=None, tokens=None):
    """完成时写 output + 整链 status + 覆盖 summary"""
    issue_num, path = _locate_by_seq(repo_key, seq)
    if not issue_num or not path: return
    with _lock_for(repo_key):
        arch = _load(path)
        if not arch: return
        for seg in arch["pipeline"]:
            if seg["seq"] == seq:
                seg["status"] = status
                seg["exit_code"] = exit_code
                seg["duration"] = duration
                seg["completed_at"] = _now()
                if output:
                    seg["output"] = {
                        "actions": output.get("actions", []),
                        "conclusion": output.get("conclusion", ""),
                        "artifacts": output.get("artifacts", []),
                        "next_step": output.get("next_step", ""),
                        "issue_summary": output.get("issue_summary", ""),
                    }
                    if output.get("issue_summary"):
                        arch["summary"] = output["issue_summary"]   # 有则覆盖
        arch["status"] = "completed" if status == "completed" else "failed"
        arch["updated_at"] = _now()
        _atomic_write(path, arch)

def load_event_json(repo_key, issue_num) -> dict | None:
    return _load(get_event_json_path(repo_key, issue_num))

def build_issue_context(repo_key, issue_num, max_chars=4000) -> str:
    """生成富上下文文本（供 build_prompt 注入）"""
    arch = load_event_json(repo_key, issue_num)
    if not arch: return ""
    lines = [f"## Issue #{issue_num} 档案摘要"]
    if arch.get("summary"):
        lines.append(f"Summary: {arch['summary']}")
    for seg in arch.get("pipeline", []):
        agent = seg.get("agent", "?")
        st = seg.get("status", "")
        icon = {"completed": "✓", "running": "▶", "failed": "✗", "pending": "○"}.get(st, "?")
        concl = seg.get("output", {}).get("conclusion", "")
        arts = ", ".join(seg.get("output", {}).get("artifacts", []))
        nxt = seg.get("output", {}).get("next_step", "")
        lines.append(f"- [{icon}] {agent}: {concl}{(' | 产出: ' + arts) if arts else ''}{(' | 下一步: ' + nxt) if nxt else ''}")
    return "\n".join(lines)[:max_chars]
```

辅助函数 `_extract_num` / `_now` / `_locate_by_seq` / `_trunc_safe` 见实现（`_locate_by_seq` 需从 agent_tasks.event_seq 或事件表反查 issue_num）。

### 1.3 `src/runtime/service.py`（服务端 RuntimeService 核心）

```python
class RuntimeService:
    def register(self, credential_id, payload) -> list[int]: ...
    def heartbeat(self, credential_id, runtime_ids) -> None: ...
    def claim(self, runtime_id) -> dict | None:
        # 确定路由 + 串行槽（D3/D9）
        with db_lock:
            c = db.cursor()
            c.execute("""SELECT * FROM agent_tasks
                         WHERE runtime_id=? AND status='queued'
                           AND NOT EXISTS (
                             SELECT 1 FROM agent_tasks a2
                             WHERE a2.agent_session_id=agent_tasks.agent_session_id
                               AND a2.status IN ('dispatched','running'))
                         ORDER BY created_at ASC LIMIT 1""", (runtime_id,))
            row = c.fetchone()
            if not row: return None
            c.execute("UPDATE agent_tasks SET status='dispatched' WHERE id=?", (row[0],))
            db.commit()
            return task_dict(row)
    def report_messages(self, task_id, messages) -> None:
        # 定位 event_seq → append_event_archive_log
        task = get_task(task_id)
        if task and task.get("event_seq"):
            for m in messages:
                append_event_archive_log(task["repo_key"], task["event_seq"], m.get("type"), m)
    def complete(self, task_id, result_text, exit_code, usage) -> None:
        # 解析结构化 output → finalize_event_archive + update_event_complete
        output = parse_agent_output_json(result_text)
        task = get_task(task_id)
        if task and task.get("event_seq"):
            finalize_event_archive(task["repo_key"], task["event_seq"],
                                   "completed" if exit_code == 0 else "failed",
                                   exit_code, duration, output)
            update_event_complete(task["event_seq"], ..., result=_output_conclusion(output, result_text))
        update_task_status(task_id, "completed", ...)
```

### 1.4 `src/runtime/agent.py`（RuntimeAgent 边端循环）

```python
def main():
    token = os.environ["RUNTIME_TOKEN"]
    server = os.environ.get("RUNTIME_SERVER_URL", "http://localhost:3000")
    poll = float(os.environ.get("RUNTIME_POLL_INTERVAL", "3"))
    heartbeat = float(os.environ.get("RUNTIME_HEARTBEAT_INTERVAL", "15"))
    max_concurrent = int(os.environ.get("RUNTIME_MAX_CONCURRENT_TASKS", "20"))

    client = RuntimeClient(server, token)
    runtimes = client.register(discover_runtimes())
    for rid in runtimes:
        client.recover_orphans(rid)

    while True:
        for rid in runtimes:
            if active(rid) >= max_concurrent: continue
            task = client.claim(rid)
            if task:
                threading.Thread(target=_run_task, args=(client, task), daemon=True).start()
        if time since last heartbeat >= heartbeat:
            client.heartbeat(runtimes)
        time.sleep(poll + jitter())
```

`_run_task`：`stream_runtime(prompt, cwd, session_id=task.runtime_session_id, ...)` → 逐事件 `client.report_messages(task_id, batch)` → 终态 `client.complete/fail`（指数退避重试）。

### 1.5 结构化产出解析（`src/api/event_store.py`）

```python
def parse_agent_output_json(final_text: str) -> dict | None:
    """从 final_text 提取末尾 JSON 块"""
    import re, json as _json
    m = re.search(r"\{.*\}", final_text or "", re.S)
    if not m: return None
    try:
        obj = _json.loads(m.group(0))
        if isinstance(obj, dict) and any(k in obj for k in ("actions","conclusion","artifacts","next_step","issue_summary")):
            return obj
    except Exception:
        return None
    return None

def evaluate_agent_output(agent_type, tool_uses, final_text):
    # 优先 JSON 块
    out = parse_agent_output_json(final_text)
    if out:
        conclusion = out.get("conclusion", "")
        if conclusion:
            return True, conclusion
    # 降级：现有正则逻辑
    ...
```

---

## M2：回报协议驱动 event.json

### 2.1 FastAPI 路由（`src/runtime/service.py` 或独立 `src/api/routes/runtime.py`）

```python
router = APIRouter(prefix="/api/runtime", tags=["runtime"])

def _auth(request: Request) -> int:
    """rta_ 凭据校验中间件 → 返回 credential_id"""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token.startswith("rta_"):
        raise HTTPException(401, "invalid runtime credential")
    h = hashlib.sha256(token.encode()).hexdigest()
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM runtime_credentials WHERE token_hash=? AND status='active'", (h,))
        row = c.fetchone()
    if not row:
        raise HTTPException(401, "runtime credential revoked or not found")
    return row[0]

@router.post("/register")
async def register(request: Request, body: RegisterBody,
                   credential_id: int = Depends(_auth)):
    return service.register(credential_id, body.model_dump())

@router.post("/heartbeat")
async def heartbeat(request: Request, body: HeartbeatBody,
                    credential_id: int = Depends(_auth)): ...

@router.post("/runtimes/{rid}/tasks/claim")
async def claim(rid: int, credential_id: int = Depends(_auth)) -> dict: ...

@router.post("/tasks/{tid}/start")     → 标记 running
@router.post("/tasks/{tid}/progress")  → 阶段进度
@router.post("/tasks/{tid}/messages")  → append_event_archive_log
@router.post("/tasks/{tid}/usage")     → usage_json
@router.post("/tasks/{tid}/complete")  → finalize + update_event_complete（幂等）
@router.post("/tasks/{tid}/fail")      → finalize（幂等）
@router.get("/tasks/{tid}/status")     → 状态（前端轮询）
@router.post("/runtimes/{rid}/recover-orphans") → 清理 dispatched/running 遗留
```

### 2.2 幂等终态

```python
def update_task_status(task_id, status, **kw):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
        if row and row[0] in ("completed", "failed", "cancelled"):
            return  # 已终态，重放安全
        c.execute("UPDATE agent_tasks SET status=?, completed_at=? WHERE id=?",
                  (status, datetime.utcnow().isoformat(), task_id))
        db.commit()
```

### 2.3 event_pipeline 切到 enqueue_task

```python
# run_pipeline 末尾（替代 threading.Thread(_execute_agent)，原 _execute_agent 在 event_pipeline.py:323）
seq = append_event(...)                 # 1
init_event_json(repo_key, seq, evt, agent_type, runtime_name, skill_name)  # 2  # runtime_name=devops_agent_session.backend_id 对应 name
from src.runtime.service import enqueue_task
enqueue_task(
    agent_session_id=session_id,
    runtime_id=runtime_id,              # devops_agent_session.runtime_id
    event_seq=seq, issue_id=issue_id or "#0",
    repo_key=repo_key, agent_type=agent_type, skill=skill_name,
    prompt=build_prompt(evt, [], "", [skill_name], skill_path=skill_path),
    cwd=os.path.join(WORKSPACE_DIR, repository, f"{agent_type}-agent"),
    runtime_session_id=oc_session_id,
    event_type=event_type, account=account, env_extra=env_extra,
)
```

### 2.4 服务端资源向远端交付（技能/凭据/A2A 地址）

> 完整设计见 `skill-delivery-remote.md`。核心：prompt 中一切 server-local 引用改为占位符，由 worker 落地替换。

**enqueue 侧打包（`service.enqueue_task`）**

```python
def enqueue_task(..., skill_name="", skill_files=None, server_url="", **kw):
    # 1) 技能目录打包（official/user/repo 三 scope 统一）
    if not skill_files:
        skill_files = {}
        path = get_skill_path(resolve_skill_id(skill_name))   # user/db.py
        if path and os.path.exists(path):
            skill_files[skill_name] = _pack_skill_dir(os.path.dirname(path))  # SKILL.md + references/**
    # 2) 凭据随任务（不再依赖 worker 本地 DB）
    env_extra = dict(kw.get("env_extra") or {})
    for k, v in _collect_server_env(kw.get("user_id", 0), kw.get("account", "")).items():
        env_extra.setdefault(k, v)      # get_credentials / 账户 AK-SK
    # 3) A2A 外呼地址
    server_url = server_url or get_meta_value("webhook_url", "") or "http://localhost:8080"
    return _insert_task(..., skill_files=json.dumps(skill_files), server_url=server_url,
                        env_extra=json.dumps(env_extra))
```

**worker 落地 + 占位符替换（`src/runtime/agent.py` `_run_task` 前置）**

```python
for skill_name, files in json.loads(task.skill_files or "{}").items():
    dest = os.path.join(cwd, ".skills", skill_name)
    _materialize(files, dest)                     # 缓存命中则跳过（content_hash）
    env["DEVOPS_SKILLS_DIR"] = os.path.join(cwd, ".skills")
prompt = task.prompt.replace("{{SKILL_DIR}}", os.path.join(cwd, ".skills")) \
                    .replace("{{SERVER_URL}}", task.server_url)
env.update(json.loads(task.env_extra or "{}"))    # 已含 DB 系凭据
```

**prompt 构建侧（`src/prompt_builder.py`）**

| 原注入 | 改为 |
|---|---|
| `Skill 文件：{SKILLS_DIR}/{skill}/SKILL.md` | `Skill 文件：{{SKILL_DIR}}/{skill}/SKILL.md` |
| `curl -X POST http://localhost:8080/api/a2a` | `curl -X POST {{SERVER_URL}}/api/a2a` |
| 历史文件指引绝对路径（`WORKSPACE_DIR/history/...`） | 远端模式不注入本地路径；改由 event.json 富上下文（`build_issue_context`）承载 |

---

## M3：远端 RuntimeAgent + 凭据

### 3.1 `src/runtime/client.py`（外连客户端 + 指数退避）

```python
class RuntimeClient:
    def __init__(self, server, token):
        self.base = server.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _post(self, path, body=None, retries=0):
        delay = 4
        while True:
            try:
                r = requests.post(self.base + path, json=body, headers=self.headers, timeout=30)
                if r.status_code in (408, 429) or r.status_code >= 500:
                    raise Retryable(r.status_code)
                if r.status_code == 404 and "runtime" in path:
                    raise RuntimeGone()
                r.raise_for_status()
                return r.json()
            except Retryable:
                if retries <= 0: raise
                time.sleep(delay); delay *= 2; retries -= 1
            except RuntimeGone:
                self._clear_local(); self._re_register(); return None

    def register(self, runtimes): ...
    def heartbeat(self, ids): ...
    def claim(self, rid): ...
    def report_messages(self, tid, batch): ...
    def complete(self, tid, result_text, exit_code, usage): ...
    def fail(self, tid, error): ...
```

> ⚠️ `requests` 不在当前 `requirements.txt`。二选一：(a) 新增 `requests` 依赖（`pip install requests` + 写入 requirements.txt）；(b) 用 `aiohttp`（已在依赖中）实现 `_post`。建议 (b) 避免新依赖。

### 3.2 管理端凭据 API（`src/api/routes/settings.py`）

```python
@router.post("/runtime-credentials")
async def create_runtime_credential(body: RuntimeCredBody, user=Depends(get_current_user)):
    token = "rta_" + secrets.token_urlsafe(32)
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO runtime_credentials (token_hash, name, scope, owner_id) VALUES (?,?,?,?)",
                  (hashlib.sha256(token.encode()).hexdigest(), body.name, body.scope, body.owner_id))
        db.commit()
    return {"token": token, "name": body.name}   # 仅展示一次明文

@router.post("/runtime-credentials/{cid}/revoke")   → status='revoked'
@router.get("/runtime-credentials")                 → 列表（不含 token_hash）
```

### 3.3 确定路由（D3）

入队时 `runtime_id` 必填（来自 `devops_agent_session.runtime_id`）；claim 按 runtime_id 过滤；绑定 runtime 离线 → 任务保持 queued（health 页可见）。

---

## M4：前端实现

### 4.1 `src/api/static/js/pages/runtimes.js`（替换 backends.js）

```javascript
function renderRuntimes() {
  API.get('/settings/agent-runtimes').then(d => {
    const devices = groupByHealth(d.runtimes);   // online / recently_lost / offline
    // 渲染设备卡片（健康徽标 + 承载 CLI + 心跳时间 + [查看任务]）
  });
  API.get('/settings/runtime-credentials').then(d => { /* 凭据列表 */ });
}
function genCredential() { /* 弹窗 → POST → 展示明文一次 + 复制 */ }
function openTasks(runtimeId) { /* 设备任务列表弹窗/子页 */ }
```

`src/api/static/index.html`（`:61`）：`#nav-backends` → `#nav-runtimes`；`app.js`（`:3` VIEWS / `:233` nav）路由 `'backends'` → `'runtimes'`。

### 4.2 Agent 配置页绑定 runtime

`src/api/static/js/pages/agents.js` Config 面板新增下拉：`GET /api/settings/agent-runtimes?online=1` → 选 runtime → `PUT /api/agents/{agent}/config` 写 `runtime_id`。

### 4.3 chat 轮询

`src/api/static/js/pages/chat.js`：
```javascript
function sendMessage() {
  API.post('/chat/send', {...}).then(r => {
    const taskId = r.task_id;
    pollTask(taskId);
  });
}
function pollTask(taskId) {
  const timer = setInterval(() => {
    API.get('/runtime/tasks/' + taskId + '/status').then(d => {
      if (d.status === 'completed' || d.status === 'failed') {
        clearInterval(timer);
        renderReply(d.result_text);
      } else { renderProgress(d.progress); }
    });
  }, 500);
}
```

### 4.4 Event Detail 结构化展示

`src/api/static/js/pages/events.js`：`GET /api/events/detail/{seq}` 返回体新增 `archive`（读取 event.json 对应段），渲染 actions/conclusion/artifacts/next_step 卡片。

---

## 五、测试策略

### 5.1 `tests/test_event_archive.py`

| 用例 | 断言 |
|------|------|
| init 建骨架 | 文件存在，pipeline 含 1 段，summary 占位 |
| init #0 跳过 | `issue_id=#0` 不建文件 |
| append_log 全量 | logs 长度与上报条数一致，token 已脱敏 |
| finalize 覆盖 summary | output 带 issue_summary → 顶层覆盖；不带 → 保持 |
| 并发写 | 多线程 append 不损坏 JSON |
| 原子替换 | 写失败后文件仍完整 |
| build_issue_context | 输出含各段结论 + summary |

### 5.2 `tests/test_runtime.py`

| 用例 | 断言 |
|------|------|
| register 幂等 upsert | 重复注册不产生重复行 |
| claim 确定路由 | 只 claim 到 runtime_id 匹配任务 |
| claim 串行槽 | 同 agent_session_id 已有 running → 不返回 |
| complete 幂等 | 二次 complete 不重复 finalize |
| recover-orphans | 清理 dispatched/running 遗留 |
| 认证 | 无 token / revoked token → 401 |

### 5.3 `tests/test_event_store.py`

| 用例 | 断言 |
|------|------|
| parse_agent_output_json 合法块 | 返回 dict |
| evaluate 优先 JSON | final_text 含 JSON 块 → 用 conclusion |
| evaluate 降级 | 无 JSON 块 → 走正则 |

### 5.4 回归

`python -m pytest tests/ -v` 全绿（VCS 相关测试不改）。

---

## 六、实施顺序与验证

| 步骤 | 内容 | 验证 |
|------|------|------|
| 1 | M0 术语重构 | 全量测试绿 |
| 2 | M1 建表 + event_archive.py + service.py 骨架 | 单测通过 |
| 3 | M1 stream_runtime 结构化 tool_use | `test_agent_executor` 通过 |
| 4 | M2 runtime 路由 + event_pipeline 切 enqueue | `test_runtime`/`test_event_archive` 通过 |
| 5 | M2 chat 改轮询 | 手工验证打字机 |
| 6 | M3 凭据 + agent.py 边端 | 本地 RuntimeAgent 端到端 |
| 7 | M4 前端 | Playwright / 手工 |
| 8 | 部署 | 按 `DEPLOYMENT-REMOTE.md` 流程 |

---

## 七、风险与回滚

| 风险 | 缓解 | 回滚 |
|------|------|------|
| 术语重构破坏调用 | 保留 `stream_agent` 兼容别名 | 恢复 git 上一提交 |
| 事件档案写失败 | 仅 log.warning，events 表为底账 | 删除 archive 代码即可回退到纯表 |
| chat 轮询降级 | 500ms 轮询近似打字机 | 保留 `send/stream` SSE 分支（可配置切换） |
| runtime 离线任务滞留 | health 页可见 + 手动重新入队 | — |
| DB 迁移失败 | 幂等脚本 + 迁移前备份 `data/sessions.db` | 恢复备份 |

---

## 八、部署注意

- 本机 RuntimeAgent：`RUNTIME_TOKEN=rta_xxx python -m runtime.agent`（作为 systemd 服务或 supervisor 托管）
- 远端 RuntimeAgent：worker 机器装同仓库，`RUNTIME_SERVER_URL=https://<server>/` 仅外连
- `configs/global-bot-token.json` 与 `data/sessions.db` 不受影响
- 前端 `?v=` 缓存版本号需提升（`index.html`）
- **严禁用 PowerShell 写含中文文件**（见 `DEPLOYMENT-REMOTE.md` §6.1 教训）
