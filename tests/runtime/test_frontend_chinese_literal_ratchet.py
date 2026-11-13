"""前端中文字面量 ratchet（PR #1231 架构复审 BLOCKED-1 配套守卫）。

回归背景：account.js 一次新增 28 处硬编码中文用户可见文案（未走 I18N.t），
en 模式回落 zh——英文用户直接看到中文。本守卫把各页面文件的「非注释行中文字面量数」
冻结成只减不增的 ratchet：新增硬编码中文 = 红，须改走 t() 并同步 zh/en 双包。

静态根迁移（2026-11-13）：旧 `src/api/static/js/pages/*.js` 已随 Vue 迁移删除，
扫描面改为 Vue 源 `src/api/frontend/app/src/{views,pages}/*.vue`，冻结值取迁移后
现状（legacy 存量债务，只减不增；新页面如 ImChannel/SettingsView 为 0，不得回潮）。

豁免口径：注释行不计；语言切换按钮等基线存量计入冻结值（不新增即可）。
新页面文件出现中文字面量必须登记进 FROZEN（过 review 时显式化）。
"""
import re
from pathlib import Path

APP_SRC = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "app" / "src"
ZH = re.compile(r"['\"]([^'\"]*[\u4e00-\u9fff][^'\"]*)['\"]")

# 2026-11-13 迁移后快照（只减不增）
FROZEN = {
    "AccountView.vue": 96, "AdminView.vue": 1, "AgentView.vue": 0,
    "AutopilotView.vue": 11, "ChatView.vue": 68, "DashboardView.vue": 121,
    "EventView.vue": 100, "FeedbackDialog.vue": 36, "ImChannel.vue": 0,
    "IssuesView.vue": 161, "KnowledgeView.vue": 11, "MyFeedback.vue": 0,
    "MyFeedbackDetail.vue": 0, "ObservabilityView.vue": 3, "Organizations.vue": 11,
    "Pipeline.vue": 5, "ReposView.vue": 18, "RuntimesView.vue": 19,
    "SceneCard.vue": 4, "SceneMarket.vue": 86, "SettingsView.vue": 0,
    "SkillsView.vue": 91, "TemplatesView.vue": 33, "BetaApply.vue": 3,
}


def _count(path: Path) -> int:
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith(("//", "/*", "*", "<!--")):
            continue
        n += len(ZH.findall(line))
    return n


def _scan() -> dict:
    files = sorted((APP_SRC / "views").glob("*.vue")) + sorted((APP_SRC / "pages").glob("*.vue"))
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