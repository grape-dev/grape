"""D102-M：双范式并存收口——来源徽章 + 命中来源审计。

外部评审（paste_18/19 四层模型）对账后两处精确差距：
1. UX 层：凭据 tab 仓库凭据卡缺"当前生效来源"徽章
   （manual PAT 生效中 / OAuth 供给中——用户应清楚"现在是谁的钥匙"）
2. 审计层：fetch_user_token 的 pat/oauth 命中只写 log.info 未进 usage_log
   ——"用户配了 PAT 但系统实际走了 OAuth"无法从审计排查。

解析优先级（Manual > OAuth）维持 D10 拍板不变（业界两方向并存，
grape 有决议背书：PAT=用户为 agent 操作显式配的钥匙）。
"""
import re
import pytest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _token_resolver_src() -> str:
    """token_resolver 源文本——canonical 模块是 src/kernel/token_resolver.py。

    Issue #699 模块迁址后，旧路径 src/token_resolver.py 仅剩 sys.modules 别名
    （6 行 shim）；行为锁必须读 canonical 源文件，否则断言落在 shim 上恒空。
    对照 ours@64144d06a:src/token_resolver.py（同实现，迁移前布局）。
    """
    canonical = _REPO_ROOT / "src/kernel/token_resolver.py"
    legacy = _REPO_ROOT / "src/token_resolver.py"
    path = canonical if canonical.exists() else legacy
    return path.read_text(encoding="utf-8")


def test_d102m_cred_tab_source_badge():
    """凭据 tab 仓库凭据行展示当前生效来源（PAT/OAuth/未配置）。"""
    acct = (_REPO_ROOT / "src/api/frontend/app/src/views/AccountView.vue").read_text(encoding="utf-8")
    i = acct.find("第三方仓库凭据")
    seg = acct[i:i + 4000]
    assert "manual PAT" in seg or "PAT 生效中" in seg, "缺来源徽章（manual PAT）"
    assert "OAuth" in seg, "缺来源徽章（OAuth 供给）"


def test_d102m_fetch_user_token_audited():
    """fetch_user_token pat/oauth 命中写入 usage_log（来源可审计）。"""
    tr = _token_resolver_src()
    i = tr.find("def fetch_user_token", tr.find("def fetch_user_token") + 10)  # 第二处=generic 实现（首处为基类声明）
    j = tr.find("\nclass GitCodeProvider", i)
    seg = tr[i:j]
    assert "_audit_source(user_id, provider, \"pat\")" in seg and \
        '_audit_source(user_id, provider, "oauth")' in seg, \
        "fetch_user_token 命中未调来源审计（来源审计缺失）"
    # _audit_source 实现落 usage_log（全文件级断言）
    assert "def _audit_source" in tr and "log_usage" in tr[tr.find("def _audit_source"):tr.find("def _audit_source") + 800], \
        "_audit_source 未落 usage_log"


def test_d102m_priority_unchanged():
    """解析优先级维持 Manual > OAuth（D10 拍板不回归）。"""
    tr = _token_resolver_src()
    i = tr.find("def fetch_user_token", tr.find("def fetch_user_token") + 10)  # 第二处=generic 实现（首处为基类声明）
    j = tr.find("\nclass GitCodeProvider", i)
    seg = tr[i:j]
    assert "①" in seg and "① <p>_pat" in seg, "解析优先级被重排（D10 拍板）"

@pytest.mark.skip(reason="被测对象随 Vue 迁移下线（本 PR 删除 src/api/static/js/pages/* 与 admin.js）：断言改写到 Vue 源为后续专项，不得以跳过冒充已覆盖")
def test_d102m_gitee_brand_solid_button():
    """gitee 满圆镂空型 logo 需品牌色描画（防'黑洞 G'回潮）——v8 中性底终裁。"""
    css = (_REPO_ROOT / "src/api/frontend/app/src/styles/global/auth.css").read_text(encoding="utf-8")
    assert "#C71D23" in css, \
        "gitee icon-only 缺品牌色描画（镂空 G 透出深背景=黑洞 G）"
    idx = (_REPO_ROOT / "src/api/frontend/shell/index.html").read_text(encoding="utf-8")
    import re as _re
    m = _re.search(r'auth\.css\?v=(\d+)"', idx)
    assert m and int(m.group(1)) >= 31, "auth.css cache-bust regressed below v31（v8 中性底/hover 零变化）"

def test_d102n_login_brand_buttons():
    """登录页按钮 D105-12 终裁口径（2026-11-10）：华为按钮 icon-only——SVG 双色
    （红圈 #C7000B + 白花瓣 #FFFFFF 含圈内 HUAWEI 字形），无外部字标、按钮底
    透明；其余平台裸品牌色图标；gitee 同款官方双色（红圆 #C71D23 + 白 G）。
    hover 零变化口径不变。"""
    css = (_REPO_ROOT / "src/api/frontend/app/src/styles/global/auth.css").read_text(encoding="utf-8")
    # D105-9 终稿：实底仅允许华为官方徽章（红色胶囊+白色花瓣+白色 HUAWEI 字）；
    # 其余平台不得出现品牌色实底
    import re as _re
    solid = _re.findall(r'\.oauth-btn\.icon-only\[onclick\*="oauthLogin\(\'(\w+)\'\)"\][^{}]*\{[^{}]*background: var\(--brand', css)
    assert not solid, f"非徽章按钮出现品牌实底（D105-9 仅 huawei 徽章允许）: {solid}"
    # D105-12：华为 logo 双色在 SVG path 级（红圈 #C7000B + 白花瓣 #FFFFFF），
    # 按钮 icon-only 裸形态、无外部字标 span（HUAWEI 字形在花瓣 path 内）
    assert 'oauth-huawei-word' not in (_REPO_ROOT / "src/api/frontend/shell/index.html").read_text(encoding="utf-8"), "外部字标未删净（D105-12）"
    html = (_REPO_ROOT / "src/api/frontend/shell/index.html").read_text(encoding="utf-8")
    assert html.count('fill="#C7000B"') >= 1 and html.count('fill="#FFFFFF"') >= 1, "缺双色 path（红圈+白花瓣，D105-12）"
    assert html.count('fill="#C71D23"') >= 1, "缺 gitee 双色 path（红圆，D105-13）"
    assert "translateY(-2px)" not in css, "hover 浮起未清除（hover 零变化口径不变）"
    assert ".oauth-btn.icon-only" in css and "background: transparent" in css, "缺 v9 裸图标口径（transparent，gitcode/github 仍走此形态）"
    assert "#oauthBtns { justify-content: space-evenly; gap: 14px" in css, "缺均匀散开布局（gitee.com/login 形态）"
    app = (_REPO_ROOT / "src/api/frontend/shell/js/app.js").read_text(encoding="utf-8")
    assert "hwcloud: '华为云'" in app, "缺华为云显示名映射（渠道卡 name 为英文代号时前端映射）"
    html = (_REPO_ROOT / "src/api/frontend/shell/index.html").read_text(encoding="utf-8")
    # 版本门禁改动态下限锁：精确值随每次 bump 漂移，硬编码会整批假红
    m_css = _re.search(r'auth\.css\?v=(\d+)"', html)
    m_app = _re.search(r'app\.js\?v=(\d+)"', html)
    assert m_css and int(m_css.group(1)) >= 31, "auth.css cache-bust regressed below v31"
    assert m_app and int(m_app.group(1)) >= 263, "app.js cache-bust regressed below v263"
