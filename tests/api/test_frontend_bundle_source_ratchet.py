"""前端 chunk 源指纹棘轮：改了 chunk 的源却没 bump 版本号时必须红。

回归背景：`test_frontend_cache_version_ratchet.py` 只冻结「页面里写的版本号」——它证明
「页面引用的版本号没被悄悄改小」，但证明不了「源码变了、版本号跟着动」。实测踩过：改了
被打进 account-view chunk 的 `LlmCredManager.vue`，`account-view.js?v=` 一个字节没动，
版本棘轮全绿，老客户端继续吃旧 chunk 跑新样式与文案。

本守卫把「chunk 的真实构建输入 + 源内容指纹」和「页面引用的 ?v=」绑在一起：
  · 源内容一变 → `src_sha` 变 → 必须同时改 FROZEN 的 src_sha **和** 该 chunk 的 ?v=
  · 组件/模块被新 import 进某条构建输入 → `sources` 集合变 → 必须同步登记

真实构建输入 = `vite.config.js` 的 rollup entry（`src/views/entries/<name>.js`），
以及它递归 import 的相对 `.js` / `.vue` 文件。不按「同名 .vue」的约定猜，因为 entry 会
引入非同名组件（如 `repos-view.js` → `SettingsView.vue` → `LlmCredManager.vue`）。
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

# rollup entry: `'name': fileURLToPath(new URL('./src/views/entries/x.js', import.meta.url))`
ENTRY_DECL = re.compile(
    r"""(?:'([\w-]+)'|([A-Za-z_$][\w$]*))\s*:\s*fileURLToPath\(\s*"""
    r"""new URL\(\s*['"]\./src/views/entries/([\w-]+)\.js['"]"""
)
# 相对 import：`import X from './a.vue'` / `import './a.js'` / `import('./a')`
IMPORT_REL = re.compile(r"""(?:from\s*|import\s*\(\s*|import\s+)['"](\.[^'"]+)['"]""")
# 页面引用：`/xxx-view.js?v=60`
PAGE_VER = re.compile(r"/([A-Za-z0-9_.\-]+\.js)\?v=(\d+)")

# 冻结：chunk -> (页面引用版本号, 源内容指纹前 16 位, 源文件相对 app/src 的清单)
# 改动任一源文件 / 新增 import 都会被下面的断言逼着同时更新本表与页面的 ?v=。
FROZEN = {
    "account-view.js": ("60", "034ac9383784bb7f", ["components/BindDialog.vue", "components/CredRepoRow.vue", "components/PatDialog.vue", "views/AccountView.vue", "views/LlmCredManager.vue", "views/entries/account-view.js"]),
    "admin-view.js": ("46", "e59a18dcd51cee02", ["components/ui/AGHelp.vue", "components/ui/AGPill.vue", "lib/bot-summary.js", "views/AdminChannels.vue", "views/AdminPlatforms.vue", "views/AdminView.vue", "views/entries/admin-view.js"]),
    "agent-view.js": ("50", "b7aeca615e092eda", ["views/AgentView.vue", "views/entries/agent-view.js"]),
    "autopilot-view.js": ("53", "12b6a690bf0970d2", ["lib/repo-provider.js", "views/AutopilotView.vue", "views/entries/autopilot-view.js"]),
    "chat-view.js": ("60", "7b820e2fa3b65d89", ["views/ChatView.vue", "views/entries/chat-view.js"]),
    "dashboard-view.js": ("83", "6b9fe1a28eda3d46", ["lib/repo-provider.js", "views/DashboardView.vue", "views/entries/dashboard-view.js"]),
    "event-view.js": ("57", "18ada96a42e66872", ["views/EventView.vue", "views/entries/event-view.js"]),
    "feedback-dialog.js": ("50", "6ab1db5105a856d4", ["views/FeedbackDialog.vue", "views/entries/feedback-dialog.js"]),
    "forgot-view.js": ("1", "503253e492ecd273", ["views/ForgotView.vue", "views/entries/forgot-view.js"]),
    "im-channel.js": ("54", "39e07d1e899fa8dc", ["views/ImChannel.vue", "views/entries/im-channel.js"]),
    "issues-view.js": ("62", "912b1bb72fdc636d", ["lib/boardRound.js", "views/IssuesView.vue", "views/entries/issues-view.js"]),
    "knowledge-view.js": ("49", "9e67a352975c636e", ["views/KnowledgeView.vue", "views/entries/knowledge-view.js"]),
    "login-view.js": ("2", "2003041303cafeb5", ["views/LoginView.vue", "views/entries/login-view.js"]),
    "my-feedback-detail.js": ("50", "97dfae99aedbefd4", ["views/MyFeedbackDetail.vue", "views/entries/my-feedback-detail.js"]),
    "my-feedback.js": ("50", "5b5bafe6239d2d35", ["views/MyFeedback.vue", "views/entries/my-feedback.js"]),
    "observability-view.js": ("57", "3955992d21777b46", ["views/ObservabilityView.vue", "views/entries/observability-view.js"]),
    "onboarding-view.js": ("8", "115ddd48aa9f0e33", ["components/BindDialog.vue", "views/OnboardingView.vue", "views/entries/onboarding-view.js"]),
    "organizations.js": ("50", "fe544b62f8f96b79", ["views/Organizations.vue", "views/entries/organizations.js"]),
    "pipeline.js": ("54", "d84b05630234d9f0", ["views/Pipeline.vue", "views/entries/pipeline.js"]),
    "register-view.js": ("1", "3358ffefb36ef8d4", ["views/RegisterView.vue", "views/entries/register-view.js"]),
    "repos-view.js": ("59", "0fb2b932fc98fa4d", ["lib/repo-provider.js", "views/LlmCredManager.vue", "views/ReposView.vue", "views/SettingsView.vue", "views/TemplatesView.vue", "views/entries/repos-view.js"]),
    "runtimes-view.js": ("76", "ba016e4008ced37b", ["views/RuntimesView.vue", "views/entries/runtimes-view.js"]),
    "sandbox-view.js": ("8", "9176e8c88e6603fe", ["views/SandboxView.vue", "views/entries/sandbox-view.js"]),
    "scene-market.js": ("58", "6049b47e1bf849e8", ["views/SceneCard.vue", "views/SceneMarket.vue", "views/entries/scene-market.js"]),
    "skills-view.js": ("54", "882311f9f2ddcd0c", ["views/SkillsView.vue", "views/entries/skills-view.js"]),
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
    target = (base.parent / ref).resolve()
    if target.is_file():
        return target
    for suffix in (".js", ".vue", ".ts", ".json"):
        alt = target.with_name(target.name + suffix)
        if alt.is_file():
            return alt
    return None


def _sources_of(entry: Path, seen=None) -> set:
    """entry 文件 + 它递归 import 的相对 .js/.vue/.ts/.json（chunk 的真实输入）。"""
    seen = set() if seen is None else seen
    if entry in seen:
        return seen
    seen.add(entry)
    for ref in IMPORT_REL.findall(entry.read_text(encoding="utf-8")):
        target = _resolve(entry, ref)
        if target is not None:
            _sources_of(target, seen)
    return seen


def _bundles() -> dict:
    """针对每个 entry 算出 (版本号, 源指纹, 源清单)。"""
    versions = _page_versions()
    out = {}
    for bundle, entry in sorted(_entries().items()):
        per_page = versions.get(bundle)
        if not per_page:
            out[bundle] = (NO_PAGE, "-", [])
            continue
        ver = next(iter(per_page)) if len(per_page) == 1 else "MULTI:" + ",".join(sorted(per_page))
        srcs = _sources_of(entry)
        h = hashlib.sha1()
        for s in sorted(srcs):
            h.update(str(s.relative_to(APP)).encode())
            h.update(b"\0")
            h.update(s.read_bytes())
            h.update(b"\0")
        out[bundle] = (ver, h.hexdigest()[:16],
                       sorted(str(s.relative_to(APP)) for s in srcs))
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
    unlinked = sorted(b for b, (ver, _s, _l) in _bundles().items() if ver == NO_PAGE)
    assert not unlinked, f"这些 chunk 没有任何壳页以 ?v= 引用（无从判缓存）：{unlinked}"


def test_bundle_source_fingerprints_are_frozen():
    actual = _bundles()
    problems = []
    for bundle, frozen in FROZEN.items():
        if bundle not in actual:
            problems.append(f"{bundle}: vite.config.js 已不再产出该 chunk（删了要同步本表）")
            continue
        ver, sha, srcs = actual[bundle]
        f_ver, f_sha, f_srcs = frozen
        if ver != f_ver:
            problems.append(f"{bundle}: 页面引用版本={ver} 冻结={f_ver}")
        if srcs != f_srcs:
            problems.append(f"{bundle}: 源清单变化 实际={srcs} 冻结={f_srcs}")
        if sha != f_sha:
            problems.append(
                f"{bundle}: 源内容指纹变化（{f_sha} → {sha}）——改了源文件就必须 bump "
                f"引用页面的 ?v= 并同步本表")
    for bundle in sorted(set(actual) - set(FROZEN)):
        problems.append(f"{bundle}: 新 chunk 未登记进 FROZEN")
    assert not problems, "前端 chunk 源指纹与冻结值不一致: " + "; ".join(problems)
