"""S119 场景用例（D92 仓库凭据语义收口）：cred_type 值域隔离守卫 + 文件
fallback 告警 + 路径 A 失败语义 + chat 链汇聚点。

背景（2026-11-07 用户评审"三种 PAT"全景分析收口）：
- "三种 PAT"正名"三类仓库相关凭据"——第 2 类实为仓库级 bot 凭据
  （机器身份），与用户 PAT（用户身份）的主体差异正是路径 A/B 分档根因。
- 破坏面前提：oauth_fill 落库后 grape_t_repo_credentials 若允许 oauth_fill 行
  占用 bot 级 cred_type，RepoBotToken（路径 A 第 2 级）会按主键精确匹配
  读到用户凭据当机器凭据用——cred_type 值域隔离是第一道防线（免费），
  source 过滤是纵深第二层。本批守卫锁 cred_type 现值域，防 provisioner
  实现时复用 bot 级命名。
- 文件 fallback 命中告警（D92 产品代码唯一变更）：隐形第二权威源可观测。

UC↔用例映射（UC23，requirements 场景用例表续编）：
- UC23① cred_type 值域守卫：全仓 cred_type 字面量清点——bot 级命名空间
  只允许 S82 既有键，新凭据类型必须用独立命名（防 oauth_fill 复用）
- UC23② 文件 fallback 命中告警守卫（结构 + 行为 caplog）
- UC23③ 路径 A 失败语义行为验证：三级全空 → 空串（不落第 4 级），
  调用方显式处理——机器身份保护闭环
- UC23④ chat 链汇聚点守卫：get_user_token_for_repo 与 TokenResolver
  UserToken 底层同源（都收敛到 get_user_token_for）——刷新/预刷新/
  OAuth-PAT 顺序物理共用，D90 调序一次生效两条链
"""
import ast
import re

from src.kernel.session_manager import db, db_lock
import pytest
from src.kernel.session_manager import db, db_lock
from pathlib import Path

_TEST_DIR = Path(__file__).resolve().parent
_SRC = _TEST_DIR.parent / "src"

# S82 既有 cred_type 值域（managed_repos 列家族等价物）
_BOT_CRED_TYPES = {"bot_gitcode_token", "bot_github_token",
                   "gitcode_webhook_token", "github_webhook_token"}


def test_uc23_cred_type_namespace_isolation():
    """UC23①：cred_type 写入点收敛守卫——grape_t_repo_credentials 的写入文件
    白名单收敛（S82 权威写入=add_managed_repo / admin config_store 轮换 /
    db_setup backfill 三处既有点），新写入文件出现（尤其 provisioner 的
    oauth_fill 落库）即红——强制走 D90 契约评审，防 oauth_fill 复用 bot 级
    cred_type 命名（cred_type 主键精确匹配下，值域隔离是第一道免费防线）。"""
    offenders = []
    for py in sorted((_SRC).rglob("*.py")):
        if "test" in py.name:
            continue
        src = py.read_text(encoding="utf-8", errors="replace")
        if "grape_t_repo_credentials" in src and re.search(
                r"INSERT (OR REPLACE )?INTO grape_t_repo_credentials|UPDATE grape_t_repo_credentials", src):
            rel = str(py.relative_to(_SRC))  # 相对 src/，如 domains/mgmt/user/db.py
            if rel not in ("domains/mgmt/user/db.py", "domains/mgmt/admin/config_store.py", "api/db_setup.py",
                       "domains/mgmt/user/credentials_backfill_n1.py"):  # D90-N3 加密回填（bot 级同值，契约评审通过）
                offenders.append(rel)
    assert not offenders, (
        f"grape_t_repo_credentials 出现新写入文件（须走 D90 契约评审：oauth_fill 行"
        f"禁占 bot 级 cred_type，独立命名如 oauth_token）: {offenders}"
    )
    # oauth_fill 写入尚未实现（provisioner 待契约）：新写入文件触发本守卫
    # 红时，cred_type 值域约束（oauth_fill 禁占 bot 级命名）在 D90 评审钉死


def test_uc23_file_fallback_hit_warns(tmp_path, monkeypatch, caplog):
    """UC23②：文件 fallback 命中必须告警（D92）——隐形第二权威源可观测，
    下线判定依赖此信号。结构 + 行为双断言。"""
    import logging
    import src.domains.mgmt.user.db as udb

    # 结构断言
    src = (_SRC / "domains" / "mgmt" / "user" / "db.py").read_text(encoding="utf-8")
    m = re.search(r"def _cached_get_meta.*?(?=\ndef )", src, re.S)
    assert m and "FILE fallback" in m.group(0), "_cached_get_meta 缺文件 fallback 告警"

    # 行为断言：meta 空 + 未迁移 + 文件有值 → 告警在场
    monkeypatch.setattr(udb, "get_meta_value",
                        lambda k: "1" if k == "global_bot_token_migrated" else "")
    monkeypatch.setattr(udb, "_global_bot_cache", {})
    with caplog.at_level(logging.WARNING):
        val = udb._cached_get_meta("global_bot_gitcode_token",
                                   file_fallback_fn=lambda: "")
    # migrated=1 → 不走 fallback（语义保持）；换未迁移场景验证告警
    monkeypatch.setattr(udb, "get_meta_value",
                        lambda k: "" if k.startswith("global_bot") else "")
    monkeypatch.setattr(udb, "_global_bot_cache", {})
    with caplog.at_level(logging.WARNING):
        val = udb._cached_get_meta("global_bot_gitcode_token",
                                   file_fallback_fn=lambda: "file_tok_123")
    assert val == "file_tok_123", "fallback 值应返回"
    assert any("FILE fallback" in m for m in caplog.messages), (
        f"文件 fallback 命中未告警（隐形第二权威源不可见）: {caplog.messages[:3]}"
    )


def test_uc23_path_a_failure_semantics(monkeypatch):
    """UC22③ 姊妹验证：路径 A 三级全空 → 策略链返回空串（不落第 4 级），
    失败语义显式——调用方处理空 token，机器身份保护闭环。"""
    from src import token_resolver as TR

    provider = TR.create_provider("gitcode")
    monkeypatch.setattr("src.domains.mgmt.user.db.get_host_user_id", lambda rk: 0)
    monkeypatch.setattr("src.domains.mgmt.user.db.get_repo_credential_value", lambda rk, ct: "")
    monkeypatch.setattr("src.domains.mgmt.user.db.get_global_bot_token_for", lambda p: "")
    monkeypatch.setattr("src.domains.mgmt.user.db.get_meta_value", lambda k: "")
    # session 级也无
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", sm.db)  # session 查询走真库（空表）

    resolver = TR.TokenResolver(provider)
    result = resolver.resolve("gitcode:org/repo-a")
    assert result.token == "", "路径 A 三级全空必须返回空串（第 4 级被 host_user_id==0 短路）"
    assert result.level in ("", "user"), f"level 异常: {result.level}"


def test_uc23_chat_chain_converges(monkeypatch):
    """UC23④：chat 链汇聚点守卫——get_user_token_for_repo 底层收敛到
    get_user_token_for；TokenResolver UserToken 经 provider.fetch_user_token
    也收敛到同函数（两级链断言）——D90 OAuth/PAT 调序一次生效两条链
    （防两链顺序分叉）。"""
    d_src = (_SRC / "api" / "deps.py").read_text(encoding="utf-8")
    m = re.search(r"def get_user_token_for_repo\b.*?(?=\ndef )", d_src, re.S)
    assert m, "get_user_token_for_repo not found"
    assert "get_user_token_for(" in m.group(0), (
        "get_user_token_for_repo 未收敛到 get_user_token_for——取用面分叉，"
        "D90 OAuth/PAT 调序将只生效一条链"
    )
    # TokenResolver 侧两级链：UserToken → provider.fetch_user_token → get_user_token_for
    tr_src = (_SRC / "token_resolver.py").read_text(encoding="utf-8")
    m_user = re.search(r"class UserToken.*?(?=\nclass |\Z)", tr_src, re.S)
    assert m_user and "fetch_user_token" in m_user.group(0), (
        "UserToken._fetch 未走 provider.fetch_user_token 端口"
    )
    # GenericVcsProvider.fetch_user_token 是收敛点（①OAuth get_user_token_for → ②PAT）
    # D105b：user_id 已 TEXT/uuid 化——不再写死 int 签名（曾致假红）；
    # 结构锚点：锚定 GenericVcsProvider 类内的实现（VCSProvider ABC 桩在 143 行同签名会误中）
    m_prov = re.search(r"class GenericVcsProvider.*?def fetch_user_token\(self, username: str[^)]*\).*?(?=\n\nclass )", tr_src, re.S)
    assert m_prov, "GenericVcsProvider.fetch_user_token not found"
    assert "get_user_token_for(" in m_prov.group(0), (
        "provider.fetch_user_token 未收敛到 get_user_token_for——"
        "chat 链与 TokenResolver 链刷新/顺序将分叉，D90 调序无法一次生效"
    )


def test_n2_1_cred_type_whitelist_rejects_pat_form():
    """D90-N2①：grape_t_repo_credentials 值域守卫——*_pat 形态拒绝+抛错（合法路径零改动）。

    三写入入口：add_managed_repo（_CRED_COLS 白名单断言）/ config_store swap
    （卡派生字段断言）/ db_setup backfill（遍历白名单本体，结构零越界）。
    守卫只看 cred_type 字段值，不碰 token 内容（不做形态过滤）。
    """
    from src.domains.mgmt.user.db import add_managed_repo
    rk = "gitcode:n2pat/repo"
    # 清场
    with db_lock:
        db.execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
        db.execute("DELETE FROM grape_t_repo_credentials WHERE repo_key=?", (rk,))
        db.commit()
    try:
        # 建仓库行（合法路径先走通）
        assert add_managed_repo(rk, "n2pat", "repo") is True
        with db_lock:
            _n_before = db.execute(
                "SELECT COUNT(*) FROM grape_t_repo_credentials WHERE repo_key=?", (rk,)).fetchone()[0]
        # 越界写入：_extra 塞 *_pat 键 → 守卫拒绝+抛错
        with pytest.raises(ValueError, match="cred_type 越界"):
            add_managed_repo(rk, "n2pat", "repo", gitee_pat="pat_should_be_rejected")
        # 确认无落库
        with db_lock:
            _n_after = db.execute(
                "SELECT COUNT(*) FROM grape_t_repo_credentials WHERE repo_key=?", (rk,)).fetchone()[0]
            _pat_rows = db.execute(
                "SELECT COUNT(*) FROM grape_t_repo_credentials WHERE cred_type LIKE '%\\_pat' ESCAPE '\\'").fetchone()[0]
        assert _pat_rows == 0, "*_pat 形态行落库（守卫失效）"
        assert _n_after >= _n_before  # 合法行不受影响
    finally:
        with db_lock:
            db.execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
            db.execute("DELETE FROM grape_t_repo_credentials WHERE repo_key=?", (rk,))
            db.commit()
