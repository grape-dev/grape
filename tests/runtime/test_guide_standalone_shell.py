"""D102-O 续：引导页必须是全屏独立壳——任何入口都不得嵌在工作区壳内渲染。

实弹现象（2026-11-08 用户报告）：首次登录（_no_vcs_binding 经 loadRepos 入口）
与语言切换重渲染（_rerenderReposView 入口）进引导页时，引导页嵌在工作区壳内
（顶栏+侧栏可见）——因为只有 goToRepos 三个入口做了壳隐藏（sidebar/tb display:none
+ mainView fixed inset:0），_renderOnboardingShell 本身不做。

修复口径：壳隐藏收口到 _renderOnboardingShell 函数体内——幂等（goToRepos 入口
先隐藏一次无害），四个入口行为归一。本测试用沙箱真执行 shell 函数，断言其对
sidebar/tb/mainView 的壳操作（行为断言，非源码字符串断言——d102e 教训）。
"""
import json
import subprocess
from pathlib import Path

APP = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "shell" / "js" / "app.js"

_RUNNER = r'''
const fnSrc = JSON.parse(require('fs').readFileSync(__FN_FILE__, 'utf8'));
const providers = JSON.parse(require('fs').readFileSync(__PROV_FILE__, 'utf8'));
let inner = '';
const els = {};
function mkEl(id) {
  return els[id] || (els[id] = { style: {}, classList: { add() {} }, set innerHTML(v) { inner = v; },
                                 getAttribute: () => '', insertAdjacentHTML() {},
                                 querySelectorAll: () => [], querySelector: () => null });
}
const doc = { getElementById: (id) => mkEl(id),
              querySelectorAll: () => [], querySelector: () => null };
const ss = { store: {}, getItem: (k) => (k in ss.store ? ss.store[k] : JSON.stringify({ bindings: [] })),
             removeItem: (k) => { delete ss.store[k]; }, setItem: (k, v) => { ss.store[k] = v; } };
const ls = { getItem: () => JSON.stringify({ bindings: [] }) };
const fn = new Function('providers','document','localStorage','sessionStorage','platformIcon','I18N','_esc','_obHeroHtml','_updateTbOrgName','_updateLobbyOrgName',
  'return (' + fnSrc + ')(providers, true, "probe", "P");');
fn(providers, doc, ls, ss,
  () => '<svg/>',
  { getLang: () => 'zh', t: (k) => k },
  (x) => String(x),
  (n, z, cards) => 'HERO' + (cards || ''),
  () => {}, () => {});
console.log(JSON.stringify({
  sidebarHidden: els['sidebar'] && els['sidebar'].style.display === 'none',
  tbHidden: els['tb'] && els['tb'].style.display === 'none',
  mvFullscreen: els['mainView'] && els['mainView'].style.position === 'fixed'
                && els['mainView'].style.inset === '0',
  cards: (inner.match(/ob-bind-card/g) || []).length,
}));
'''


def _extract_shell_fn(src: str) -> str:
    i = src.find("function _renderOnboardingShell")
    assert i >= 0
    j = src.find("\n  function ", i)
    assert j > i
    return src[i:j]


def _run_shell() -> dict:
    import tempfile
    src = APP.read_text(encoding="utf-8")
    fn_src = _extract_shell_fn(src)
    providers = [
        {"provider": "gitcode", "name": "GitCode", "pat_create_url": "u", "oauth_configured": False},
        {"provider": "gitee", "name": "Gitee", "pat_create_url": "u", "oauth_configured": True},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as ff:
        ff.write(json.dumps(fn_src))
        fn_file = ff.name
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as pf:
        pf.write(json.dumps(providers))
        prov_file = pf.name
    runner = (_RUNNER
              .replace("__FN_FILE__", json.dumps(fn_file))
              .replace("__PROV_FILE__", json.dumps(prov_file)))
    r = subprocess.run(["node", "-e", runner], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node 沙箱执行失败: {r.stderr[-800:]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_guide_shell_hides_workspace_chrome():
    """引导壳渲染必须自带壳隐藏：sidebar/tb 隐藏 + mainView 全屏（四入口归一）。"""
    out = _run_shell()
    assert out["sidebarHidden"], (
        "引导壳未隐藏 sidebar——loadRepos/_rerenderReposView 入口会把引导页嵌在工作区壳内"
    )
    assert out["tbHidden"], "引导壳未隐藏顶栏 tb——同上"
    assert out["mvFullscreen"], "引导壳未把 mainView 置全屏（fixed inset:0）"
    assert out["cards"] >= 2, "回归锁：卡片区照常渲染"
