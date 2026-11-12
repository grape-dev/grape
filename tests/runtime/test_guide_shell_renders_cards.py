"""引导页卡片行为测试——沙箱真实执行 _renderOnboardingShell（防"孤儿表达式"类回潮）。

实弹事故（2026-11-08）：cherry-pick 后 forEach 内 platformCards += 拼接行丢失，
残留孤儿 `+ '</div>';` 表达式（语法合法，node --check 与源码字符串断言全绿），
引导页卡片区永远空白。本测试把 app.js 里的 shell 函数抽出来（花括号平衡扫描
（node 真执行，跳过字符串/注释）断言：卡片数 == 平台数、无角标残留、无置灰按钮
（2026-11-08 口径 v4：卡样式回归基线，角标方案已废弃；双模式在弹层内）。
平台绑定按钮置灰。
"""
import json
import re
import subprocess
from pathlib import Path

APP = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "static" / "js" / "app.js"

_RUNNER = r'''
const fnSrc = JSON.parse(require('fs').readFileSync(__FN_FILE__, 'utf8'));
const providers = JSON.parse(require('fs').readFileSync(__PROV_FILE__, 'utf8'));
let inner = '';
const doc = { getElementById: (id) => (els[id] || (els[id] = { style: {}, set innerHTML(v) { inner = v; }, classList: { add() {} } })) };
const els = {};
const ls = { getItem: () => JSON.stringify({ bindings: ['gitee'] }) };
const fn = new Function('providers','document','localStorage','platformIcon','I18N','_esc','_obHeroHtml','_updateTbOrgName','_updateLobbyOrgName',
  'return (' + fnSrc + ')(providers, true, "probe", "P");');
fn(providers, doc, ls,
  () => '<svg/>',
  { getLang: () => 'zh', t: (k) => k },
  (x) => String(x),
  (n, z, cards) => 'HERO[' + cards + ']',
  () => {}, () => {});
console.log(JSON.stringify({
  cards: (inner.match(/ob-bind-card/g) || []).length,
  badges: (inner.match(/ob-status-badge/g) || []).length,
  grayBtn: (inner.match(/aria-disabled/g) || []).length,
  boundOk: inner.indexOf('已绑定该平台') >= 0,
  unconfTip: inner.indexOf('暂未开通 OAuth 授权') >= 0,
}));
'''


def _extract_shell_fn(src: str) -> str:
    """截取 _renderOnboardingShell 整函数：到下一个 '\n  function ' 兄弟声明。

    （不做花括号平衡扫描——JS 正则字面量里的引号会让朴素扫描器失步；
    顶栏格式由 test_d102h 结构守卫+node --check 兜底，这里信任 2 空格
    函数声明的仓库约定。截取完整性由本文件的执行测试自证。）
    """
    i = src.find("function _renderOnboardingShell")
    assert i >= 0
    j = src.find("\n  function ", i)
    assert j > i, "找不到下一个兄弟函数（结构变更需同步本测试）"
    return src[i:j]


def _run_shell() -> dict:
    src = APP.read_text(encoding="utf-8")
    fn_src = _extract_shell_fn(src)
    providers = [
        {"provider": "gitcode", "name": "GitCode", "pat_create_url": "u", "oauth_configured": False},
        {"provider": "gitee", "name": "Gitee", "pat_create_url": "u", "oauth_configured": True},
    ]
    runner = Path(__file__).parent / "_guide_shell_runner.js"
    fn_file = Path(__file__).parent / "_guide_shell_fn.json"
    prov_file = Path(__file__).parent / "_guide_shell_providers.json"
    fn_file.write_text(json.dumps(fn_src), encoding="utf-8")
    prov_file.write_text(json.dumps(providers), encoding="utf-8")
    runner.write_text(_RUNNER
                      .replace("__FN_FILE__", json.dumps(str(fn_file)))
                      .replace("__PROV_FILE__", json.dumps(str(prov_file))), encoding="utf-8")
    r = subprocess.run(
        ["node", str(runner)],
        capture_output=True, text=True, timeout=60)
    fn_file.unlink()
    prov_file.unlink()
    out = r.stdout.strip()
    assert out, f"node 执行失败: {r.stderr[:400]}"
    runner.unlink()
    return json.loads(out)


def test_shell_renders_one_card_per_platform():
    r = _run_shell()
    # 场景：gitcode（未配置）/ gitee（已配置，用户已绑定 gitee）→ 2 卡
    assert r["cards"] == 2, f"卡片数错误: {r}"
    assert r["badges"] == 0, "基线卡无角标（口径 v4）"


def test_unconfigured_platform_renders_plain_bind_button():
    r = _run_shell()
    assert r["grayBtn"] == 0, "基线卡绑定按钮恒可用（置灰方案废弃）"


def test_bound_platform_no_pat_mention_on_card():
    r = _run_shell()
    assert not r["boundOk"], "卡面不应有绑定态 title（绑定状态在弹层/账号页呈现）"
