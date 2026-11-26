"""Issue #4341：字母数字单号按「首个数字串」归组 → events 串号 / pipeline 恒空。

生产现象（Issue 正文实测，`example/hello-skill`）：

- `#IKXR3A/B/C` 三个单号的事件被 `_num_from_issue_id()` 的 `re.search(r"(\\d+)")`
  全部抠成键 `"3"`，合并进同一个桶，并与纯数字 `#3` 撞桶；
- `get_issue_pipeline()` 用完整编号查该 map 恒 miss → `segments=[]`、`status=unknown`；
- 插件面板因此节点链全灰、实时卡片退化成单个 `?`。

本文件 seam：

- 单号解析/归组键/键取值 → 模块函数（`_num_from_issue_id` / `_group_events_by_issue` /
  `_issue_group_key` / `_issue_group_events`），与 `tests/insight/test_issue_859_event_output_logs.py` 同层；
- 端点级串号 → HTTP 路由层（`TestClient`），与 `test_issue_3891_pipeline_shared_resolution.py`
  同层，auth 用会话级 token（`_enforce_session_repo_scope` 覆盖）。
"""
import os
import uuid

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))
os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.kernel.session_manager import db, db_lock

client = TestClient(app)

OWNER, REPO = "acme", "hello4341"
BARE_RK = f"{OWNER}/{REPO}"
GITEE_RK = f"gitee:{BARE_RK}"
GITCODE_RK = f"gitcode:{BARE_RK}"

# 事件 seq 段位：避免与其它测试文件撞键
BASE_SEQ = 4341000


# ------------------------------------------------------------------ 夹具

def _cleanup():
    with db_lock:
        c = db.cursor()
        for table in ("events", "issue", "issue_state", "agent_tasks"):
            try:
                c.execute(f"DELETE FROM {table} WHERE repo_key IN (?,?,?)",
                          (BARE_RK, GITEE_RK, GITCODE_RK))
            except Exception:
                pass
        db.commit()


@pytest.fixture(autouse=True)
def _db():
    from src.api.db_setup import init_database
    init_database()
    _cleanup()
    yield
    _cleanup()


def _seed_event(seq, repo_key, issue_id, agent="vod", event_type="issue_opened"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, source, issue_id, title, summary, "
            "actor, payload, agent_type, runtime, skill, status, exit_code, duration, "
            "agent_logs, result, created_at, started_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'),datetime('now'))",
            (seq, repo_key, event_type, "webhook", issue_id, f"t-{issue_id}", f"s-{issue_id}",
             "", "{}", agent, "opencode", f"{agent}-cli", "completed", 0, 1.0, "[]", "r"))
        db.commit()


def _seed_issue(repo_key, number, stage="coding"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status) "
            "VALUES ('u1','',?,?,?,'active')", (repo_key, number, f"card-{number}"))
        c.execute("UPDATE issue SET stage=?, owner_agent=? WHERE id=?",
                  (stage, stage, c.lastrowid))
        db.commit()


def _seed_issue_state(repo_key, issue_id, stage="coding"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue_state (repo_key, issue_id, stage, owner_agent) VALUES (?,?,?,?)",
            (repo_key, issue_id, stage, stage))
        db.commit()


def _headers(repo_key):
    from src.api import auth_token
    tok = auth_token.issue_session_token(
        session_key=f"i4341-{uuid.uuid4().hex}", repo_key=repo_key,
        agent_type="coding", user_id="")
    return {"Authorization": f"Bearer {tok}"}


def _mod():
    from src.domains.runtime.routes import issues as _issues
    return _issues


# ------------------------------------------------- ① 单号解析：完整 ident

def test_num_from_issue_id_full_ident():
    """`#` 后整串即编号——不得再抠首个数字串（#IKXR3B 曾被解析成 "3"）。"""
    iss = _mod()
    assert iss._num_from_issue_id("#IKXR3B") == "IKXR3B"
    assert iss._num_from_issue_id("IKXR3B") == "IKXR3B"
    assert iss._num_from_issue_id("#IKXR3A") == "IKXR3A"
    assert iss._num_from_issue_id("#IKXR3C") == "IKXR3C"


def test_num_from_issue_id_numeric_compat():
    """纯数字输入保持兼容（5 / #5 → "5"）。"""
    iss = _mod()
    assert iss._num_from_issue_id("5") == "5"
    assert iss._num_from_issue_id("#5") == "5"
    assert iss._num_from_issue_id("3") == "3"


def test_num_from_issue_id_pseudo_pr_empty():
    """`#pr_*` 订阅伪 ID 仍返回空（#252 契约不回归）。"""
    iss = _mod()
    assert iss._num_from_issue_id("#pr_1_scene") == ""
    assert iss._num_from_issue_id("") == ""


# ------------------------------------------------- ② 归组键：provider + ident

def test_group_keys_unique_mixed_idents():
    """同仓混合「纯数字 + 字母数字」单号 → 分组键唯一（验收条款）。"""
    iss = _mod()
    _seed_event(BASE_SEQ + 1, GITEE_RK, "#IKXR3A")
    _seed_event(BASE_SEQ + 2, GITEE_RK, "#IKXR3B")
    _seed_event(BASE_SEQ + 3, GITEE_RK, "#IKXR3C")
    _seed_event(BASE_SEQ + 4, GITCODE_RK, "#3")
    _seed_event(BASE_SEQ + 5, GITEE_RK, "#pr_7_scene")

    group = iss._group_events_by_issue([GITEE_RK, GITCODE_RK])

    assert set(group.keys()) == {"gitee:IKXR3A", "gitee:IKXR3B", "gitee:IKXR3C", "gitcode:3"}, \
        f"分组键不唯一：{sorted(group.keys())}"
    for key, evs in group.items():
        assert len(evs) == 1, f"{key} 混入其它 issue 的事件：{[e['seq'] for e in evs]}"


def test_group_key_shape_uses_provider_prefix():
    """键形状 = `<provider>:<完整编号>`，provider 取 repo_key 平台前缀。"""
    iss = _mod()
    assert iss._issue_group_key(GITEE_RK, "IKXR3B") == "gitee:IKXR3B"
    assert iss._issue_group_key(GITCODE_RK, "3") == "gitcode:3"
    assert iss._issue_group_key(GITEE_RK, "#IKXR3B") == "gitee:IKXR3B"
    # 裸 repo_key（历史写入行）按平台卡默认 provider 归组，仍可被候选键查到
    assert iss._issue_group_key(BARE_RK, "IKXR3B") == "gitcode:IKXR3B"


def test_group_events_not_merged_across_providers():
    """归组层按平台分桶（不再「跨 repo_keys 合并同号」）；取值层只合并同一 ident。"""
    iss = _mod()
    _seed_event(BASE_SEQ + 11, GITCODE_RK, "#3", agent="vod")
    _seed_event(BASE_SEQ + 12, GITEE_RK, "#3", agent="coding")

    group = iss._group_events_by_issue([GITCODE_RK, GITEE_RK])

    assert set(group.keys()) == {"gitcode:3", "gitee:3"}, f"归组层应分桶：{sorted(group)}"
    assert [e["agent_type"] for e in group["gitcode:3"]] == ["vod"]
    assert [e["agent_type"] for e in group["gitee:3"]] == ["coding"]
    # 取值层：同一 ident 的平台别名/镜像仓双写都可见（不静默丢事件），seq 升序
    merged = iss._issue_group_events(group, [GITCODE_RK, GITEE_RK], "3")
    assert [e["seq"] for e in merged] == [BASE_SEQ + 11, BASE_SEQ + 12]


def test_group_events_lookup_by_ident():
    """取值助手按候选键命中自身桶，字母数字单号互不串号。"""
    iss = _mod()
    _seed_event(BASE_SEQ + 21, GITEE_RK, "#IKXR3A")
    _seed_event(BASE_SEQ + 22, GITEE_RK, "#IKXR3C")

    group = iss._group_events_by_issue(iss._repo_keys_of(OWNER, REPO))

    assert [e["seq"] for e in iss._issue_group_events(group, iss._repo_keys_of(OWNER, REPO), "IKXR3A")] \
        == [BASE_SEQ + 21]
    assert [e["seq"] for e in iss._issue_group_events(group, iss._repo_keys_of(OWNER, REPO), "IKXR3C")] \
        == [BASE_SEQ + 22]
    assert iss._issue_group_events(group, iss._repo_keys_of(OWNER, REPO), "IKXR3B") == []


# ------------------------------------------------- ③ stage map 兜底对齐

def test_stage_map_issue_state_fallback_full_ident():
    """`issue_state` 兜底键必须与 `issue` 单表同形（完整 ident）。"""
    iss = _mod()
    _seed_issue_state(GITEE_RK, "#IKXR3C")

    stage_map = iss._issue_stage_map([GITEE_RK])

    assert "IKXR3C" in stage_map, f"兜底键未对齐完整 ident：{sorted(stage_map)}"
    assert "3" not in stage_map


def test_stage_map_issue_table_and_state_same_key():
    """两源同号落同一键：issue 单表键（完整 ident）不被兜底行另开一桶。"""
    iss = _mod()
    _seed_issue(GITEE_RK, "IKXR3B", stage="coding")
    _seed_issue_state(GITEE_RK, "#IKXR3B", stage="qa")

    stage_map = iss._issue_stage_map([GITEE_RK])

    assert sorted(stage_map) == ["IKXR3B"]
    assert stage_map["IKXR3B"]["stage"] == "coding", "issue 单表为权威，兜底不得覆盖"


# ------------------------------------------------- ④ 端点级：events / pipeline

def test_events_endpoint_no_cross_issue_leak():
    """`#IKXR3B` 自身 0 条事件时不得返回 IKXR3A/IKXR3C 的事件（串号主症状）。"""
    for num in ("IKXR3A", "IKXR3B", "IKXR3C"):
        _seed_issue(GITEE_RK, num)
    _seed_event(BASE_SEQ + 31, GITEE_RK, "#IKXR3A")
    _seed_event(BASE_SEQ + 32, GITEE_RK, "#IKXR3C")

    r = client.get(f"/api/issues/{OWNER}/{REPO}/IKXR3B/events", headers=_headers(GITEE_RK))
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0, f"串号：{r.json()['events']}"

    r = client.get(f"/api/issues/{OWNER}/{REPO}/IKXR3A/events", headers=_headers(GITEE_RK))
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 1
    assert [e["seq"] for e in d["events"]] == [BASE_SEQ + 31]


def test_pipeline_endpoint_segments_non_empty_for_alnum_ident():
    """`IKXR3A` 的 pipeline 必须有 segments（曾恒空、status=unknown）。"""
    for num in ("IKXR3A", "IKXR3B"):
        _seed_issue(GITEE_RK, num)
    _seed_event(BASE_SEQ + 41, GITEE_RK, "#IKXR3A", agent="vod")
    _seed_event(BASE_SEQ + 42, GITEE_RK, "#IKXR3A", agent="issue", event_type="a2a_event")

    d = client.get(f"/api/issues/{OWNER}/{REPO}/IKXR3A/pipeline",
                   headers=_headers(GITEE_RK)).json()
    assert [s["seq"] for s in d["segments"]] == [BASE_SEQ + 41, BASE_SEQ + 42]
    assert [s["agent"] for s in d["segments"]] == ["vod", "issue"]
    assert d["status"] == "completed"


def test_pipeline_endpoint_empty_for_ident_without_events():
    """`IKXR3B` 无事件 → 空 pipeline（不是拿到别人的段）。"""
    for num in ("IKXR3A", "IKXR3B"):
        _seed_issue(GITEE_RK, num)
    _seed_event(BASE_SEQ + 51, GITEE_RK, "#IKXR3A")

    d = client.get(f"/api/issues/{OWNER}/{REPO}/IKXR3B/pipeline",
                   headers=_headers(GITEE_RK)).json()
    assert d["segments"] == []
    assert d["status"] == "unknown"


def test_pipeline_numeric_number_does_not_return_gitee_events():
    """幽灵 `#3`：纯数字单号只返回自身事件，不再返回 Gitee 字母数字单号的事件。"""
    _seed_issue(GITCODE_RK, "3")
    for num in ("IKXR3A", "IKXR3C"):
        _seed_issue(GITEE_RK, num)
    _seed_event(BASE_SEQ + 61, GITCODE_RK, "#3", agent="vod")
    _seed_event(BASE_SEQ + 62, GITEE_RK, "#IKXR3A", agent="issue")
    _seed_event(BASE_SEQ + 63, GITEE_RK, "#IKXR3C", agent="qa")

    d = client.get(f"/api/issues/{OWNER}/{REPO}/3/pipeline", headers=_headers(GITCODE_RK)).json()
    assert [s["seq"] for s in d["segments"]] == [BASE_SEQ + 61], \
        f"纯数字 #3 串入了 Gitee 事件：{[s['seq'] for s in d['segments']]}"


def test_detail_endpoint_events_scoped_per_ident():
    """`issues detail` 内嵌 events 与同编号 pipeline 同源同键（口径不再分裂）。"""
    for num in ("IKXR3A", "IKXR3B"):
        _seed_issue(GITEE_RK, num)
    _seed_event(BASE_SEQ + 71, GITEE_RK, "#IKXR3A")

    d = client.get(f"/api/issues/{OWNER}/{REPO}/IKXR3A", headers=_headers(GITEE_RK)).json()
    assert [e["seq"] for e in d["events"]] == [BASE_SEQ + 71]
    assert [s["seq"] for s in d["pipeline"]["segments"]] == [BASE_SEQ + 71]
    assert d["events_count"] == 1

    d = client.get(f"/api/issues/{OWNER}/{REPO}/IKXR3B", headers=_headers(GITEE_RK)).json()
    assert d["events"] == []
    assert d["pipeline"]["segments"] == []
    assert d["events_count"] == 0


# ------------------------------------------------- ⑤ 前缀路径：裸键存量行可见（QA Optional）

def test_repo_keys_of_prefixed_owner_is_single_prefixed_key():
    """#4343 设计决策：owner 自带 provider 前缀 = 前缀即权威，候选键只有单键、不再扇出/回补裸键。"""
    iss = _mod()
    keys = iss._repo_keys_of(f"gitee:{OWNER}", REPO)
    assert keys == [GITEE_RK], f"前缀路径应只有单键 {GITEE_RK}，实际 {keys}"


def test_prefixed_path_does_not_fan_out_to_bare_key_rows():
    """#4343：前缀路径只认前缀键，裸键存量行不再被扇出命中（与「前缀即权威」一致）。"""
    _seed_issue(GITEE_RK, "IKXR3D")
    _seed_event(BASE_SEQ + 101, BARE_RK, "#IKXR3D")

    r = client.get(f"/api/issues/gitee:{OWNER}/{REPO}/IKXR3D/events",
                   headers=_headers(GITEE_RK))
    assert r.status_code == 200, r.text
    assert [e["seq"] for e in r.json()["events"]] == [], \
        f"前缀路径不应再命中裸键行：{r.json()['events']}"


# ------------------------------------------------- ⑥ 随迁路径：tasks/current + event_archive

def _seed_task(task_id, repo_key, issue_id, seq):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, skill, prompt, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_id, "sess-4341", 1, seq, issue_id, repo_key, "coding", "coding-cli",
             "prompt body", "running"))
        db.commit()


def _task_headers(task_id, repo_key):
    from src.api import auth_token
    tk = auth_token.issue_session_token(session_key=task_id, repo_key=repo_key,
                                        agent_type="coding", user_id="")
    return {"Authorization": f"Bearer {tk}"}


def test_tasks_current_events_count_uses_full_ident():
    """`tasks/current` 随迁回归：gitee 字母数字 ident 的 events_count/latest_event 非空。

    旧缺陷：`_group_events_by_issue(_repo)` 把裸 str 当 repo_keys 传，`repo_keys + params`
    直接 TypeError 被外层 `except` 静默吞掉 → events_count/latest_event 恒空。本用例
    断言真实取值，回退到该形态立即变红（不再是「仅静态改动」）。
    """
    task_id = f"t4341-{uuid.uuid4().hex[:8]}"
    _seed_task(task_id, GITEE_RK, "#IKXR3A", BASE_SEQ + 111)
    _seed_event(BASE_SEQ + 111, GITEE_RK, "#IKXR3A")
    _seed_event(BASE_SEQ + 112, GITEE_RK, "#IKXR3A")
    _seed_event(BASE_SEQ + 113, GITEE_RK, "#IKXR3C")

    r = client.get("/api/tasks/current", headers=_task_headers(task_id, GITEE_RK))
    assert r.status_code == 200, r.text
    t = r.json()["task"]
    assert t["issue"]["issue_number"] == "IKXR3A"
    assert t["events_count"] == 2, f"events_count 未取到（随迁路径回退）：{t['events_count']}"
    assert t["latest_event"]["seq"] == BASE_SEQ + 112


def test_build_issue_context_alnum_ident_events_non_empty():
    """`build_issue_context` 随迁回归：字母数字 ident 的事件摘要必须非空。"""
    from src.api.event_archive import build_issue_context
    _seed_event(BASE_SEQ + 121, GITEE_RK, "#IKXR3A")
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE events SET summary=? WHERE seq=?",
                  ("IKXR3A 处理完成", BASE_SEQ + 121))
        db.commit()

    ctx = build_issue_context(GITEE_RK, "IKXR3A")
    assert "Issue #IKXR3A 档案摘要" in ctx
    assert "IKXR3A 处理完成" in ctx, f"事件摘要为空（按裸编号查桶回退）：{ctx!r}"
    # 取值不跨 ident：IKXR3B 不得看到 IKXR3A 的摘要
    assert "IKXR3A 处理完成" not in build_issue_context(GITEE_RK, "IKXR3B")
