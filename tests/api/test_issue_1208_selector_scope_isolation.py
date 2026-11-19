"""Issue #1208 追加修复：selector 候选集必须叠加 claim 第 ⑥ 级 scope 隔离。

2026-11-19 线上现象（beta，场景 356339a6…「通用开发-copy」issue 节点，selector
= engine:opencode）：场景详情页徽章「标签命中 6 个引擎 · 6 在线」，弹窗命中预览
「4 个引擎 · 4 在线」。差出来的 2 台是别人的 user 档引擎与别的仓库的 repo 档引擎
——claim_by_tag 的 scope SQL 永远认领不到本场景任务，但 selector_candidate_engines
此前对非 SRE 角色不做 owner/scope 判定，把它们算成命中。

后果不止展示：入队 fail-closed 门禁（selector_enqueue_failure_reason）用的是同一把
尺子，若某 selector 只有外档引擎命中，任务会照常入队、无人认领、卡到超时。

本文件锁定修复口径：候选集 = 真正可能认领到本场景任务的引擎集合。
"""
import datetime as _dt
import sqlite3
import threading

import pytest


@pytest.fixture()
def scope_db(monkeypatch):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.executescript("""
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT DEFAULT '', credential_id INTEGER DEFAULT 0,
        scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '', owner_name TEXT DEFAULT '',
        status TEXT DEFAULT 'offline', healthy INTEGER DEFAULT 1, installed INTEGER DEFAULT 1,
        last_seen_at TEXT, scene_tags TEXT DEFAULT '["default"]', role_tags TEXT DEFAULT '',
        engine_uid VARCHAR(36) DEFAULT NULL, org_id INTEGER DEFAULT 0
    );
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY, scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '',
        owner_name TEXT DEFAULT '', device_name TEXT DEFAULT '', status TEXT DEFAULT 'active'
    );
    CREATE TABLE engine_capabilities (
        runtime_id INTEGER, tag TEXT, max_concurrent INTEGER DEFAULT 4,
        priority INTEGER DEFAULT 50, enabled INTEGER DEFAULT 1,
        created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '',
        engine_uid VARCHAR(36) DEFAULT NULL
    );
    """)
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", c)
    monkeypatch.setattr(sm, "db_lock", threading.RLock())
    yield c
    c.close()


def _mk_engine(c, rid, name="opencode", cred=7, scope="repo", owner="o/r",
               status="online", cred_scope=None, cred_owner=None):
    seen = _dt.datetime.now(_dt.timezone.utc).isoformat()
    c.execute("INSERT INTO runtime_credentials (id, scope, owner_id, status) "
              "VALUES (?,?,?,'active')",
              (cred, cred_scope or scope, cred_owner if cred_owner is not None else owner))
    c.execute("INSERT INTO agent_runtimes (id, name, credential_id, scope, owner_id, status, "
              "healthy, installed, last_seen_at) VALUES (?,?,?,?,?,?,1,1,?)",
              (rid, name, cred, scope, owner, status, seen))
    # 能力行：'all' 让 claim 第 1 级能力匹配放行（selector 派生标签由 sync 补齐）
    c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)",
              (rid, "all"))
    from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
    st = c.execute("SELECT scene_tags FROM agent_runtimes WHERE id=?", (rid,)).fetchone()[0]
    sync_derived_agent_tags(c.cursor(), rid, name, scope, st)
    c.commit()


def _ids(matched):
    return [m["runtime_id"] for m in matched]


def _reason(dropped, rid):
    return next((d["reason"] for d in dropped if d["runtime_id"] == rid), "")


SEL = {"tags": ["engine:opencode"]}
REPO = "gitcode:And-ZJ2/snake-game8081"


def test_foreign_user_engine_not_counted(scope_db):
    """别人的 user 档引擎：标签命中但认领不到本用户任务 → reason=scope，不计命中。"""
    from src.domains.runtime.engine.engine_select import selector_candidate_engines
    _mk_engine(scope_db, 1, name="opencode", cred=11, scope="user", owner="3")
    matched, dropped = selector_candidate_engines(SEL, "issue", repo_key=REPO, user_id=57)
    assert _ids(matched) == []
    assert _reason(dropped, 1) == "scope"


def test_own_user_engine_counted(scope_db):
    from src.domains.runtime.engine.engine_select import selector_candidate_engines
    _mk_engine(scope_db, 1, name="opencode", cred=11, scope="user", owner="57")
    matched, dropped = selector_candidate_engines(SEL, "issue", repo_key=REPO, user_id=57)
    assert _ids(matched) == [1] and dropped == []


def test_other_repo_engine_not_counted(scope_db):
    """别的仓库的 repo 档引擎：claim 只认领本仓任务 → 不计命中。"""
    from src.domains.runtime.engine.engine_select import selector_candidate_engines
    _mk_engine(scope_db, 1, name="opencode", cred=21, scope="repo",
               owner="gitcode:And-ZJ2/hello-calculator")
    matched, dropped = selector_candidate_engines(SEL, "issue", repo_key=REPO, user_id=57)
    assert _ids(matched) == []
    assert _reason(dropped, 1) == "scope"


def test_same_repo_engine_counted_with_or_without_platform_prefix(scope_db):
    from src.domains.runtime.engine.engine_select import selector_candidate_engines
    _mk_engine(scope_db, 1, name="opencode", cred=21, scope="repo",
               owner="And-ZJ2/snake-game8081")          # 不带前缀
    _mk_engine(scope_db, 2, name="opencode", cred=22, scope="repo", owner=REPO)  # 带前缀
    matched, _dropped = selector_candidate_engines(SEL, "issue", repo_key=REPO, user_id=57)
    assert _ids(matched) == [1, 2]


def test_local_and_global_engines_counted(scope_db):
    """本机直连（credential_id=0）与 global 档不受 scope 隔离影响。"""
    from src.domains.runtime.engine.engine_select import selector_candidate_engines
    _mk_engine(scope_db, 1, name="opencode", cred=0, scope="global", owner="")
    _mk_engine(scope_db, 2, name="opencode", cred=31, scope="global", owner="And-ZJ2")
    matched, _dropped = selector_candidate_engines(SEL, "issue", repo_key=REPO, user_id=57)
    assert _ids(matched) == [1, 2]


def test_missing_context_keeps_previous_behavior(scope_db):
    """调用方没给 user_id / repo_key 时该维度不设限（宁放行不误杀）。"""
    from src.domains.runtime.engine.engine_select import selector_candidate_engines
    _mk_engine(scope_db, 1, name="opencode", cred=11, scope="user", owner="3")
    _mk_engine(scope_db, 2, name="opencode", cred=21, scope="repo", owner="gitcode:o/other")
    matched, _dropped = selector_candidate_engines(SEL, "issue")
    assert _ids(matched) == [1, 2]


def test_enqueue_gate_rejects_when_only_foreign_engines_match(scope_db):
    """只有外档引擎时，入队 fail-closed 必须生效（修复前会误判「有命中」而入队）。"""
    from src.domains.runtime.engine.engine_select import selector_enqueue_failure_reason
    _mk_engine(scope_db, 1, name="opencode", cred=11, scope="user", owner="3")
    _mk_engine(scope_db, 2, name="opencode", cred=21, scope="repo",
               owner="gitcode:And-ZJ2/hello-calculator")
    reason = selector_enqueue_failure_reason(SEL, "issue", repo_key=REPO, user_id=57)
    assert reason, "外档引擎不应被算作可承接命中"


def test_user_owner_non_numeric_blocked(scope_db):
    """owner_id 为用户名形态（非账号数字 id）→ 认领侧按 owner_name 反查账号，
    候选侧无法证明匹配，按未命中处理（QA 架构守护 I-3 反例）。"""
    from src.domains.runtime.engine.engine_select import selector_candidate_engines
    _mk_engine(scope_db, 1, name="opencode", cred=11, scope="user", owner="songspace")
    matched, dropped = selector_candidate_engines(SEL, "issue", repo_key=REPO, user_id=57)
    assert _ids(matched) == []
    assert _reason(dropped, 1) == "scope"


def test_repo_owner_with_prefix_does_not_leak_cross_platform(scope_db):
    """owner 自带平台前缀时，claim 的三候选拼出双前缀 → 跨平台同 owner/repo 认不到；
    候选侧按同一候选集合判定，不得放宽（QA 架构守护 I-2）。"""
    from src.domains.runtime.engine.engine_select import selector_candidate_engines
    _mk_engine(scope_db, 1, name="opencode", cred=21, scope="repo",
               owner="gitcode:o/r")
    # 同平台：repo_key 与 owner 逐字相等 → 命中
    matched, _dropped = selector_candidate_engines(SEL, "issue", repo_key="gitcode:o/r",
                                                   user_id=57)
    assert _ids(matched) == [1]
    # 跨平台同路径：claim 不会认领 → 候选同样不得算命中
    matched2, dropped2 = selector_candidate_engines(SEL, "issue", repo_key="github:o/r",
                                                    user_id=57)
    assert _ids(matched2) == []
    assert _reason(dropped2, 1) == "scope"


def test_credential_scope_wins_over_mirror_columns(scope_db):
    """QA 架构守护 I-1：档位以凭据列为准（claim 读 credentials），镜像列漂移不得放行。"""
    from src.domains.runtime.engine.engine_select import selector_candidate_engines
    # 镜像列写成 global（看起来可认领任意仓库），凭据实际是别的仓库的 repo 档
    _mk_engine(scope_db, 1, name="opencode", cred=21, scope="global", owner="",
               cred_scope="repo", cred_owner="gitcode:And-ZJ2/hello-calculator")
    matched, dropped = selector_candidate_engines(SEL, "issue", repo_key=REPO, user_id=57)
    assert _ids(matched) == []
    assert _reason(dropped, 1) == "scope"


def test_missing_credential_table_falls_back_to_mirror_columns(scope_db):
    """QA 架构守护 I-3：缺 runtime_credentials（旧库/精简 schema）时按镜像列继续判定，
    候选集不得因 join 失败退化成空集。"""
    from src.domains.runtime.engine.engine_select import selector_candidate_engines
    _mk_engine(scope_db, 1, name="opencode", cred=21, scope="repo", owner=REPO)
    scope_db.execute("DROP TABLE runtime_credentials")
    matched, _dropped = selector_candidate_engines(SEL, "issue", repo_key=REPO, user_id=57)
    assert _ids(matched) == [1]


def test_pr_subscription_enqueue_paths_thread_user_id():
    """PR 订阅 extra/redispatch 两条入队路径必须传 task user id（复审 finding）：

    否则 gate 按 user_id=0 走 fail-open，user 档外档引擎会骗过零命中门禁并入队。
    静态守护：两条路径的 gate 与任务行都用同一个 _sub_uid。
    """
    import ast
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "..", "src", "domains", "runtime",
                        "pipeline", "event_pipeline.py")
    src = open(os.path.abspath(path), encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in ("_dispatch_pr_subscription_extra", "_dispatch_pr_subscription_redispatch"):
        fn = funcs.get(name)
        assert fn is not None, f"{name} 不存在（函数改名需同步本守护）"
        seg = ast.get_source_segment(src, fn) or ""
        assert '_scene_pool_gate(scene_id, "qa", repo_key=repo_key, user_id=_sub_uid)' in seg, \
            f"{name}: _scene_pool_gate 未传 user_id"
        tail = seg.split("_enqueue_pipeline_task", 1)
        assert len(tail) == 2 and "user_id=_sub_uid" in tail[1][:400], \
            f"{name}: _enqueue_pipeline_task 未传 user_id（任务行与 gate 不同尺）"


def test_preview_endpoint_marks_foreign_engines_scope(scope_db):
    """预览接口与候选集同尺：外档引擎 hit=False / reason=scope（徽章不再多报）。"""
    from src.domains.mgmt.routes.scenes import EngineSelectorPreviewBody, engine_selector_preview
    _mk_engine(scope_db, 1, name="opencode", cred=11, scope="user", owner="3")
    _mk_engine(scope_db, 2, name="opencode", cred=21, scope="repo",
               owner="gitcode:And-ZJ2/hello-calculator")
    _mk_engine(scope_db, 3, name="opencode", cred=31, scope="global", owner="And-ZJ2")
    out = engine_selector_preview(
        EngineSelectorPreviewBody(tags=SEL["tags"], agent_type="issue",
                                  repo="And-ZJ2/snake-game8081"),
        user={"id": 57})
    assert out["matched"] == [3]
    assert out["engines"]["1"] == {"hit": False, "reason": "scope"}
    assert out["engines"]["2"] == {"hit": False, "reason": "scope"}
    assert out["engines"]["3"] == {"hit": True, "reason": ""}
