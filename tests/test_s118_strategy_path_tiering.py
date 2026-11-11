"""S118 场景用例（D91 仓库凭据策略链收口）：路径分档显式化 + GlobalBot 缺失告警。

背景（2026-11-07 用户评审 D90 契约清单时实码核对沉淀）：
- TokenResolver 策略链按仓库路径分档——路径 A（平台 bot 托管）= RepoBot
  → GlobalBot 兜底、无用户 token；路径 B（用户托管）= 归属者 User token
  第一公民、bot 系全跳过。该分档语义只活在代码注释，本批守卫锁死防漂移。
- GlobalBotToken 缺失曾静默返回空串（路径 A UserToken 不生效 → 策略链必然
  失败），告警是唯一观测点——本批补告警 + 守卫。

UC↔用例映射（UC22，requirements 场景用例表续编）：
- UC22① 策略链路径分档守卫（AST 级：RepoBot/GlobalBot 的 _fetch 必含
  _is_path_b 跳过分支；UserToken._fetch 必含 host_user_id=0 短路）
- UC22② GlobalBot 缺失告警守卫（GlobalBotToken._fetch 空值必须 log.warning，
  静默返回空串=配置错误不可见）
- UC22③ 场景行为验证：路径 A 仓 GlobalBot 缺失 → 策略链空 + 告警在场；
  路径 B 仓 → 归属者 User token 胜出、bot 系零调用
- UC22④ 写入侧单写守卫（S82 语义：add_managed_repo 源码零 managed_repos
  bot 列写回——迁移中间态只许存在于读取侧）
"""
import ast
import re
from pathlib import Path

_TEST_DIR = Path(__file__).resolve().parent
_SRC = _TEST_DIR.parent / "src"


def _class_source(module_path: Path, class_name: str) -> str:
    """提取模块中指定类的源码段（AST 定位 + 行号切片）。"""
    src = module_path.read_text(encoding="utf-8") if hasattr(module_path, "read_text") else str(module_path)
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            lines = src.split("\n")
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"class {class_name} not found in {module_path}")


def _method_source(cls_src: str, method_name: str) -> str:
    m = re.search(rf"def {method_name}\b", cls_src)
    assert m, f"method {method_name} not found"
    nxt = re.search(r"\n    def |\n    @", cls_src[m.end():])
    end = m.end() + nxt.start() if nxt else len(cls_src)
    return cls_src[m.start():end]


def test_uc22_strategy_path_tiering():
    """UC22①：策略链路径分档结构守卫——路径 B 跳过 bot 系、路径 A 拒绝
    用户 token，分档语义从注释升为守卫（防"空则继续"式漂移）。"""
    tr_src = (_SRC / "token_resolver.py").read_text(encoding="utf-8")
    repo_bot = _method_source(_class_source(tr_src, "RepoBotToken"), "_fetch")
    global_bot = _method_source(_class_source(tr_src, "GlobalBotToken"), "_fetch")
    user_token = _method_source(_class_source(tr_src, "UserToken"), "_fetch")
    assert "_is_path_b" in repo_bot, "RepoBotToken._fetch 丢失路径 B 跳过分支"
    assert "_is_path_b" in global_bot, "GlobalBotToken._fetch 丢失路径 B 跳过分支"
    assert "host_user_id == 0" in user_token or "host_user_id==0" in user_token \
        or 'str(host_user_id or "") in ("", "0")' in user_token, (
        "UserToken._fetch 丢失路径 A 短路（host_user_id 为 0/空返回空；"
        "D105b 起 int 比较已改字符串哨兵 in (\"\", \"0\")——uuid str user_id 下"
        "int 比较恒 False）——路径 A 走用户 token = 机器身份语义被破坏"
    )


def test_uc22_global_bot_missing_warns():
    """UC22②：GlobalBotToken._fetch 空值必须告警——路径 A 的唯一观测点，
    静默返回空串 = 配置错误不可见（D91 收口）。"""
    tr_src = (_SRC / "token_resolver.py").read_text(encoding="utf-8")
    fetch = _method_source(_class_source(tr_src, "GlobalBotToken"), "_fetch")
    # 结构断言：空值分支存在且走 log.warning
    assert "if not token" in fetch, "GlobalBotToken._fetch 缺空值分支"
    assert "log.warning" in fetch, "GlobalBotToken._fetch 空值未告警"


def test_uc22_path_tiering_behavior(tmp_path, monkeypatch, caplog):
    """UC22③：行为验证——路径 A GlobalBot 缺失→空+告警；路径 B→归属者
    token 胜出（bot 系零调用）。告警捕获用 pytest caplog（手工 root
    handler 在 pytest logging 插件下不可靠）。"""
    import logging
    from src import token_resolver as TR

    provider = TR.create_provider("gitcode")

    # 路径 A：host_user_id=0、rc 无值、全局 bot 缺失
    monkeypatch.setattr(TR, "create_provider", lambda p: provider, raising=False)
    monkeypatch.setattr("src.domains.mgmt.user.db.get_host_user_id", lambda rk: 0)
    monkeypatch.setattr("src.domains.mgmt.user.db.get_repo_credential_value", lambda rk, ct: "")
    monkeypatch.setattr("src.domains.mgmt.user.db.get_global_bot_token_for", lambda p: "")
    monkeypatch.setattr("src.domains.mgmt.user.db.get_meta_value", lambda k: "")

    resolver = TR.TokenResolver(provider)

    with caplog.at_level(logging.WARNING):
        result = resolver.resolve("gitcode:org/repo")
    assert result.token == "", "路径 A GlobalBot 缺失应返回空（不降级到用户 token）"
    assert any("global bot token missing" in m for m in caplog.messages), (
        f"GlobalBot 缺失未告警——配置错误不可见（捕获到: {caplog.messages[:3]}）"
    )

    # 路径 B：host_user_id=42 → UserToken 胜出（归属者身份）
    monkeypatch.setattr("src.domains.mgmt.user.db.get_host_user_id", lambda rk: 42)
    monkeypatch.setattr(
        "src.domains.mgmt.auth.grape_account_db.get_grape_account_by_id",
        lambda uid: {"username": "host_user"} if uid == 42 else None)
    # mock 网络缝（provider.fetch_user_token 真实现走 deps→external_identities）
    monkeypatch.setattr(
        type(provider), "fetch_user_token",
        lambda self, username, user_id: f"{username}_oauth_token")

    result_b = resolver.resolve("gitcode:org/repo-b")
    assert result_b.token == "host_user_oauth_token", (
        f"路径 B 应归属者 User token 胜出，实际 token={result_b.token!r} level={result_b.level}"
    )
    assert result_b.level == "user", f"路径 B 胜出级别应为 user，实际 {result_b.level}"


def test_uc22_add_managed_repo_single_write():
    """UC22④：写入侧单写守卫（S82 语义）——add_managed_repo 源码零
    managed_repos bot 列写回（迁移中间态只许存在于读取侧回落）。"""
    db_src = (_SRC / "domains" / "mgmt" / "user" / "db.py").read_text(encoding="utf-8")
    m = re.search(r"def add_managed_repo\b", db_src)
    assert m, "add_managed_repo not found"
    nxt = re.search(r"\ndef ", db_src[m.end():])
    body = db_src[m.start():m.end() + nxt.start()] if nxt else db_src[m.start():]
    # bot 凭据列不得出现在 UPDATE managed_repos / 列式写入形态中
    offenders = [col for col in ("bot_gitcode_token=", "bot_github_token=",
                                 "gitcode_webhook_token=", "github_webhook_token=")
                 if col in body and "UPDATE managed_repos" in body]
    assert not offenders, (
        f"add_managed_repo 出现 managed_repos 凭据列写回（破坏 S82 单写权威）: {offenders}"
    )
    assert "grape_t_repo_credentials" in body, "add_managed_repo 丢失 grape_t_repo_credentials 单写"
