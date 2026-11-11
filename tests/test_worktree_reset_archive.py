"""Issue #2041：worktree 重置前置保全（reset archive guard）测试。

验收覆盖（docs/issue-2041/spec.md，沿用 Spec a-d）：
- a) 存在未推送 commit / 未提交改动时，删除先归档（bundle 可 `git fetch` +
     `git reset --hard FETCH_HEAD` 完整恢复）；归档失败拒绝删除（目录保留）
- b) 重置记录机器可读（worktree_reset / lost_commits / bundle_path /
     archived_at / reset_reason），下一轮任务注入 env（GRAPE_WORKTREE_RESET*）+ prompt
- c) 无风险路径零改动：不产 bundle、不产记录，既有清理语义保持（repogit 旧用例不回归）
- d) upstream 与 HEAD 一致（已推送）且干净 → 零副作用
失败路径：bundle create 失败 / bundle verify 失败 / 守卫异常 → 拒绝删除保留现场
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
    """本地源仓库（含一个 commit），并让 repogit 的 clone_url 指向它（同 test_repogit）。"""
    src = tmp_path / "src"
    src.mkdir()
    _git(["init", "-b", "main"], str(src))
    _git(["config", "user.email", "test@example.com"], str(src))
    _git(["config", "user.name", "Test User"], str(src))
    (src / "a.txt").write_text("v1", encoding="utf-8")
    _git(["add", "-A"], str(src))
    _git(["commit", "-m", "init"], str(src))
    return src


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """隔离 workspace：仓库工作区根目录指到 tmp（archive 落在 worktree 之外）。"""
    monkeypatch.setattr("src.kernel.config.WORKSPACE_DIR", str(tmp_path / "ws"))
    return str(tmp_path / "ws")


@pytest.fixture
def repo(git_src, ws, monkeypatch):
    """gitcode:owner/repo 检出环境：bare mirror + issue worktree 就绪。"""
    rk = "gitcode:owner/repo"
    monkeypatch.setattr(repogit, "clone_url_for", lambda repo_key: str(git_src))
    return rk


def _wt(rk, num="7", agent="coding"):
    wt = repogit.ensure_issue_worktree(rk, agent, num)
    assert wt and os.path.isdir(wt)
    # worktree 无全局身份时 commit 会被拒（测试机 root 无 git identity），显式配置
    _git(["config", "user.email", "test@example.com"], wt)
    _git(["config", "user.name", "Test User"], wt)
    return wt


def _commit(wt, msg, *files):
    for rel, text in files:
        p = os.path.join(wt, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    _git(["add", "-A"], wt)
    r = _git(["commit", "-m", msg], wt)
    assert r.returncode == 0, r.stderr


# ===================== 前置检查（风险判定） =====================

def test_clean_worktree_no_risk(repo):
    """c/d：全新 worktree（HEAD==origin/main、干净）→ 无风险、无副作用。"""
    wt = _wt(repo)
    r = wg.collect_reset_risk(wt)
    assert r["risk"] is False
    assert r["unpushed_count"] == 0
    assert r["dirty"] == [] and r["untracked"] == []


def test_platform_light_files_whitelisted(repo):
    """平台物化轻文件（askpass/.skills/.project-info/.knowledge/marker）不触发归档。"""
    wt = _wt(repo, "11")
    for rel in (".git-askpass.sh", ".git-askpass-wrapper.sh", ".grape-workspace.json",
                ".skills/coding/SKILL.md", ".project-info/p.md", ".knowledge/k.md"):
        p = os.path.join(wt, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("x\n")
    assert wg.collect_reset_risk(wt)["risk"] is False
    assert repogit.cleanup_issue_worktree(repo, "coding", "11") is True
    assert wg.load_reset_record(repo, "11", "coding") is None, "无风险不应产记录"


def test_untracked_file_is_risk(repo):
    """未跟踪文件（白名单外）命中风险；-uall 展开到文件级。"""
    wt = _wt(repo, "12")
    p = os.path.join(wt, "packages", "newpkg", "a.py")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("impl\n")
    r = wg.collect_reset_risk(wt)
    assert r["risk"] is True
    assert "packages/newpkg/a.py" in r["untracked"]
    assert r["unpushed_count"] == 0


def test_unpushed_commit_is_risk_with_lost_commits(repo):
    """未推送 commit 计入 lost_commits（worktree add -b 自动追踪 origin/main →
    @{upstream} 主路径生效；unpushed_count 相对基线为 1）。"""
    wt = _wt(repo, "13")
    _commit(wt, "unpushed work", ("packages/newpkg/a.py", "impl\n"))
    head = _git(["rev-parse", "HEAD"], wt).stdout.strip()
    r = wg.collect_reset_risk(wt)
    assert r["risk"] is True
    assert r["unpushed_count"] == 1
    assert r["lost_commits"] == [head]
    assert r["compare_ref"] == "@{upstream}", "上游为主判定路径"


def test_pushed_with_upstream_no_risk(repo, git_src):
    """d：push 成功 + upstream 一致 + 干净 → 零风险零副作用。"""
    wt = _wt(repo, "14")
    _commit(wt, "pushed work", ("b.txt", "v2\n"))
    assert _git(["push", "origin", "HEAD:refs/heads/feat/issue-14"], wt).returncode == 0
    _git(["branch", "--set-upstream-to=origin/feat/issue-14"], wt)
    r = wg.collect_reset_risk(wt)
    assert r["risk"] is False and r["unpushed_count"] == 0
    assert r["compare_ref"] == "@{upstream}"
    assert repogit.cleanup_issue_worktree(repo, "coding", "14") is True
    assert not os.path.isdir(wt)
    assert not os.path.exists(wg.reset_archive_root(repo)), "已推送干净路径不应产生归档"


def test_no_compare_ref_falls_back_to_remotes(repo):
    """无 upstream 且无基线 ref：`rev-list HEAD --not --remotes` 兜底判定。"""
    wt = _wt(repo, "15")
    # 剥掉全部对比 ref（bare mirror refs/remotes/origin/* 与本地 main/master），
    # 构造「无任何候选 ref」环境
    base = repogit.repo_base_dir(repo)
    for ref in ("refs/remotes/origin/main", "refs/remotes/origin/master",
               "refs/heads/main", "refs/heads/master"):
        _git(["update-ref", "-d", ref], base)
    _commit(wt, "orphan work", ("c.txt", "v3\n"))
    r = wg.collect_reset_risk(wt)
    assert r["risk"] is True
    assert r["unpushed_count"] >= 1
    assert r["compare_ref"] == ""


# ===================== 归档保全 + 放行/拒绝（验收 a） =====================

def test_untracked_archived_then_cleanup_allowed(repo):
    """未跟踪文件：归档 bundle + untracked 快照 + 机器可读记录，然后放行删除。"""
    wt = _wt(repo)
    p = os.path.join(wt, "packages", "newpkg", "a.py")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("impl\n")
    assert repogit.cleanup_issue_worktree(repo, "coding", "7", reason="sweep_done_issue") is True
    assert not os.path.isdir(wt), "归档成功后放行删除"

    rec = wg.load_reset_record(repo, "7", "coding")
    assert rec and rec["worktree_reset"] is True
    assert rec["reset_reason"] == "sweep_done_issue"
    assert rec["archived_at"]
    assert os.path.isfile(rec["bundle_path"]), "bundle 必须落盘"
    assert rec["untracked"] == ["packages/newpkg/a.py"]
    assert rec["untracked_snapshot"] == ["packages/newpkg/a.py"]
    archive_dir = os.path.dirname(rec["bundle_path"])
    snap = os.path.join(archive_dir, "untracked", "packages", "newpkg", "a.py")
    assert os.path.isfile(snap) and open(snap, encoding="utf-8").read() == "impl\n"
    assert os.path.isfile(os.path.join(archive_dir, wg.MANIFEST_NAME))


def test_unpushed_commit_recovers_from_bundle(repo):
    """a：未推送 commit → 归档后放行；重建 worktree 后 `git fetch <bundle> &&
    git reset --hard FETCH_HEAD` 完整恢复（含新增包目录）。"""
    wt = _wt(repo, "8")
    _commit(wt, "unpushed work", ("packages/newpkg/a.py", "impl\n"))
    lost = _git(["rev-parse", "HEAD"], wt).stdout.strip()
    assert repogit.cleanup_issue_worktree(repo, "coding", "8") is True

    rec = wg.load_reset_record(repo, "8", "coding")
    assert rec and rec["lost_commits"] == [lost]

    # 下一轮：重建 worktree；生产事故形态是 worktree 被重置回基线（reflog:
    # reset: moving to HEAD，Issue #11 实证），此处显式模拟后再验证 bundle 恢复。
    wt2 = repogit.ensure_issue_worktree(repo, "coding", "8")
    assert wt2
    _git(["reset", "--hard", "origin/main"], wt2)
    assert not os.path.exists(os.path.join(wt2, "packages")), "重置后实现不应仍在本地"
    # 恢复：fetch bundle → reset --hard FETCH_HEAD
    ok, err = repogit.fetch_from_bundle(wt2, rec["bundle_path"])
    assert ok, err
    assert _git(["reset", "--hard", "FETCH_HEAD"], wt2).returncode == 0
    assert _git(["rev-parse", "HEAD"], wt2).stdout.strip() == lost
    assert os.path.isfile(os.path.join(wt2, "packages", "newpkg", "a.py"))
    assert open(os.path.join(wt2, "packages", "newpkg", "a.py"),
                encoding="utf-8").read() == "impl\n"


def test_dirty_tracked_archives_patch(repo):
    """未提交（tracked 修改）不在 bundle 内 → dirty.patch 单独保全。"""
    wt = _wt(repo, "16")
    with open(os.path.join(wt, "a.txt"), "w", encoding="utf-8") as f:
        f.write("dirty-change\n")
    assert repogit.cleanup_issue_worktree(repo, "coding", "16") is True
    rec = wg.load_reset_record(repo, "16", "coding")
    assert rec and rec["dirty"] == ["a.txt"]
    assert os.path.isfile(rec["dirty_patch"])
    assert "dirty-change" in open(rec["dirty_patch"], encoding="utf-8").read()


def test_archive_bundle_failure_refuses_cleanup(repo, monkeypatch):
    """归档失败（bundle create）→ 拒绝删除，worktree 保留、无记录。"""
    wt = _wt(repo)
    with open(os.path.join(wt, "new.txt"), "w", encoding="utf-8") as f:
        f.write("x\n")
    monkeypatch.setattr(repogit, "bundle_create",
                        lambda wt_dir, path, token="": (False, "disk full"))
    assert repogit.cleanup_issue_worktree(repo, "coding", "7") is False
    assert os.path.isdir(wt), "归档失败必须保留 worktree"
    assert wg.load_reset_record(repo, "7", "coding") is None


def test_bundle_verify_failure_refuses_cleanup(repo, monkeypatch):
    """bundle 生成但 verify 失败（不可信归档）→ 拒绝删除。"""
    wt = _wt(repo)
    with open(os.path.join(wt, "new.txt"), "w", encoding="utf-8") as f:
        f.write("x\n")
    monkeypatch.setattr(repogit, "bundle_verify",
                        lambda wt_dir, path, token="": False)
    assert repogit.cleanup_issue_worktree(repo, "coding", "7") is False
    assert os.path.isdir(wt)


def test_guard_exception_refuses_cleanup(repo, monkeypatch):
    """守卫自身异常 → 拒绝删除（宁可残留，不可丢改动）。"""
    wt = _wt(repo)
    with open(os.path.join(wt, "new.txt"), "w", encoding="utf-8") as f:
        f.write("x\n")
    def _boom(*a, **k):
        raise RuntimeError("guard exploded")
    monkeypatch.setattr("src.domains.runtime.engine.worktree_guard."
                        "archive_worktree_before_reset", _boom)
    assert repogit.cleanup_issue_worktree(repo, "coding", "7") is False
    assert os.path.isdir(wt)


# ===================== 既有清理语义回归（验收 c） =====================

def test_clean_cleanup_removes_without_archive(repo):
    """干净 worktree：删除行为与现状一致（无 bundle/记录），返回 True。"""
    wt = _wt(repo, "17")
    assert repogit.cleanup_issue_worktree(repo, "coding", "17") is True
    assert not os.path.isdir(wt)
    assert not os.path.exists(wg.reset_archive_root(repo))


def test_remove_if_pushed_keeps_unpushed_worktree(repo, git_src):
    """remove_issue_worktree_if_pushed：未推送 → 保留且不归档（未发生重置）。"""
    wt = _wt(repo, "18")
    _commit(wt, "unpushed work", ("d.txt", "v\n"))
    assert repogit.remove_issue_worktree_if_pushed(repo, "coding", "18") is False
    assert os.path.isdir(wt)
    assert wg.load_reset_record(repo, "18", "coding") is None, "未重置不应有记录"


def test_remove_if_pushed_archives_dirty_then_removes(repo, git_src):
    """已推送但残留未提交改动：先归档再删除（原实现会静默丢未提交内容）。"""
    wt = _wt(repo, "19")
    _commit(wt, "pushed work", ("e.txt", "v\n"))
    assert _git(["push", "origin", "HEAD:refs/heads/feat/issue-19"], wt).returncode == 0
    with open(os.path.join(wt, "a.txt"), "w", encoding="utf-8") as f:
        f.write("uncommitted\n")
    assert repogit.remove_issue_worktree_if_pushed(repo, "coding", "19") is True
    assert not os.path.isdir(wt)
    rec = wg.load_reset_record(repo, "19", "coding")
    assert rec and rec["dirty"] == ["a.txt"]
    assert os.path.isfile(rec["dirty_patch"])


# ===================== 僵尸路径闸（cleanup._zombie_issue_dirs） =====================

def test_parse_issue_workdir(repo, ws):
    """路径解析：新结构 provider 前缀 / 旧结构回退 gitcode / 非法路径。"""
    assert wg.parse_issue_workdir(
        os.path.join(ws, "gitcode", "owner", "repo", "issues", "12", "coding"),
        ws) == (repo, "12", "coding")
    assert wg.parse_issue_workdir(
        os.path.join(ws, "owner", "repo", "issues", "13", "qa"),
        ws) == ("gitcode:owner/repo", "13", "qa")
    assert wg.parse_issue_workdir(os.path.join(ws, "owner", "repo"), ws) == ("", "", "")
    assert wg.parse_issue_workdir(os.path.join(ws, "gitcode", "owner", "repo",
                                               "issues", "14", "vod"), ws) == ("", "", "")


def test_guard_issue_workdir_removal_archives_then_allows(repo, ws):
    """僵尸路径命中风险 → 归档后放行；非 worktree 残留直接放行。"""
    wt = _wt(repo, "20")
    with open(os.path.join(wt, "new.txt"), "w", encoding="utf-8") as f:
        f.write("x\n")
    assert wg.guard_issue_workdir_removal(wt, reason="zombie_ttl") is True
    rec = wg.load_reset_record(repo, "20", "coding")
    assert rec and rec["reset_reason"] == "zombie_ttl"
    # 非 git 目录（无 .git）→ 直接放行、不产记录
    plain = os.path.join(ws, "gitcode", "owner", "repo", "issues", "21", "coding")
    os.makedirs(plain, exist_ok=True)
    with open(os.path.join(plain, "junk.txt"), "w", encoding="utf-8") as f:
        f.write("x\n")
    assert wg.guard_issue_workdir_removal(plain) is True
    assert wg.load_reset_record(repo, "21", "coding") is None


def test_guard_issue_workdir_removal_refuses_on_archive_failure(repo, ws, monkeypatch):
    """僵尸路径归档失败 → 拒绝删除。"""
    wt = _wt(repo, "22")
    with open(os.path.join(wt, "new.txt"), "w", encoding="utf-8") as f:
        f.write("x\n")
    monkeypatch.setattr(repogit, "bundle_create",
                        lambda wt_dir, path, token="": (False, "io error"))
    assert wg.guard_issue_workdir_removal(wt, reason="zombie_ttl") is False
    assert os.path.isdir(wt)


# ===================== 下一轮告知注入（验收 b） =====================

def test_inject_reset_context_machine_readable(repo):
    """record 命中 → env 双变量 + prompt 提示块（机器可读字段）。"""
    record = {
        "worktree_reset": True,
        "lost_commits": ["abc123"],
        "unpushed_count": 1,
        "bundle_path": "/ws/.grape/archive/2041/x/worktree.bundle",
        "archived_at": "2026-11-11T10:00:00+00:00",
        "reset_reason": "sweep_done_issue",
    }
    assert wg._atomic_json_write(wg.reset_record_path(repo, "2041", "coding"), record)
    task = {"repo_key": repo, "agent_type": "coding", "issue_id": "#2041"}
    env, prompt = {}, "BASE PROMPT"
    prompt2, injected = wg.inject_reset_context(task, env, prompt)
    assert injected is True
    assert env["GRAPE_WORKTREE_RESET"] == "true"
    info = json.loads(env["GRAPE_WORKTREE_RESET_INFO"])
    assert info["worktree_reset"] is True
    assert info["lost_commits"] == ["abc123"]
    assert info["bundle_path"] == record["bundle_path"]
    assert "was reset" in prompt2
    assert record["bundle_path"] in prompt2
    assert "Do NOT assume" in prompt2


def test_inject_reset_context_noop_without_record(repo):
    """无记录 / 非 git agent → prompt/env 原样（零副作用）。"""
    task = {"repo_key": repo, "agent_type": "coding", "issue_id": "#2041"}
    env, prompt = {}, "P"
    assert wg.inject_reset_context(task, env, prompt) == ("P", False)
    assert env == {}
    task2 = {"repo_key": repo, "agent_type": "issue", "issue_id": "#2041"}
    assert wg.inject_reset_context(task2, env, "P") == ("P", False)
    assert env == {}


# ===================== 记录路径布局 =====================

def test_reset_record_path_layout(repo, ws):
    """记录落在 worktree 删除路径之外的仓库级 .grape/archive 下（per-repo 隔离）。"""
    p = wg.reset_record_path(repo, "2041", "coding")
    assert ".grape" in p and "archive" in p
    assert os.sep + "issues" + os.sep not in p, "记录不得落在 issue workdir 内"
    assert p.endswith(os.path.join("2041", "coding-reset.json"))
