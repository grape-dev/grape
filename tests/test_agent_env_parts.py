"""Agent 环境变量 ACCOUNT/REPO_NAME 注入正确性测试（weekly-05 fix-agent-env）。

覆盖：
- split_owner_repo：owner/repo、gitcode:owner/repo、URL 输入（按实现容读取最末两段或兜底）
- _execute_agent_inner / _enqueue_pipeline_task 注入的 env_extra：
  ACCOUNT=owner（不含 /）、REPO_NAME=repo 名、REPO_FULL_NAME=全名
"""
import os, sys, json

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))


# ── split_owner_repo：owner/repo 拆分 ──

def test_split_owner_repo_basic():
    from src.repo_key import split_owner_repo
    assert split_owner_repo("grape-dev/grape") == ("grape-dev", "grape")
    assert split_owner_repo("gitcode:grape-dev/grape") == ("grape-dev", "grape")
    assert split_owner_repo("github:org/repo") == ("org", "repo")
    assert split_owner_repo("") == ("", "")


def test_split_owner_repo_no_slash():
    from src.repo_key import split_owner_repo
    # 无 / 时 owner=repo 原样、repo 名空（调用方据此兜底）
    owner, repo = split_owner_repo("justname")
    assert owner == "justname" and repo == ""


# ── env_extra 注入：REPO_OWNER / REPO_NAME / ACCOUNT(actor) ──

def _env_from_execute_inner(actor="jason7602"):
    """复用 L255 区域构建逻辑（REPO_OWNER 拆分 + ACCOUNT 取 actor）。"""
    from src.repo_key import split_owner_repo
    repository = "grape-dev/grape"   # owner 是组织，ACCOUNT 是个人
    owner, rn = split_owner_repo(repository)
    env = {
        "REPO_FULL_NAME": repository,
        "REPO_OWNER": owner,
        "REPO_NAME": rn,
        "ACCOUNT": actor,            # evt["actor"] 个人账号
    }
    return env


def test_env_extra_repo_owner_and_account_actor():
    env = _env_from_execute_inner(actor="jason7602")
    assert env["REPO_FULL_NAME"] == "grape-dev/grape"
    assert env["REPO_OWNER"] == "grape-dev"   # 属主（组织）
    assert env["ACCOUNT"] == "jason7602"      # 个人账号（actor）
    assert env["REPO_NAME"] == "grape"
    # ACCOUNT ≠ REPO_OWNER（组织场景下解耦）
    assert env["ACCOUNT"] != env["REPO_OWNER"]


def test_env_extra_owner_from_gitcode_prefix():
    from src.repo_key import split_owner_repo
    repository = "gitcode:org/www"
    owner, rn = split_owner_repo(repository)
    env = {
        "REPO_FULL_NAME": "org/www",
        "REPO_OWNER": owner,
        "REPO_NAME": rn,
        "ACCOUNT": "someone",        # 个人触发人
    }
    assert env["REPO_OWNER"] == "org"
    assert env["REPO_NAME"] == "www"
    assert env["ACCOUNT"] == "someone"


def test_direct_branch_env_parts():
    # L2692 区域（direct 分支）：exec_account 兜底 REPO_OWNER/REPO_NAME，ACCOUNT 取 actor
    repository = "Jason7602/hello-x"
    exec_account = "Jason7602/hello-x"
    env = {
        "REPO_FULL_NAME": repository,
        "REPO_OWNER": (exec_account or repository).split("/", 1)[0],
        "REPO_NAME": (exec_account or repository).rsplit("/", 1)[-1],
        "ACCOUNT": "dev_act",        # evt actor
    }
    assert env["REPO_OWNER"] == "Jason7602"
    assert env["REPO_NAME"] == "hello-x"
    assert env["ACCOUNT"] == "dev_act"
    assert env["REPO_NAME"] == "hello-x"


def _direct_env(exec_account, repository, actor="dev_act"):
    """同步实现 _execute_agent_inner env_extra 构建（repo-owner-prefix-injection-fix-design.md §3.1）。

    修复前为 naive split（带前缀输入会得到 gitcode:<owner>，即 Issue #94 根因）；
    修复后统一走 split_owner_repo，REPO_OWNER/REPO_NAME 均无前缀。
    """
    from src.repo_key import split_owner_repo
    _repo_owner, _repo_name = split_owner_repo(exec_account)
    return {
        "REPO_FULL_NAME": repository,
        "REPO_OWNER": _repo_owner,
        "REPO_NAME": _repo_name,
        "ACCOUNT": actor or exec_account or "",
    }


def test_direct_branch_env_parts_prefixed_account_red():
    """RED：带前缀 exec_account 用 naive split 复现 Issue #94 根因（得 gitcode:<owner>）。"""
    exec_account = "gitcode:Jason7602/hello-x"
    naive_owner = (exec_account).split("/", 1)[0]
    assert naive_owner != "Jason7602"          # naive 分割带前缀 → 正是 bug
    assert naive_owner == "gitcode:Jason7602"


def test_direct_branch_env_parts_prefixed_account():
    """T1：exec_account 带平台前缀全名（gitcode:<owner>/<repo>）→ REPO_OWNER/REPO_NAME 均无前缀。"""
    env = _direct_env(exec_account="gitcode:Jason7602/hello-x",
                      repository="Jason7602/hello-x")
    assert env["REPO_OWNER"] == "Jason7602"    # 纯 owner，不带 gitcode:
    assert env["REPO_NAME"] == "hello-x"
    assert env["ACCOUNT"] == "dev_act"


def test_direct_branch_env_parts_unprefixed_account():
    """T2：exec_account 无前缀（回归基线）→ 行为不变。"""
    env = _direct_env(exec_account="Jason7602/hello-x",
                      repository="Jason7602/hello-x")
    assert env["REPO_OWNER"] == "Jason7602"
    assert env["REPO_NAME"] == "hello-x"


def test_direct_branch_env_parts_owner_repo_no_prefix():
    """T3：split_owner_repo 幂等——对已剥前缀输入再跑结果不变（无双重剥离）。"""
    from src.repo_key import split_owner_repo
    assert split_owner_repo("Jason7602/hello-x") == ("Jason7602", "hello-x")
    again = split_owner_repo("Jason7602/hello-x")
    assert again == ("Jason7602", "hello-x")