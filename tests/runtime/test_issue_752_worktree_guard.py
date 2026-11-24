"""Issue #752（M2/M3）：worktree 冲突标记推送守卫 + 功能文件指纹保真校验测试。

覆盖（Spec 验收标准 AC2）：
- 暂存扫描：硬命中（阻断语义）/软命中/干净放行；未暂存文件不参与
- 指纹保真：快照 → 篡改/删除检出（modified/removed）；产物路径排除；恢复一致 → 空
- 基线文件在 worktree 外（不会被 add -A 误提交）
- repogit.ensure_issue_worktree 建/复用时自动保存基线
"""
import json
import os
import subprocess

import pytest

from src.domains.runtime.engine import repogit
from src.domains.runtime.engine import worktree_guard as wg


def _git(args, cwd):
    return subprocess.run(["git", "-C", cwd] + args, capture_output=True, text=True)


@pytest.fixture
def git_src(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _git(["init", "-b", "main"], str(src))
    _git(["config", "user.email", "test@example.com"], str(src))
    _git(["config", "user.name", "Test User"], str(src))
    (src / "runbook.md").write_text("v1\n", encoding="utf-8")
    (src / "code.py").write_text("print(1)\n", encoding="utf-8")
    os.makedirs(str(src / "dist"))
    (src / "dist" / "out.js").write_text("compiled\n", encoding="utf-8")
    _git(["add", "-A"], str(src))
    _git(["commit", "-m", "init"], str(src))
    return src


@pytest.fixture(autouse=True)
def _patch(git_src, tmp_path, monkeypatch):
    # workspace 根目录走 canonical kernel 配置（repo_key.workspace_repo_dir 调用时读取）
    monkeypatch.setattr("src.kernel.config.WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(repogit, "clone_url_for", lambda repo_key: str(git_src))
    yield


# ===================== 产物路径白名单 =====================

@pytest.mark.parametrize("path,expected", [
    ("dist/app.js", True), ("build/x.js", True), ("node_modules/a/b.js", True),
    (".skills/coding/SKILL.md", True), (".project-info/x.md", True),
    ("skills/issue/runbook.md", False), ("src/api/main.py", False), ("README.md", False),
])
def test_is_artifact_path(path, expected):
    assert wg.is_artifact_path(path) is expected


# ===================== 暂存冲突标记扫描 =====================

def test_scan_staged_hard_hit(tmp_path):
    wt = tmp_path / "wt"
    _git(["clone", str(tmp_path / "src"), str(wt)], str(tmp_path))
    (wt / "runbook.md").write_text("a\n<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> dev\n", encoding="utf-8")
    _git(["add", "-A"], str(wt))
    r = wg.scan_staged_files(str(wt))
    assert r["hard"] == {"runbook.md": 2}
    assert r["soft"] == {"runbook.md": 1}


def test_scan_staged_soft_only(tmp_path):
    wt = tmp_path / "wt"
    _git(["clone", str(tmp_path / "src"), str(wt)], str(tmp_path))
    (wt / "runbook.md").write_text("Title\n=======\n", encoding="utf-8")
    _git(["add", "-A"], str(wt))
    r = wg.scan_staged_files(str(wt))
    assert r["hard"] == {}
    assert r["soft"] == {"runbook.md": 1}


def test_scan_staged_clean_and_unstaged_ignored(tmp_path):
    wt = tmp_path / "wt"
    _git(["clone", str(tmp_path / "src"), str(wt)], str(tmp_path))
    # 干净改动（无标记）暂存 → 无命中
    (wt / "code.py").write_text("print(2)\n", encoding="utf-8")
    _git(["add", "code.py"], str(wt))
    # 未暂存的冲突残留不参与（守卫只看暂存区=将被提交的内容）
    (wt / "runbook.md").write_text("<<<<<<< HEAD\n", encoding="utf-8")
    r = wg.scan_staged_files(str(wt))
    assert r["hard"] == {} and r["soft"] == {}


def test_scan_staged_blob_not_disk_content(tmp_path):
    """PR #874 Optional：暂存后磁盘又被改出冲突标记——扫描对象是暂存 blob，不误报。"""
    wt = tmp_path / "wt"
    _git(["clone", str(tmp_path / "src"), str(wt)], str(tmp_path))
    # 暂存干净内容，随后磁盘写入冲突标记（未暂存）
    (wt / "code.py").write_text("print(2)\n", encoding="utf-8")
    _git(["add", "code.py"], str(wt))
    (wt / "code.py").write_text("<<<<<<< HEAD\nprint(2)\n=======\nprint(3)\n>>>>>>> dev\n", encoding="utf-8")
    r = wg.scan_staged_files(str(wt))
    assert r["hard"] == {} and r["soft"] == {}
    # 暂存含标记的 blob（即实际将提交的内容）→ 命中
    _git(["add", "code.py"], str(wt))
    r = wg.scan_staged_files(str(wt))
    assert r["hard"] == {"code.py": 2}


def test_scan_staged_unmerged_falls_back_to_disk(tmp_path):
    """unmerged 路径无单 stage blob → 降级扫工作区文件（git 冲突标记在工作区）。"""
    src = tmp_path / "src"
    _git(["checkout", "-b", "other", "HEAD"], str(src))
    (src / "code.py").write_text("print(99)\n", encoding="utf-8")
    _git(["commit", "-am", "other change"], str(src))
    _git(["checkout", "main"], str(src))
    (src / "code.py").write_text("print(2)\n", encoding="utf-8")
    _git(["commit", "-am", "main change"], str(src))
    wt = tmp_path / "wt"
    _git(["clone", str(src), str(wt)], str(tmp_path))
    _git(["-c", "user.email=test@example.com", "-c", "user.name=Test User",
          "merge", "origin/other"], str(wt))
    r = wg.scan_staged_files(str(wt))
    assert "code.py" in (r["hard"] or {})


# ===================== 指纹保真校验 =====================

def test_fidelity_detects_modify_and_remove(tmp_path):
    wt = tmp_path / "wt"
    _git(["clone", str(tmp_path / "src"), str(wt)], str(tmp_path))
    wg.save_baseline(str(wt))
    # 篡改功能文件 + 删除功能文件 + 改产物（产物不在保真范围）
    (wt / "runbook.md").write_text("damaged by cleanup\n", encoding="utf-8")
    os.remove(str(wt / "code.py"))
    (wt / "dist" / "out.js").write_text("regenerated\n", encoding="utf-8")
    r = wg.verify_fidelity(str(wt))
    assert r["modified"] == ["runbook.md"]
    assert r["removed"] == ["code.py"]


def test_fidelity_clean_when_unchanged(tmp_path):
    wt = tmp_path / "wt"
    _git(["clone", str(tmp_path / "src"), str(wt)], str(tmp_path))
    wg.save_baseline(str(wt))
    r = wg.verify_fidelity(str(wt))
    assert r["modified"] == [] and r["removed"] == []
    assert r["baseline_missing"] is False


def test_baseline_file_outside_worktree(tmp_path):
    """基线是 worktree 兄弟文件（<wt>.fidelity.json），git 不追踪（不被 add -A 提交）。"""
    wt = tmp_path / "wt"
    _git(["clone", str(tmp_path / "src"), str(wt)], str(tmp_path))
    snap = wg.save_baseline(str(wt))
    baseline_path = str(wt) + wg.BASELINE_SUFFIX
    assert os.path.isfile(baseline_path)
    assert os.path.dirname(baseline_path) == str(tmp_path)
    r = _git(["ls-files"], str(wt))
    assert ".fidelity.json" not in (r.stdout or "")
    assert "runbook.md" in snap and "dist/out.js" not in snap  # 产物排除
    # 基线可加载
    assert wg.load_baseline(str(wt)) == snap


def test_fidelity_without_baseline(tmp_path):
    wt = tmp_path / "wt"
    _git(["clone", str(tmp_path / "src"), str(wt)], str(tmp_path))
    r = wg.verify_fidelity(str(wt))
    assert r["baseline_missing"] is True


# ===================== repogit 接线：建/复用 worktree 自动保存基线 =====================

def test_ensure_issue_worktree_saves_baseline():
    wt = repogit.ensure_issue_worktree("gitcode:owner/repo", "qa", "5")
    assert wt
    baseline = wt + wg.BASELINE_SUFFIX
    assert os.path.isfile(baseline)
    with open(baseline, encoding="utf-8") as f:
        snap = json.load(f)
    assert "runbook.md" in snap and "dist/out.js" not in snap


def test_ensure_issue_worktree_reuse_resets_baseline():
    """复用（复测重跑）时重置基线=本次任务开始快照（清理前语义）。"""
    wt = repogit.ensure_issue_worktree("gitcode:owner/repo", "qa", "5")
    assert os.path.isfile(wt + wg.BASELINE_SUFFIX)
    # 任务内改动功能文件（旧基线会报警）
    with open(os.path.join(wt, "code.py"), "w", encoding="utf-8") as f:
        f.write("changed\n")
    assert wg.verify_fidelity(wt)["modified"] == ["code.py"]
    # 复测重跑：ensure 重新保存基线 → 本次任务起点一致
    wt2 = repogit.ensure_issue_worktree("gitcode:owner/repo", "qa", "5")
    assert wt2 == wt
    assert wg.verify_fidelity(wt)["modified"] == []

# ===== 2026-11 事故回归：未跟踪快照不得吞 .venv / node_modules，且快照要有保留策略 =====

@pytest.mark.parametrize("path,expected", [
    (".venv/bin/python", True),
    (".venv/lib/python3.12/site-packages/x.py", True),
    ("node_modules/react/index.js", True),
    ("app/__pycache__/m.cpython-312.pyc", True),
    ("dist/app.js", True),
    ("src/app.py", False),
    ("new_notes.txt", False),
])
def test_snapshot_excludes_rebuildable_dirs(path, expected):
    """可重建大目录不进快照；源码与新增文档仍要保全。"""
    assert wg._is_snapshot_excluded(path) is expected


def test_snapshot_untracked_skips_venv_without_marking_incomplete(tmp_path):
    """.venv 被跳过要记在 excluded，而非 skipped —— 否则归档被判不完整而拒绝清理。"""
    wt = tmp_path / "wt"
    os.makedirs(wt / ".venv" / "bin")
    (wt / ".venv" / "bin" / "python").write_text("bin\n", encoding="utf-8")
    (wt / "notes.txt").write_text("hello\n", encoding="utf-8")
    arc = tmp_path / "archive" / "20261124T000000Z-abcdef01"

    copied, skipped, excluded = wg._snapshot_untracked(
        str(wt), str(arc), [".venv/bin/python", "notes.txt"])

    assert copied == ["notes.txt"]
    assert skipped == []                      # 不阻断归档
    assert excluded == [".venv/bin/python"]
    assert (arc / "untracked" / "notes.txt").is_file()
    assert not (arc / "untracked" / ".venv").exists()
    rec = wg._build_reset_record(
        {"lost_commits": 0, "unpushed_count": 0, "dirty": [], "untracked": ["notes.txt"],
         "branch": "main", "head": "abc", "compare_ref": "origin/main"},
        "gitcode:o/r", 1, "coding", "", "", copied, skipped, "2026-11-24T00:00:00Z",
        "reset", "t_x", snap_excluded=excluded)
    assert rec["untracked_excluded"] == [".venv/bin/python"]
    assert rec["archive_complete"] is True


def test_prune_archive_snapshots_keeps_newest(tmp_path, monkeypatch):
    """每个 issue 只保留最新 N 份快照，旧的按时间序删除。"""
    parent = tmp_path / "issues" / "93"
    parent.mkdir(parents=True)
    for name in ("20261119T000000Z-aaaaaaa1", "20261120T000000Z-bbbbbbb2",
                 "20261121T000000Z-ccccccc3"):
        (parent / name).mkdir()
    (parent / "worktree.bundle").write_text("x", encoding="utf-8")
    monkeypatch.setenv("GRAPE_WORKTREE_ARCHIVE_KEEP", "2")

    assert wg._prune_archive_snapshots(str(parent)) == 1
    assert sorted(p.name for p in parent.iterdir()) == [
        "20261120T000000Z-bbbbbbb2", "20261121T000000Z-ccccccc3", "worktree.bundle"]

def _init_repo(path):
    os.makedirs(path, exist_ok=True)
    _git(["init", "-b", "main"], str(path))
    _git(["config", "user.email", "test@example.com"], str(path))
    _git(["config", "user.name", "Test User"], str(path))
    (path / "app.py").write_text("print(1)\n", encoding="utf-8")
    _git(["add", "-A"], str(path))
    _git(["commit", "-m", "init"], str(path))
    return path


def test_overflow_is_computed_after_excluding_rebuildable_dirs(tmp_path):
    """QA #2098 F1：.venv 的数千文件不得占用 2000 配额、也不得推高 overflow。

    否则 overflow>0 → skipped=[file_count_cap] → 归档被判不完整 → 仍然拒绝重置，
    事故主场景在 node_modules 仓库上会原样复发。
    """
    wt = _init_repo(tmp_path / "wt")
    venv = wt / ".venv" / "lib"
    venv.mkdir(parents=True)
    for i in range(2100):
        (venv / f"m{i}.py").write_text("x\n", encoding="utf-8")
    (wt / "notes.txt").write_text("hello\n", encoding="utf-8")

    risk = wg.collect_reset_risk(str(wt))

    assert risk["untracked"] == ["notes.txt"]
    assert risk["untracked_overflow"] == 0
    assert risk["untracked_count"] == 1
    assert risk["untracked_excluded_count"] >= 2100


def test_incomplete_archive_is_discarded_and_old_snapshots_pruned(tmp_path, monkeypatch):
    """QA #2098 F2：失败/不完整路径不得留残档，且仍要执行保留裁剪。"""
    parent = tmp_path / "issues" / "7"
    parent.mkdir(parents=True)
    for name in ("20261120T000000Z-aaaaaaa1", "20261121T000000Z-bbbbbbb2",
                 "20261122T000000Z-ccccccc3"):
        (parent / name).mkdir()
    monkeypatch.setenv("GRAPE_WORKTREE_ARCHIVE_KEEP", "2")

    def _fake_impl(*a, **kw):
        new_dir = parent / "20261124T000000Z-ddddddd4"
        new_dir.mkdir()
        (new_dir / "worktree.bundle").write_text("x", encoding="utf-8")
        return {"ok": False, "error": "untracked snapshot incomplete",
                "archive_dir": str(new_dir), "archived": True}

    monkeypatch.setattr(wg, "_archive_worktree_before_reset_impl", _fake_impl)
    res = wg.archive_worktree_before_reset(str(tmp_path), "gitcode:o/r", 7, "coding")

    assert res["ok"] is False
    assert res["archive_dir"] == ""
    assert not (parent / "20261124T000000Z-ddddddd4").exists()
    assert res["pruned"] >= 1
    assert len([d for d in parent.iterdir() if d.is_dir()]) == 2
