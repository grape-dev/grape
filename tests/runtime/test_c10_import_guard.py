# -*- coding: utf-8 -*-
"""C10/A7 · SDK import 边界门禁（P0b）。

裁定口径（e2e 用例 C10 + 批0 contract spec §4）:
- 上层核心（executor.py、engine/**、cloud_worker.py）不得 import 厂商/沙箱 SDK
  —— SDK 依赖圈死在 backends/（白名单显式列模块，扩白名单须 spec 级裁定）。
- 门禁必须「人为加 SDK import 会红并指出 文件:行」（步 2/3）——本套件用
  checker 函数 + 注入样例把该行为测试化（此前只有一次性 grep，无机器守卫）。

黑名单（批0 §4.4-1 + P0b 等效断言口径合并）:
  cubesandbox / devbox（沙箱 SDK，圈死 backends/）
  anthropic / openai / google / boto3 / dashscope / qwen / zhipuai / codearts_sdk
  （厂商模型 SDK——引擎接入走 descriptor + backend 子类，核心不 import SDK）

误伤防线: 类型-only import 与字符串引用不匹配（正则锚定行首 import/from 语句）。
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

# 受控核心面（A7 口径: executor + engine/** 全目录，含 engine/cloud_worker.py）
_CORE_TARGETS = [
    "src/domains/runtime/agents/executor.py",
    "src/domains/runtime/engine",
]

# SDK 黑名单（顶层模块名; 匹配 import X / from X import）
_SDK_MODULES = (
    "cubesandbox", "devbox",
    "anthropic", "openai", "google", "boto3",
    "dashscope", "qwen", "zhipuai", "codearts_sdk",
)

# 行首 import/from 语句（允许缩进——函数内 import 也算核心面泄漏）
_SDK_IMPORT_RE = re.compile(
    rf"^\s*(?:import|from)\s+(?:{'|'.join(_SDK_MODULES)})\b"
)

# 基线豁免清单（非本批引入，历史行为冻结；新违规不在此列 → 必红）:
# - cloud_worker.py 批末清扫的 cubesandbox lazy import: 基线既有（725d14c44），
#   属批0「SDK 圈死 backends/」工作流收编范围，待批0 落地后删除本条豁免。
#   行号随基线演进漂移：1352（arch11 一代）→ 1397（11-19 基线）→ 1519（11-23）→ 1565（179-commit rebase 后实测）
#   （11-23 基线前移 433 commit 后实测，rebase 2026-11-23 同步）。
_BASELINE_ALLOWLIST = {
    "src/domains/runtime/engine/cloud_worker.py:1565",
}


def find_sdk_imports(paths, repo_root: Path | None = None) -> list[str]:
    """扫描受控面的 SDK import，返回 ['相对路径:行号: 行文本', ...]。

    基线豁免（_BASELINE_ALLOWLIST）按 路径:行号 精确豁免——只豁免历史行，
    该文件任何其他行/任何新文件违规照红。
    """
    root = repo_root or _REPO
    hits: list[str] = []
    for p in paths:
        full = root / p
        if full.is_dir():
            files = sorted(full.rglob("*.py"))
        else:
            files = [full] if full.exists() else []
        for f in files:
            rel = f.relative_to(root).as_posix()
            for i, line in enumerate(
                f.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if _SDK_IMPORT_RE.match(line) and f"{rel}:{i}" not in _BASELINE_ALLOWLIST:
                    hits.append(f"{rel}:{i}: {line.strip()}")
    return hits


class TestImportGuard:

    def test_a1_core_zero_sdk_imports(self):
        """A1: 核心面 SDK import 零命中（命中 → 白名单过宽或泄漏）。"""
        hits = find_sdk_imports(_CORE_TARGETS)
        assert not hits, "SDK import 泄漏进核心面:\n" + "\n".join(hits)

    def test_step2_injected_violation_flags_with_file_line(self):
        """C10 步2/3 机器化: 人为注入 SDK import → 门禁红并指出 文件:行；
        还原后（另一棵干净树）复绿。"""
        # 用临时副本树注入违规，不碰工作树（等效于「加一行→红→还原→绿」全链）
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td)
            core_dir = fake / "src/domains/runtime/engine"
            core_dir.mkdir(parents=True)
            good = core_dir / "fine.py"
            good.write_text("import os\nimport json\n")
            bad = core_dir / "leak.py"
            bad.write_text(
                "import os\n"
                "from anthropic import AnthropicClient  # 注入违规\n"
                "    from cubesandbox import PtySize  # 缩进内泄漏也算\n"
            )
            hits = find_sdk_imports(_CORE_TARGETS, repo_root=fake)
            assert len(hits) == 2, f"应抓 2 处违规, 实抓: {hits}"
            assert "leak.py:2:" in hits[0] and "anthropic" in hits[0], \
                f"未指出 文件:行: {hits}"
            assert "leak.py:3:" in hits[1] and "cubesandbox" in hits[1]
            assert not any("fine.py" in h for h in hits), "合法 stdlib import 被误伤"

    def test_a2_batch0_boundary_no_conflict(self):
        """A2: 与批0 门禁口径并存——本门禁扫描面不含 sandbox/backends（圈死面
        归批0 管），两套同跑不冲突；且批0 C1 终极验收同口径复扫仍零命中
        （executor/cloud_worker 无 SDK transport 类的 import 语句——注释/
        docstring 提及不算，正则只锚定 import/from）。"""
        assert not any("backends" in t for t in _CORE_TARGETS)
        _transport_re = re.compile(
            r"^\s*(?:import|from)\s+.*\b(CubeSandboxTransport|DevBoxTransport)\b")
        for f in ["src/domains/runtime/agents/executor.py",
                  "src/domains/runtime/engine/cloud_worker.py"]:
            for i, line in enumerate((_REPO / f).read_text(
                    encoding="utf-8").splitlines(), 1):
                assert not _transport_re.match(line), \
                    f"{f}:{i} 不得 import SDK transport 类: {line.strip()}"
