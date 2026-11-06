"""Issue #993（M3）：合入门禁重建文档结构校验测试（AC4）。

- pr_number: 0 残留 / 元数据口径矛盾 → 硬阻断；
- 代码围栏不配平 → 软告警（不阻断）；
- 决策写入合入日志。
"""
import pytest

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
    r = mg.run_merge_gate("o", "r", 1, "gitcode", "tok")
    assert r["decision"] == "blocked"
    assert r["structure_hard_hits"] == {"docs/spec.md": 1}


def test_run_merge_gate_warns_fence_imbalance(monkeypatch):
    monkeypatch.setattr(mg, "_pull_files_all_pages", lambda *a, **k: [
        {"filename": "docs/spec.md", "patch": "@@\n+```\n+code\n"}])
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