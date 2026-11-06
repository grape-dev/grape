"""Issue #993（M3）：合入门禁重建文档结构校验测试（AC4）。

- pr_number: 0 残留 / 元数据口径矛盾 → 硬阻断；
- 代码围栏不配平 → 软告警（不阻断）；
- 决策写入合入日志。
"""
import pytest
from fastapi import HTTPException

from src.api import merge_gate as mg


def test_pr_number_zero_is_hard():
    r = mg.scan_structure_text("meta:\npr_number: 0\n")
    assert r["pr_number_zero"] == 1
    assert r["fence_unbalanced"] is False


def test_metadata_conflict_is_hard():
    text = "pr_number: 12\nstatus: merged\nstatus: open\n"
    r = mg.scan_structure_text(text)
    assert r["metadata_conflicts"] == {"status": ["merged", "open"]}


def test_fence_unbalanced_is_soft():
    r = mg.scan_structure_text("```python\nprint(1)\n")
    assert r["fence_unbalanced"] is True


def test_balanced_fences_clean():
    r = mg.scan_structure_text("```python\nprint(1)\n```\n")
    assert r["fence_unbalanced"] is False
    assert r["pr_number_zero"] == 0
    assert r["metadata_conflicts"] == {}


def test_structure_patch_only_added_lines():
    patch = ("@@ -1,1 +1,2 @@\n"
             "-pr_number: 0\n"
             "+pr_number: 12\n"
             "+```\n")
    r = mg.scan_structure_patch(patch)
    assert r["pr_number_zero"] == 0
    assert r["fence_unbalanced"] is True


def test_scan_structure_files_aggregates():
    files = [
        {"filename": "docs/a.md", "patch": {"diff": "@@\n+pr_number: 0\n"}},
        {"filename": "docs/b.md", "patch": "@@\n+```\n+code\n"},
        {"filename": "src/x.py", "patch": "@@\n+print(1)\n"},
    ]
    r = mg.scan_structure_files(files)
    assert r["hard_hits"] == {"docs/a.md": 1}
    assert r["soft_hits"] == {"docs/b.md": 1}


def test_run_merge_gate_blocks_structural_defect(monkeypatch):
    monkeypatch.setattr(mg, "_pull_files_all_pages", lambda *a, **k: [
        {"filename": "docs/spec.md", "patch": "@@\n+pr_number: 0\n"}])
    monkeypatch.setattr(mg, "_pull_head_ref", lambda *a, **k: "")
    r = mg.run_merge_gate("o", "r", 1, "gitcode", "tok")
    assert r["decision"] == "blocked"
    assert r["structure_hard_hits"] == {"docs/spec.md": 1}
    # 仅结构缺陷时不得混报为冲突标记命中
    assert r["conflict_hard_hits"] == {}


def test_python_annotations_and_assignments_not_metadata():
    """Python 注解/赋值不应被判为元数据矛盾（回归 QA 自阻断）。"""
    files = [{"filename": "src/x.py", "patch": (
        "@@\n"
        "+def f(pr_number: int = 0):\n"
        "+    state = \"merged\"\n"
        "+    state = \"open\"\n")}]
    r = mg.scan_structure_files(files)
    assert r["hard_hits"] == {} and r["soft_hits"] == {}


def test_doc_prose_reference_not_hard_defect():
    """文档正文里的 `pr_number: 0` 引用（非行首元数据）不应自阻断。"""
    files = [{"filename": "docs/issue-993/spec.md", "patch": (
        "@@\n"
        "+- M3 结构校验：扫描 `pr_number: 0` 残留与元数据矛盾。\n")}]
    r = mg.scan_structure_files(files)
    assert r["hard_hits"] == {}


def test_non_doc_file_out_of_scope():
    files = [{"filename": "src/api/merge_gate.py", "patch": "@@\n+status: merged\n+status: open\n"}]
    r = mg.scan_structure_files(files)
    assert r["hard_hits"] == {}


def test_run_merge_gate_warns_fence_imbalance(monkeypatch):
    monkeypatch.setattr(mg, "_pull_files_all_pages", lambda *a, **k: [
        {"filename": "docs/spec.md", "patch": "@@\n+```\n+code\n"}])
    monkeypatch.setattr(mg, "_pull_head_ref", lambda *a, **k: "")
    r = mg.run_merge_gate("o", "r", 1, "gitcode", "tok")
    assert r["decision"] == "warned"
    assert r["structure_soft_hits"] == {"docs/spec.md": 1}


def test_run_merge_gate_passes_clean(monkeypatch):
    monkeypatch.setattr(mg, "_pull_files_all_pages", lambda *a, **k: [
        {"filename": "src/x.py", "patch": "@@\n+print(1)\n"}])
    r = mg.run_merge_gate("o", "r", 1, "gitcode", "tok")
    assert r["decision"] == "passed"


def test_gate_decision_recorded_to_log():
    from src.session_manager import db
    try:
        db.execute("DELETE FROM grape_t_merge_gate_log")
        db.commit()
    except Exception:
        pass
    mg.record_gate_log("gitcode:o/r", 9, "pre_merge", "blocked",
                       {"structure_hard_hits": {"docs/spec.md": 1}})
    rows = db.execute(
        "SELECT decision, hits_json FROM grape_t_merge_gate_log "
        "WHERE repo_key=? AND pr_number=?", ("gitcode:o/r", 9)).fetchall()
    assert rows and rows[-1][0] == "blocked"
    assert "structure_hard_hits" in rows[-1][1]


def test_fenced_examples_are_ignored():
    """代码围栏内的 YAML/JSON 示例不得判为元数据矛盾/硬缺陷。"""
    text = (
        "```yaml\n"
        "status: open\n"
        "status: closed\n"
        "pr_number: 0\n"
        "```\n"
    )
    r = mg.scan_structure_text(text)
    assert r["pr_number_zero"] == 0
    assert r["metadata_conflicts"] == {}
    assert r["fence_unbalanced"] is False


def test_fenced_example_not_hard_in_patch():
    patch = ("@@ -1,1 +1,7 @@\n"
             "+```yaml\n"
             "+status: open\n"
             "+status: closed\n"
             "+pr_number: 0\n"
             "+```\n")
    r = mg.scan_structure_patch(patch)
    assert r["pr_number_zero"] == 0
    assert r["metadata_conflicts"] == {}
    assert r["fence_unbalanced"] is False


def test_added_lines_inside_existing_fence_are_ignored():
    """围栏开标记在 context、新增行位于围栏内时不得判元数据硬缺陷。"""
    patch = ("@@ -1,4 +1,6 @@\n"
             " ```yaml\n"
             " status: open\n"
             "+status: closed\n"
             "+pr_number: 0\n"
             " ```\n")
    r = mg.scan_structure_patch(patch)
    assert r["pr_number_zero"] == 0
    assert r["metadata_conflicts"] == {}
    assert r["fence_unbalanced"] is False


def test_added_line_after_existing_fence_is_still_hard():
    """既有围栏闭合后的新增元数据仍应判定（围栏上下文不能放过真实残留）。"""
    patch = ("@@ -1,3 +1,4 @@\n"
             " ```\n"
             " example\n"
             " ```\n"
             "+pr_number: 0\n")
    r = mg.scan_structure_patch(patch)
    assert r["pr_number_zero"] == 1


def test_metadata_conflict_with_existing_context_value():
    """PR 新增值必须与文件既有（context）值比对，不能只看新增行。"""
    patch = ("@@ -1,3 +1,4 @@\n"
             " meta:\n"
             " status: merged\n"
             "+status: open\n"
             " issue_number: 42\n")
    r = mg.scan_structure_patch(patch)
    assert r["metadata_conflicts"] == {"status": ["merged", "open"]}


def test_metadata_conflict_not_reported_without_added_key():
    """仅路过已有矛盾文档（新增行不含该 key）不应阻断。"""
    patch = ("@@ -1,3 +1,4 @@\n"
             " status: merged\n"
             " status: open\n"
             "+notes: unrelated\n")
    r = mg.scan_structure_patch(patch)
    assert r["metadata_conflicts"] == {}


def test_removed_conflicting_value_is_not_conflict():
    """删除旧值后新增单一值不构成矛盾。"""
    patch = ("@@ -1,2 +1,2 @@\n"
             "-status: merged\n"
             "+status: open\n")
    r = mg.scan_structure_patch(patch)
    assert r["metadata_conflicts"] == {}


# ===================== 复审修复：hunk 之外既有值比对 =====================

def test_metadata_conflict_beyond_hunk_with_full_file_text():
    """新增值须与文件全文既有值比对：既有值在 hunk 之外也能检出矛盾。"""
    patch = ("@@ -40,1 +40,2 @@\n"
             "+status: open\n"
             "+notes: unrelated\n")
    # patch-only（无全文）保守口径：hunk 外既有值看不到
    assert mg.scan_structure_patch(patch)["metadata_conflicts"] == {}
    file_text = ("line one\nstatus: merged\n" + "filler\n" * 50
                 + "status: open\nnotes: unrelated\n")
    got = mg.scan_structure_patch(patch, file_text=file_text)
    assert got["metadata_conflicts"] == {"status": ["merged", "open"]}


def test_run_merge_gate_compares_full_file_beyond_hunk(monkeypatch):
    """run_merge_gate 取 PR head 全文后，hunk 之外的既有值矛盾仍硬阻断。"""
    monkeypatch.setattr(mg, "_pull_files_all_pages", lambda *a, **k: [
        {"filename": "docs/spec.md", "patch": (
            "@@ -40,1 +40,2 @@\n+status: open\n+notes: unrelated\n")}])
    monkeypatch.setattr(mg, "_pull_head_ref", lambda *a, **k: "headsha")
    monkeypatch.setattr(mg, "_fetch_rebuilt_doc_text", lambda *a, **k: (
        "line one\nstatus: merged\n" + "filler\n" * 50
        + "status: open\nnotes: unrelated\n"))
    r = mg.run_merge_gate("o", "r", 1, "gitcode", "tok")
    assert r["decision"] == "blocked"
    assert r["structure_hard_hits"] == {"docs/spec.md": 1}


def test_run_merge_gate_falls_back_to_patch_when_file_text_unavailable(monkeypatch):
    """取全文失败时回退 patch 口径，不因读取失败误阻断/漏扫既有行为。"""
    monkeypatch.setattr(mg, "_pull_files_all_pages", lambda *a, **k: [
        {"filename": "docs/spec.md", "patch": "@@\n+pr_number: 0\n"}])
    monkeypatch.setattr(mg, "_pull_head_ref", lambda *a, **k: "headsha")
    monkeypatch.setattr(mg, "_fetch_rebuilt_doc_text", lambda *a, **k: "")
    r = mg.run_merge_gate("o", "r", 1, "gitcode", "tok")
    assert r["decision"] == "blocked"
    assert r["structure_hard_hits"] == {"docs/spec.md": 1}


# ===================== 复审修复：merge_pull 结构命中留痕 =====================

def _run_merge_pull_blocked(monkeypatch, gate, p="gitcode"):
    import src.api.git_repo_api as gra
    captured = []

    monkeypatch.setattr(mg, "get_pull_base",
                        lambda *a, **k: {"ref": "weekly-mys", "sha": "base0"})
    monkeypatch.setattr(mg, "run_merge_gate", lambda *a, **k: gate)
    monkeypatch.setattr(mg, "record_gate_log", lambda *a, **k: None)
    monkeypatch.setattr(gra, "_repo_token", lambda *a, **k: "tok")

    def _vcs_req(provider, method, url, token, body=None):
        captured.append((method, url, body))
        return {}

    monkeypatch.setattr(gra, "_vcs_req", _vcs_req)
    with pytest.raises(HTTPException) as ei:
        gra.merge_pull("o", "r", 1, body=None, user={"username": "u"}, p=p)
    return ei.value, captured


def _trace_comments(captured):
    return [body for method, url, body in captured
            if method == "POST" and "/comments" in url and body]


def test_merge_pull_structure_only_emits_structural_trace(monkeypatch):
    """仅结构缺陷命中时也必须发留痕评论，且文案不得误称冲突标记（原 NameError）。"""
    gate = {"decision": "blocked", "hard_hits": {"docs/spec.md": 2},
            "soft_hits": {}, "conflict_hard_hits": {},
            "structure_hard_hits": {"docs/spec.md": 2}}
    err, captured = _run_merge_pull_blocked(monkeypatch, gate)
    assert err.status_code == 409
    assert "structural gate (Issue #993)" in err.detail["message"]
    assert "conflict-marker gate" not in err.detail["message"]
    bodies = _trace_comments(captured)
    assert bodies, "结构命中必须发出 PR 留痕评论"
    body = bodies[0]["body"]
    assert "structural defects" in body and "docs/spec.md" in body
    assert "conflict markers" not in body.lower()


def test_merge_pull_conflict_only_trace_keeps_conflict_wording(monkeypatch):
    """仅冲突标记命中时留痕评论仍按冲突门禁措辞。"""
    gate = {"decision": "blocked", "hard_hits": {"runbook.md": 2},
            "soft_hits": {}, "conflict_hard_hits": {"runbook.md": 2},
            "structure_hard_hits": {}}
    err, captured = _run_merge_pull_blocked(monkeypatch, gate)
    assert err.status_code == 409
    assert "conflict-marker gate (Issue #752)" in err.detail["message"]
    body = _trace_comments(captured)[0]["body"]
    assert "conflict markers" in body and "runbook.md" in body
    assert "structural defects" not in body


def test_merge_pull_both_sources_trace_both(monkeypatch):
    """冲突标记与结构缺陷同时命中时，留痕评论分别归因。"""
    gate = {"decision": "blocked", "hard_hits": {"runbook.md": 1, "docs/spec.md": 1},
            "soft_hits": {}, "conflict_hard_hits": {"runbook.md": 1},
            "structure_hard_hits": {"docs/spec.md": 1}}
    err, captured = _run_merge_pull_blocked(monkeypatch, gate)
    assert err.status_code == 409
    body = _trace_comments(captured)[0]["body"]
    assert "conflict markers" in body and "runbook.md" in body
    assert "structural defects" in body and "docs/spec.md" in body
