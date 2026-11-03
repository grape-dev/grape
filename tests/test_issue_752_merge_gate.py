"""Issue #752（M1/M3）：合入流程冲突标记扫描门禁测试。

覆盖（Spec 验收标准）：
- 扫描函数：硬命中（<<<<<<< / >>>>>>>）、软命中（孤立 =======）、误报豁免
  （markdown 标题下划线不阻断；正文中提及标记不命中；diff 只看新增行）
- merge 路由：硬命中 → 409 阻断 + 合入日志 blocked；软命中 → 放行 + warned/alarmed 日志；
  干净 PR → 放行 + passed 日志；门禁扫描异常 → fail-open 放行（存量合入不受影响）
- 合入日志表：grape_t_merge_gate_log 每次决策落库（AC3）
"""
import pytest
from fastapi.testclient import TestClient

from src.api.deps import get_current_user_or_api_key
from src.api.main import app
from src.api.merge_gate import run_merge_gate, scan_patch, scan_pull_files, scan_text

UID = 98752
OWNER = "o"
REPO = "hello"
BOT = "bot_tok_752"


@pytest.fixture(autouse=True)
def _client(monkeypatch):
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "merge_gate_tester", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "session_key": "s752",
        "repo_key": f"gitcode:{OWNER}/{REPO}"}
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: BOT)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _merge(client, number=9, **payload):
    return client.put(f"/api/git-repo/{OWNER}/{REPO}/pulls/{number}/merge", json=payload or None)


# ===================== 扫描函数 =====================

def test_scan_text_hard_hits():
    text = "line1\n<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> dev\nline2\n"
    r = scan_text(text)
    assert r["hard"] == 2   # <<<<<<< + >>>>>>>
    assert r["soft"] == 1   # =======


def test_scan_text_soft_only_not_hard():
    """孤立 ======= 是软命中（markdown 下划线同形，不参与阻断）。"""
    r = scan_text("Title\n=======\nbody")
    assert r["hard"] == 0
    assert r["soft"] == 1


def test_scan_text_inline_mentions_not_hits():
    """正文行内提及标记（非行首）不命中，防误报。"""
    text = "scan for >>>>>>> markers, see docs\nrun `=======` check\nsee <<<<<<< inline >>>>>>> mention"
    r = scan_text(text)
    assert r["hard"] == 0
    assert r["soft"] == 0


def test_scan_text_start_marker_no_label():
    """无标签的七字符标记也命中（git 冲突形态允许 <<<<<<< 行尾无内容）。"""
    r = scan_text("a\n<<<<<<<\nb\n>>>>>>>\n")
    assert r["hard"] == 2


def test_scan_patch_only_added_lines():
    """diff 只统计新增行；被删除的旧冲突标记不计。"""
    patch = ("@@ -1,3 +1,4 @@\n"
             "-<<<<<<< HEAD\n"
             "+<<<<<<< HEAD\n"
             "+=======\n"
             "+>>>>>>> dev\n"
             " context\n")
    r = scan_patch(patch)
    assert r["hard"] == 2
    assert r["soft"] == 1


def test_scan_pull_files_aggregates():
    files = [
        {"filename": "a.md", "patch": "+<<<<<<< HEAD\n+x\n"},
        {"filename": "b.md", "patch": "+=======\n"},
        {"filename": "bin.dat", "patch": None},          # 无 patch 跳过
        {"filename": "c.md", "patch": "+++ c.md\n@@\n+ok\n"},  # 文件头行不算新增内容
    ]
    r = scan_pull_files(files)
    assert r["hard_hits"] == {"a.md": 1}
    assert r["soft_hits"] == {"b.md": 1}


# ===================== merge 路由门禁 =====================

def _fake_req_dispatch(files_payload, merge_payload=None, get_error=None):
    def _req(method, url, token="", body=None):
        if "/files" in url:
            if get_error:
                raise get_error
            return files_payload
        return merge_payload or {"merged": True, "sha": "sha752"}
    return _req


def _gate_rows(number=9):
    from src.session_manager import db
    c = db.cursor()
    c.execute("SELECT phase, decision, hits_json FROM grape_t_merge_gate_log "
              "WHERE pr_number=? ORDER BY rowid", (number,))
    return c.fetchall()


def test_merge_blocked_on_hard_hit(monkeypatch, _client):
    """硬命中 → 409 阻断（AC1），上游 merge 未被调用，合入日志 blocked（AC3）。"""
    # 每用例独立 PR 号（合入日志按 pr_number 断言，防跨用例串扰）
    files = [{"filename": "runbook.md",
              "patch": "@@\n+<<<<<<< HEAD\n+step\n+=======\n+step2\n+>>>>>>> dev\n"}]
    monkeypatch.setattr("src.user.repo_manager._req", _fake_req_dispatch(files))
    r = _merge(_client, number=91)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "conflict-marker gate" in detail["message"]
    assert detail["gate"]["hard_hits"] == {"runbook.md": 2}
    rows = _gate_rows(91)
    assert rows and rows[0][0] == "pre_merge" and rows[0][1] == "blocked"
    assert "runbook.md" in rows[0][2]
    assert len(rows) == 1  # 阻断后无 post_merge 记录


def test_merge_soft_hit_warns_and_passes(monkeypatch, _client):
    """仅软命中（孤立 =======）→ 放行 + pre warned / post alarmed 日志。"""
    # 每用例独立 PR 号
    files = [{"filename": "doc.md", "patch": "@@\n+Title\n+=======\n"}]
    monkeypatch.setattr("src.user.repo_manager._req", _fake_req_dispatch(files))
    r = _merge(_client, number=92)
    assert r.status_code == 200
    rows = _gate_rows(92)
    assert [(x[0], x[1]) for x in rows] == [("pre_merge", "warned"), ("post_merge", "alarmed")]


def test_merge_clean_pr_passes(monkeypatch, _client):
    """干净 PR → 200 + pre passed / post passed 日志，返回体不变。"""
    # 每用例独立 PR 号
    files = [{"filename": "code.py", "patch": "@@\n+print('ok')\n"}]
    monkeypatch.setattr("src.user.repo_manager._req", _fake_req_dispatch(files))
    r = _merge(_client, number=93)
    assert r.status_code == 200
    assert r.json() == {"merged": True, "sha": "sha752"}
    rows = _gate_rows(93)
    assert [(x[0], x[1]) for x in rows] == [("pre_merge", "passed"), ("post_merge", "passed")]


def test_merge_gate_fail_open_on_scan_error(monkeypatch, _client):
    """门禁扫描异常 → fail-open 放行（error 决策落日志），存量合入不受影响。"""
    # 每用例独立 PR 号
    monkeypatch.setattr("src.user.repo_manager._req",
                        _fake_req_dispatch(None, get_error=RuntimeError("scan net down")))
    r = _merge(_client, number=94)
    assert r.status_code == 200
    rows = _gate_rows(94)
    decisions = [x[1] for x in rows]
    assert decisions[0] == "error"


def test_run_merge_gate_unit(monkeypatch):
    """run_merge_gate 单元：blocked/warned 决策与命中明细。"""
    monkeypatch.setattr("src.api.git_repo_api._vcs_req",
                        lambda p, m, u, t: [{"filename": "f.py", "patch": "+>>>>>>> x\n"}])
    g = run_merge_gate(OWNER, REPO, 7, "gitcode", BOT)
    assert g["decision"] == "blocked" and g["hard_hits"] == {"f.py": 1}

    monkeypatch.setattr("src.api.git_repo_api._vcs_req",
                        lambda p, m, u, t: [{"filename": "f.py", "patch": "+=======\n"}])
    g = run_merge_gate(OWNER, REPO, 7, "gitcode", BOT)
    assert g["decision"] == "warned" and g["soft_hits"] == {"f.py": 1}

    monkeypatch.setattr("src.api.git_repo_api._vcs_req", lambda p, m, u, t: [])
    assert run_merge_gate(OWNER, REPO, 7, "gitcode", BOT)["decision"] == "passed"


def test_record_gate_log_best_effort():
    """record_gate_log 异常不抛出（best-effort，不影响合入主流程）。"""
    from src.api import merge_gate
    # 正常写入一条
    merge_gate.record_gate_log("o/hello", 95, "pre_merge", "passed", {})
    rows = _gate_rows(95)
    assert rows and rows[-1][1] == "passed"
