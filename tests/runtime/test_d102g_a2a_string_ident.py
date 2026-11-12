"""D102-G：A2A issue_number 校验对齐平台现实——字符串 ident 合法化（行为锁）。

两条历史决策打架（用户实弹）：S70 确立 gitee issue ident 为字符串
（'IKTREU'，库中合法形态）；A2A 入口 2026-10-14 事故后的防垃圾校验
写成 isdigit()——把内部代理链（需求中心移交 issues.py:1257 直调，
payload 来自自家 DB）的合法 ident 拦成 400。

修法语义：拒绝"垃圾"目标不变（空白/#前缀/含空格分隔符的标题文本），
但接受合法 ident 形态（字母数字、≤32 长度）。同时清同族：
- webhook.py:907（A2A redo 查询接口同款 isdigit）
- event_pipeline.resolve_issue_card_id（ident 门槛致 GRAPE_ISSUE_ID 恒空）
"""
import os
os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import inspect


def _valid_ident_rule():
    """约定：校验函数名。webhook 内实现为 _is_valid_issue_number。"""
    from src.domains.runtime.routes import webhook
    assert hasattr(webhook, "_is_valid_issue_number"), \
        "webhook.py 缺 _is_valid_issue_number（D102-G 未实施）"
    return webhook._is_valid_issue_number


def test_d102g_int_still_valid():
    """int 形态合法（GitCode/GitHub 主路径不回归）。"""
    fn = _valid_ident_rule()
    assert fn("123") and fn(123)
    assert fn("0")  # 0 也允许（防御语义收窄到形态，不掺值域判断）


def test_d102g_string_ident_valid():
    """gitee 字符串 ident 合法（本批核心语义）。"""
    fn = _valid_ident_rule()
    assert fn("IKTREU") and fn("IKSZ3I")
    assert fn("abc123")


def test_d102g_garbage_rejected():
    """10-14 事故形态（垃圾）仍拒绝——防线不回潮。"""
    fn = _valid_ident_rule()
    assert not fn("")            # 空
    assert not fn("#1")          # # 前缀
    assert not fn("fix the login bug")  # 标题文本（含空格）
    assert not fn("issue-123")   # 连字符（非 ident 字符集）
    assert not fn(None)
    assert not fn("x" * 64)      # 超长（>32 非法 ident）


def test_d102g_a2a_endpoint_accepts_ident():
    """/api/a2a 端到端：字符串 ident 不再 400（垃圾仍 400）。"""
    src = inspect.getsource(__import__("src.domains.runtime.routes.webhook", fromlist=["x"]))
    # 端点校验点消费 _is_valid_issue_number（两处：POST /api/a2a + redo 查询）
    assert src.count("_is_valid_issue_number") >= 2, \
        "webhook 两处 A2A 入口未全部切换新校验"


def test_d102g_resolve_card_id_accepts_ident():
    """resolve_issue_card_id 字符串 ident 不再被门槛拦（GRAPE_ISSUE_ID 可解析）。"""
    import inspect
    from src.api import event_pipeline
    src = inspect.getsource(event_pipeline.resolve_issue_card_id)
    assert "isdigit" not in src, \
        "resolve_issue_card_id 仍保留 isdigit 门槛（ident 被拒→GRAPE_ISSUE_ID 恒空）"
