"""S116 场景用例（D89 双卡体系治理方向修正）：三域解耦结构守卫 + 挂账#5 文档对账。

背景（2026-11-07 用户拍板，业界锚点：GitHub OAuth App/GitHub App 双连接、
VeeCode SCM 轴×Identity 轴、Keycloak broker）：登录身份域与 VCS 资源集成域
领域分离是目标态而非待统一项；默认禁止合并领域实体/权限模型/凭据语义；
薄共享仅限三处基础设施（配置校验框架/展示元数据协议/凭据存储适配层），
其中凭据存储为最高风险 gate（先威胁模型后实现）。

UC↔用例映射（requirements「范围外/后续批次清单」#5 + UC21 行）：
- UC21① 登录 OAuth 链路函数体零仓库域引用（领域分离的结构证据，防回潮）
- UC21② providers.py 仓库域引用唯一收敛点 = _channel_emails_endpoint
  （github 专属邮箱补抓兜底，try/except 包裹取不到即空串，不影响登录）
- UC21③ external_identities 表结构零 VCS 列（账号体系域物理独立）
- UC21④ 挂账 #5 文档口径守卫：零"卡体系统一"旧表述（方向已修正为
  领域分离+薄共享收敛，旧措辞会让后续批次按"统一 provider"错误目标动工）
"""
import ast
import re
from pathlib import Path

_TEST_DIR = Path(__file__).resolve().parent
_SRC = _TEST_DIR.parent / "src"
_DOCS = _TEST_DIR.parent / "docs"

_REPO_DOMAIN_TOKENS = ("src.domains.runtime.vcs", "platform_card", "TokenResolver", "repo_credential")


def _function_body(source: str, func_name: str) -> str:
    """提取顶层函数体源码（从 def 到下一个同级 def/文件尾）。"""
    m = re.search(rf"^def {func_name}\b", source, re.M)
    assert m, f"function {func_name} not found"
    nxt = re.search(r"^def |^class ", source[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(source)
    return source[m.start():end]


def test_uc21_login_chain_zero_repo_domain_refs():
    """UC21①：登录 OAuth 链路（回调/校验/登录/绑定/注销）函数体内零仓库域
    引用——登录只答"你是谁"，不触碰仓库凭据域（D40 三域解耦的结构证据）。"""
    ga = (_SRC / "api" / "routes" / "grape_auth.py").read_text(encoding="utf-8")
    offenders = {}
    for fn in ("oauth_callback", "_validate_channel_callback",
               "_handle_oauth_login", "_handle_oauth_bind", "_handle_oauth_delete"):
        body = _function_body(ga, fn)
        hits = [tok for tok in _REPO_DOMAIN_TOKENS if tok in body]
        if hits:
            offenders[fn] = hits
    assert not offenders, (
        "登录链路函数体出现仓库域引用（领域分离被破坏）: "
        f"{offenders}——登录凭据与仓库凭据语义禁止合并（挂账#5 gate）"
    )


def test_uc21_providers_repo_ref_single_funnel():
    """UC21②：providers.py 的仓库域引用唯一收敛点 = _channel_emails_endpoint
    （github 邮箱补抓兜底：卡缺失走 except 空串，纯登录路径不受影响）。
    出现第二个引用函数 = 登录域对仓库域的新增依赖，必须走独立方案评审。"""
    pr = (_SRC / "domains" / "mgmt" / "auth" / "providers.py").read_text(encoding="utf-8")
    tree = ast.parse(pr)
    fns_with_refs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # 结构锚点：只扫真实代码引用（import/属性/名称）——docstring/注释里的
            # "对齐 platform_card.get_platform_card 现行形态"类表述是文档对齐先例，
            # 不是登录域→仓库域依赖，不得计入（曾致 get_provider 假红）。
            has_ref = False
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom) and (sub.module or "").startswith("src.domains.runtime.vcs"):
                    has_ref = True
                elif isinstance(sub, ast.Import) and any(
                        a.name.startswith("src.domains.runtime.vcs") for a in sub.names):
                    has_ref = True
                elif isinstance(sub, (ast.Name, ast.Attribute)):
                    tok = getattr(sub, "id", None) or getattr(sub, "attr", None) or ""
                    if "platform_card" in tok:
                        has_ref = True
            if has_ref:
                fns_with_refs.add(node.name)
    assert fns_with_refs == {"_channel_emails_endpoint"}, (
        f"providers.py 仓库域引用超出唯一收敛点: {sorted(fns_with_refs)} "
        "（登录域→仓库域新依赖需独立方案文档评审，禁止单点顺手合并）"
    )


def test_uc21_external_identities_no_vcs_columns():
    """UC21③：external_identities（账号体系域）DDL 零 VCS 专属列——
    身份存储与仓库凭据存储物理分离（凭据按用途隔离原则）。"""
    ds = (_SRC / "api" / "db_setup.py").read_text(encoding="utf-8")
    m = re.search(r"CREATE TABLE IF NOT EXISTS external_identities.*?\)", ds, re.S)
    assert m, "external_identities DDL not found"
    ddl = m.group(0)
    vcs_cols = [c for c in ("gitcode_user", "github_user", "repo_key",
                            "webhook_secret", "bot_token") if re.search(rf"\b{c}\b", ddl)]
    assert not vcs_cols, f"external_identities 混入 VCS 列: {vcs_cols}"


def test_uc21_no_unify_card_wording():
    """UC21④：文档零"卡体系统一"旧口径作为**目标表述**（挂账#5 已修正为
    "领域分离+基础设施薄共享"——旧措辞残留会让后续批次按"统一 provider"
    错误目标动工）。
    守卫口径（S112 教训：圈定被检对象自身，防误伤）：命中行若同时含
    方向修正语境词（方向偏差/旧口径/原措辞/非"统一"）= 引用性提及（修正
    说明本身需要点名旧措辞），豁免；裸出现 = 目标表述残留，红。"""
    offenders = []
    pat = re.compile(r"两套卡体系统一|卡体系统一")
    # 引用性提及的语境标记（修正说明/守卫描述自身需要点名旧措辞）
    quotable = ("方向偏差", "旧口径", "原措辞", "非\"统一\"", "非“统一”")
    for md in sorted(_DOCS.rglob("*.md")):
        for i, line in enumerate(md.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pat.search(line) and not any(q in line for q in quotable):
                offenders.append(f"{md.relative_to(_DOCS)}:{i}")
    assert not offenders, (
        f"文档残留'卡体系统一'目标表述（引用性提及需带修正语境词）: {offenders}"
    )
