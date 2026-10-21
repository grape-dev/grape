"""obs_log（日志热数据）测试：脱敏 / 上下文抽取 / 入库幂等 / 用户面可见性 / 留存 / 路由。

钉死四件容易回归的事：
1. **只落 WARNING/ERROR**（INFO 丢弃），同批重复上报幂等（worker 心跳天然会重传）
2. **凭据类信息入库前就打码**（token / 密码 / URL 里的口令），非凭据内容不动
3. **用户面只出可见仓库的日志**：他人仓库不可见；无 repo 维度的主控/平台日志**不出现**
4. 越权 repo → RepoNotVisible（路由 404）；留存按天清理
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.observability import logs as lg  # noqa: E402

UID = 9911
REPO = "gitcode:org/log-mine"
OTHER = "gitcode:org/log-theirs"
MARK = "lgt"          # 本文件写入行的标记，便于清理


def _clean():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM obs_log WHERE source LIKE ? OR msg LIKE ?", (MARK + "%", "%" + MARK + "%"))
            c.execute("DELETE FROM user_repos WHERE user_id=?", (UID,))
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE ?", (MARK + "%",))
        except Exception:
            pass
        db.commit()


def _grant(repo_key):
    from src.session_manager import db, db_lock
    owner, _, name = repo_key.split(":", 1)[1].partition("/")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join)"
                  " VALUES (?,?,?,?,1,1)", (UID, repo_key, owner, name))
        db.commit()


def _mk_cred(name, token, device="dev-lg"):
    """创建主动上报用的 worker 凭据；返回凭据 id。"""
    import hashlib
    from src.session_manager import db, db_lock
    h = hashlib.sha256(token.encode()).hexdigest()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO runtime_credentials (token_hash, name, device_name, scope, status)"
                  " VALUES (?,?,?,'global','active')", (h, MARK + name, device))
        db.commit()
        c.execute("SELECT id FROM runtime_credentials WHERE token_hash=?", (h,))
        return int(c.fetchone()[0])


def _rows(**kw):
    from src.session_manager import db, db_lock
    sql = "SELECT * FROM obs_log"
    conds, params = [], []
    for k, v in kw.items():
        conds.append(f"{k}=?")
        params.append(v)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    with db_lock:
        c = db.cursor()
        c.execute(sql, tuple(params))
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, r)) for r in c.fetchall()]


@pytest.fixture(autouse=True)
def _setup():
    _clean()
    yield
    _clean()


# ---------------------------------------------------------------- 脱敏

def test_redact_masks_credentials_only():
    s1 = "调用失败 token=grape_ABCdef123456 gcw_zzz999 handled"
    out = lg.redact(s1)
    assert "grape_ABCdef123456" not in out and "gcw_zzz999" not in out
    assert "***" in out
    assert lg.redact("password=Sup3rSecret!") == "password=***"
    assert "://user:***@" in lg.redact("mysql://user:pass@10.0.0.1:3306/db")
    assert "Bearer ***" in lg.redact("Authorization: Bearer abc.def.ghi")
    assert lg.redact(lg.redact(s1)) == lg.redact(s1), "脱敏应幂等"
    # 非凭据内容不动（IP 保留：设备页本来就对用户可见，打掉反而没法排障）
    plain = "worker 10.0.0.2 心跳失败 code=500"
    assert lg.redact(plain) == plain


def test_extract_context_from_worker_line():
    line = ("[task t_deadbeef00] [seq=588|issue:#1|agent:coding|"
            "repo:gitcode:requiredsa/hello-html] engine crashed")
    task_id, repo = lg.extract_context(line)
    assert task_id == "t_deadbeef00"
    assert repo == "gitcode:requiredsa/hello-html"
    # 无 provider 前缀时补默认
    _, repo2 = lg.extract_context("... repo:owner/name ...")
    assert repo2 == "gitcode:owner/name"
    assert lg.extract_context("no context here") == ("", "")


# ---------------------------------------------------------------- 入库

def test_ingest_only_warning_error_and_is_idempotent():
    recs = [
        {"ts": "2026-10-17T10:00:00+00:00", "level": "INFO", "msg": f"info line {MARK}", "source": MARK + "s"},
        {"ts": "2026-10-17T10:00:01+00:00", "level": "WARNING", "msg": f"warn line {MARK}", "source": MARK + "s"},
        {"ts": "2026-10-17T10:00:02+00:00", "level": "ERROR", "msg": f"err line {MARK}", "source": MARK + "s"},
    ]
    n = lg.ingest(recs)
    assert n == 2, "INFO 必须丢弃，只落 W/E"
    assert len(_rows(source=MARK + "s")) == 2
    # 重放同批：幂等
    assert lg.ingest(recs) == 0
    assert len(_rows(source=MARK + "s")) == 2, "重复上报不得重复落行"


def test_ingest_redacts_and_extracts_context():
    rec = {"ts": "2026-10-17T10:01:00+00:00", "level": "ERROR", "source": MARK + "w",
           "msg": f"[task t_deadbeef00] [seq=1|agent:coding|repo:{REPO}] token=grape_SECRETVALUE12345 失败 {MARK}"}
    assert lg.ingest([rec]) == 1
    row = _rows(source=MARK + "w")[0]
    assert row["task_id"] == "t_deadbeef00"
    assert row["repo_key"] == REPO, "应从正文抽出仓库，否则用户面看不到"
    assert "grape_SECRETVALUE12345" not in row["msg"], "入库前必须打码"


# ---------------------------------------------------------------- 用户面可见性

def _seed_scoped():
    lg.ingest([
        {"ts": "2026-10-17T10:02:00+00:00", "level": "ERROR", "source": MARK + "a",
         "repo_key": REPO, "msg": f"mine {MARK}"},
        {"ts": "2026-10-17T10:02:01+00:00", "level": "ERROR", "source": MARK + "b",
         "repo_key": OTHER, "msg": f"theirs {MARK}"},
        {"ts": "2026-10-17T10:02:02+00:00", "level": "ERROR", "source": MARK + "c",
         "repo_key": "", "msg": f"master 进程级 {MARK}"},
    ])


def test_query_only_returns_visible_repo_logs():
    _grant(REPO)
    _seed_scoped()
    user = {"id": UID, "username": "u", "org_id": 0}
    res = lg.query(user, limit=100)
    keys = {i["repo_key"] for i in res["items"]}
    assert REPO in keys
    assert OTHER not in keys, "他人仓库日志不可见"
    assert "" not in keys, "无 repo 维度的主控/平台日志不进用户面"
    assert res["total"] == 1


def test_query_out_of_scope_repo_raises():
    _grant(REPO)
    user = {"id": UID, "username": "u", "org_id": 0}
    with pytest.raises(Exception):
        lg.query(user, repo="org/log-theirs")


def test_query_filters_level_keyword_and_scope():
    _grant(REPO)
    lg.ingest([
        {"ts": "2026-10-17T10:03:00+00:00", "level": "WARNING", "source": MARK + "f",
         "repo_key": REPO, "msg": f"alpha {MARK}"},
        {"ts": "2026-10-17T10:03:01+00:00", "level": "ERROR", "source": MARK + "f",
         "repo_key": REPO, "msg": f"beta {MARK}"},
    ])
    user = {"id": UID, "username": "u", "org_id": 0}
    only_err = lg.query(user, level="ERROR", q="beta")
    assert only_err["total"] == 1 and only_err["items"][0]["msg"].endswith(MARK)
    assert only_err["facets"]["levels"] == {"ERROR": 1}
    only_warn = lg.query(user, level="WARNING")
    assert only_warn["total"] == 1
    # 分页
    page1 = lg.query(user, limit=1, offset=0)
    page2 = lg.query(user, limit=1, offset=1)
    assert page1["items"][0]["id"] != page2["items"][0]["id"]


def test_no_visible_repo_returns_empty():
    user = {"id": 424242, "username": "nobody", "org_id": 0}
    res = lg.query(user)
    assert res["total"] == 0 and res["items"] == []


# ---------------------------------------------------------------- 留存

def test_cleanup_removes_expired_rows_only():
    old = (datetime.now(timezone.utc) - timedelta(days=lg.RETENTION_DAYS + 2)).isoformat()
    new = datetime.now(timezone.utc).isoformat()
    lg.ingest([
        {"ts": old, "level": "ERROR", "source": MARK + "old", "repo_key": REPO, "msg": f"old {MARK}"},
        {"ts": new, "level": "ERROR", "source": MARK + "new", "repo_key": REPO, "msg": f"new {MARK}"},
    ])
    assert len(_rows(source=MARK + "old")) == 1
    n = lg.cleanup()
    assert n >= 1
    assert _rows(source=MARK + "old") == []
    assert len(_rows(source=MARK + "new")) == 1


# ---------------------------------------------------------------- 路由

def test_routes_logs_query_ingest_and_auth():
    from fastapi.testclient import TestClient
    from src.api.deps import require_org_member
    from src.api.main import app

    _grant(REPO)
    _seed_scoped()
    token = "rta_" + MARK + "token123456"
    _mk_cred("w1", token, device="dev-" + MARK)

    user = {"id": UID, "username": "u", "org_id": 0, "org_role": "owner"}
    app.dependency_overrides[require_org_member] = lambda: user
    try:
        with TestClient(app) as cli:
            # 用户面检索：只出自己仓库
            r = cli.get("/api/obs/logs")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["total"] == 1 and body["items"][0]["repo_key"] == REPO
            # 越权仓库 → 404
            r = cli.get("/api/obs/logs", params={"repo": "org/log-theirs"})
            assert r.status_code == 404, r.text
            # 来源 facet
            r = cli.get("/api/obs/logs/sources")
            assert r.status_code == 200 and isinstance(r.json()["sources"], list)
    finally:
        app.dependency_overrides.pop(require_org_member, None)

    # worker 上报：无凭据 401；带凭据成功且 source = device_name
    with TestClient(app) as cli:
        r = cli.post("/api/obs/logs/ingest", json={"records": [{"level": "ERROR", "msg": "x"}]})
        assert r.status_code == 401, r.text
        r = cli.post("/api/obs/logs/ingest",
                     headers={"Authorization": f"Bearer {token}"},
                     json={"records": [
                         {"level": "ERROR", "msg": f"[task t_cafebabe01] [repo:{REPO}] boom {MARK}"},
                         {"level": "INFO", "msg": f"ignored {MARK}"},
                     ]})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["ingested"] == 1, j
        assert j["source"] == "dev-" + MARK, j
    rows = _rows(source="dev-" + MARK)
    assert len(rows) == 1 and rows[0]["task_id"] == "t_cafebabe01" and rows[0]["repo_key"] == REPO