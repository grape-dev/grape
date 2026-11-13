"""XSS 转义守卫 ratchet（扫雷⑧ S2 收口配套）：禁止用户可控字段未经 escHtml 系
helper 直接拼进 innerHTML。

背景：前端各页面曾各自维护本地 escape（部分缺单引号转义），且大量模板串
直接插值 title/summary/display_name/description/message 等用户可控字段 →
存储型 XSS 高危面。S2 已收敛：app.js 顶部定义全局唯一 window.escHtml
（含 ' →&#39;），各页本地 _esc/_escHtml/_escapeHtml/esc 一律内部代理；
高危插值点逐个包 helper（清单见 commit fix(xss) S2）。

扫描模式：src/api/static/js/**/*.js（vendor 除外）非注释行，逐个提取
模板插值 `${...}` 表达式；表达式引用用户可控字段（title/summary/
display_name/description/message，含 `||` 兜底链）且未包任何转义 helper
（escHtml/_esc/_escHtml/_escapeHtml/_evEsc494/_escChatAttr）→ 记为高危
候选行。注意：本模式是启发式——纯 textContent/字符串比较等安全用法也会
命中，故按 ratchet 手法以基线豁免存量（逐条给理由），新增/超量一律红。

基线豁免：BASELINE 按 (相对 js/ 的路径, 行内容 strip 后) 计数豁免；同
文件同内容行数超出基线计数即红。

RED 验证（XPRESS）：临时向任一 js 注入一行 `<div>${t.title}</div>` 拼接 →
本测试红 → 撤销 → 绿。注入不进 commit。
"""
import re
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
JS_ROOT = REPO_ROOT / "src" / "api" / "static" / "js"

# 用户可控字段（返回值可被攻击者控制的字段名）
FIELD_RE = re.compile(r"(?:^|[^.\w])([A-Za-z_$][\w.$]*\.)?(title|summary|display_name|description|message)\b")
# 已收敛的转义 helper（含代理与包装层）
ESC_RE = re.compile(r"escHtml|_escapeHtml|_esc\s*\(|_evEsc494|_escChatAttr")

BASELINE = {
    # ── dashboard.js：_confirmDeleteScene onclick——scene.name 已改经
    #    encodeURIComponent 编码 + decodeURIComponent 还原（双上下文安全），
    #    本扫描无法识别该模式，登记豁免。──
    ('pages/dashboard.js', '${isCustomScene ? `<button class="ap-btn ap-btn-d" style="margin-top:4px" onclick="_confirmDeleteScene(\'${scene.id}\', decodeURIComponent(\'${encodeURIComponent(scene.display_name||scene.name||\'\')}\'),null)">${I18N.t(\'common.delete\')||\'删除\'}</button>` : \'\'}'): 1,
    # ── observability.js：title/subtitle 均来自 I18N 静态文案，非用户可控 ──
    ('pages/observability.js', 'return `<div style="margin-bottom:8px;margin-top:16px"><h2 style="font-size:13px;font-weight:600">${title}</h2>${subtitle ? `<span style="font-size:10px;color:var(--t3);margin-left:8px">${subtitle}</span>` : \'\'}</div>`;'): 1,
    # ── runtimes.js：I18N 静态文案 + rt.id（服务端数值 id，agent_runtimes.id
    #    仍 INTEGER）；本行命中是 FIELD_RE 对裸词 'title' 的误报（I18N key 内）。
    #    条目曾误抄引号（resetEngineHealth(\'${rt.id}\') 与源码不符致基线失配
    #    生而红），已对齐源码实际行内容──
    ('pages/runtimes.js', '<div style="display:flex;align-items:center;gap:8px"><span style="color:var(--t3);display:inline-block;min-width:70px">健康分</span> ${_healthScoreBadge(rt) || \'—\'}${_healthCanReset(rt) ? `<button class="bt bt-o" title="${I18N.t(\'runtimes.action.reset_health\')}" style="font-size:11px;padding:3px 10px;border-radius:4px;margin-left:4px" onclick="event.stopPropagation();resetEngineHealth(${rt.id})">${I18N.t(\'runtimes.action.reset_health\')}</button>` : \'\'}</div>'): 1,
    # ── settings.js：e.message 为服务端错误消息（非用户输入回流）+ I18N key
    #    'settings.host.title' 裸词误报 ──
    ('pages/settings.js', '`<div class="cd"><p style="color:var(--r);font-size:12px">${I18N.t(\'app.account.load_failed\', {msg: e.message})}</p></div>`;'): 1,
    ('pages/settings.js', 'let html = `<div class="cd"><div class="cd-h"><h2>${I18N.t(\'settings.host.title\')}</h2></div>`;'): 1,
    # ── skills.js：scenesTitle 由 I18N 文案拼接（非用户可控）──
    ('pages/skills.js', '${scenesStr ? `<div class="ro" style="color:var(--a)" title="${scenesTitle.replace(/"/g,\'&quot;\')}">${I18N.t(\'skills.info_scenes\')||\'所属场景\'}：${scenesStr}</div>` : \'\'}'): 1,
    # ── skills.js：showSkillUpdate onclick——display_name 已做 JS 引号转义
    #    （存量双上下文口径，S2 冻结；如回潮请改 encodeURIComponent 委托）──
    ('pages/skills.js', 'btns += `<button class="bt bt-o bt-s" onclick="event.stopPropagation();showSkillUpdate(\'${s.id}\',\'${(s.display_name || s.name || \'\').replace(/\'/g, "\\\\\'\")}\')">${I18N.t(\'skills.action.update_skill\', null, \'上传更新\')}</button>`;'): 1,
}

_BASELINE_COUNTS = Counter(BASELINE)


def _iter_js_files():
    for p in sorted(JS_ROOT.rglob("*.js")):
        if "vendor" in p.parts or "node_modules" in p.parts:
            continue
        yield p


def _collect():
    """返回 [(rel_path, line_no, stripped_line)] 高危候选行。"""
    hits = []
    for p in _iter_js_files():
        rel = p.relative_to(JS_ROOT).as_posix()
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("//") or s.startswith("*") or s.startswith("/*"):
                continue
            if "innerHTML" not in line and "+=" not in line and "return `" not in line and "`" not in line:
                continue
            for m in re.finditer(r"\$\{([^}]*)\}", line):
                expr = m.group(1)
                if FIELD_RE.search(expr) and not ESC_RE.search(expr):
                    hits.append((rel, i, s))
                    break
    return hits


def test_xss_escape_guard():
    hits = _collect()
    counts = Counter((rel, s) for rel, _, s in hits)
    new = []
    for key, n in counts.items():
        base = _BASELINE_COUNTS.get(key, 0)
        if n > base:
            new.append((key, base, n))
    assert not new, (
        "发现未经 escHtml 系 helper 转义的用户可控字段插值（新增/超基线）：\n"
        + "\n".join(f"  {k[0]}: {k[1][:120]!r} (基线 {b} → 现 {n})" for k, b, n in new)
        + "\n请用 window.escHtml / 页面本地代理 helper 包裹后再拼 innerHTML；"
          "若确认为非 HTML 语境的安全用法，请在 BASELINE 登记并注明理由。"
    )
