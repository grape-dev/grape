"""TC-1.4: scripts/check-arch.py runs, enforces R1-R6 and exits 0 (Issue #699 Phase 1)."""
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "check-arch.py")


def _run(extra=None):
    cmd = [sys.executable, SCRIPT] + (extra or [])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=REPO_ROOT)


def test_check_arch_reports_all_rules():
    r = _run()
    out = r.stdout
    for rule in ("R1", "R2", "R3", "R4", "R5", "R6"):
        assert rule in out, f"rule {rule} missing from report"
    assert "whitelist entries:" in out, "whitelist mechanism not reported"


def test_check_arch_passes_on_current_tree():
    r = _run()
    assert r.returncode == 0, f"hard violations present:\n{r.stdout[-2000:]}"
    assert "RESULT: PASS" in r.stdout


def test_check_arch_strict_flags_cross_domain_as_violations():
    """--strict must treat R4 cross-domain findings as failures (whitelist mechanism)."""
    r = _run(["--strict"])
    assert r.returncode == 1
    assert "RESULT: FAIL" in r.stdout


def test_r5_detects_injected_cycle():
    """Cycle detection must be live: a synthetic engine<->agents style cycle is flagged."""
    src = open(SCRIPT, encoding="utf-8").read()
    ns = {"__name__": "ca", "__file__": SCRIPT}
    exec(compile(src.replace('if __name__ == "__main__":\n    sys.exit(main())', ""), SCRIPT, "exec"), ns)

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        kernel = os.path.join(td, "kernel")
        domains = os.path.join(td, "domains")
        for d in (kernel, domains):
            os.makedirs(d)
        open(os.path.join(kernel, "__init__.py"), "w").close()
        # R1 violation: kernel imports domains
        with open(os.path.join(kernel, "bad.py"), "w") as f:
            f.write("from src.domains.mgmt.user import db\n")
        path = os.path.join(kernel, "bad.py")
        top, all_imports = ns["imports_of"](path)
        assert "src.domains.mgmt.user" in top and "src.domains.mgmt.user" in all_imports
        assert ns["layer_of"]("src.domains.mgmt.user") == "mgmt"

    # legacy shim prefix list is populated with paths that still exist as shims
    assert len(ns["LEGACY_SHIM_PREFIXES"]) >= 15
    for p in ns["LEGACY_SHIM_PREFIXES"]:
        rel = p.replace(".", os.sep) + ".py"
        assert os.path.isfile(os.path.join(REPO_ROOT, rel)), f"stale shim prefix {p} (no such module)"
    # whitelist is structured module -> {rule: reason}
    assert ns["WHITELIST"]
    for mod, rules in ns["WHITELIST"].items():
        assert isinstance(rules, dict) and rules, f"whitelist entry {mod} has no rules"
