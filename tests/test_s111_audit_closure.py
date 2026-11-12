"""S111 场景用例（D83 审计修复批次）：P0 app.js 截断 / gitee 凭据脱敏 / 测试基建环境无关性。

UC↔用例映射（requirements"候选集占位符家族场景用例"表续编）：
- UC13 app.js stripRepoPrefix 闭合完整性（D83 P0 闭环）
  ①结构断言：stripRepoPrefix 在 return 后立即闭合（大括号深度回到 0 后
    才允许出现下一个顶层 function——36ca1cfb4 截断形态守卫）
  ②node 实弹（跳过条件：环境无 node）：node --check + stub 全局直接 eval
    全文件（浏览器顶层语义，IIFE 包装会假绿）+ 顶层函数行为验证
  → test_uc13_appjs_strip_prefix_closure / test_uc13_appjs_toplevel_reachable /
    test_uc13_appjs_host_from_catalog
- UC14 web 凭据不入日志（D83 N1 闭环）：gitee query 鉴权 URL 在异常路径
  脱敏 access_token（str(e)/log.warning 渲染面无凭据）
  → test_uc14_gitee_url_redaction
- UC15 测试基建环境无关性（D83）：tests/ 零 /root 硬编码守卫
  → test_uc15_no_root_hardcoded_paths
  （缓存隔离行为锁 = test_s87_global_bot_usernames_for_provider /
    test_s88_bot_username_gitcode_pool_first，fixtures 内 _invalidate_global_bot_cache）
"""
import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_SRC = Path(__file__).resolve().parent.parent / "src"
_TESTS = Path(__file__).resolve().parent
_APP_JS = _SRC / "api" / "static" / "js" / "app.js"
_INDEX_HTML = _SRC / "api" / "static" / "index.html"


def _js_brace_depth_at(line_no: int, lines: list) -> int:
    """app.js 第 line_no 行（1-based）之前的大括号深度（剔除行注释与字符串字面量的粗粒度口径：
    本文件守卫只关心函数体结构性大括号，正则剔除 // 注释与 '...'/"..." 字面量内的括号）。"""
    depth = 0
    for line in lines[: line_no - 1]:
        code = re.sub(r"'[^']*'|\"[^\"]*\"", "", line)
        code = code.split("//")[0]
        depth += code.count("{") - code.count("}")
    return depth


# ---------- UC13：app.js stripRepoPrefix 闭合完整性 ----------

def test_uc13_appjs_strip_prefix_closure():
    """UC13①：stripRepoPrefix 函数体在 return 后立即闭合——其后的
    PlatformCatalog 函数必须出现在大括号深度 0（顶层），不允许 36ca1cfb4
    截断形态（新函数嵌套在 stripRepoPrefix 体内、文件末尾顶层调用
    ReferenceError）。"""
    lines = _APP_JS.read_text(encoding="utf-8").splitlines()
    fn_line = next(i for i, l in enumerate(lines, 1) if "function stripRepoPrefix" in l)
    # 找函数内 return 行之后的第一个顶层 function 声明
    next_fn = next((i for i, l in enumerate(lines[fn_line:], fn_line + 1)
                    if re.match(r"function \w+", l.strip())), None)
    assert next_fn is not None, "stripRepoPrefix 后未找到任何 function 声明"
    depth = _js_brace_depth_at(next_fn, lines)
    assert depth == 0, (
        f"app.js line {next_fn} 的 function 声明出现在大括号深度 {depth}（应为 0）——"
        "stripRepoPrefix 未在其 return 后闭合（36ca1cfb4 截断形态回潮）"
    )


def test_uc13_appjs_toplevel_reachable():
    """UC13②a：node 实弹——node --check 语法 + stub 全局直接 eval 全文件
    （浏览器顶层执行语义；IIFE 包装会假绿不可用）。文件末尾顶层
    _appLoadVcsCatalog() 调用必须零异常完成加载。"""
    if shutil.which("node") is None:  # pragma: no cover - 无 node 环境跳过实弹段
        import pytest
        pytest.skip("node 不可用，实弹段跳过（结构守卫见 test_uc13_appjs_strip_prefix_closure）")
    r = subprocess.run(["node", "--check", str(_APP_JS)], capture_output=True, text=True)
    assert r.returncode == 0, f"node --check 失败: {r.stderr}"

    harness = r'''
const fs=require('fs');
globalThis.window=globalThis;
window.addEventListener=()=>{};window.removeEventListener=()=>{};
globalThis.document={addEventListener:()=>{},removeEventListener:()=>{},getElementById:()=>null,querySelector:()=>null,querySelectorAll:()=>[],createElement:()=>({style:{},classList:{add:()=>{},remove:()=>{}},setAttribute:()=>{},appendChild:()=>{}}),body:{appendChild:()=>{}},head:{appendChild:()=>{}}};
globalThis.localStorage={getItem:()=>null,setItem:()=>{},removeItem:()=>{}};
globalThis.location={href:'',pathname:'/',search:'',hash:''};
globalThis.fetch=()=>Promise.resolve({ok:true,json:()=>Promise.resolve({})});
globalThis.setInterval=()=>{};globalThis.requestAnimationFrame=()=>{};
globalThis.API={get:()=>Promise.resolve({platforms:[]}),post:()=>Promise.resolve({})};
globalThis.I18N={t:(k)=>k};
let crashed=false;
try{ (0,eval)(fs.readFileSync(process.argv.pop(),'utf8')); }
catch(e){ crashed=true; console.log('CRASH: '+e.message); }
console.log(crashed?'RESULT:CRASH':'RESULT:OK');
'''
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "uc13_eval_appjs.js"
    tmp.write_text(harness, encoding="utf-8")
    r = subprocess.run(["node", str(tmp), str(_APP_JS)], capture_output=True, text=True,
                       timeout=60)
    assert "RESULT:OK" in r.stdout, f"app.js 直接 eval 加载失败: {r.stdout} {r.stderr}"


def test_uc13_appjs_host_from_catalog():
    """UC13②b：目录三函数顶层可达且行为正确——stripRepoPrefix 三形态 +
    _repoWebHost 卡派生（gitee host 不再回退 gitcode.com）。缓存版本随
    内容变更递增（?v= >= 216，UC11 纪律在本批的落点）。"""
    if shutil.which("node") is None:  # pragma: no cover
        import pytest
        pytest.skip("node 不可用，实弹段跳过")
    harness = r'''
const fs=require('fs');
globalThis.window=globalThis;
window.addEventListener=()=>{};window.removeEventListener=()=>{};
globalThis.document={addEventListener:()=>{},getElementById:()=>null,querySelector:()=>null,querySelectorAll:()=>[],createElement:()=>({style:{},classList:{add:()=>{},remove:()=>{}},setAttribute:()=>{},appendChild:()=>{}}),body:{appendChild:()=>{}},head:{appendChild:()=>{}}};
globalThis.localStorage={getItem:()=>null,setItem:()=>{},removeItem:()=>{}};
globalThis.location={href:'',pathname:'/',search:'',hash:''};
globalThis.fetch=()=>Promise.resolve({ok:true,json:()=>Promise.resolve({})});
globalThis.setInterval=()=>{};globalThis.requestAnimationFrame=()=>{};
globalThis.API={get:()=>Promise.resolve({platforms:[{provider:'gitee',host:'gitee.com'},{provider:'github',host:'github.com'},{provider:'gitcode',host:'gitcode.com'}]}),post:()=>Promise.resolve({})};
globalThis.I18N={t:(k)=>k};
(0,eval)(fs.readFileSync(process.argv.pop(),'utf8'));
const assert=(c,m)=>{if(!c){console.error('ASSERT_FAIL: '+m);process.exit(1);}};
assert(stripRepoPrefix('github:a/b')==='a/b','strip github prefix');
assert(stripRepoPrefix('a/b')==='a/b','bare key passthrough');
assert(typeof window._repoWebHost==='function','_repoWebHost top-level');
// S103 竞态窗口修复后：目录未载入时按已知前缀同步兜底（gitee→gitee.com），
// 不再误跳 gitcode.com——与 UC13②b docstring「gitee host 不再回退 gitcode.com」一致
assert(_repoWebHost('gitee:o/r')==='gitee.com','known-prefix sync fallback in race window');
_appLoadVcsCatalog().then(()=>{
  assert(_repoWebHost('gitee:o/r')==='gitee.com','gitee host from catalog');
  assert(_repoWebHost('github:o/r')==='github.com','github host from catalog');
  assert(_repoWebHost('o/r')==='gitcode.com','bare key fallback gitcode');
  assert(_repoWebLink('gitee:o/r','o/r')==='https://gitee.com/o/r','web link from catalog');
  console.log('UC13_FUNCS_OK');
}).catch(e=>{console.error('CRASH: '+e.message);process.exit(1);});
'''
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "uc13_funcs_appjs.js"
    tmp.write_text(harness, encoding="utf-8")
    r = subprocess.run(["node", str(tmp), str(_APP_JS)], capture_output=True, text=True,
                       timeout=60)
    assert "UC13_FUNCS_OK" in r.stdout, f"行为验证失败: {r.stdout} {r.stderr}"

    m = re.search(r'app\.js\?v=(\d+)', _INDEX_HTML.read_text(encoding="utf-8"))
    assert m and int(m.group(1)) >= 216, "app.js 缓存版本应随本批修复递增（?v=216 起）"


# ---------- UC14：web 凭据不入日志 ----------

def test_uc14_gitee_url_redaction():
    """UC14：gitee query 鉴权 URL 异常路径脱敏——access_token 值替换 ***，
    前缀/后缀/无参数三形态；URL 其余部分保留（_vcs_req log.warning 与
    UpstreamAPIError str() 渲染面均无凭据）。"""
    from src.domains.runtime.vcs.providers.gitee import _redact_url
    u1 = "https://gitee.com/api/v5/repos/o/r/issues?access_token=SECRET123&per_page=20"
    u2 = "https://gitee.com/api/v5/repos/o/r/issues?per_page=20&access_token=SECRET123"
    u3 = "https://gitee.com/api/v5/repos/o/r/issues"
    r1, r2, r3 = _redact_url(u1), _redact_url(u2), _redact_url(u3)
    assert "SECRET123" not in r1 and r1.endswith("access_token=***&per_page=20")
    assert "SECRET123" not in r2 and "per_page=20&access_token=***" in r2
    assert r3 == u3, "无凭据 URL 不应被改写"
    # 结构守卫：HTTPError raise 路径必须走 _redact_url（防回潮）
    src = (_SRC / "domains" / "runtime" / "vcs" / "providers" / "gitee.py").read_text(encoding="utf-8")
    assert re.search(r"raise UpstreamAPIError\(method,\s*_redact_url\(url\)", src), (
        "gitee HTTPError 路径未走 _redact_url——token 会随 UpstreamAPIError 进日志"
    )


# ---------- UC15：测试基建环境无关性 ----------

def test_uc15_no_root_hardcoded_paths():
    """UC15①：tests/ 零 /root 硬编码路径**用于读取仓库文件**（任意检出路径
    可跑）。口径：Path("/root/...") 形态才是 PermissionError 风险——测试数据
    字符串（脱敏用例样例）与文档引用不含 Path 构造，不在守卫范围。"""
    offenders = []
    pat = re.compile(r"""Path\(\s*['"]/root/""")
    for py in sorted(_TESTS.rglob("*.py")):
        if py == Path(__file__):
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pat.search(line):
                offenders.append(f"{py.relative_to(_TESTS.parent)}:{i}")
    assert not offenders, f"/root 硬编码 Path 构造残留（其他环境 PermissionError 全红）: {offenders}"


def test_uc15_platform_card_no_duplicate_fields():
    """D83 F5 闭环守卫：PlatformCard dataclass 字段无重复定义
    （issue_write_form 曾定义两次——frozen dataclass 无害但说明无 review，
    守卫防回潮）。"""
    src = (_SRC / "domains" / "runtime" / "vcs" / "platform_card.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "PlatformCard":
            anns = [n.target.id for n in node.body if isinstance(n, ast.AnnAssign)]
            dups = sorted(x for x in set(anns) if anns.count(x) > 1)
            assert not dups, f"PlatformCard 重复字段: {dups}"


def test_uc15_db_no_duplicate_s80b_block():
    """D83 R4 闭环守卫：get_webhook_verify_token 函数体内无逐字重复的
    ≥6 行块（S80b 块曾连续出现两遍）。"""
    db_lines = (_SRC / "domains" / "mgmt" / "user" / "db.py").read_text(encoding="utf-8").splitlines()
    idx = next((i for i, l in enumerate(db_lines) if "def get_webhook_verify_token" in l), None)
    assert idx is not None, "get_webhook_verify_token 不存在"
    body = db_lines[idx: idx + 120]
    seen = {}
    for i in range(len(body) - 6):
        key = "\n".join(body[i: i + 6])
        if len(key.strip()) > 80:
            if key in seen:
                raise AssertionError(
                    f"get_webhook_verify_token 存在逐字重复块（line {seen[key]+idx+1} 与 {i+idx+1}）"
                )
            seen[key] = i


def test_uc15_sqlalchemy_in_requirements():
    """D83 P1 闭环守卫：SQLAlchemy 必须在 requirements.txt 与
    requirements-local.txt（grape_account_db 模块级 import src.kernel.persistence，
    缺失则主服务起不来）。Dockerfile 走 requirements.txt 全量安装自动覆盖，
    无需单独断言。
    D85 更新：requirements-worker.txt **必须含** SQLAlchemy（chat token 注入链
    agent.py:2372 + engine_select.py:439 走 grape_account_db → persistence ORM；
    原断言"worker 清单不含"基于"worker 链零 persistence 引用"——演练证明
    deps.py+persistence 入包后该前提失效，旧断言按 D85 语义反转）。
    """
    req = (_TESTS.parent / "requirements.txt").read_text(encoding="utf-8")
    local = (_TESTS.parent / "requirements-local.txt").read_text(encoding="utf-8")
    worker = (_TESTS.parent / "requirements-worker.txt").read_text(encoding="utf-8")
    assert re.search(r"(?i)^sqlalchemy\s*>=", req, re.M), "requirements.txt 缺 SQLAlchemy"
    assert re.search(r"(?i)^sqlalchemy\s*>=", local, re.M), "requirements-local.txt 缺 SQLAlchemy"
    assert re.search(r"(?i)^sqlalchemy\s*>=", worker, re.M), (
        "requirements-worker.txt 缺 SQLAlchemy>=2.0 —— chat token 注入链/身份解析"
        "依赖 persistence ORM（D85），缺失则 import 被吞、token 静默为空"
    )
    # D85 配套：deps.py 顶层 fastapi 同样必须在 worker 清单
    assert re.search(r"(?i)^fastapi\s*>=", worker, re.M), (
        "requirements-worker.txt 缺 fastapi —— deps.py 顶层 from fastapi import（D85）"
    )
