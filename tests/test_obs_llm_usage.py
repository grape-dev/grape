"""LLM 用量台账（obs_llm_usage）与引擎账本采集测试。

设计文档：docs/weekly-07/observability-llm-usage-design.md

钉死四条容易回归的约定：
1. **绝不写 agent_tasks.usage_json** —— 那是会话续接指针，被 routes/events.py 等消费，
   历史上 /tasks/{id}/usage 用 UPDATE ... SET usage_json=? 覆盖它（破坏多轮续接）。
2. **追加不覆盖** —— 一个任务多轮多模型 → 多行。
3. **幂等** —— 同 (source, model, turn) 重报覆盖而非累加，worker 重试不翻倍。
4. **增量 vs 累计** —— hermes 账本是累计值必须取差值；其余引擎是增量必须 SUM。
   搞反会让用量翻好几倍或少算。
"""
import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.observability import llm_usage as lu
from src.runtime import usage_collect as uc


# ------------------------------------------------------- 运行主体分型（2026-10-17 决定 B）

def _raw_rows(where: str, params=()):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(f"SELECT task_id, run_id, run_kind, dedup_key, repo_key, user_id, scene_id,"
                  f" agent_type, engine_id, input_tokens FROM obs_llm_usage WHERE {where}"
                  f" ORDER BY id", tuple(params))
        return c.fetchall()


def _cleanup_run(run_id: str):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM obs_llm_usage WHERE run_id=?", (run_id,))
        db.commit()


def test_task_rows_keep_legacy_dedup_key():
    """回归：worker 任务的幂等键**必须**仍是老的 `{task_id}:{seq}`。

    升级成 `{run_kind}:{run_id}:{seq}` 会让升级后重报的旧数据命中不到老行 → 用量翻倍。
    """
    tid = "t_test_runkey"
    _make_task(tid)
    rec = [{"model": "glm-5.3", "input": 11, "output": 2, "turn": 0}]
    assert lu.record_usage(tid, rec, source="hermes") == 1
    seq = lu.stable_seq("hermes", "glm-5.3", 0)
    row = _raw_rows("task_id=?", (tid,))[0]
    assert row[3] == f"{tid}:{seq}", row          # dedup_key 老格式
    assert row[1] == tid and row[2] == "task"     # run_id=task_id, run_kind=task
    assert row[4] == "gitcode:org/repo" and row[5] == 42   # 归属仍是反查 agent_tasks
    _cleanup(tid)


def test_master_side_run_recorded_with_own_kind():
    """主控机内跑的引擎（chat/devops/im/wiki）：没有 agent_tasks 行也能落台账。

    task_id 空串、主体放 run_id、幂等键用 `{run_kind}:{run_id}:{seq}`、归属由 attr 传入。
    """
    run_id = "chat:conv123:msg456"
    lu.record_usage("", [{"model": "glm-5.3", "input": 999, "output": 88, "turn": 0,
                          "cost_usd": 0.5, "cost_status": "ok"}],
                    source="hermes", run_kind="chat", run_id=run_id,
                    attr={"repo_key": "gitcode:org/repo", "scene_id": "scene-x",
                          "agent_type": "chat", "engine_id": 4, "user_id": 42})
    seq = lu.stable_seq("hermes", "glm-5.3", 0)
    row = _raw_rows("run_id=?", (run_id,))[0]
    assert row[0] == "", row                        # task_id 空
    assert row[1] == run_id and row[2] == "chat"
    assert row[3] == f"chat:{run_id}:{seq}", row    # 分型幂等键
    assert row[4] == "gitcode:org/repo" and row[5] == 42 and row[6] == "scene-x"
    assert row[7] == "chat" and row[8] == 4
    assert row[9] == 999
    # 幂等：重报不翻倍
    lu.record_usage("", [{"model": "glm-5.3", "input": 999, "output": 88, "turn": 0}],
                    source="hermes", run_kind="chat", run_id=run_id,
                    attr={"agent_type": "chat", "user_id": 42})
    assert len(_raw_rows("run_id=?", (run_id,))) == 1
    # 读取：任务详情看不到它，run_usage 能看到；按类型聚合能分出来
    assert lu.task_usage("") == []
    got = lu.run_usage("chat", run_id)
    assert len(got) == 1 and got[0]["input_tokens"] == 999
    kinds = {r["run_kind"]: r for r in lu.usage_by_kind()}
    assert kinds["chat"]["rows_cnt"] >= 1 and kinds["chat"]["input_tokens"] >= 999
    _cleanup_run(run_id)


def test_master_side_run_requires_run_id():
    """没有主体的调用不落库（防止出现无法归属的孤儿行）。"""
    assert lu.record_usage("", [{"model": "glm", "input": 5}], run_kind="chat", run_id="") == 0
    assert lu.record_usage("", [{"model": "glm", "input": 5}], run_kind="task") == 0


# ---------------------------------------------------- 主控侧采集器（MasterRun）

def test_master_run_writes_chat_rows(monkeypatch):
    """主控机内运行（chat）：MasterRun.finish() 直接把用量写进台账（不经 HTTP 上报口）。"""
    from src.observability import master_usage as mu
    # 采集器记录的真实形态：input/output/cache_*/reasoning + _source_suffix（见 _usage_payload）
    recs = [{"provider": "custom", "model": "glm-5.3", "input": 321, "output": 12,
             "cache_read": 7, "cache_write": 0, "reasoning": 0,
             "cost_usd": 0.01, "cost_status": "ok"},
            {"provider": "custom", "model": "glm-5.3", "input": 40, "output": 3,
             "_source_suffix": "title_generation"}]

    class _Fake:
        engine = "hermes"
        def __init__(self, *a, **k): pass
        def start(self): pass
        def feed(self, *a, **k): pass
        def finish(self, session_id=""):
            return list(recs)

    monkeypatch.setattr("src.runtime.usage_collect.UsageCollector", _Fake)
    run_id = "chat:convZZ:msg1"
    mr = mu.MasterRun("chat", run_id, engine="hermes", cwd="/tmp/x",
                      attr={"repo_key": "gitcode:org/repo", "scene_id": "convZZ",
                            "agent_type": "chat", "user_id": 7, "engine_id": 259})
    mr.start()
    mr.feed("text", "hi")
    assert mr.finish() == 2                       # 两条记录各一行
    assert mr.finish() == 0                       # 幂等：重复 finish 不再写
    rows = _raw_rows("run_id=?", (run_id,))
    assert len(rows) == 2, rows
    assert {r[0] for r in rows} == {""}           # task_id 空
    assert {r[2] for r in rows} == {"chat"}
    assert {r[4] for r in rows} == {"gitcode:org/repo"} and {r[5] for r in rows} == {7}
    assert {r[8] for r in rows} == {259}, f"engine_id 应落 259（哪台引擎跑的），实际 {[r[8] for r in rows]}"
    srcs = {r[10] for r in rows} if False else None
    # 逐条保留 source（title_generation 与主调用分开记账）
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT source, input_tokens FROM obs_llm_usage WHERE run_id=?", (run_id,))
        got = dict(c.fetchall())
    assert got.get("hermes") == 321 and got.get("hermes:title_generation") == 40, got
    _cleanup_run(run_id)


# ------------------------------------------- 事件流水线 / autopilot（有 agent_tasks 行）

class _FakeCollector:
    engine = "hermes"
    def __init__(self, *a, **k): pass
    def start(self): pass
    def feed(self, *a, **k): pass
    def finish(self, session_id=""): return [{"model": "glm-5.3", "input": 77, "output": 5}]


def test_for_task_uses_task_kind_and_auto_attribution(monkeypatch):
    """事件流水线/autopilot：有 agent_tasks 行 → run_kind='task'、归属自动反查、
    幂等键与 worker 采集**一致**（同一任务跨本机/worker 不会重复计）。"""
    from src.observability import master_usage as mu
    tid = "t_test_fortask"
    _cleanup(tid)
    _make_task(tid, repo_key="gitcode:org/repo")          # 建 agent_tasks 行（user_id=42）
    monkeypatch.setattr("src.runtime.usage_collect.UsageCollector", _FakeCollector)
    m = mu.for_task(tid, "hermes", cwd="/tmp/ws", engine_id=999)
    assert m.run_kind == "task" and m.run_id == tid, (m.run_kind, m.run_id)
    m.start()
    assert m.finish() == 1
    rows = _raw_rows("task_id=?", (tid,))
    assert len(rows) == 1, rows
    seq = lu.stable_seq("hermes", "glm-5.3", 0)
    assert rows[0][3] == f"{tid}:{seq}", rows[0][3]     # 老格式幂等键＝worker 侧同一行
    assert rows[0][1] == tid and rows[0][2] == "task"
    assert rows[0][4] == "gitcode:org/repo" and rows[0][5] == 42   # 归属自动反查 agent_tasks
    # engine_id：agent_tasks.runtime_id 优先（这里 _make_task 写的是 7），调用方给的 999 不覆盖它
    assert rows[0][8] == 7, f"engine_id 应以 agent_tasks.runtime_id 为准，实际 {rows[0][8]}"
    _cleanup(tid)


def test_for_task_fills_engine_id_when_task_row_lacks_it(monkeypatch):
    """task 行的 runtime_id 为空时，用调用方知道的 engine_id 兜底（补空，不覆盖已有值）。"""
    from src.observability import master_usage as mu
    from src.session_manager import db, db_lock
    tid = "t_test_fortask_noeng"
    _cleanup(tid)
    _make_task(tid, repo_key="gitcode:org/repo")
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET runtime_id=0 WHERE id=?", (tid,))
        db.commit()
    monkeypatch.setattr("src.runtime.usage_collect.UsageCollector", _FakeCollector)
    m = mu.for_task(tid, "hermes", cwd="/tmp/ws", engine_id=259)
    m.start(); assert m.finish() == 1
    rows = _raw_rows("task_id=?", (tid,))
    assert rows[0][8] == 259, f"应补上调用方给的引擎 id，实际 {rows[0][8]}"
    _cleanup(tid)


def test_for_task_falls_back_for_seq_only_runs(monkeypatch):
    """没有 agent_tasks 行的兜底运行（d_<seq>）：run_kind 兜底 + attr 传归属，不产生孤儿行。"""
    from src.observability import master_usage as mu
    monkeypatch.setattr("src.runtime.usage_collect.UsageCollector", _FakeCollector)
    m = mu.for_task("", "hermes", cwd="/tmp", seq="d_9", repo_key="gitcode:org/x",
                    agent_type="coding", user_id=0, engine_id=88)
    assert m.run_kind == "devops" and m.run_id == "devops:d_9", (m.run_kind, m.run_id)
    m.start()
    assert m.finish() == 1
    rows = _raw_rows("run_id=?", ("devops:d_9",))
    assert len(rows) == 1 and rows[0][0] == "" and rows[0][2] == "devops", rows
    assert rows[0][4] == "gitcode:org/x" and rows[0][7] == "coding"
    assert rows[0][8] == 88, f"兜底路径的 engine_id 应来自 attr，实际 {rows[0][8]}"
    _cleanup_run("devops:d_9")


def test_master_run_without_subject_is_noop(monkeypatch):
    """没有 run_id（或引擎）时不采集、不写库——避免产生无法归属的孤儿行。"""
    from src.observability import master_usage as mu
    mr = mu.MasterRun("chat", "", engine="hermes")
    mr.start()
    assert mr.finish() == 0
    assert mu.new_run_id("chat").startswith("chat:")


# ------------------------------------------------------------------ 归一化

def test_token_aliases_normalized():
    """各引擎叫法不同（prompt_tokens/input、completion_tokens/output…）→ 同一组列。"""
    got = lu.normalize_tokens({"prompt_tokens": 100, "completion_tokens": 20,
                               "cache_read_input_tokens": 5, "reasoning_output_tokens": 3})
    assert got == {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 5,
                   "cache_write_tokens": 0, "reasoning_tokens": 3}
    # opencode 的叫法
    got2 = lu.normalize_tokens({"input": 7, "output": 2, "reasoning": 1})
    assert got2["input_tokens"] == 7 and got2["output_tokens"] == 2 and got2["reasoning_tokens"] == 1


def test_token_normalize_ignores_garbage():
    got = lu.normalize_tokens({"input_tokens": "abc", "output_tokens": None, "prompt_tokens": -5})
    assert got["input_tokens"] == 0 and got["output_tokens"] == 0


def test_model_normalization_only_kills_noise():
    assert lu.normalize_model("Anthropic/Claude-Sonnet-4.5") == "claude-sonnet-4.5"
    assert lu.normalize_model("  GLM-5.3  ") == "glm-5.3"
    assert lu.normalize_model("deepseek_v4_flash") == "deepseek-v4-flash"
    # P0 刻意不做点/横线合并：那是 obs_model_map 的职责，归错比不归更糟
    assert lu.normalize_model("claude-sonnet-4.5") != lu.normalize_model("claude-sonnet-4-5")


def test_stable_seq_deterministic_and_fits_int():
    a = lu.stable_seq("hermes", "glm-5.3", 1)
    assert a == lu.stable_seq("hermes", "glm-5.3", 1)          # 确定性
    assert a != lu.stable_seq("hermes", "glm-5.3", 2)          # 轮次不同 → 不同行
    assert a != lu.stable_seq("opencode", "glm-5.3", 1)        # 来源不同 → 不同行
    assert 0 <= a <= 0x7FFFFFFF, "必须落在 MySQL 有符号 INT 范围内"


# ------------------------------------------------------------------ 台账落库

def _make_task(task_id: str, usage_json: str = "{}", repo_key: str = "gitcode:org/repo"):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (task_id,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt,"
            " repo_key, agent_type, user_id, status, usage_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, "sess-" + task_id, 7, "p", repo_key, "coding", 42, "completed", usage_json))
        db.commit()


def _rows(task_id: str):
    return lu.task_usage(task_id)


def _cleanup(task_id: str):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM obs_llm_usage WHERE task_id=?", (task_id,))
        c.execute("DELETE FROM agent_tasks WHERE id=?", (task_id,))
        db.commit()


def test_record_usage_is_idempotent_on_retry():
    """worker 重试同一次上报 → 覆盖不累加。"""
    tid = "t_test_idem"
    _make_task(tid)
    rec = [{"model": "claude-sonnet-4.5", "input": 1000, "output": 200, "turn": 1}]
    assert lu.record_usage(tid, rec, source="codex") == 1
    assert lu.record_usage(tid, list(rec), source="codex") == 1
    rows = _rows(tid)
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 1000 and rows[0]["output_tokens"] == 200
    _cleanup(tid)


def test_record_usage_appends_multiple_models_and_turns():
    tid = "t_test_append"
    _make_task(tid)
    lu.record_usage(tid, [{"model": "m-a", "input": 10, "turn": 1}], source="opencode")
    lu.record_usage(tid, [{"model": "m-a", "input": 20, "turn": 2},
                          {"model": "m-b", "input": 30, "turn": 1}], source="opencode")
    rows = _rows(tid)
    assert len(rows) == 3
    assert sum(r["input_tokens"] for r in rows) == 60
    _cleanup(tid)


def test_record_usage_skips_all_zero_records():
    """0 与"未知"是两件事：全 0 不落库，避免污染台账让成本看起来是 0。"""
    tid = "t_test_zero"
    _make_task(tid)
    assert lu.record_usage(tid, [{"model": "m", "input": 0, "output": 0}], source="hermes") == 0
    assert _rows(tid) == []
    _cleanup(tid)


def test_attribution_comes_from_task_not_from_caller():
    """归属维度由主控反查 agent_tasks，上报方伪造的 repo_key/user_id 一律被忽略。"""
    tid = "t_test_attr"
    _make_task(tid, repo_key="gitcode:org/real-repo")
    lu.record_usage(tid, [{"model": "m", "input": 5, "repo_key": "gitcode:evil/other",
                           "user_id": 999, "engine_id": 12345}], source="report")
    rows = _rows(tid)
    assert len(rows) == 1
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, user_id, engine_id, agent_type FROM obs_llm_usage WHERE task_id=?",
                  (tid,))
        repo_key, user_id, engine_id, agent_type = c.fetchone()
    assert repo_key == "gitcode:org/real-repo", "归属必须来自 agent_tasks"
    assert user_id == 42 and engine_id == 7 and agent_type == "coding"
    _cleanup(tid)


def test_usage_endpoint_never_touches_usage_json():
    """回归：/tasks/{id}/usage 曾用 UPDATE 覆盖 usage_json（冲掉会话续接指针）。"""
    from src.api.routes.runtime import UsageBody, runtime_task_usage
    tid = "t_test_endpoint"
    pointer = json.dumps({"session_id": "acp_123", "work_dir": "/w", "engine": "hwcloud-x"})
    _make_task(tid, usage_json=pointer)

    runtime_task_usage(tid, UsageBody(provider="anthropic", model="Anthropic/Claude-Sonnet-4.5",
                                      input=1200, output=340, cache_read=8000, cache_write=100,
                                      reasoning=42, turn=1, source="hwcloud"), credential_id=1)

    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT usage_json FROM agent_tasks WHERE id=?", (tid,))
        after = c.fetchone()[0]
    assert after == pointer, "usage_json 是会话续接指针，绝不能被用量上报覆盖"

    rows = _rows(tid)
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 1200 and rows[0]["cache_read_tokens"] == 8000
    assert rows[0]["model_key"] == "claude-sonnet-4.5"
    _cleanup(tid)


# ------------------------------------------------------------------ 引擎账本适配器

def _tmp_sqlite(rows_sql, path):
    conn = sqlite3.connect(path)
    for sql in rows_sql:
        conn.execute(sql)
    conn.commit()
    conn.close()


def test_opencode_adapter_aggregates_session_and_respects_watermark(tmp_path, monkeypatch):
    db_path = str(tmp_path / "opencode.db")
    _tmp_sqlite([
        "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)",
    ], db_path)

    def _msg(sid, ts, model, i, o, r=0, cr=0):
        return ("INSERT INTO message VALUES (?,?,?,?)",
                (f"m{ts}", sid, ts, json.dumps({
                    "modelID": model, "providerID": "p",
                    "tokens": {"input": i, "output": o, "reasoning": r,
                               "cache": {"read": cr, "write": 0}}})))

    conn = sqlite3.connect(db_path)
    for args in [_msg("ses_new", 100, "glm-5.3", 1000, 50),
                 _msg("ses_new", 200, "glm-5.3", 500, 25, r=5, cr=700),
                 _msg("ses_old", 50, "glm-5.3", 99999, 9999)]:
        conn.execute(args[0], args[1])
    conn.commit()
    conn.close()

    monkeypatch.setattr(uc, "_opencode_db_path", lambda: db_path)

    # 水位=100 → 只算 200 那条（本任务新增）
    col = uc.UsageCollector("opencode", session_id="ses_new", opencode_db=db_path)
    col._baseline = {"opencode": 100}
    got = col.finish()
    assert len(got) == 1
    assert got[0]["input"] == 500 and got[0]["output"] == 25
    assert got[0]["reasoning"] == 5 and got[0]["cache_read"] == 700

    # 水位=0 → 该 session 全量（旧 session 的数据不混入）
    col2 = uc.UsageCollector("opencode", session_id="ses_new", opencode_db=db_path)
    got2 = col2.finish()
    assert sum(g["input"] for g in got2) == 1500


def test_hermes_adapter_takes_delta_not_sum(tmp_path, monkeypatch):
    """hermes 的 session_model_usage 是累计值：取差值，SUM 会把历史任务重复计入。"""
    db_path = str(tmp_path / "state.db")
    _tmp_sqlite([
        "CREATE TABLE session_model_usage (session_id TEXT, model TEXT, task TEXT,"
        " input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,"
        " cache_write_tokens INTEGER, reasoning_tokens INTEGER)",
        ("INSERT INTO session_model_usage VALUES ('s1','glm-5.3','', 1000, 200, 5000, 0, 30)"),
        ("INSERT INTO session_model_usage VALUES ('s1','glm-5.3','title_generation', 60, 20, 0, 0, 10)"),
    ], db_path)

    monkeypatch.setattr(uc, "_hermes_iso_home", lambda cwd: str(tmp_path))
    col = uc.UsageCollector("hermes", cwd="/whatever")
    col._baseline = {"hermes": {
        "s1|glm-5.3|": {"input": 900, "output": 150, "cache_read": 5000,
                        "cache_write": 0, "reasoning": 30},
        "s1|glm-5.3|title_generation": {"input": 60, "output": 20, "cache_read": 0,
                                        "cache_write": 0, "reasoning": 10},
    }}
    got = col.finish()
    main = [g for g in got if not g.get("_source_suffix")]
    aux = [g for g in got if g.get("_source_suffix")]
    assert len(main) == 1 and main[0]["input"] == 100 and main[0]["output"] == 50
    assert main[0]["cache_read"] == 0 and main[0]["reasoning"] == 0
    assert not aux, "辅助任务本次无新增 → 不该产生行"
    # 基线等于当前值 → 无增量
    col2 = uc.UsageCollector("hermes", cwd="/whatever")
    col2._baseline = {"hermes": col2._hermes_rows()}
    assert col2.finish() == []


def test_hwcloud_adapter_reads_only_appended_lines(tmp_path, monkeypatch):
    cwd = "/tmp/fake-hw-cloud-cwd"
    iso = tmp_path / uc._cwd_hash(cwd)
    (iso / "logs").mkdir(parents=True)
    log = iso / "logs" / "hwcloud.log"
    log.write_text(json.dumps({"msg": "agent end", "model": "old",
                               "prompt_tokens": 111, "completion_tokens": 11}) + "\n")
    monkeypatch.setattr(uc, "_hwcloud_iso_home", lambda c: str(iso))

    col = uc.UsageCollector("hwcloud", cwd=cwd)
    col.start()                       # 记基线偏移
    with log.open("a") as f:
        f.write(json.dumps({"msg": "agent end", "model": "deepseek-v4",
                            "prompt_tokens": 700, "completion_tokens": 40}) + "\n")
        f.write(json.dumps({"msg": "agent end", "model": "deepseek-v4",
                            "prompt_tokens": 300, "completion_tokens": 20}) + "\n")
        f.write(json.dumps({"msg": "tool start"}) + "\n")
    got = col.finish()
    assert len(got) == 1, "同模型多次 run 应聚合为一条"
    assert got[0]["input"] == 1000 and got[0]["output"] == 60, "旧行(111)不得混入"
    assert got[0]["turn"] == 2


def test_hwcloud_adapter_falls_back_when_log_rotated(tmp_path, monkeypatch):
    """日志被轮转（当前长度 < 基线）→ 退化为整文件解析，而不是静默 0。"""
    cwd = "/tmp/fake-hw-rotated"
    iso = tmp_path / uc._cwd_hash(cwd)
    (iso / "logs").mkdir(parents=True)
    log = iso / "logs" / "hwcloud.log"
    log.write_text("x" * 500 + "\n")
    monkeypatch.setattr(uc, "_hwcloud_iso_home", lambda c: str(iso))
    col = uc.UsageCollector("hwcloud", cwd=cwd)
    col.start()
    log.write_text(json.dumps({"msg": "agent end", "model": "m",
                               "prompt_tokens": 10, "completion_tokens": 2}) + "\n")
    got = col.finish()
    assert len(got) == 1 and got[0]["input"] == 10


def test_codex_usage_fed_from_event_stream():
    """codex 无落盘账本，用量随 step_finish 事件带出。"""
    col = uc.UsageCollector("codex", cwd="/tmp/x")
    col.feed("step_start", "Step 1")
    col.feed("step_finish", {"tokens": 130, "usage": {"input": 100, "output": 20, "reasoning": 10}})
    col.feed("text", "hi")
    col.feed("step_finish", {"tokens": 15, "usage": {"input": 10, "output": 5}})
    got = col.finish()
    assert len(got) == 2
    assert [g["turn"] for g in got] == [1, 2]
    assert got[0]["input"] == 100 and got[1]["output"] == 5


def test_collector_never_raises_on_broken_ledger(tmp_path, monkeypatch):
    """账本损坏/路径不存在 → 返回空，绝不抛给任务主链路。"""
    monkeypatch.setattr(uc, "_hermes_iso_home", lambda cwd: str(tmp_path / "nonexistent"))
    col = uc.UsageCollector("hermes", cwd="/x")
    col.start()
    assert col.finish() == []


# ------------------------------------------------------------------ 上报载荷

def test_usage_payload_mapping():
    from src.runtime.agent import _usage_payload
    p = _usage_payload({"model": "glm-5.3", "provider": "custom", "input": 10,
                        "output": 2, "cache_read": 3, "cache_write": 1,
                        "reasoning": 4, "turn": 2}, "opencode")
    assert p == {"provider": "custom", "model": "glm-5.3", "input": 10, "output": 2,
                 "cache_read": 3, "cache_write": 1, "reasoning": 4, "turn": 2,
                 "cost_usd": None, "cost_status": "", "source": "opencode"}
    # source 记录"用量怎么来的"：hermes 的辅助调用需要与主任务区分
    aux = _usage_payload({"model": "glm-5.3", "input": 1, "_source_suffix": "title_generation"},
                         "hermes")
    assert aux["source"] == "hermes:title_generation"
    # 无引擎名（引擎直报路径）→ source 留空，由服务端落为 report
    assert _usage_payload({"input": 1}, "")["source"] == ""