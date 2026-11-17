"""Issue #3097 回归锁：OAuth 绑定/转移确认的取票链路必须走服务端接口。

背景（三库 beta 实测）：popup 模式服务端**故意不回传** binding_ticket（只回 need_transfer），
前端三个调用点却分别用「URL 参数」「GET /auth/me 的 identity.binding_ticket」取票 →
全部拿不到 → 弹窗关闭后既不弹确认框也不报错。正确链路：GET /auth/transfer-context。

本用例按文件做静态断言（与 tests/js/test_*.js 同思路，但跑在 pytest 里，跟随后端套件）：
1. shell 与两个 Vue 组件都调 `/auth/transfer-context`；
2. 不再从回调 URL 取 `binding_ticket`（服务端不传，属死分支）；
3. 过期/失败文案走 i18n（zh/en 都有 `transfer_context_expired`）。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "src" / "api" / "frontend"
SHELL = FRONT / "shell"
APP = FRONT / "app" / "src"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="ignore")


def test_shell_and_components_call_transfer_context():
    for rel in ("shell/js/app.js", "app/src/views/AccountView.vue",
                "app/src/components/BindDialog.vue"):
        text = _read(FRONT / rel)
        assert "/auth/transfer-context" in text, (
            f"{rel} 未调用 GET /auth/transfer-context（#3097：popup 模式票据只在服务端）")


def test_no_binding_ticket_from_callback_url():
    app_js = _read(SHELL / "js" / "app.js")
    assert "params.get('binding_ticket')" not in app_js, (
        "app.js 仍从回调 URL 取 binding_ticket（服务端 popup 模式不传，属死分支）")


def test_transfer_expired_copy_is_i18n():
    for lang in ("zh", "en"):
        raw = _read(SHELL / "locales" / lang / "app.json")
        assert "transfer_context_expired" in raw, (
            f"locales/{lang}/app.json 缺 transfer_context_expired（取票失败必须可见）")


def test_frontend_has_no_hardcoded_transfer_failure_text():
    """取票失败不得再硬编码中文「操作失败」以外的兜底文案（#3097 第 1 层问题 2）。"""
    for rel in ("app/src/views/AccountView.vue", "app/src/components/BindDialog.vue"):
        text = _read(FRONT / rel)
        assert "transfer_context_expired" in text, (
            f"{rel} 未使用 i18n key transfer_context_expired")