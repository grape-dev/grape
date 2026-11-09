# -*- coding: utf-8 -*-
"""Issue #1635：技能物化包与仓库基线双向漂移校验回归。

覆盖：
  1. collect_manifest / package_digest 确定性（排除 .tmp 与 FALLBACK.md）；
  2. validate_skill_dir 反向漂移 extra（包多基线外文件）+ baseline_lag 版本语义；
  3. official 漂移 → 阻断（不落盘，走既有回退）；
  4. user 漂移 → 不阻断，仅记录 drift；
  5. stale_package（包 < 基线）→ official 阻断；
  6. content_mismatch（同版本文件集不同）→ official 阻断；
  7. 基线缺失 baseline_unavailable 不阻断；来源行含 skill_id/scope/version/digest。
"""
import io
import os
import subprocess
import sys
import zipfile

import pytest

from src.runtime import skill_materialize as sm
from src.runtime.task_log_stream import build_skill_source_message


def _write_skill(root, name, files):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    for rel, content in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    return d


SKILL_310 = """---
name: issue-cli
version: 3.1.0
---
# issue-cli
See references/issue-runbook.md
"""
SKILL_340 = SKILL_310.replace("3.1.0", "3.4.0")


@pytest.fixture
def baseline(tmp_path):
    return _write_skill(str(tmp_path / "baseline"), "issue-cli", {
        "SKILL.md": SKILL_310,
        "references/issue-runbook.md": "# runbook 3.1.0",
    })


# ── 1. manifest / digest ─────────────────────────────────────────

def test_collect_manifest_and_digest_deterministic(tmp_path):
    d = _write_skill(str(tmp_path), "demo", {
        "SKILL.md": "# demo",
        "references/a.md": "a",
    })
    # 非技能资产不参与清单
    with open(os.path.join(d, "FALLBACK.md"), "w", encoding="utf-8") as f:
        f.write("fallback")
    tmp = os.path.join(d, ".tmp-x")
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, "junk"), "w", encoding="utf-8") as f:
        f.write("junk")

    assert sm.collect_manifest(d) == ["SKILL.md", "references/a.md"]
    first = sm.package_digest(d)
    assert len(first) == 16 and first == sm.package_digest(d)
    # 内容变化但大小不变 → digest 不变（仅 rel+size 指纹）
    with open(os.path.join(d, "references/a.md"), "w", encoding="utf-8") as f:
        f.write("b")
    assert sm.package_digest(d) == first


# ── 2. 反向漂移 extra + baseline_lag ─────────────────────────────

def test_validate_detects_extra_and_baseline_lag(tmp_path, baseline):
    pkg = _write_skill(str(tmp_path / "pkg"), "issue-cli", {
        "SKILL.md": SKILL_340,
        "references/issue-runbook.md": "# runbook 3.4.0",
        "references/cross-scene-handoff.md": "# handoff",
    })
    ok, missing, extra, drift = sm.validate_skill_dir(pkg, baseline_dir=baseline)
    assert ok is True and missing == []
    assert extra == ["references/cross-scene-handoff.md"]
    assert drift["missing"] == []
    assert drift["version"]["package"] == "3.4.0"
    assert drift["version"]["baseline"] == "3.1.0"
    # 包比基线新 → 仅提示补基线，不阻断
    assert drift["version"]["kind"] == "baseline_lag"
    assert sm.drift_blocks(drift, "official") is False


# ── 3. official 漂移阻断 ─────────────────────────────────────────

def test_baseline_lag_not_block_official(tmp_path, baseline):
    pkg = _write_skill(str(tmp_path / "pkg"), "issue-cli", {
        "SKILL.md": SKILL_340,
        "references/issue-runbook.md": "# runbook 3.4.0",
        "references/cross-scene-handoff.md": "# handoff",
    })
    skills_root = str(tmp_path / ".skills")
    meta = {}
    ok, reason = sm.materialize_skill_dir_atomic(
        skills_root, "issue-cli", pkg,
        baseline_dir=baseline, scope="official", meta=meta)
    # 包比基线新（baseline_lag）→ 提示补基线，不阻断
    assert ok is True, reason
    assert meta["drift"]["extra"] == ["references/cross-scene-handoff.md"]


# ── 4. user 漂移仅告警不阻断 ─────────────────────────────────────

def test_user_drift_warns_not_blocks(tmp_path, baseline):
    pkg = _write_skill(str(tmp_path / "pkg"), "issue-cli", {
        "SKILL.md": SKILL_340,
        "references/issue-runbook.md": "# runbook 3.4.0",
        "references/cross-scene-handoff.md": "# handoff",
    })
    skills_root = str(tmp_path / ".skills")
    meta = {}
    ok, reason = sm.materialize_skill_dir_atomic(
        skills_root, "issue-cli", pkg,
        baseline_dir=baseline, scope="user", meta=meta)
    assert ok is True, reason
    assert os.path.isfile(os.path.join(
        skills_root, "issue-cli", "references", "cross-scene-handoff.md"))
    assert meta["drift"]["extra"] == ["references/cross-scene-handoff.md"]

    # zip 渠道同样执行漂移校验（三渠道统一）
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("issue-cli/SKILL.md", SKILL_340)
        zf.writestr("issue-cli/references/issue-runbook.md", "# runbook")
        zf.writestr("issue-cli/references/cross-scene-handoff.md", "# handoff")
    zmeta = {}
    zok, zskills, zreason = sm.materialize_skill_zip_atomic(
        str(tmp_path / ".skills-zip"), buf.getvalue(),
        baseline_dir=baseline, scope="user", meta=zmeta)
    assert zok is True, zreason
    assert "issue-cli" in zskills
    assert zmeta["skills"]["issue-cli"]["drift"]["extra"] == \
        ["references/cross-scene-handoff.md"]


# ── 5. stale_package（包 < 基线）official 阻断 ───────────────────

def test_stale_package_blocks_official(tmp_path, baseline):
    pkg = _write_skill(str(tmp_path / "pkg"), "issue-cli", {
        "SKILL.md": SKILL_310.replace("3.1.0", "3.0.0"),
        "references/issue-runbook.md": "# old",
    })
    drift = sm.compute_skill_drift(pkg, baseline)
    assert drift["version"]["kind"] == "stale_package"
    assert sm.drift_blocks(drift, "official") is True

    skills_root = str(tmp_path / ".skills")
    ok, reason = sm.materialize_skill_files_atomic(
        skills_root, "issue-cli",
        {"SKILL.md": SKILL_310.replace("3.1.0", "3.0.0"),
         "references/issue-runbook.md": "# old"},
        baseline_dir=baseline, scope="official")
    assert ok is False
    assert "baseline drift" in reason


# ── 6. content_mismatch（同版本文件集不同）official 阻断 ─────────

def test_content_mismatch_same_version_blocks_official(tmp_path, baseline):
    pkg = _write_skill(str(tmp_path / "pkg"), "issue-cli", {
        "SKILL.md": SKILL_310,
        "references/issue-runbook.md": "# runbook 3.1.0",
        "references/extra.md": "# extra",
    })
    drift = sm.compute_skill_drift(pkg, baseline)
    assert drift["version"]["package"] == drift["version"]["baseline"] == "3.1.0"
    assert drift["version"]["kind"] == "content_mismatch"
    assert sm.drift_blocks(drift, "official") is True
    # user scope 下同样内容只告警不阻断
    assert sm.drift_blocks(drift, "user") is False


# ── 7. 基线缺失不阻断 + 来源行字段 ──────────────────────────────

def test_baseline_unavailable_not_block_and_source_line(tmp_path):
    pkg = _write_skill(str(tmp_path / "pkg"), "issue-cli", {
        "SKILL.md": SKILL_340,
        "references/issue-runbook.md": "# runbook",
    })
    missing_base = str(tmp_path / "no-such-baseline")
    drift = sm.compute_skill_drift(pkg, missing_base)
    assert drift["baseline_available"] is False
    assert drift["version"]["kind"] == "baseline_unavailable"
    assert sm.drift_blocks(drift, "official") is False

    skills_root = str(tmp_path / ".skills")
    ok, reason = sm.materialize_skill_dir_atomic(
        skills_root, "issue-cli", pkg,
        baseline_dir=missing_base, scope="official")
    assert ok is True, reason

    info = sm.build_skill_drift_and_source(
        skills_root, "issue-cli", skill_id=3842, scope="user",
        skill_type="issue", baseline_dir=missing_base)
    assert info["source"]["skill_id"] == 3842
    assert info["source"]["scope"] == "user"
    assert info["source"]["version"] == "3.4.0"
    assert len(info["source"]["digest"]) == 16

    msg = build_skill_source_message(
        "issue-cli", "issue-cli", "bound", ok=True,
        source_info=info["source"], drift=info["drift"])
    text = msg["data"]["text"]
    assert "skill_id=3842" in text
    assert "scope=user" in text
    assert "version=3.4.0" in text
    assert "digest=" in text
    assert msg["data"]["skill_source"]["digest"] == info["source"]["digest"]
    assert msg["data"]["skill_source"]["drift"]["version"]["kind"] == \
        "baseline_unavailable"

# ── 8. review 修复：兼容调用不误算漂移 / 回退与降级来源 ──────────

def test_legacy_call_no_baseline_skips_drift(tmp_path):
    """旧调用（无 scope/baseline）不自动解析基线，drift 为空、source 仍构造。"""
    pkg = _write_skill(str(tmp_path / "pkg"), "issue-cli", {
        "SKILL.md": SKILL_340,
        "references/issue-runbook.md": "# runbook",
    })
    res = sm.materialize_skill_with_fallback(
        str(tmp_path / ".skills"), "issue-cli", lambda: ("dir", pkg))
    assert res["status"] == "ok"
    assert res.get("drift") is None
    assert res["source"]["scope"] == "unknown"
    assert res["source"]["version"] == "3.4.0"


def test_official_fallback_source_uses_official_scope(tmp_path, monkeypatch):
    """官方包回退成功：逐源为 official、skill_id=0（不混用主渠道 id）。"""
    official = _write_skill(str(tmp_path / "official"), "issue-cli", {
        "SKILL.md": SKILL_310,
        "references/issue-runbook.md": "# runbook",
    })
    import src.config as cfg
    monkeypatch.setattr(cfg, "resolve_official_skill_path",
                        lambda name: os.path.join(official, "SKILL.md"))
    res = sm.materialize_skill_with_fallback(
        str(tmp_path / ".skills"), "issue-cli",
        lambda: ("none", "zip unavailable"), scope="user", skill_id=999)
    assert res["status"] == "fallback"
    assert res["source"]["scope"] == "official"
    assert res["source"]["skill_id"] == 0
    assert res["source"]["version"] == "3.1.0"


def test_degraded_has_source_and_drift(tmp_path, monkeypatch):
    """degraded 结果也构造 source/drift，调用点取用不落空。"""
    import src.config as cfg
    monkeypatch.setattr(cfg, "resolve_official_skill_path", lambda name: "")
    res = sm.materialize_skill_with_fallback(
        str(tmp_path / ".skills"), "issue-cli",
        lambda: ("none", "unavailable"), scope="user", skill_id=42)
    assert res["status"] == "degraded"
    assert res["source"]["skill_id"] == 42
    assert res["source"]["scope"] == "user"
    assert res["drift"]["baseline_available"] is False


def test_agent_type_fallback_has_official_source(tmp_path, monkeypatch):
    """agent_type 回退成功同样构造 official source/drift。"""
    import src.config as cfg
    monkeypatch.setattr(cfg, "resolve_official_skill_path", lambda name: "")
    checker = _write_skill(str(tmp_path / "official"), "grape-skill-validator", {
        "SKILL.md": "# checker\n",
    })
    resolver = lambda at: ({"skill_name": "grape-skill-validator", "skill_id": 2789,
                            "skill_dir": checker} if at == "qa" else None)
    res = sm.materialize_skill_with_fallback(
        str(tmp_path / ".skills"), "missing-skill",
        lambda: ("none", "not found"),
        agent_type="qa", agent_type_fallback=resolver)
    assert res["status"] == "fallback"
    assert res["source"]["scope"] == "official"
    assert res["source"]["skill_id"] == 2789
    assert "drift" in res and res["drift"] is not None


# ── 9. review 修复：全量扫描脚本按四元组解包可运行 ───────────────

def test_scan_script_runs_without_unpack_error(tmp_path):
    _write_skill(str(tmp_path), "issue-cli", {
        "SKILL.md": SKILL_310,
        "references/issue-runbook.md": "# runbook",
    })
    script = os.path.join(os.path.dirname(__file__), "..",
                          "scripts", "scan_skill_cross_skill_refs.py")
    out = subprocess.run([sys.executable, script, str(tmp_path)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "扫描技能总数: 1" in out.stdout
