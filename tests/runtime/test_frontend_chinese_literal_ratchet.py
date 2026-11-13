"""前端中文字面量 ratchet（PR #1231 架构复审 BLOCKED-1 配套守卫）。

回归背景：account.js 一次新增 28 处硬编码中文用户可见文案（未走 I18N.t），
en 模式回落 zh——英文用户直接看到中文。已全部 key 化（zh/en/app.json 的
app.account 段双包同步）；本守卫把各页面文件的"非注释行中文字面量数"冻结成
只减不增的 ratchet：新增硬编码中文 = 红，须改走 I18N.t 并同步 zh/en 双包。

豁免口径：语言切换按钮"中文"标签等基线存量计入冻结值（不新增即可）；
注释行不计。新页面文件出现中文字面量也须登记进 FROZEN（过 review 时显式化）。
"""
import re
from pathlib import Path

PAGES = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "static" / "js" / "pages"
_ZH = re.compile(r"[\x27\"]([^\x27\"]*[\u4e00-\u9fff][^\x27\"]*)[\x27\"]")

FROZEN = {
    "account.js": 4,
    "autopilot.js": 8,
    "chat.js": 8,
    "dashboard.js": 204,
    "events.js": 22,
    "feedback.js": 35,
    "issues.js": 146,
    "knowledge.js": 11,
    "observability.js": 12,
    "organizations.js": 12,
    "pipeline.js": 7,
    "repos.js": 19,
    "runtimes.js": 30,
    "scene-market.js": 107,
    "skills.js": 102,
    "templates.js": 14
}


def _count(path: Path) -> int:
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("//") or s.startswith("/*") or s.startswith("*"):
            continue
        n += len(_ZH.findall(line))
    return n


def test_chinese_literal_counts_never_increase():
    regressions = []
    for name, cap in FROZEN.items():
        cur = _count(PAGES / name)
        if cur > cap:
            regressions.append(f"{name}: {cur} > 基线 {cap}（新增硬编码中文须走 I18N.t）")
    assert not regressions, "\n".join(regressions)


def test_new_pages_with_chinese_literals_must_register():
    unregistered = []
    for p in sorted(PAGES.glob("*.js")):
        if p.name in FROZEN:
            continue
        if _count(p) > 0:
            unregistered.append(p.name)
    assert not unregistered, (
        f"新页面文件含中文字面量未登记 FROZEN: {unregistered}（登记须过 review）")
