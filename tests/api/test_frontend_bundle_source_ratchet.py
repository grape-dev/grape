"""前端 bundle 源指纹棘轮：改了 .vue 源却忘 bump cacheVer 时必须红。

回归背景（PR #1696 第 2 轮架构复审 Medium）：`test_frontend_cache_version_ratchet.py`
只冻结「index.html 里写的版本号」——它证明「页面引用的版本号没被悄悄改小」，但证明不了
「源码变了、版本号跟着动」。实测踩过：本 PR 改了 `LlmCredManager.vue`（被打进
account-view bundle），`account-view.js?v=59` 一个字节没动，棘轮全绿、老客户端吃旧
chunk 跑新页面文案/样式。同为一次 PR 修改的其它 4 个 view 都 bump 了，唯独漏的这个
没有任何守卫能把关。

本守卫把「bundle 的源文件集合 + 源内容指纹」和「index.html 里的版本号」绑在一起：
  · 源内容一变 → `src_sha` 变 → 必须同时改 FROZEN 的 src_sha **和** 该 bundle 的 ?v=
  · 组件被新 import 进某个 view → `sources` 集合变 → 必须同步登记（否则新组件改动无人看护）

bundle ↔ 源的对应关系从 import 图推出来（view 文件 + 它递归 import 的 .vue 组件），
不靠人工维护表；只有 bundle 名到 view 文件的约定是 `RuntimesView.vue -> runtimes-view.js`。
"""
import hashlib
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "src" / "api" / "frontend" / "app" / "src"
INDEX = REPO / "src" / "api" / "frontend" / "shell" / "index.html"

IMPORT_VUE = re.compile(r"""from\s+['"]([^'"]+\.vue)['"]""")
IDX_VER = re.compile(r"/([A-Za-z0-9_.\-]+\.js)\?v=(\d+)")

# 冻结：bundle -> (index.html 里的版本号, 源内容指纹前 16 位, 源文件相对 app/src 的清单)
# 改动任一源文件 / 新增 import 都会被下面的断言逼着同时更新本表与 index.html 的 ?v=。
FROZEN = {
    "account-view.js": ("60", "7a13d6d48c067111",
                        ["components/BindDialog.vue", "components/CredRepoRow.vue",
                         "components/PatDialog.vue", "views/AccountView.vue",
                         "views/LlmCredManager.vue"]),
    "agent-view.js": ("50", "80aa875c833cc26b", ["views/AgentView.vue"]),
    "autopilot-view.js": ("53", "8f0a7b5e802c34cb", ["views/AutopilotView.vue"]),
    "chat-view.js": ("60", "ac77951a0e9fcb55", ["views/ChatView.vue"]),
    "dashboard-view.js": ("83", "9b8a54240a7afe20", ["views/DashboardView.vue"]),
    "event-view.js": ("57", "ea200dd579c4d72b", ["views/EventView.vue"]),
    "feedback-dialog.js": ("50", "e6e424f54abd735f", ["views/FeedbackDialog.vue"]),
    "im-channel.js": ("54", "368cf40190b56adf", ["views/ImChannel.vue"]),
    "issues-view.js": ("62", "396252cb6d77471f", ["views/IssuesView.vue"]),
    "knowledge-view.js": ("49", "ddcbede9b6ef112c", ["views/KnowledgeView.vue"]),
    "login-view.js": ("2", "9e38c73d758a8d02", ["views/LoginView.vue"]),
    "my-feedback.js": ("50", "38f9da5a3012f5f7", ["views/MyFeedback.vue"]),
    "my-feedback-detail.js": ("50", "cb278dfb2318f9e6", ["views/MyFeedbackDetail.vue"]),
    "observability-view.js": ("57", "e79a5e0186f4593e", ["views/ObservabilityView.vue"]),
    "onboarding-view.js": ("8", "4f6661f97714c0d0",
                           ["components/BindDialog.vue", "views/OnboardingView.vue"]),
    "organizations.js": ("50", "1cc6928004b18e1d", ["views/Organizations.vue"]),
    "pipeline.js": ("54", "f33d0b4f99b1e167", ["views/Pipeline.vue"]),
    "repos-view.js": ("59", "7755f1077ec5a4fb", ["views/ReposView.vue", "views/TemplatesView.vue"]),
    "runtimes-view.js": ("76", "e11ad9850fa0b539", ["views/RuntimesView.vue"]),
    "sandbox-view.js": ("8", "1349e7e861ad119b", ["views/SandboxView.vue"]),
    "scene-market.js": ("58", "0416d0ae5becf00f", ["views/SceneCard.vue", "views/SceneMarket.vue"]),
    "skills-view.js": ("54", "27a3fcc999b8df24", ["views/SkillsView.vue"]),
}


def _kebab(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()


def _sources_of(view: Path, seen=None) -> set:
    """view 文件 + 它递归 import 的 .vue 组件（bundle 的真实输入）。"""
    seen = set() if seen is None else seen
    if view in seen:
        return seen
    seen.add(view)
    for ref in IMPORT_VUE.findall(view.read_text(encoding="utf-8")):
        target = (view.parent / ref).resolve()
        if target.exists():
            _sources_of(target, seen)
    return seen


def _bundles() -> dict:
    """按 index.html 的实际引用，算出每个 bundle 的 (版本号, 源指纹, 源清单)。"""
    html = INDEX.read_text(encoding="utf-8")
    versions = dict(IDX_VER.findall(html))
    out = {}
    for view in sorted((APP / "views").glob("*.vue")):
        bundle = _kebab(view.stem) + ".js"
        if bundle not in versions:
            continue  # 不是页级 chunk（如纯子组件）→ 由引用它的 bundle 覆盖
        srcs = _sources_of(view)
        h = hashlib.sha1()
        for s in sorted(srcs):
            h.update(str(s.relative_to(APP)).encode())
            h.update(b"\0")
            h.update(s.read_bytes())
            h.update(b"\0")
        out[bundle] = (versions[bundle], h.hexdigest()[:16],
                       sorted(str(s.relative_to(APP)) for s in srcs))
    return out


def test_bundle_source_fingerprints_are_frozen():
    actual = _bundles()
    problems = []
    for bundle, frozen in FROZEN.items():
        if bundle not in actual:
            problems.append(f"{bundle}: index.html 已不再引用该 bundle（删了要同步本表）")
            continue
        ver, sha, srcs = actual[bundle]
        f_ver, f_sha, f_srcs = frozen
        if ver != f_ver:
            problems.append(f"{bundle}: index.html 版本={ver} 冻结={f_ver}")
        if srcs != f_srcs:
            problems.append(f"{bundle}: 源清单变化 实际={srcs} 冻结={f_srcs}")
        if sha != f_sha:
            problems.append(
                f"{bundle}: 源内容指纹变化（{f_sha} → {sha}）——改了源文件就必须 bump "
                f"index.html 的 ?v= 并同步本表")
    for bundle in sorted(set(actual) - set(FROZEN)):
        problems.append(f"{bundle}: 新 bundle 未登记进 FROZEN")
    assert not problems, "前端 bundle 源指纹与冻结值不一致: " + "; ".join(problems)
