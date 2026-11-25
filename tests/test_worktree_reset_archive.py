"""Issue #2041：worktree 重置前置保全（reset archive guard）测试。

验收覆盖（docs/issue-2041/spec.md，沿用 Spec a-d）：
- a) 存在未推送 commit / 未提交改动时，删除先归档（bundle 可 `git fetch` +
     `git reset --hard FETCH_HEAD` 完整恢复）；归档失败拒绝删除（目录保留）
- b) 重置记录机器可读（worktree_reset / lost_commits / bundle_path /
     archived_at / reset_reason），下一轮任务注入 env（GRAPE_WORKTREE_RESET*）+ prompt
- c) 无风险路径零改动：不产 bundle、不产记录，既有清理语义保持（repogit 旧用例不回归）
- d) upstream 与 HEAD 一致（已推送）且干净 → 零副作用
失败路径：bundle create 失败 / bundle verify 失败 / 守卫异常 → 拒绝删除保留现场
第 2 轮审查（fail-closed）：工作区扫描失败（HEAD 可解析也拒绝）/ dirty diff
失败或空输出 → 拒绝删除保留现场
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
    """d：已推送且干净 → 零风险零副作用（真实平台推送形态：不带 `-u`）。

    worktree 分支的 upstream 是基线分支（`origin/main`），平台 push 用
    `HEAD:refs/heads/<branch>` 不更新 upstream。判定必须取远端事实，否则已推送
    的干净 worktree 会被误判为有未推送内容（每轮 sweep 产整仓 bundle + 误注入）。
    """
    wt = _wt(repo, "14")
    _commit(wt, "pushed work", ("b.txt", "v2\n"))
    assert _git(["push", "origin", "HEAD:refs/heads/feat/issue-14"], wt).returncode == 0
    upstream = _git(["rev-parse", "--abbrev-ref", "@{upstream}"], wt)
    assert upstream.stdout.strip() == "origin/main", "upstream 仍指向基线（未带 -u）"
    r = wg.collect_reset_risk(wt)
    assert r["risk"] is False and r["unpushed_count"] == 0
    assert r["compare_ref"] == "ls-remote:origin/feat/issue-14"
    assert r["remote_head"] == r["head"]
    assert repogit.cleanup_issue_worktree(repo, "coding", "14") is True
    assert not os.path.isdir(wt)
    assert not os.path.exists(wg.reset_archive_root(repo)), "已推送干净路径不应产生归档"


def test_remote_branch_behind_head_is_risk(repo, git_src):
    """已推送后又本地提交（远端落后）→ 仍判风险，且 lost_commits 只含新增提交。"""
    wt = _wt(repo, "26")
    _commit(wt, "pushed work", ("b.txt", "v2\n"))
    assert _git(["push", "origin", "HEAD:refs/heads/feat/issue-26"], wt).returncode == 0
    _commit(wt, "unpushed work", ("c.txt", "v3\n"))
    head = _git(["rev-parse", "HEAD"], wt).stdout.strip()
    r = wg.collect_reset_risk(wt)
    assert r["risk"] is True
    assert r["unpushed_count"] == 1
    assert r["lost_commits"] == [head]
    assert r["compare_ref"] == "ls-remote:origin/feat/issue-26"


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


def test_non_git_residue_dir_removed(repo):
    """QA N1：非 git 残留目录（无 .git，`git worktree add` 失败/中断留下）
    → 直接删除，不再退化为「恒拒绝、只能等僵尸 TTL」。"""
    issue_dir = repogit.issue_dir_for(repo, "coding", "31")
    os.makedirs(issue_dir, exist_ok=True)
    with open(os.path.join(issue_dir, "partial.txt"), "w", encoding="utf-8") as f:
        f.write("residue\n")
    assert not os.path.exists(os.path.join(issue_dir, ".git"))
    assert repogit.cleanup_issue_worktree(repo, "coding", "31") is True
    assert not os.path.isdir(issue_dir), "非 git 残留必须被回收"
    assert wg.load_reset_record(repo, "31", "coding") is None, "无可丢内容不应产记录"


def test_residue_with_git_marker_still_fail_closed(repo):
    """N1 判据不得放宽：目录含 .git（此处为损坏的 gitdir 指针文件，git 无法解析）
    → 仍走归档守卫，扫描失败即拒绝删除（保留现场人工确认）。"""
    issue_dir = repogit.issue_dir_for(repo, "coding", "32")
    os.makedirs(issue_dir, exist_ok=True)
    with open(os.path.join(issue_dir, ".git"), "w", encoding="utf-8") as f:
        f.write("gitdir: /nonexistent/worktrees/32\n")
    with open(os.path.join(issue_dir, "partial.txt"), "w", encoding="utf-8") as f:
        f.write("residue\n")
    assert repogit.cleanup_issue_worktree(repo, "coding", "32") is False
    assert os.path.isdir(issue_dir), "含 .git 的残留必须 fail-closed 保留"


def test_remove_if_pushed_unpushed_archives_then_removes(repo, git_src):
    """#2514 收紧语义：未推送提交 → 区间证据存档后仍删除（终态必清、不丢码）。

    旧断言（未推送 → 保留不删）是 #2281 时代的宽松语义，与合并带入的
    #2514 终态清理闸门互斥；新语义 = unpushed 提交先落
    ``artifacts/workspace-*-unpushed.patch``（log -p --binary 区间）再移除。
    """
    wt = _wt(repo, "18")
    _commit(wt, "unpushed work", ("d.txt", "v\n"))
    assert repogit.remove_issue_worktree_if_pushed(repo, "coding", "18") is True
    assert not os.path.isdir(wt), "#2514：终态必清（证据已存档）"
    from src.kernel.repo_key import workspace_repo_dir
    art = os.path.join(workspace_repo_dir(repo), "artifacts")
    patches = [p for p in os.listdir(art) if p.endswith("-unpushed.patch")]
    assert patches, "未推送提交必须有 -unpushed 区间存档"
    body = open(os.path.join(art, patches[0]), encoding="utf-8", errors="replace").read()
    assert "unpushed work" in body, "存档须含未推送提交信息"


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
    # Issue #3375：同 Issue 多 PR / issue-less PR 目录也可解析
    assert wg.parse_issue_workdir(
        os.path.join(ws, "gitcode", "owner", "repo", "issues", "12", "pr-7", "coding"),
        ws) == (repo, "12", "coding")
    assert wg.parse_issue_workdir(
        os.path.join(ws, "gitcode", "owner", "repo", "prs", "452", "qa"),
        ws) == (repo, "452", "qa")


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


# ===================== 审查整改：未保全即拒绝（review Required#1） =====================

def test_untracked_oversize_file_refuses_cleanup(repo):
    """单文件 >10MB 无法快照 → 归档不完整 → 拒绝删除并记 skipped 明细。"""
    wt = _wt(repo, "27")
    big = os.path.join(wt, "big.bin")
    with open(big, "wb") as f:
        f.truncate(wg._MAX_UNTRACKED_FILE_BYTES + 1024)
    assert repogit.cleanup_issue_worktree(repo, "coding", "27") is False
    assert os.path.isdir(wt), "未保全内容必须保留现场"
    rec = wg.load_reset_record(repo, "27", "coding")
    assert rec is None, "归档不完整不得产 reset 记录（避免误告知已保全）"
    archive_root = wg.reset_archive_root(repo)
    manifests = []
    for root, _dirs, files in os.walk(archive_root):
        if wg.MANIFEST_NAME in files:
            manifests.append(os.path.join(root, wg.MANIFEST_NAME))
    assert manifests, "不完整归档仍需写 manifest 供事后取证"
    data = json.load(open(manifests[0], encoding="utf-8"))
    assert data["archive_complete"] is False
    assert any(s.get("reason") == "too_large" for s in data["untracked_skipped"])


def test_untracked_file_count_cap_refuses_cleanup(repo, monkeypatch):
    """未跟踪文件数超上限（超出部分无法保全）→ 拒绝删除，记 file_count_cap。"""
    monkeypatch.setattr(wg, "_MAX_UNTRACKED_FILES", 3)
    wt = _wt(repo, "28")
    for i in range(5):
        with open(os.path.join(wt, "new%d.txt" % i), "w", encoding="utf-8") as f:
            f.write("v%d\n" % i)
    assert repogit.cleanup_issue_worktree(repo, "coding", "28") is False
    assert os.path.isdir(wt)
    res = wg.archive_worktree_before_reset(wt, repo, "28", "coding")
    assert res["ok"] is False
    assert any(s.get("reason") == "file_count_cap" for s in res["skipped"])
    assert res["record"]["archive_complete"] is False


def test_untracked_copy_failure_refuses_cleanup(repo, monkeypatch):
    """copy 抛异常（磁盘/权限）→ 该项未保全 → 拒绝删除。"""
    wt = _wt(repo, "29")
    with open(os.path.join(wt, "impl.py"), "w", encoding="utf-8") as f:
        f.write("impl\n")

    def _boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(wg.shutil, "copy2", _boom)
    assert repogit.cleanup_issue_worktree(repo, "coding", "29") is False
    assert os.path.isdir(wt)
    res = wg.archive_worktree_before_reset(wt, repo, "29", "coding")
    assert res["ok"] is False
    assert any(s.get("reason") == "copy_error" for s in res["skipped"])


# ===================== 审查整改：record 只注入一次（review Required#2） =====================

def test_reset_record_injected_only_once(repo):
    """同一记录只注入一次：首次注入后标记 consumed，第二轮任务零注入。"""
    record = {"worktree_reset": True, "lost_commits": ["abc"], "unpushed_count": 1,
              "bundle_path": "/ws/x/worktree.bundle",
              "archived_at": "2026-11-11T10:00:00+00:00",
              "reset_reason": "sweep_done_issue"}
    assert wg._atomic_json_write(wg.reset_record_path(repo, "2041", "coding"), record)

    env1, prompt = {}, "BASE"
    p1, injected1 = wg.inject_reset_context(
        {"repo_key": repo, "agent_type": "coding", "issue_id": "#2041",
         "task_id": "t_first"}, env1, prompt)
    assert injected1 is True and "was reset" in p1
    assert "ONLY IF" in p1, "prompt 必须带「仅无改动时才 reset」前置条件"
    rec = wg.load_reset_record(repo, "2041", "coding")
    assert rec["consumed_at"] and rec["consumed_by_task"] == "t_first"

    env2 = {}
    p2, injected2 = wg.inject_reset_context(
        {"repo_key": repo, "agent_type": "coding", "issue_id": "#2041",
         "task_id": "t_second"}, env2, "BASE")
    assert injected2 is False
    assert env2 == {} and p2 == "BASE", "已消费记录不得再次注入"


def test_mark_consumed_keeps_archive_manifest(repo):
    """consumed 标记只作用于最新记录，归档目录内 manifest 原样保留。"""
    wt = _wt(repo, "30")
    with open(os.path.join(wt, "x.txt"), "w", encoding="utf-8") as f:
        f.write("x\n")
    assert repogit.cleanup_issue_worktree(repo, "coding", "30") is True
    rec = wg.load_reset_record(repo, "30", "coding")
    manifest = os.path.join(os.path.dirname(rec["bundle_path"]), wg.MANIFEST_NAME)
    before = json.load(open(manifest, encoding="utf-8"))
    assert wg.mark_reset_record_consumed(repo, "30", "coding", task_id="t_x") is True
    after = json.load(open(manifest, encoding="utf-8"))
    assert before == after and "consumed_at" not in after


# ===================== 审查整改：HEAD 不可解析不再放行（review Required#3） =====================

def test_head_unresolvable_with_untracked_refuses_cleanup(repo, ws):
    """unborn HEAD（无 commit）+ 未跟踪文件 → 判风险并拒绝删除，不静默放行。"""
    d = os.path.join(ws, "gitcode", "owner", "repo", "issues", "31", "coding")
    os.makedirs(d)
    _git(["init", "-b", "main"], d)
    with open(os.path.join(d, "impl.py"), "w", encoding="utf-8") as f:
        f.write("impl\n")
    r = wg.collect_reset_risk(d)
    assert r["head_missing"] is True
    assert r["untracked"] == ["impl.py"]
    assert r["risk"] is True
    assert wg.guard_issue_workdir_removal(d, reason="zombie_ttl") is False
    assert os.path.isdir(d)


def test_head_unresolvable_empty_worktree_not_blocked(repo, ws):
    """unborn HEAD 且工作区无内容可丢 → 不阻塞清理（避免永久残留）。"""
    d = os.path.join(ws, "gitcode", "owner", "repo", "issues", "32", "coding")
    os.makedirs(d)
    _git(["init", "-b", "main"], d)
    r = wg.collect_reset_risk(d)
    assert r["head_missing"] is True and r["risk"] is False
    assert wg.guard_issue_workdir_removal(d) is True


# ===================== 审查整改：rename 两列判定（review Could） =====================

def test_unstaged_rename_parsed_without_garbage(repo):
    """未暂存 rename（" R new" + 原路径字段）→ 跳过原路径，清单不含垃圾项。"""
    wt = _wt(repo, "33")
    os.rename(os.path.join(wt, "a.txt"), os.path.join(wt, "renamed.txt"))
    entries = wg._porcelain_entries_all(wt)
    assert entries is not None
    paths = [p for _xy, p in entries]
    assert "renamed.txt" in paths
    assert not any(p.startswith("a.txt") and p != "a.txt" for p in paths), \
        "原路径字段不得被解析成脏项"
    r = wg.collect_reset_risk(wt)
    assert r["risk"] is True
    assert all("\x00" not in p for p in r["dirty"] + r["untracked"])


# ===================== 审查整改：旧布局解析/闸门（review F2） =====================

def test_parse_issue_workdir_legacy_agent_first(repo, ws):
    """过渡期旧布局 {repo}/{agent}/issues/{n} 也须解析（否则绕过保全闸 rmtree）。"""
    assert wg.parse_issue_workdir(
        os.path.join(ws, "owner", "repo", "coding", "issues", "24"), ws) == (
            "gitcode:owner/repo", "24", "coding")
    assert wg.parse_issue_workdir(
        os.path.join(ws, "gitcode", "owner", "repo", "qa", "issues", "25"), ws) == (
            "gitcode:owner/repo", "25", "qa")


def test_legacy_layout_removal_goes_through_guard(repo, ws):
    """旧布局含 .git 且带未跟踪文件 → 归档后放行，记录可读。"""
    d = os.path.join(ws, "owner", "repo", "coding", "issues", "34")
    os.makedirs(d)
    _git(["init", "-b", "main"], d)
    _git(["config", "user.email", "test@example.com"], d)
    _git(["config", "user.name", "Test User"], d)
    with open(os.path.join(d, "a.txt"), "w", encoding="utf-8") as f:
        f.write("v1\n")
    _git(["add", "-A"], d)
    _git(["commit", "-m", "init"], d)
    with open(os.path.join(d, "impl.py"), "w", encoding="utf-8") as f:
        f.write("impl\n")
    assert wg.guard_issue_workdir_removal(d, reason="zombie_ttl") is True
    rec = wg.load_reset_record("gitcode:owner/repo", "34", "coding")
    assert rec and rec["untracked"] == ["impl.py"]


def test_unparsable_git_dir_kept(repo, ws):
    """含 .git 但布局未知 → 保守保留（宁可残留，不可静默丢现场）。"""
    d = os.path.join(ws, "strange", "dir")
    os.makedirs(d)
    _git(["init", "-b", "main"], d)
    assert wg.guard_issue_workdir_removal(d) is False


# ===================== 审查整改：归档权限与 bundle 范围（建议项） =====================

def test_archive_dir_and_artifacts_private(repo):
    """归档目录 0700、bundle/快照 0600（未跟踪文件可能含凭据类内容）。"""
    wt = _wt(repo, "35")
    with open(os.path.join(wt, "secret.env"), "w", encoding="utf-8") as f:
        f.write("TOKEN=placeholder\n")
    assert repogit.cleanup_issue_worktree(repo, "coding", "35") is True
    rec = wg.load_reset_record(repo, "35", "coding")
    archive_dir = os.path.dirname(rec["bundle_path"])
    assert oct(os.stat(archive_dir).st_mode & 0o777) == oct(0o700)
    assert oct(os.stat(rec["bundle_path"]).st_mode & 0o777) == oct(0o600)
    snap = os.path.join(archive_dir, "untracked", "secret.env")
    assert oct(os.stat(snap).st_mode & 0o777) == oct(0o600)
    rec_file = wg.reset_record_path(repo, "35", "coding")
    assert oct(os.stat(rec_file).st_mode & 0o777) == oct(0o600)


def test_bundle_contains_only_head(repo):
    """bundle 只含 HEAD 链（不再 --all 打包其它 issue 分支/基线）。"""
    wt = _wt(repo, "36")
    _commit(wt, "head work", ("impl.py", "impl\n"))
    _git(["branch", "other-issue"], wt)  # --all 会一并打包的分支
    assert repogit.cleanup_issue_worktree(repo, "coding", "36") is True
    rec = wg.load_reset_record(repo, "36", "coding")
    assert rec, "未推送提交应产 reset 记录"
    heads = _git(["bundle", "list-heads", rec["bundle_path"]],
                 os.path.dirname(rec["bundle_path"]))
    assert heads.returncode == 0, heads.stderr
    refs = [line.split()[-1] for line in heads.stdout.strip().splitlines() if line]
    assert "HEAD" in refs
    assert not any(r.startswith("refs/heads/") for r in refs), \
        "bundle 不应包含其它 refs（体积约等于整仓）"


# ===================== 审查整改：超时具名常量（review F3） =====================

def test_bundle_timeouts_are_named_constants(repo, monkeypatch):
    """bundle 归档超时取自具名常量（可配置、有 rationale），不再内联字面量。"""
    seen = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake(args, token="", timeout=900):
        seen.append((tuple(args), timeout))
        return _R()
    monkeypatch.setattr(repogit, "_run_git", _fake)
    ok, err = repogit.bundle_create("/wt", "/tmp/x.bundle")
    assert ok and err == ""
    assert repogit._BUNDLE_CREATE_TIMEOUT > 0
    assert seen and seen[0][1] == repogit._BUNDLE_CREATE_TIMEOUT
    seen.clear()
    ok, err = repogit.fetch_from_bundle("/wt", "/tmp/x.bundle")
    assert ok and err == ""
    assert seen and seen[0][1] == repogit._BUNDLE_FETCH_TIMEOUT


# ===================== 第 2 轮审查整改：归档链 fail-closed（review Required） =====================

def test_scan_failure_with_resolvable_head_refuses_cleanup(repo, monkeypatch):
    """R1：HEAD 可解析 + status 扫描失败 → 一律判风险且 cleanup False、目录保留。

    扫描失败（index.lock/索引损坏/超时/权限）不能证明「无内容可丢」：dirty/
    untracked 清单不可枚举，bundle 只含已提交对象、归档无从保全未提交内容，
    不得产「空归档」后放行删除（曾按 HEAD 可解析直接放行 = fail-open）。
    """
    wt = _wt(repo, "37")
    with open(os.path.join(wt, "unseen.txt"), "w", encoding="utf-8") as f:
        f.write("invisible to a failed scan\n")
    monkeypatch.setattr(wg, "_porcelain_entries_all",
                        lambda wt_dir, token="": None)
    r = wg.collect_reset_risk(wt)
    assert r["risk"] is True
    assert r["scan_failed"] is True, "扫描失败必须显式标记（归档侧据此拒绝）"
    assert repogit.cleanup_issue_worktree(repo, "coding", "37") is False
    assert os.path.isdir(wt), "扫描失败必须保留 worktree"
    assert os.path.isfile(os.path.join(wt, "unseen.txt")), "不可枚举的内容不得被删"
    assert wg.load_reset_record(repo, "37", "coding") is None, \
        "清单不可枚举不得产 reset 记录（避免误告知已保全）"


def test_dirty_diff_failure_refuses_cleanup(repo, monkeypatch):
    """R2：dirty 非空 + `git diff HEAD --binary` 执行失败 → cleanup False、
    worktree 保留、无 reset 记录（tracked 未提交改动不得无声丢失）。"""
    wt = _wt(repo, "38")
    with open(os.path.join(wt, "a.txt"), "w", encoding="utf-8") as f:
        f.write("dirty\n")
    real_run = repogit._run_git

    def _fail_diff(args, *a, **k):
        if "diff" in args and "HEAD" in args:
            return None  # git exec failed（超时/锁/仓库异常）
        return real_run(args, *a, **k)

    monkeypatch.setattr(repogit, "_run_git", _fail_diff)
    assert repogit.cleanup_issue_worktree(repo, "coding", "38") is False
    assert os.path.isdir(wt), "补丁未保全必须保留 worktree"
    assert open(os.path.join(wt, "a.txt"), encoding="utf-8").read() == "dirty\n"
    assert wg.load_reset_record(repo, "38", "coding") is None, \
        "dirty.patch 缺失不得产 reset 记录（避免误告知已保全）"


def test_dirty_diff_empty_output_refuses_cleanup(repo, monkeypatch):
    """R2：dirty 非空但 diff 空输出 → 同判未保全，拒绝删除（不留空 patch 放行）。"""
    wt = _wt(repo, "39")
    with open(os.path.join(wt, "a.txt"), "w", encoding="utf-8") as f:
        f.write("dirty\n")
    real_run = repogit._run_git

    class _Empty:
        returncode = 0
        stdout = ""
        stderr = ""

    def _empty_diff(args, *a, **k):
        if "diff" in args and "HEAD" in args:
            return _Empty()
        return real_run(args, *a, **k)

    monkeypatch.setattr(repogit, "_run_git", _empty_diff)
    res = wg.archive_worktree_before_reset(wt, repo, "39", "coding")
    assert res["ok"] is False
    assert "dirty diff failed" in res["error"]
    assert repogit.cleanup_issue_worktree(repo, "coding", "39") is False
    assert os.path.isdir(wt)
    assert wg.load_reset_record(repo, "39", "coding") is None


def test_archive_tree_dirs_private(repo):
    """建议项：归档树新建各级目录（.grape / archive / {issue} / 叶子）全部 0700，
    不再只收紧叶子目录（父目录按 umask 0755 会暴露归档清单元数据）。"""
    wt = _wt(repo, "40")
    with open(os.path.join(wt, "secret.env"), "w", encoding="utf-8") as f:
        f.write("TOKEN=placeholder\n")
    assert repogit.cleanup_issue_worktree(repo, "coding", "40") is True
    rec = wg.load_reset_record(repo, "40", "coding")
    assert rec
    leaf = os.path.dirname(rec["bundle_path"])           # {archive}/{issue}/{stamp}
    issue_dir = os.path.dirname(leaf)                    # {archive}/{issue}
    archive_root = wg.reset_archive_root(repo)           # {repo}/.grape/archive
    for d, name in ((leaf, "归档叶子"), (issue_dir, "issue 目录"),
                    (archive_root, "archive 目录"),
                    (os.path.dirname(archive_root), ".grape 目录")):
        assert oct(os.stat(d).st_mode & 0o777) == oct(0o700), \
            f"{name} 必须 0700: {d}"

# ===================== Issue #2281：重建找回 + 推送重试通道 =====================

def test_ensure_worktree_reuses_branch_with_unpushed_commits(repo):
    """AC2：清理后分支 ref 仍在 → 新一轮 ensure 复用 feat/issue-N，未推送 commit 保留。"""
    wt = _wt(repo, "50")
    _commit(wt, "local unpushed work", ("impl.py", "x = 1\n"))
    head = _git(["rev-parse", "HEAD"], wt).stdout.strip()
    assert repogit.cleanup_issue_worktree(repo, "coding", "50") is True
    assert not os.path.isdir(wt)
    wt2 = repogit.ensure_issue_worktree(repo, "coding", "50")
    assert wt2 and os.path.isdir(wt2)
    assert _git(["rev-parse", "HEAD"], wt2).stdout.strip() == head
    assert "local unpushed work" in _git(["log", "--oneline"], wt2).stdout
    assert _git(["status", "--porcelain"], wt2).stdout.strip() == ""
    rec = wg.load_reset_record(repo, "50", "coding")
    assert rec and rec.get("auto_restored") is True
    assert rec.get("auto_restored_source") == "branch", "分支复用标记 source=branch"


def test_ensure_worktree_recovers_from_bundle_when_branch_ref_gone(repo):
    """AC2：分支 ref 已从 bare mirror 消失 → 从最近归档 bundle fetch+reset 找回。"""
    wt = _wt(repo, "51")
    _commit(wt, "bundle-recovered work", ("impl.py", "y = 2\n"))
    head = _git(["rev-parse", "HEAD"], wt).stdout.strip()
    assert repogit.cleanup_issue_worktree(repo, "coding", "51") is True
    base = repogit.repo_base_dir(repo)
    assert _git(["branch", "-D", "feat/issue-51"], base).returncode == 0
    wt2 = repogit.ensure_issue_worktree(repo, "coding", "51")
    assert wt2 and os.path.isdir(wt2)
    assert _git(["rev-parse", "HEAD"], wt2).stdout.strip() == head, \
        "bundle 恢复必须还原到归档 HEAD"
    assert "bundle-recovered work" in _git(["log", "--oneline"], wt2).stdout
    assert _git(["status", "--porcelain"], wt2).stdout.strip() == ""
    rec = wg.load_reset_record(repo, "51", "coding")
    assert rec and rec.get("auto_restored") is True
    assert rec.get("auto_restored_head") == head


def test_ensure_worktree_without_bundle_keeps_baseline(repo):
    """无归档/无未推送 commit → 行为不变：基线重建、无副作用、无记录。"""
    wt = _wt(repo, "52")
    assert repogit.cleanup_issue_worktree(repo, "coding", "52") is True
    assert wg.load_reset_record(repo, "52", "coding") is None
    base = repogit.repo_base_dir(repo)
    _git(["branch", "-D", "feat/issue-52"], base)
    wt2 = repogit.ensure_issue_worktree(repo, "coding", "52")
    assert wt2 and os.path.isdir(wt2)
    _st = [ln for ln in _git(["status", "--porcelain"], wt2).stdout.splitlines()
           if ".git-askpass" not in ln]
    assert _st == []
    assert wg.load_reset_record(repo, "52", "coding") is None


def test_ensure_worktree_dirty_wip_refused_but_archived(repo):
    """#2514 闸门语义：脏 worktree → 拒绝复用，WIP 先存档再重建干净基线树。

    旧断言（脏树直接复用、wip.txt 原样保留）是 #2281 时代的宽松语义；
    #2514 dirty 闸门下同一场景 = 证据落 artifacts 后隔离重建，改动不丢
    （可从存档 patch 还原），但重建树必须是干净的基线工作区。
    """
    wt = _wt(repo, "53")
    with open(os.path.join(wt, "wip.txt"), "w", encoding="utf-8") as f:
        f.write("wip\n")
    wt2 = repogit.ensure_issue_worktree(repo, "coding", "53")
    assert wt2 and os.path.isdir(wt2)
    assert not os.path.isfile(os.path.join(wt2, "wip.txt")), \
        "#2514 dirty 闸门：重建后的工作区不得携带拒绝复用前的 WIP 文件"
    from src.kernel.repo_key import workspace_repo_dir
    art = os.path.join(workspace_repo_dir(repo), "artifacts")
    patches = [p for p in os.listdir(art)
              if "reuse-refused" in p or "issue53" in p]
    assert patches, "拒绝复用的 WIP 必须有证据存档"
    body = open(os.path.join(art, patches[0]), encoding="utf-8", errors="replace").read()
    assert "wip" in body, "存档须含被拒复用工作区的内容（可还原）"


def test_inject_reset_context_push_retry_entry(repo):
    """AC3：未自动找回 → prompt 给出推送重试入口（而非只引导手工 reset）。"""
    record = {"worktree_reset": True, "lost_commits": ["abc"], "unpushed_count": 1,
              "bundle_path": "/ws/x/worktree.bundle", "branch": "feat/issue-2281",
              "archived_at": "2026-11-12T10:00:00+00:00",
              "reset_reason": "sweep_done_issue"}
    assert wg._atomic_json_write(wg.reset_record_path(repo, "2281", "coding"), record)
    env, prompt = {}, "BASE"
    p, injected = wg.inject_reset_context(
        {"repo_key": repo, "agent_type": "coding", "issue_id": "#2281",
         "task_id": "t_push"}, env, prompt)
    assert injected is True
    assert "git push origin HEAD:refs/heads/feat/issue-2281" in p
    assert "git fetch <bundle_path>" in p


def test_inject_reset_context_auto_restored_no_manual_reset(repo):
    """AC2/AC4：auto_restored 记录 → prompt 声明已自动找回，只需重试 push，不再要求手工 reset。"""
    record = {"worktree_reset": True, "lost_commits": ["abc"], "unpushed_count": 1,
              "bundle_path": "/ws/x/worktree.bundle", "branch": "feat/issue-2282",
              "archived_at": "2026-11-12T10:00:00+00:00",
              "reset_reason": "sweep_done_issue",
              "auto_restored": True, "auto_restored_head": "d" * 40}
    assert wg._atomic_json_write(wg.reset_record_path(repo, "2282", "coding"), record)
    env, prompt = {}, "BASE"
    p, injected = wg.inject_reset_context(
        {"repo_key": repo, "agent_type": "coding", "issue_id": "#2282",
         "task_id": "t_auto"}, env, prompt)
    assert injected is True
    assert "restored from the archived bundle" in p
    assert "do NOT re-implement" in p
    assert "git push origin HEAD:refs/heads/feat/issue-2282" in p
    info = json.loads(env["GRAPE_WORKTREE_RESET_INFO"])
    assert info["auto_restored"] is True


def test_sweep_base_branch_env_precedence(monkeypatch):
    """sweeper 未推送判定使用场景开发基线（env 优先），而非恒为 origin/main。"""
    from src.domains.runtime.engine import cleanup
    monkeypatch.setenv("GRAPE_WORK_BRANCH", "arch-weekly-11-zj")
    assert cleanup._sweep_base_branch("gitcode:owner/repo") == "arch-weekly-11-zj"
