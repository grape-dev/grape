"""S121 场景用例（D94 OAuth 未配置 + 用户 PAT 场景）：
GitHub OAuth 渠道未配置时，用户密码注册登录 → 配置 GitHub PAT →
全链路（提单/回帖/任务）功能完好性守卫。

背景（2026-11-07 用户指定场景深度核对）：
- 登录：/api/auth/oauth/providers 按 configured=bool(client_id) 渲染——
  渠道卡在但 client_id 空 → 无 GitHub 登录按钮，用户走密码/邮箱注册
  （grape_accounts 本地账号体系，独立于 OAuth）。
- PAT 配置：PUT /settings/credentials {github_token}（专用字段路径，
  _LEGACY_PAT_FIELD_PROVIDERS）→ 验证 api.github.com/user → 写
  external_identities（access_token=PAT 加密，status=1，refresh_token
  为空）+ unified_user_mapping.github_login。**不写 user_credentials
  <p>_pat**（专用字段路径语义）。
- 消费：TokenResolver 第 4 级 fetch_user_token ①
  get_user_token_for → _get_oauth_token_from_identity（status IN
  (1,4) 解密）→ 命中 PAT 值；_has_refresh_token=False（refresh_token
  空）→ 跳过刷新（R2：PAT 不过期不刷新，防误打刷新端点）。
- 建单特例：create_task_via_issue 的 github 分支按 Issue #343 拍板
  统一走仓库 bot token（get_github_token），用户 PAT 不参与 github
  建单（归属=仓库 bot；用户 PAT 用于 chat 回帖等触发者场景）。
- 401 文案：github 用户路径走 BOT_TOKEN_INVALID_MSG（bot 文案）——
  与 gitcode 用户路径的 GITCODE_USER_TOKEN_INVALID_MSG 不对称，
  属 Issue #343 决策的连带，登记不修。

UC↔用例映射（UC25，requirements 场景用例表续编）：
- UC25① PAT 写入链守卫：github PAT 配置必写 external_identities
  （access_token 加密 + status=1 + refresh_token 空）——消费者读取
  链的载体
- UC25② PAT 刷新豁免守卫：_has_refresh_token 空 → get_user_token_for
  跳过刷新直返（防 PAT 被误送刷新端点）
- UC25③ github 建单 bot 拍板守卫：create_task_via_issue github 分支
  统一仓库 bot（Issue #343），不静默切用户 PAT
- UC25④ 401 文案不对称登记守卫：github 用户路径走 bot 文案（现状
  锁定，待后续批次的用户文案对称化）
"""
import re
from pathlib import Path

_TEST_DIR = Path(__file__).resolve().parent
_SRC = _TEST_DIR.parent / "src"


def _func_source(src: str, func_name: str) -> str:
    m = re.search(rf"def {func_name}\b", src)
    assert m, f"function {func_name} not found"
    nxt = re.search(r"\ndef ", src[m.end():])
    return src[m.start():m.end() + nxt.start()] if nxt else src[m.start()]


def test_uc25_pat_writes_identity_domain():
    """UC25①：github PAT 配置写入链守卫——专用字段路径必须写
    external_identities（access_token 加密）；消费者读取链的载体在场。"""
    s_src = (_SRC / "api" / "routes" / "settings.py").read_text(encoding="utf-8")
    m = re.search(r'if "github_token" in explicit:.*?(?=\n    if "|\n    for _provider)', s_src, re.S)
    assert m, "github_token 专用字段段 not found"
    body = m.group(0)
    assert "update_external_identity_tokens(" in body and "create_external_identity(" in body, (
        "github PAT 写入链丢失 external_identities 载体（更新/新建两分支）"
    )
    # D105k 解耦收口：PAT 权威存储 = 凭据域 github_pat（set_credential 写 token），
    # ei.access_token 置空（OAuth 独占）——旧「access_token=_gh_tok 直写 ei」锚点已废弃。
    assert "github_pat" in body and '"token": _gh_tok' in body, (
        "github PAT 值未写入凭据域 github_pat（D105k：PAT 权威存储=凭据域）"
    )
    assert 'access_token=""' in body, (
        "ei.access_token 必须置空（OAuth 独占，清 PAT 残留）"
    )
    # 读取链载体（同表）
    d_src = (_SRC / "api" / "deps.py").read_text(encoding="utf-8")
    read_body = _func_source(d_src, "_get_oauth_token_from_identity")
    assert "external_identities" in read_body and "status IN (1, 4)" in read_body, (
        "_get_oauth_token_from_identity 读取链漂移"
    )


def test_uc25_pat_refresh_exempt():
    """UC25②：PAT 刷新豁免守卫——_has_refresh_token 基于 refresh_token
    列空判断，get_user_token_for 无 refresh_token 直接返回不进刷新分支
    （防 PAT 被误送刷新端点）。"""
    d_src = (_SRC / "api" / "deps.py").read_text(encoding="utf-8")
    body = _func_source(d_src, "get_user_token_for")
    # 豁免分支先于刷新分支
    idx_exempt = body.find("_has_refresh_token")
    idx_refresh = body.find("_try_refresh_user_token")
    assert 0 <= idx_exempt < idx_refresh, (
        "刷新豁免分支必须先于刷新调用（PAT 无 refresh_token 不刷新，R2 语义）"
    )
    g_src = (_SRC / "domains" / "mgmt" / "auth" / "grape_account_db.py").read_text(encoding="utf-8")
    h_body = _func_source(g_src, "_has_refresh_token")
    assert "refresh_token FROM external_identities" in h_body, (
        "_has_refresh_token 数据源漂移（应基于 external_identities.refresh_token）"
    )


def test_uc25_github_issue_creation_uses_repo_bot():
    """UC25③：github 建单 bot 优先守卫（Issue #343 → D107-D 语义演进）——
    create_task_via_issue 的 github 分支仓库 bot 优先（归属=仓库 bot），
    bot 缺席时回落用户 token（D107-D：部署实弹发现"只查 bot 不回落"导致
    github 建单 400/502——对齐 gitcode/gitee 链；双侧均无 → 400 明确引导）。"""
    ic_src = (_SRC / "api" / "issues_center.py").read_text(encoding="utf-8")
    body = _func_source(ic_src, "create_task_via_issue")
    m = re.search(r"elif _is_github:.*?(?=\n    else:)", body, re.S)
    assert m, "github 分支 not found"
    gh_body = m.group(0)
    assert "get_github_token" in gh_body, "github 分支丢失仓库 bot 链"
    # D107-D 新契约：bot 优先（先 get_github_token）+ 缺席回落用户 token
    assert "get_user_token_for_repo" in gh_body, (
        "github 建单分支缺用户 token 回落——D107-D（bot 缺席时建单 400/502，"
        "实弹验证要求对齐 gitcode/gitee 回落链）"
    )
    assert gh_body.index("api_token = get_github_token") < gh_body.index(
        "api_token = get_user_token_for_repo"), (
        "回落顺序倒置：必须仓库 bot 优先、用户 token 仅在 bot 缺席时兜底"
    )


def test_uc25_github_401_msg_asymmetry_registered():
    """UC25④：401 文案不对称现状锁定——github 用户路径（走 bot）401
    文案=BOT_TOKEN_INVALID_MSG；gitcode 用户路径=GITCODE_USER_TOKEN_
    INVALID_MSG。对称化属后续批次，本守卫防现状被无意改写。"""
    te_src = (_SRC / "domains" / "runtime" / "vcs" / "token_error.py").read_text(encoding="utf-8")
    assert "BOT_TOKEN_INVALID_MSG" in te_src and "GITCODE_USER_TOKEN_INVALID_MSG" in te_src, (
        "token_error.py 两套文案常量丢失"
    )
    ic_src = (_SRC / "api" / "issues_center.py").read_text(encoding="utf-8")
    assert "GITCODE_USER_TOKEN_INVALID_MSG" in ic_src, (
        "gitcode 用户 401 文案引用丢失"
    )
