"""D102-P 职责分域收口：PAT 归位凭据域，ei 不再承载 PAT token。

三条行为锁（源码级，防回潮）：
1. settings platform_tokens 写归属时 access_token 恒空（PAT 只进凭据域）；
2. PAT 供给链独立：fetch_user_token ① 命中凭据域 <p>_pat（双通道顺序）；
3. 启动回填存在且语义精确（解密比对命中才迁移历史 PAT-as-ei-token 行）。
"""
import re
from pathlib import Path

SETTINGS = Path(__file__).resolve().parent.parent.parent / "src" / "domains" / "mgmt" / "routes" / "settings.py"
TR = Path(__file__).resolve().parent.parent.parent / "src" / "token_resolver.py"
MAIN = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "main.py"
GADB = Path(__file__).resolve().parent.parent.parent / "src" / "auth" / "grape_account_db.py"


def test_pat_write_does_not_enter_ei_token():
    """A 批：PAT 不再触碰身份行（结构守卫随对象模型更新）。

    原断言为「归属行存在但 access_token 恒空 + mark_identity_pat 打标」。
    PAT = 仓库执行凭据归位后，PR 保不变量的加强形态是：泛型段内不得出现任何
    身份行写入 / 票据生成，且凭据域写入必须在场。
    """
    src = SETTINGS.read_text(encoding="utf-8")
    i = src.find("_platform_tokens = explicit.get")
    j = src.find("# 绑定/解绑平台后清理 user_repos 缓存", i)
    seg = src[i:j]
    assert "set_credential" in seg, "PAT 权威存储缺失（凭据域 <p>_pat）"
    for banned in ("create_external_identity", "update_external_identity_tokens",
                   "mark_identity_pat", "save_binding_ticket"):
        assert banned not in seg, f"PAT 泛型段仍触碰身份行/票据: {banned}"
    assert "access_token=_tok" not in seg, "PAT token 泄漏进 ei 归属行"


def test_pat_supply_chain_is_credential_domain_first():
    src = TR.read_text(encoding="utf-8")
    first = src.find("def fetch_user_token")
    i = src.find("def fetch_user_token", first + 1)   # 适配器实现（抽象基类之后）
    j = src.find("\n    def ", i + 10)
    seg = src[i:j if j > i else i + 4000]
    assert "get_credential" in seg and "_pat" in seg, "供给链缺凭据域 PAT ① 通道"
    pat_first = seg.find("pat_token = ")
    assert "get_user_token_for" in seg[pat_first:], "OAuth 兜底应在 PAT 之后（② 兜底）"


def test_startup_backfill_registered():
    src = MAIN.read_text(encoding="utf-8")
    assert "backfill_pat_identity_tokens" in src, "启动回填未注册"


def test_backfill_semantics_precise_match():
    src = GADB.read_text(encoding="utf-8")
    i = src.find("def backfill_pat_identity_tokens")
    seg = src[i:i + 5000]
    assert "decrypt(m.access_token)" in seg and "at.strip() != tok.strip()" in seg,         "回填必须精确比对（解密后 token strip 归一相等才迁移），防止 OAuth 行误清"
    assert 'm.access_token = ""' in seg, "迁移动作缺失（清空 ei token 字段）"
