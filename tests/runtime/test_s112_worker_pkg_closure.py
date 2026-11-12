"""S112 worker 包 import 闭包完整性（D84 P0 闭环，UC16）。

背景（2026-11-05 第九轮续审深度演练发现）：S75（bc8c8f6b5）给
src/domains/runtime/engine/service.py L11 加模块级 ``from src.domains.runtime.vcs.api_client import
_native_issue_number``，但 worker 包 MANIFEST / release_pack tools 闭包
均无 ``src/domains/runtime/vcs/`` → 存量 worker 升级新包后 agent 启动即
``ModuleNotFoundError: No module named 'src.domains.runtime.vcs'``（基线同条件 IMPORT OK
= 分支净回归实锤）。既有守卫 test_worker_package_import_closure 在基线
绿、分支红（真阳性）——守卫是活的，作者未跑。

本文件补两类守卫（防"下一个 src/vcs"回潮）：
- 结构守卫：MANIFEST / release_pack tools 闭包必须含 src/domains/runtime/vcs/
- 依赖面守卫：闭包内模块顶层 import 了 src/vcs 时，MANIFEST 必须含
  src/domains/runtime/vcs/（通用化为"顶层 import 面 ⊆ MANIFEST"口径的 vcs 特例断言）

运行时级 import 演练由既有 test_worker_package_import_closure 覆盖
（子进程干净隔离 + build_package 实打包），本文件不重复其重演练。
"""
import ast
import os
import re

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src")
_ROOT = os.path.dirname(_SRC)

# worker 闭包内（非 src/vcs 自身）已知的 src/vcs 顶层 import 面（D84 时点实查）
_CLOSURE_MODULES_USING_VCS = [
    os.path.join("src", "domains", "runtime", "engine", "service.py"),      # S75: _nin_evt
    os.path.join("src", "api", "session_store.py"),    # S106: list_release_subissues
]


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _module_toplevel_vcs_imports(rel_path):
    """返回文件顶层（模块级，非函数内）import src.domains.runtime.vcs 的语句描述列表。

    用 ast 判定作用域，避免把函数内惰性 import（合法，不要求入包内含）
    误报为顶层依赖。
    """
    src = _read(rel_path)
    tree = ast.parse(src)
    hits = []
    for node in tree.body:  # 仅顶层，不递归进 FunctionDef
        if isinstance(node, ast.ImportFrom) and node.module and (
            node.module == "src.domains.runtime.vcs" or node.module.startswith("src.domains.runtime.vcs.")
        ):
            hits.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "src.domains.runtime.vcs" or a.name.startswith("src.domains.runtime.vcs."):
                    hits.append(f"line {node.lineno}: import {a.name}")
    return hits


def _load_release_pack():
    """行为锚点装载：scripts/release_pack.py 非包成员，importlib 按路径装载。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "release_pack", os.path.join(_ROOT, "scripts", "release_pack.py"))
    assert spec and spec.loader, "release_pack.py 装载失败"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestWorkerPkgClosure:
    def test_manifest_contains_vcs(self):
        """结构守卫①：MANIFEST 必须含 src/domains/runtime/vcs/（D84 修复本体）。"""
        from src.kernel.worker_package import MANIFEST
        assert "src/domains/runtime/vcs/" in MANIFEST, (
            "MANIFEST 缺 src/domains/runtime/vcs/ —— domains/runtime/engine/service.py + api/session_store.py "
            "顶层 import src.domains.runtime.vcs，缺目录则 worker agent 启动即 ModuleNotFoundError"
            "（D84 事故回潮）"
        )

    def test_release_pack_tools_closure_contains_vcs(self):
        """结构守卫②：release_pack.py tools 闭包与 MANIFEST 同步。

        S112 根治后清单由 tools_manifest_files() 从 MANIFEST 派生（字面量块
        已退役），守卫改行为锚点：实调派生函数断言 src/domains/runtime/vcs/ 在闭包内。
        """
        rp = _load_release_pack()
        files = rp.tools_manifest_files()
        assert "src/domains/runtime/vcs/" in files, (
            "release_pack tools 闭包缺 src/domains/runtime/vcs/ —— 与 worker_package.MANIFEST 漂移，"
            "release 包 worker 会缺 src.domains.runtime.vcs"
        )

    def test_release_pack_loop_handles_directories(self):
        """守卫②b（D84 假绿教训）：manifest_files 循环必须处理目录项。

        背景：D84 首版修复把 "src/auth/"/"src/domains/runtime/vcs/" 加进 manifest_files 字面量，
        但 pack_tools 循环只 `os.path.isfile` 判断——目录项被静默跳过，守卫查
        字面量在块里就绿（假绿），实弹演练才发现 release 包仍缺 src/vcs。
        教训：往清单加目录项时必须确认消费循环支持目录；守卫不能只看字面量。
        """
        src = _read(os.path.join("scripts", "release_pack.py"))
        rp = _load_release_pack()
        has_dir_item = any(x.endswith("/") for x in rp.tools_manifest_files())
        if not has_dir_item:
            return  # 清单全文件项时循环无需处理目录
        # 清单含目录项 ⇒ 取项循环自身必须有 isdir 分支。
        # 口径：只看本循环体（manifest_files 取项循环 → “整目录”注记之前），
        # 硬编码目录循环的 os.path.isdir 不能替本循环作证（D84 假绿教训）。
        loop = src[src.index("for f in tools_manifest_files"):] if "for f in tools_manifest_files" in src else src[src.index("for f in manifest_files"):]
        loop = loop[: loop.find("# 整目录")] if "# 整目录" in loop else loop
        assert "os.path.isdir" in loop, (
            "manifest_files 含目录项但取项循环只处理 isfile —— 目录项被静默跳过"
            "（D84 假绿回潮：release 包缺目录、守卫却绿）"
        )

    def test_release_pack_closure_aligned_with_manifest(self):
        """守卫②c：release_pack tools 闭包与 MANIFEST 逐项对齐（防再漂移）。

        背景：a7cf5ffc4（2026-10-21，基线内）修 MANIFEST 加 token_service/
        token_resolver 但没同步 release_pack → release tools 包 service.py L13
        顶层 import 即崩（同 D84 形态的基线预存漂移，本批实弹演练发现并修复）。
        双清单手工双维护是漂移温床，此守卫锁逐项一致。
        """
        import ast as _ast  # noqa: F401  （对齐守卫改行为锚点后仅个别用例仍用）
        from src.kernel.worker_package import MANIFEST
        rp = _load_release_pack()
        rp_items = set(rp.tools_manifest_files())
        assert rp_items, "release_pack.py tools_manifest_files() 派生为空"
        norm = lambda s: s.rstrip("/")
        man = {norm(x) for x in MANIFEST if x != "requirements-worker.txt"}
        # S112 根治：清单由 tools_manifest_files() 从 MANIFEST 派生，闭包 ⊇ MANIFEST
        # 按构造成立；守卫保留逐项对齐断言，作为派生关系被意外拆除时的红灯。
        rp_set = {norm(x) for x in rp_items}
        missing = man - rp_set
        assert not missing, (
            f"release_pack tools 闭包缺 MANIFEST 项（基线 a7cf5ffc4 漂移形态回潮）: {sorted(missing)}"
        )
        # 反向：release_pack 独有项须逐个登记白名单（当前应为空——event_archive
        # 已随 weekly-05 停写语义移除）
        extra = rp_set - man
        assert not extra, (
            f"release_pack tools 闭包有 MANIFEST 无的项（对齐 event_archive 先例：停写即移除）: {sorted(extra)}"
        )

    def test_closure_toplevel_vcs_import_face_covered(self):
        """依赖面守卫：闭包内模块顶层 import src.domains.runtime.vcs ⇒ MANIFEST 必须含 src/domains/runtime/vcs/。

        RED 验证（D84 修复前）：service.py L11 顶层 import 命中 + MANIFEST 无
        src/domains/runtime/vcs/ → 本测试红；修复后绿。
        通用化口径：未来任何闭包内模块新增顶层 src.domains.runtime.vcs import（如 adapter）
        本守卫无需改名单仍有效——先扫 MANIFEST 已含 src/domains/runtime/vcs/ 则直接过。
        """
        from src.kernel.worker_package import MANIFEST
        if "src/domains/runtime/vcs/" in MANIFEST:
            return  # 闭包已含目录，任何顶层 import 都安全
        offenders = []
        for rel in _CLOSURE_MODULES_USING_VCS:
            hits = _module_toplevel_vcs_imports(rel)
            if hits:
                offenders.append(f"{rel}: {hits}")
        assert not offenders, (
            f"worker 闭包模块顶层 import src.domains.runtime.vcs 但 MANIFEST 无 src/domains/runtime/vcs/（D84 回潮）: {offenders}"
        )

    def test_known_vcs_import_sites_are_toplevel_documented(self):
        """名单完整性守卫：_CLOSURE_MODULES_USING_VCS 名单与实况一致。

        名单里每个文件必须真的有顶层 src.domains.runtime.vcs import（名单过期即红），
        防守卫退化为永不命中的死名单。
        """
        for rel in _CLOSURE_MODULES_USING_VCS:
            assert os.path.isfile(os.path.join(_ROOT, rel)), f"名单文件不存在: {rel}"
            hits = _module_toplevel_vcs_imports(rel)
            assert hits, (
                f"{rel} 已无顶层 src.domains.runtime.vcs import —— 名单过期，更新 "
                f"_CLOSURE_MODULES_USING_VCS（保留守卫口径）"
            )

    def test_vcs_package_dependency_free_of_third_party(self):
        """入包前置守卫：src/vcs 依赖面必须保持纯 stdlib（D84 论证前提）。

        MANIFEST 整目录携带 src/vcs 的安全性建立在"零第三方依赖"上；
        若未来引入第三方 import（requests 等），worker venv 未必有 →
        必须同步加 requirements-worker.txt（此测试红即提醒）。
        """
        import sys
        std_mods = set(sys.stdlib_module_names)
        offenders = []
        for dirpath, _dirs, files in os.walk(os.path.join(_SRC, "vcs")):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, _ROOT)
                tree = ast.parse(open(path, encoding="utf-8").read())
                for node in ast.walk(tree):
                    mods = []
                    if isinstance(node, ast.Import):
                        mods = [a.name.split(".")[0] for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        mods = [node.module.split(".")[0]]
                    for m in mods:
                        if m not in std_mods and not m.startswith("src"):
                            offenders.append(f"{rel}: import {m}")
        assert not offenders, (
            f"src/vcs 引入第三方依赖（worker 闭包需同步 requirements-worker.txt）: {offenders}"
        )
