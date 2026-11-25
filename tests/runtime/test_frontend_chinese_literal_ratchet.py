"""前端中文字面量 ratchet（PR #1231 架构复审 BLOCKED-1 配套守卫）。

回归背景：account.js 一次新增 28 处硬编码中文用户可见文案（未走 I18N.t），
en 模式回落 zh——英文用户直接看到中文。本守卫把各页面文件的「非注释行中文字面量数」
冻结成只减不增的 ratchet：新增硬编码中文 = 红，须改走 t() 并同步 zh/en 双包。

静态根迁移（2026-11-13）：旧 `src/api/static/js/pages/*.js` 已随 Vue 迁移删除，
扫描面改为 Vue 源 `src/api/frontend/app/src/{views,pages}/*.vue`，冻结值取迁移后
现状（legacy 存量债务，只减不增；新页面如 ImChannel/SettingsView 为 0，不得回潮）。

豁免口径：注释行不计；语言切换按钮等基线存量计入冻结值（不新增即可）。
新页面文件出现中文字面量必须登记进 FROZEN（过 review 时显式化）。

基线重刷（#3355，2026-11-19）：ChatView 68→76 / DashboardView 121→122 /
    PluginMarketView 首登 100 为本单 t()/tp()/td() zh 兜底串新增（键已同步 zh/en
    双包）；AdminPlatforms 1 / EventView 102 / SceneMarket 87 / SkillsView 92 为
    #3169 系列遗留超基线存量——按「现状重刷、此后只减不增」登记，守卫恢复可执行。
    整线合并重刷（2026-11-19）：arch-weekly-11 并入后 SceneMarket 87→89 /
    SkillsView 92→100 / TemplatesView 33→34 为两侧 .vue 并集的 zh 兜底串存量，
    同款「现状重刷、此后只减不增」如实入账。
    #4230 注册（2026-11-24）：SceneCard 4→5 为新增 no_description 的 zh 兜底串
    （键已同步 zh/en 双包，缺失语言包时首帧可用）。
    现状重刷（2026-11-24，weekly-mys 刷新到 arch-weekly-11 之后）：
    EventView 102→113 为 #3433 新增失败卡片 10 个键的 zh 兜底串（键已同步
    locales/{zh,en}/events.json）；IssuesView 163→180（#4234 之后仍比刷新前少 1）、
    Organizations 11→12 / ReposView 18→19 / SkillsView 179→186 / TemplatesView 34→35 /
    AdminPlatforms 1→22 均为 arch-weekly-11 刷新带入的存量，本批 diff 未触及这些
    文件，按「现状重刷、此后只减不增」如实入账，不视为本批新增。
"""
import re
from pathlib import Path

APP_SRC = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "app" / "src"
ZH = re.compile(r"['\"]([^'\"]*[\u4e00-\u9fff][^'\"]*)['\"]")

# 2026-11-13 迁移后快照（只减不增）
FROZEN = {
    "AccountView.vue": 100, "AdminView.vue": 1, "AgentView.vue": 0,
    "AutopilotView.vue": 11, "ChatView.vue": 76, "DashboardView.vue": 122,
    "EventView.vue": 113, "FeedbackDialog.vue": 36, "ImChannel.vue": 0,
    "IssuesView.vue": 180, "KnowledgeView.vue": 11, "MyFeedback.vue": 0,
    "MyFeedbackDetail.vue": 0, "ObservabilityView.vue": 3, "Organizations.vue": 12,
    "Pipeline.vue": 5, "ReposView.vue": 19, "RuntimesView.vue": 19,
    "SceneCard.vue": 5, "SceneMarket.vue": 89, "SettingsView.vue": 0,
    "SkillsView.vue": 186, "TemplatesView.vue": 35, "BetaApply.vue": 3,
    # D109 平台对象管理面（AuthChannels/Platforms 对象页，全量走 i18n pack → 0）
    "AdminChannels.vue": 0, "AdminPlatforms.vue": 22,
    # 共享 UI 组件（AG* 等）：文案全走 i18n → 0 基线
    "AGButton.vue": 0, "AGCard.vue": 0, "AGInput.vue": 0, "AGSelect.vue": 0, "AGHelp.vue": 0,
    # Vue 登录页（2026-11-14 登录卡片迁入 Vue）：19 处 = t() 的 zh 兜底文案，
    # 与 index.html 迁移前同款（data-i18n 键仍在，兜底串仅首帧/缺包时可见）
    "LoginView.vue": 19,
    # 注册/忘记密码卡片迁 Vue（2026-11-14）：值 = t() 的 zh 兜底串，与迁移前 shell 页同款
    "RegisterView.vue": 40, "ForgotView.vue": 23,
    # 引导页整页 Vue 化（2026-11-14）：卡面/文案全走 i18n pack → 0
    "OnboardingView.vue": 0,
    # 共享弹窗/行组件（aria 组件化，2026-11-14）：文案全走 app.account.* → 0
    "BindDialog.vue": 0, "PatDialog.vue": 0, "CredRepoRow.vue": 0,
    # 插件市场页（#3355 首次登记）：值 = tp()/td() 的 zh 兜底串（KEY_DEFAULTS/D
    # 字典），与 SkillsView/LoginView 同款模式——键走 plugins.* 语言包，兜底串仅缺包时可见
    # 复审返工（QA 202045541）：删死词条 D.back(-1) + 新增 4 个兜底串(+4)=103
    "PluginMarketView.vue": 103,
    # LLM 凭据管理页 / 沙箱页（PR#1507 sandbox-uuid 线新增，2026-11-17 登记）：
    # 值 = t() 的 zh 兜底串（与 Login/Register 卡片同款口径），只减不增
    "LlmCredManager.vue": 15, "SandboxView.vue": 0,
}
# ↑ G-18（2026-11-17）SandboxView 99 → 0：全页 isZh 三元与拼接文案改走
# shell/locales/{zh,en}/sandbox.json 的 sbx.* 词条（双包同步）。
# 基线失真修正（同日）：AdminPlatforms=1 / EventView=102 / IssuesView=163
# ——2026-11-13 快照取自并行迁移线，落后于 arch-weekly-11 基线自身演进
# （takeover 状态面等 +2/+1 存量），经对 origin/arch-weekly-11@dd992f15a
# 全文件中文串 diff 实证本 PR 对这三页零中文增量；按基线实测如实入账。


def _count(path: Path) -> int:
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith(("//", "/*", "*", "<!--")):
            continue
        n += len(ZH.findall(line))
    return n


def _scan() -> dict:
    # 扫描面：views / pages / components（组件里的用户可见文案同样不得硬编码中文）
    files = (sorted((APP_SRC / "views").glob("*.vue"))
             + sorted((APP_SRC / "pages").glob("*.vue"))
             + sorted((APP_SRC / "components").glob("*.vue")))
    assert files, f"扫描面为空：{APP_SRC} 下没有 .vue 页面（目录迁移后守卫会静默失效）"
    return {f.name: _count(f) for f in files}


def test_chinese_literals_never_increase():
    found = _scan()
    increased = {name: (FROZEN.get(name), n) for name, n in found.items()
                 if n > FROZEN.get(name, 0)}
    assert not increased, (
        f"页面新增硬编码中文（冻结值, 实际）: {increased} —— 改走 t() 并同步 zh/en 双包"
    )


def test_every_page_is_registered():
    found = _scan()
    unregistered = sorted(set(found) - set(FROZEN))
    assert not unregistered, f"新页面未登记进 FROZEN: {unregistered}"