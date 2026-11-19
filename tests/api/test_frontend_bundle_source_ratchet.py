"""前端 chunk 源指纹棘轮：改了 chunk 的源却没抬版本号时必须红。

回归背景：`test_frontend_cache_version_ratchet.py` 只冻结「页面里写的版本号」——它证明
「页面引用的版本号没被悄悄改小」，但证明不了「源码变了、版本号跟着动」。实测踩过：改了
被打进 account-view chunk 的 `LlmCredManager.vue`，`account-view.js?v=` 一个字节没动，
版本棘轮全绿，老客户端继续吃旧 chunk 跑新样式与文案。

本守卫把「chunk 的真实构建输入 + 源内容指纹」与两套独立缓存键绑在一起：

  · 真实构建输入 = `vite.config.js` 的 rollup entry（`src/views/entries/<name>.js`）
    及其递归 import 的相对 / `@/` 别名 `.js`/`.vue` 文件；
  · entry 运行时注入的 `assets/bridge.css`、`assets/ag-ui.css`（AG 组件样式聚合）、
    `assets/viewcss/<name>.css` 同样是该 chunk 的输入，另算一条 CSS 指纹；
  · 页面 `<script src=...?v=N>` 与 entry 运行时注入的 CSS `?v=N` 是两套缓存键，
    各自绑指纹：源变要抬页面版本，注入的 CSS 变要抬注入版本。

两张表：
  · `FROZEN`  = 当前树的（页面版本, 注入版本, 源指纹, 源清单）——源一变更就红；
  · `SHIPPED` = 浏览器手上可能已有的那一版（页面版本, 注入版本, 源指纹, CSS 指纹）。
    `FROZEN` 的源已变、页面版本却仍是 `SHIPPED` 里的值 → 红；注入 CSS 的指纹变了、
    注入版本没抬 → 红。它堵的是「改了源、只重冻指纹、`?v=` 原地不动」。
    健康树上两张表逐字段相同；抬档时一起更新（这是唯一被允许的更新方式）。
    另一路径「两表同抬版本再一起重冻指纹」属越权重写：浏览器实持的 tuple
    是仓外事实，本守卫不设防（此前曾靠约定而非断言看管，本守卫显式登记为不设防项）。
"""
import hashlib
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "src" / "api" / "frontend"
APP = FRONTEND / "app" / "src"
VITE_CONFIG = FRONTEND / "app" / "vite.config.js"
SHELL = FRONTEND / "shell"
ENTRIES = APP / "views" / "entries"
UI = APP / "components" / "ui"

# rollup entry: `'name': fileURLToPath(new URL('./src/views/entries/x.js', import.meta.url))`
ENTRY_DECL = re.compile(
    r"""(?:'([\w-]+)'|([A-Za-z_$][\w$]*))\s*:\s*fileURLToPath\(\s*"""
    r"""new URL\(\s*['"]\./src/views/entries/([\w-]+)\.js['"]"""
)
# import 说明符：相对路径 `./a.vue` 与 vite `@/` 别名（`@` -> app/src）
IMPORT_REL = re.compile(r"""(?:from\s*|import\s*\(\s*|import\s+)['"]((?:\.|@/)[^'"]+)['"]""")
ALIAS = "@"
# 页面引用：`/xxx-view.js?v=60`
PAGE_VER = re.compile(r"/([A-Za-z0-9_.\-]+\.js)\?v=(\d+)")
# entry 运行时注入：`link.href = '/static-vue/assets/' + f + '?v=61'`
INJECT_VER = re.compile(r"link\.href\s*=\s*'/static-vue/assets/'\s*\+\s*f\s*\+\s*'\?v=(\d+)'")
INJECT_FILES = re.compile(r"\[\s*((?:'[\w./-]+\.css'\s*,?\s*)+)\]")
CSS_NAME = re.compile(r"'([\w./-]+\.css)'")

# 注入产物的源：稳定名产物 -> app/src 下的真实来源；ag-ui.css 是 AG 组件样式聚合
CSS_SOURCE = {"bridge.css": (APP / "styles" / "bridge.css",)}
AG_AGGREGATE = "ag-ui.css"

# 冻结：chunk -> (页面 ?v=, 注入 CSS 的 ?v=（未注入为 None）, 源内容指纹前 16 位, 源清单)
# 页面版本 / 注入版本 / 源指纹 / 源清单任一变，都必须同步改本表与对应页面的 ?v=。
FROZEN = {
    "account-view.js": ("60", None, "034ac9383784bb7f", ["components/BindDialog.vue", "components/CredRepoRow.vue", "components/PatDialog.vue", "views/AccountView.vue", "views/LlmCredManager.vue", "views/entries/account-view.js"]),
    "admin-view.js": ("46", None, "e59a18dcd51cee02", ["components/ui/AGHelp.vue", "components/ui/AGPill.vue", "lib/bot-summary.js", "views/AdminChannels.vue", "views/AdminPlatforms.vue", "views/AdminView.vue", "views/entries/admin-view.js"]),
    "agent-view.js": ("51", None, "06f9a9d9c11fc69d", ["components/ui/AGButton.vue", "components/ui/AGCard.vue", "views/AgentView.vue", "views/entries/agent-view.js"]),
    "autopilot-view.js": ("54", "60", "e586d0e94604d800", ["components/ui/AGButton.vue", "components/ui/AGCard.vue", "components/ui/AGHelp.vue", "components/ui/AGInput.vue", "components/ui/AGModal.vue", "components/ui/AGNav.vue", "components/ui/AGPill.vue", "components/ui/AGSelect.vue", "components/ui/AGTable.vue", "components/ui/AGToast.vue", "lib/repo-provider.js", "styles/bridge.css", "views/AutopilotView.vue", "views/entries/autopilot-view.js"]),
    "chat-view.js": ("61", None, "613b5554a773d619", ["lib/onboard.js", "views/ChatView.vue", "views/entries/chat-view.js"]),
    "dashboard-view.js": ("85", "61", "6987249862446785", ["components/ui/AGButton.vue", "components/ui/AGCard.vue", "components/ui/AGHelp.vue", "components/ui/AGInput.vue", "components/ui/AGModal.vue", "components/ui/AGNav.vue", "components/ui/AGPill.vue", "components/ui/AGSelect.vue", "components/ui/AGTable.vue", "components/ui/AGToast.vue", "lib/repo-provider.js", "styles/bridge.css", "views/DashboardView.vue", "views/entries/dashboard-view.js"]),
    "event-view.js": ("59", "60", "7515a76ed9c92d73", ["components/ui/AGButton.vue", "components/ui/AGCard.vue", "components/ui/AGHelp.vue", "components/ui/AGInput.vue", "components/ui/AGModal.vue", "components/ui/AGNav.vue", "components/ui/AGPill.vue", "components/ui/AGSelect.vue", "components/ui/AGTable.vue", "components/ui/AGToast.vue", "styles/bridge.css", "views/EventView.vue", "views/entries/event-view.js"]),
    "feedback-dialog.js": ("50", None, "6ab1db5105a856d4", ["views/FeedbackDialog.vue", "views/entries/feedback-dialog.js"]),
    "forgot-view.js": ("1", None, "503253e492ecd273", ["views/ForgotView.vue", "views/entries/forgot-view.js"]),
    "im-channel.js": ("54", None, "18e46f9e514dddbc", ["components/ui/AGButton.vue", "components/ui/AGCard.vue", "lib/onboard.js", "views/ImChannel.vue", "views/entries/im-channel.js"]),
    "issues-view.js": ("62", None, "912b1bb72fdc636d", ["lib/boardRound.js", "views/IssuesView.vue", "views/entries/issues-view.js"]),
    "knowledge-view.js": ("49", None, "9e67a352975c636e", ["views/KnowledgeView.vue", "views/entries/knowledge-view.js"]),
    "login-view.js": ("2", "1", "2003041303cafeb5", ["views/LoginView.vue", "views/entries/login-view.js"]),
    "my-feedback-detail.js": ("50", None, "f5b774c8263b4573", ["components/ui/AGButton.vue", "components/ui/AGCard.vue", "views/MyFeedbackDetail.vue", "views/entries/my-feedback-detail.js"]),
    "my-feedback.js": ("50", None, "4c5998a63a7c9b27", ["components/ui/AGButton.vue", "components/ui/AGCard.vue", "views/MyFeedback.vue", "views/entries/my-feedback.js"]),
    "observability-view.js": ("57", None, "3955992d21777b46", ["views/ObservabilityView.vue", "views/entries/observability-view.js"]),
    "onboarding-view.js": ("9", "6", "57f9ab1981a5a4c1", ["components/BindDialog.vue", "styles/bridge.css", "views/OnboardingView.vue", "views/entries/onboarding-view.js"]),
    "organizations.js": ("50", None, "5f3800b99e562b90", ["components/ui/AGButton.vue", "components/ui/AGCard.vue", "components/ui/AGInput.vue", "components/ui/AGModal.vue", "components/ui/AGSelect.vue", "views/Organizations.vue", "views/entries/organizations.js"]),
    "pipeline.js": ("54", None, "d84b05630234d9f0", ["views/Pipeline.vue", "views/entries/pipeline.js"]),
    "register-view.js": ("1", None, "3358ffefb36ef8d4", ["views/RegisterView.vue", "views/entries/register-view.js"]),
    "repos-view.js": ("59", None, "0fb2b932fc98fa4d", ["lib/repo-provider.js", "views/LlmCredManager.vue", "views/ReposView.vue", "views/SettingsView.vue", "views/TemplatesView.vue", "views/entries/repos-view.js"]),
    "runtimes-view.js": ("77", "61", "10bdb0a8b8f7b101", ["components/ui/AGButton.vue", "components/ui/AGCard.vue", "components/ui/AGHelp.vue", "components/ui/AGInput.vue", "components/ui/AGModal.vue", "components/ui/AGNav.vue", "components/ui/AGPill.vue", "components/ui/AGSelect.vue", "components/ui/AGTable.vue", "components/ui/AGToast.vue", "styles/bridge.css", "views/RuntimesView.vue", "views/entries/runtimes-view.js"]),
    "sandbox-view.js": ("10", "2", "8d03e1cd818b1548", ["components/ui/AGButton.vue", "components/ui/AGCard.vue", "components/ui/AGHelp.vue", "components/ui/AGInput.vue", "components/ui/AGModal.vue", "components/ui/AGNav.vue", "components/ui/AGPill.vue", "components/ui/AGSelect.vue", "components/ui/AGTable.vue", "components/ui/AGToast.vue", "styles/bridge.css", "views/SandboxView.vue", "views/entries/sandbox-view.js"]),
    "scene-market.js": ("58", None, "fd117f417f9527a0", ["components/ui/AGButton.vue", "components/ui/AGInput.vue", "components/ui/AGModal.vue", "components/ui/AGSelect.vue", "views/SceneCard.vue", "views/SceneMarket.vue", "views/entries/scene-market.js"]),
    "skills-view.js": ("54", None, "d49b7b6128e0b3fb", ["components/ui/AGButton.vue", "components/ui/AGModal.vue", "components/ui/AGSelect.vue", "views/SkillsView.vue", "views/entries/skills-view.js"]),
}


# 已发布（浏览器可能已缓存）的那一版：chunk -> (页面 ?v=, 注入 CSS 的 ?v=, 源指纹, CSS 指纹)
# 与 FROZEN 在健康树上逐字段相同；留在这里是为了让「只重冻 FROZEN」这条路径变红。
SHIPPED = {
    "account-view.js": ("60", None, "034ac9383784bb7f", None),
    "admin-view.js": ("46", None, "e59a18dcd51cee02", None),
    "agent-view.js": ("51", None, "06f9a9d9c11fc69d", None),
    "autopilot-view.js": ("54", "60", "e586d0e94604d800", "3f9f3319ade97fe3"),
    "chat-view.js": ("61", None, "613b5554a773d619", None),
    "dashboard-view.js": ("85", "61", "6987249862446785", "654328b9208f445f"),
    "event-view.js": ("59", "60", "7515a76ed9c92d73", "e6e407235e58a689"),
    "feedback-dialog.js": ("50", None, "6ab1db5105a856d4", None),
    "forgot-view.js": ("1", None, "503253e492ecd273", None),
    "im-channel.js": ("54", None, "18e46f9e514dddbc", None),
    "issues-view.js": ("62", None, "912b1bb72fdc636d", None),
    "knowledge-view.js": ("49", None, "9e67a352975c636e", None),
    "login-view.js": ("2", "1", "2003041303cafeb5", "9e38c73d758a8d02"),
    "my-feedback-detail.js": ("50", None, "f5b774c8263b4573", None),
    "my-feedback.js": ("50", None, "4c5998a63a7c9b27", None),
    "observability-view.js": ("57", None, "3955992d21777b46", None),
    "onboarding-view.js": ("9", "6", "57f9ab1981a5a4c1", "270e442198de2ec4"),
    "organizations.js": ("50", None, "5f3800b99e562b90", None),
    "pipeline.js": ("54", None, "d84b05630234d9f0", None),
    "register-view.js": ("1", None, "3358ffefb36ef8d4", None),
    "repos-view.js": ("59", None, "0fb2b932fc98fa4d", None),
    "runtimes-view.js": ("77", "61", "10bdb0a8b8f7b101", "df79e786f8268055"),
    "sandbox-view.js": ("10", "2", "8d03e1cd818b1548", "ce39afc3c912a737"),
    "scene-market.js": ("58", None, "fd117f417f9527a0", None),
    "skills-view.js": ("54", None, "d49b7b6128e0b3fb", None),
}


NO_PAGE = "<未被任何页面引用>"


def _entries() -> dict:
    """vite.config.js 的 rollup entry：chunk 名 -> entry 文件（真实构建输入起点）。"""
    out = {}
    for quoted, bare, fname in ENTRY_DECL.findall(VITE_CONFIG.read_text(encoding="utf-8")):
        out[(quoted or bare) + ".js"] = ENTRIES / (fname + ".js")
    return out


def _page_versions() -> dict:
    """页面里各 chunk 的 ?v=（跨 index/admin/register/forgot 等所有壳页）。"""
    found = {}
    for page in sorted(SHELL.glob("*.html")):
        for name, ver in PAGE_VER.findall(page.read_text(encoding="utf-8")):
            found.setdefault(name, {}).setdefault(ver, []).append(page.name)
    return found


def _resolve(base: Path, ref: str):
    if ref.startswith(ALIAS + "/"):
        target = (APP / ref[len(ALIAS) + 1:]).resolve()
    else:
        target = (base.parent / ref).resolve()
    if target.is_file():
        return target
    for suffix in (".js", ".vue", ".ts", ".json"):
        alt = target.with_name(target.name + suffix)
        if alt.is_file():
            return alt
    return None


def _kebab(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()


_VIEW_BY_CSS = None


def _view_by_css_name() -> dict:
    """`viewcss/bind-dialog.css` -> BindDialog.vue 这类映射（Vue 侧 kebab 命名约定）。"""
    global _VIEW_BY_CSS
    if _VIEW_BY_CSS is None:
        _VIEW_BY_CSS = {}
        for vue in APP.rglob("*.vue"):
            _VIEW_BY_CSS.setdefault(_kebab(vue.stem) + ".css", vue)
    return _VIEW_BY_CSS


def _injected(entry: Path):
    """entry 运行时注入的 (CSS 版本常量, 注入文件清单)；不注入时返回 (None, [])。"""
    text = entry.read_text(encoding="utf-8")
    m = INJECT_VER.search(text)
    if not m:
        return None, []
    block = INJECT_FILES.search(text)
    names = CSS_NAME.findall(block.group(1)) if block else []
    return m.group(1), names


def _css_sources(names: list) -> set:
    """注入清单里每个产物的真实源（bridge.css / AG 组件样式聚合 / 各 view 的 .vue）。"""
    seen = set()
    for name in names:
        if name in CSS_SOURCE:
            seen.add(CSS_SOURCE[name][0])
        elif name == AG_AGGREGATE:
            seen.update(UI.glob("AG*.vue"))
        else:
            vue = _view_by_css_name().get(name.rsplit("/", 1)[-1])
            assert vue is not None, (
                f"注入的 {name} 找不到对应的 .vue 源——构建契约变了，本守卫要同步")
            seen.add(vue)
    return seen


def _sha(paths) -> str:
    h = hashlib.sha1()
    for s in sorted(paths):
        h.update(str(s.relative_to(APP)).encode())
        h.update(b"\0")
        h.update(s.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:16]


def _sources_of(entry: Path, seen=None) -> set:
    """entry 文件 + 它递归 import 的 `.js`/`.vue`/`.ts`/`.json` + 运行时注入的 CSS 源。"""
    seen = set() if seen is None else seen
    if entry in seen:
        return seen
    seen.add(entry)
    for ref in IMPORT_REL.findall(entry.read_text(encoding="utf-8")):
        target = _resolve(entry, ref)
        if target is not None:
            _sources_of(target, seen)
    _, names = _injected(entry)
    if names:
        seen.update(_css_sources(names))
    return seen


def _bundles() -> dict:
    """每个 entry 算出 (页面版本, 注入版本, 源指纹, 源清单, CSS 指纹)。"""
    versions = _page_versions()
    out = {}
    for bundle, entry in sorted(_entries().items()):
        per_page = versions.get(bundle)
        if not per_page:
            out[bundle] = (NO_PAGE, None, "-", [], None)
            continue
        ver = next(iter(per_page)) if len(per_page) == 1 else "MULTI:" + ",".join(sorted(per_page))
        css_ver, names = _injected(entry)
        srcs = _sources_of(entry)
        css_sha = _sha(_css_sources(names)) if names else None
        out[bundle] = (ver, css_ver, _sha(srcs),
                       sorted(str(s.relative_to(APP)) for s in srcs), css_sha)
    return out


def test_every_entry_is_covered():
    """vite 的 entry 清单与守卫覆盖面必须一致：vite 产出的 chunk 全在棘轮内。"""
    entries = _entries()
    assert entries, "vite.config.js 未解析出任何 entry"
    on_disk = {p.name for p in ENTRIES.glob("*.js")}
    declared = {e.name for e in entries.values()}
    assert on_disk == declared, (
        f"views/entries/ 与 vite.config.js 不一致："
        f"仅磁盘={sorted(on_disk - declared)} 仅配置={sorted(declared - on_disk)}")
    assert set(SHIPPED) == set(FROZEN), (
        f"SHIPPED 与 FROZEN 的 chunk 集合不一致：仅 FROZEN={sorted(set(FROZEN)-set(SHIPPED))} "
        f"仅 SHIPPED={sorted(set(SHIPPED)-set(FROZEN))}")
    unlinked = sorted(b for b, row in _bundles().items() if row[0] == NO_PAGE)
    assert not unlinked, f"这些 chunk 没有任何壳页以 ?v= 引用（无从判缓存）：{unlinked}"
    # 抬档忘更 SHIPPED：两表前三字段（页面版本/注入版本/源指纹）
    # 必须逐 chunk 相同。一旦分叉，该 chunk 的 SHIPPED 锚点会因 `ver == s_ver` 恒不成立
    # 而永久短路，静默复发难以察觉，当场拦。
    for b in sorted(FROZEN):
        assert SHIPPED[b][:3] == FROZEN[b][:3], (
            f"{b}: 抬档必须 FROZEN 与 SHIPPED 一起更新："
            f"SHIPPED={SHIPPED[b][:3]} FROZEN={FROZEN[b][:3]}（分叉会让 SHIPPED 锚点永久失效）")


def test_bundle_source_fingerprints_are_frozen():
    actual = _bundles()
    problems = []
    for bundle, frozen in FROZEN.items():
        if bundle not in actual:
            problems.append(f"{bundle}: vite.config.js 已不再产出该 chunk（删了要同步本表）")
            continue
        ver, css_ver, sha, srcs, css_sha = actual[bundle]
        f_ver, f_css_ver, f_sha, f_srcs = frozen
        if ver != f_ver:
            problems.append(f"{bundle}: 页面引用版本={ver} 冻结={f_ver}")
        if css_ver != f_css_ver:
            problems.append(f"{bundle}: 入口注入的 CSS 版本={css_ver} 冻结={f_css_ver}")
        if srcs != f_srcs:
            problems.append(f"{bundle}: 源清单变化 实际={srcs} 冻结={f_srcs}")
        if sha != f_sha:
            problems.append(
                f"{bundle}: 源内容指纹变化（{f_sha} → {sha}）——改了源文件就必须抬页面 "
                f"`?v=` 并同步本表与 SHIPPED")
        shipped = SHIPPED.get(bundle)
        if not shipped:
            continue
        s_ver, s_css_ver, s_sha, s_css_sha = shipped
        if sha != s_sha and ver == s_ver:
            problems.append(
                f"{bundle}: 源已变但仍沿用已发布（浏览器可能缓存）的页面 ?v={ver}——"
                f"必须抬档，并把 FROZEN 与 SHIPPED 一起更新")
        if css_sha and s_css_sha and css_sha != s_css_sha and css_ver == s_css_ver:
            problems.append(
                f"{bundle}: 注入的 CSS 内容已变但仍用 ?v={css_ver}——entry 运行时注入的 "
                f"bridge.css / ag-ui.css / viewcss 会继续吃旧缓存，抬注入版本并同步两表")
    for bundle in sorted(set(actual) - set(FROZEN)):
        problems.append(f"{bundle}: 新 chunk 未登记进 FROZEN")
    assert not problems, "前端 chunk 源指纹与冻结值不一致: " + "; ".join(problems)
