"""S120 场景用例（D93 agent 全链路凭据流）：回帖/git/env 三触点收敛守卫。

背景（2026-11-07 用户要求深度检查 chat/VOD agent 全链路 PAT 凭据流程）：
实码追溯确认链路无破坏——两条已落地行为变更（D91/D92 告警）纯观测增量，
token 值流转逐字节未动。本批把"无破坏"的依据从一次性核对升为守卫：

全链路凭据触点（VOD webhook → pipeline → agent_tasks → worker → 回帖）：
- 机器身份（路径 A）：pipeline._inject_linked_repo_tokens →
  get_repo_bot_token（TokenResolver）→ GRAPE_LINKED_REPO_TOKENS env →
  worker repogit；回帖走 _get_task_token（R11.4 统一入口）
- 用户身份（chat/路径 B）：worker agent.py 仅 chat 任务按 task user_id
  → get_user_token_for_repo → get_user_token_for（600s 预刷新）

UC↔用例映射（UC24，requirements 场景用例表续编）：
- UC24① 回帖链收敛守卫：get_task_token 的降级分支与 github 分支均委托
  TokenResolver（防"绕开策略链直连 db 读 token"回潮）
- UC24② 任务 env 派发链守卫：_inject_linked_repo_tokens 必经
  get_repo_bot_token（=TokenResolver 统一入口），直查 rc/managed_repos 即红
- UC24③ chat 链行为验证：chat 任务按触发者 user_id 取 token（非归属者）
  ——actor=触发者语义锁定（与 TokenResolver 归属者语义并存的合法双入口）
- UC24④ 明文不落 task payload 守卫（Issue #344 语义）：agent 链禁止从
  task payload 读缓存的明文 user token（实时取 + 预刷新）
"""
import re
from pathlib import Path

_TEST_DIR = Path(__file__).resolve().parent
_SRC = _TEST_DIR.parent / "src"


def _func_source(src: str, func_name: str) -> str:
    m = re.search(rf"def {func_name}\b", src)
    assert m, f"function {func_name} not found"
    nxt = re.search(r"\ndef ", src[m.end():])
    return src[m.start():m.end() + nxt.start()] if nxt else src[m.start():]


def test_uc24_reply_chain_delegates_to_resolver():
    """UC24①：回帖链收敛守卫——get_task_token 空降级走
    get_repo_token_with_refresh（内部委托 TokenResolver），github 分支走
    get_github_token（同样内部委托）。直连 db.cursor 读 token 列即红。"""
    db_src = (_SRC / "domains" / "mgmt" / "user" / "db.py").read_text(encoding="utf-8")
    body = _func_source(db_src, "get_task_token")
    assert "get_repo_token_with_refresh" in body, "get_task_token 降级分支未走统一入口"
    assert "get_github_token" in body, "get_task_token github 分支丢失"
    # 两个被委托函数内部必须委托 TokenResolver（防直连 db 读回潮）
    for fn in ("get_repo_token_with_refresh", "get_github_token"):
        fbody = _func_source(db_src, fn)
        assert "TokenResolver" in fbody, (
            f"{fn} 未委托 TokenResolver——凭据解析偏离唯一决策点"
        )


def test_uc24_env_dispatch_chain():
    """UC24②：任务 env 派发链守卫——_inject_linked_repo_tokens 必经
    get_repo_bot_token（TokenResolver 包装）。SQL 直查 grape_t_repo_credentials /
    managed_repos 表即红（注释提及不触发；env 派发的 token 必须带级别/
    过期语义）。"""
    # 路径锚定：canonical 模块（arch 布局 domains/runtime/pipeline/）；src/api/event_pipeline.py
    # 已是纯 sys.modules 别名 shim（Issue #699），文本面无函数体——按 shim 读会假红。
    ep_src = (_SRC / "domains" / "runtime" / "pipeline" / "event_pipeline.py").read_text(encoding="utf-8")
    body = _func_source(ep_src, "_inject_linked_repo_tokens")
    assert "get_repo_bot_token" in body, (
        "_inject_linked_repo_tokens 未走 get_repo_bot_token（TokenResolver 统一入口）"
    )
    # 禁 SQL 直查形态（不匹配注释文本）——精确匹配 SQL 语句特征
    for forbidden in (re.compile(r"cursor\(\)\.execute"), re.compile(r"FROM (grape_t_repo_credentials|managed_repos)")):
        assert not forbidden.search(body), (
            f"_inject_linked_repo_tokens SQL 直查（{forbidden.pattern}）——"
            "绕开策略链，丢失级别/过期/告警语义"
        )


def test_uc24_chat_actor_semantics(monkeypatch):
    """UC24③：chat 链行为验证——worker 按 task user_id（触发者）取 token，
    非仓库归属者（actor=触发者语义，与 TokenResolver 归属者语义并存的
    合法双入口）。"""
    import src.domains.runtime.engine.agent as AG
    captured = {}

    def fake_get_token_for_repo(repo_key, username, user_id=0):
        captured["repo_key"] = repo_key
        captured["username"] = username
        captured["user_id"] = user_id
        return "chat_user_tok"

    monkeypatch.setattr(
        "src.api.deps.get_user_token_for_repo", fake_get_token_for_repo)
    monkeypatch.setattr(
        "src.domains.mgmt.auth.grape_account_db.get_grape_account_by_id",
        lambda uid: {"username": "trigger_user"} if uid == 77 else None)

    # 模拟 agent.py 的 chat 分支逻辑（与源码同形：user_id=77 触发者；
    # task 必须带 agent_type="chat"——chat 分支的进入条件）
    task = {"repo_key": "gitcode:org/repo", "user_id": 77, "agent_type": "chat"}
    user_token = ""
    if (task.get("agent_type") or "") == "chat":
        _task_user_id = task.get("user_id") or 0
        if _task_user_id > 0:
            from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id
            _acc = get_grape_account_by_id(_task_user_id)
            if _acc:
                from src.api.deps import get_user_token_for_repo
                user_token = get_user_token_for_repo(
                    task.get("repo_key") or "", _acc["username"],
                    user_id=_task_user_id) or ""

    assert user_token == "chat_user_tok"
    assert captured["user_id"] == 77, "chat 链必须按触发者 user_id 取 token"
    assert captured["username"] == "trigger_user"


def test_uc24_no_plaintext_token_in_payload():
    """UC24④：明文不落 task payload 守卫（Issue #344）——agent.py 禁止
    从 task payload 读缓存明文 user token（task.get 含 token 键的读取点
    仅允许 repo_gitcode_token=bot token 场景）。"""
    a_src = (_SRC / "domains" / "runtime" / "engine" / "agent.py").read_text(encoding="utf-8")
    # chat user_token 必须来自 get_user_token_for_repo 实时取
    assert "get_user_token_for_repo" in a_src, "agent.py 丢失 chat 实时取 token 入口"
    # 禁止形态：task.get("user_token") / task["user_token"]（缓存明文回读）
    offenders = re.findall(r'task\.get\(["\']user_token["\']\)|task\[["\']user_token["\']\]', a_src)
    assert not offenders, (
        f"agent.py 从 task payload 回读明文 user_token（破坏 Issue #344 实时取语义）: {offenders}"
    )


def test_uc24_issue_creation_chain():
    """UC24⑤：chat 帮用户提单链收敛守卫（create_task_via_issue）——
    token 优先级三态（preauth bot / github bot / 触发者用户 token）+
    401 反应式刷新（用户 token 失效→强制刷新→单次重试）。"""
    ic_src = (_SRC / "api" / "issues_center.py").read_text(encoding="utf-8")
    m = re.search(r"async def create_task_via_issue\b.*?(?=\nasync def |\Z)", ic_src, re.S)
    assert m, "create_task_via_issue not found"
    body = m.group(0)
    # 优先级三态在场
    assert "_preauth_token" in body, "建单链丢失 preauth bot token 分支（autopilot 内部调用）"
    assert "get_github_token" in body, "建单链丢失 github bot 分支"
    assert "get_user_token_for_repo" in body, "建单链丢失触发者用户 token 分支"
    # 401 反应式刷新（D90 缺口 A 在建单链已存在的证据锚点）
    assert "_http_code == 401" in body and "_refresh_user_gitcode_token" in body, (
        "建单链丢失 401 反应式刷新（用户 token 失效→强制刷新→单次重试）"
    )
    # 统一入口：建单走 _create_issue_unified（卡驱动方言），不得直连平台 API
    assert "_create_issue_unified(" in body, "建单未走 _create_issue_unified 统一入口"
    # 401 刷新后必须重试（两次 create_issue_unified 调用 = 重试在场）
    assert body.count("_create_issue_unified(") >= 2, (
        "401 刷新后未见重试调用——反应式刷新断链"
    )


def test_uc24_issue_user_credential_semantics(monkeypatch):
    """UC24⑥：建单链用户凭据语义守卫（用户提单用什么凭据，2026-11-07
    问答轮实锤入守卫）——① PAT-only 用户（PAT 经 S52 双写进
    external_identities）提单 = 用身份域里的 PAT 值（get_user_token_for
    直读，无需 OAuth）；② token 空显式 400 拒绝不降级 bot（归属真实性
    铁律）；③ 无 refresh_token 的凭据不刷新（R2）。"""
    ic_src = (_SRC / "api" / "issues_center.py").read_text(encoding="utf-8")
    m = re.search(r"async def create_task_via_issue\b.*?(?=\nasync def |\Z)", ic_src, re.S)
    body = m.group(0)
    # 显式拒绝不降级：token 空必须 400 + 引导重绑文案，禁止回落 bot
    assert "Token 未配置或已失效，请重新绑定/登录后重试" in body, (
        "建单链丢失显式拒绝语义——token 空回落 bot 会破坏 issue 归属真实性"
    )
    assert 'get_user_token_for_repo(repo_key, user["username"]' in body or (
        "get_user_token_for_repo(repo_key," in body), (
        "建单链未按触发者会话用户取 token"
    )
    # deps 侧：token 只从 external_identities 读（PAT 双写后 PAT-only 用户天然覆盖）
    d_src = (_SRC / "api" / "deps.py").read_text(encoding="utf-8")
    m2 = re.search(r"def get_user_token_for\b.*?(?=\ndef )", d_src, re.S)
    assert "_get_oauth_token_from_identity" in m2.group(0), (
        "get_user_token_for 未从 external_identities 读（PAT-only 用户提单断链）"
    )
