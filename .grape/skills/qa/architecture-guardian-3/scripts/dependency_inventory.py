#!/usr/bin/env python3
"""Inventory dependencies, lockfiles and pinning discipline.

Read-only. Standard library only. No network access: this reports what the
repository declares, not which CVEs are known. Vulnerability data must come from
a scanner or an advisory database, and this script deliberately does not guess
at it, because an invented CVE claim is worse than none.

Usage:
    python dependency_inventory.py [path] [--json]
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git", "node_modules", "vendor", "dist", "build", ".next", ".nuxt",
    "__pycache__", ".venv", "venv", "env", ".tox", "target", ".gradle",
    "coverage", ".terraform", "out", ".cache", ".netlify", ".vercel",
    "site-packages", ".output", ".turbo",
}

LOCK_FOR = {
    "package.json": ["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb"],
    "pyproject.toml": ["poetry.lock", "uv.lock", "pdm.lock", "requirements.lock"],
    "requirements.txt": ["requirements.lock"],
    "Pipfile": ["Pipfile.lock"],
    "Gemfile": ["Gemfile.lock"],
    "composer.json": ["composer.lock"],
    "Cargo.toml": ["Cargo.lock"],
    "go.mod": ["go.sum"],
}

# Ecosystem-specific notions of "not pinned".
FLOATING_NPM = re.compile(r"^\s*(\^|~|>|>=|<|<=|\*|latest|x$|\d+\.x|\d+\.\d+\.x)")
GIT_DEP = re.compile(r"(git\+|github:|gitlab:|bitbucket:|file:|link:|https?://)", re.I)


def walk_shallow(root: Path, max_depth: int = 3):
    """Walk, but not into vendored trees, and not arbitrarily deep."""
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).resolve().relative_to(root).parts)
        if depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git")]
        for fn in filenames:
            yield Path(dirpath) / fn


def read_text(p: Path, limit: int = 400_000) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def analyse_npm(path: Path, rel: str, out: dict) -> None:
    try:
        data = json.loads(read_text(path) or "{}")
    except json.JSONDecodeError:
        out["errors"].append(f"{rel}: not valid JSON")
        return

    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (data.get(section) or {}).items():
            spec = str(spec)
            entry = {
                "name": name, "spec": spec, "section": section,
                "manifest": rel, "ecosystem": "npm",
            }
            if GIT_DEP.search(spec):
                entry["pinning"] = "external-source"
                out["external_source"].append(entry)
            elif FLOATING_NPM.match(spec):
                entry["pinning"] = "floating"
                out["floating"].append(entry)
            else:
                entry["pinning"] = "exact"
            out["all"].append(entry)

    if data.get("engines"):
        out["runtime_constraints"].append({"manifest": rel, "engines": data["engines"]})
    else:
        out["notes"].append(
            f"{rel}: no 'engines' field. The Node version used in production is not "
            "declared, so local, CI and production may differ."
        )

    scripts = data.get("scripts") or {}
    for hook in ("preinstall", "postinstall", "prepare"):
        if hook in scripts:
            out["install_hooks"].append({"manifest": rel, "hook": hook, "command": scripts[hook]})


def analyse_python_requirements(path: Path, rel: str, out: dict) -> None:
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            if line.startswith(("-e", "--editable")) or "git+" in line:
                out["external_source"].append({
                    "name": line, "spec": line, "section": "requirements",
                    "manifest": rel, "ecosystem": "pip", "pinning": "external-source",
                })
            continue
        m = re.match(r"^([A-Za-z0-9._\-\[\]]+)\s*(.*)$", line)
        if not m:
            continue
        name, spec = m.group(1), m.group(2).strip()
        entry = {"name": name, "spec": spec or "unpinned", "section": "requirements",
                 "manifest": rel, "ecosystem": "pip"}
        if not spec:
            entry["pinning"] = "unpinned"
            out["unpinned"].append(entry)
        elif spec.startswith("=="):
            entry["pinning"] = "exact"
        else:
            entry["pinning"] = "floating"
            out["floating"].append(entry)
        out["all"].append(entry)


def analyse_generic(path: Path, rel: str, out: dict, ecosystem: str) -> None:
    """Count declared dependencies for ecosystems without a dedicated parser."""
    text = read_text(path)
    patterns = {
        "go": r"^\s+([a-zA-Z0-9./\-]+)\s+v([0-9][^\s]*)",
        "cargo": r'^\s*([A-Za-z0-9._\-]+)\s*=\s*["{]',
        "composer": None,
        "gem": r"^\s*gem\s+['\"]([^'\"]+)['\"]\s*(?:,\s*['\"]([^'\"]+)['\"])?",
    }
    if ecosystem == "composer":
        try:
            data = json.loads(text or "{}")
        except json.JSONDecodeError:
            return
        for section in ("require", "require-dev"):
            for name, spec in (data.get(section) or {}).items():
                entry = {"name": name, "spec": str(spec), "section": section,
                         "manifest": rel, "ecosystem": "composer"}
                entry["pinning"] = "floating" if re.match(r"^[\^~>*]", str(spec)) else "exact"
                if entry["pinning"] == "floating":
                    out["floating"].append(entry)
                out["all"].append(entry)
        return

    pat = patterns.get(ecosystem)
    if not pat:
        return
    for m in re.finditer(pat, text, re.M):
        spec = m.group(2) if m.lastindex and m.lastindex > 1 and m.group(2) else "see manifest"
        out["all"].append({"name": m.group(1), "spec": spec, "section": "require",
                           "manifest": rel, "ecosystem": ecosystem, "pinning": "unknown"})


def analyse_ci_pinning(root: Path, out: dict) -> None:
    """CI actions referenced by mutable tag can be repointed at new code."""
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return
    for wf in list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml")):
        rel = str(wf.relative_to(root)).replace("\\", "/")
        for i, line in enumerate(read_text(wf).splitlines(), 1):
            m = re.search(r"uses:\s*([^\s#]+)", line)
            if not m:
                continue
            ref = m.group(1)
            if ref.startswith("./") or ref.startswith("."):
                continue
            if "@" not in ref:
                continue
            action, version = ref.rsplit("@", 1)
            pinned_to_sha = bool(re.fullmatch(r"[0-9a-f]{40}", version))
            entry = {"file": rel, "line": i, "action": action, "ref": version,
                     "pinned_to_sha": pinned_to_sha}
            out["ci_actions"].append(entry)
            if not pinned_to_sha:
                out["ci_unpinned"].append(entry)


def scan(root: Path) -> dict:
    root = root.resolve()
    out = {
        "root": str(root),
        "manifests": [],
        "lockfiles": [],
        "missing_lockfiles": [],
        "gitignored_lockfiles": [],
        "all": [],
        "floating": [],
        "unpinned": [],
        "external_source": [],
        "install_hooks": [],
        "runtime_constraints": [],
        "ci_actions": [],
        "ci_unpinned": [],
        "notes": [],
        "errors": [],
    }

    all_locks = set()
    for f in walk_shallow(root):
        rel = str(f.relative_to(root)).replace("\\", "/")
        name = f.name

        if name in LOCK_FOR:
            out["manifests"].append(rel)
            if name == "package.json":
                analyse_npm(f, rel, out)
            elif name in ("requirements.txt", "Pipfile"):
                analyse_python_requirements(f, rel, out)
            elif name == "go.mod":
                analyse_generic(f, rel, out, "go")
            elif name == "Cargo.toml":
                analyse_generic(f, rel, out, "cargo")
            elif name == "composer.json":
                analyse_generic(f, rel, out, "composer")
            elif name == "Gemfile":
                analyse_generic(f, rel, out, "gem")

        if re.match(r"^requirements.*\.txt$", name) and rel not in out["manifests"]:
            out["manifests"].append(rel)
            analyse_python_requirements(f, rel, out)

        for locks in LOCK_FOR.values():
            if name in locks:
                out["lockfiles"].append(rel)
                all_locks.add(name)

    for manifest, accepted in LOCK_FOR.items():
        if (root / manifest).is_file() and not (all_locks & set(accepted)):
            out["missing_lockfiles"].append({"manifest": manifest, "expected_one_of": accepted})

    # A lockfile that exists locally but is ignored by git gives no reproducibility.
    gitignore = read_text(root / ".gitignore")
    if gitignore:
        for lock in out["lockfiles"]:
            base = Path(lock).name
            if re.search(rf"^\s*/?{re.escape(base)}\s*$", gitignore, re.M):
                out["gitignored_lockfiles"].append(lock)

    analyse_ci_pinning(root, out)

    if out["install_hooks"]:
        out["notes"].append(
            "Install hooks present. These execute during dependency installation, "
            "including in CI, which makes them part of the supply chain surface."
        )
    if out["ci_unpinned"]:
        out["notes"].append(
            "CI actions referenced by tag rather than commit SHA. A tag can be moved "
            "to point at different code without any change in this repository."
        )
    if out["gitignored_lockfiles"]:
        out["notes"].append(
            "A lockfile is gitignored. Builds are not reproducible across machines."
        )

    out["notes"].append(
        "No vulnerability data was collected. This script does not have network access "
        "and does not guess at CVEs. Run an SCA tool for advisory data."
    )
    return out


def print_report(r: dict) -> None:
    def section(t):
        print(f"\n{t}\n{'-' * len(t)}")

    print(f"DEPENDENCY INVENTORY: {r['root']}")
    print(f"\nManifests: {len(r['manifests'])}   declared dependencies: {len(r['all'])}")

    section("Manifests and lockfiles")
    for m in r["manifests"]:
        print(f"  manifest  {m}")
    for l in r["lockfiles"]:
        flag = "  [GITIGNORED]" if l in r["gitignored_lockfiles"] else ""
        print(f"  lock      {l}{flag}")
    for miss in r["missing_lockfiles"]:
        print(f"  MISSING   {miss['manifest']} has no lockfile "
              f"(expected one of: {', '.join(miss['expected_one_of'])})")

    counts = {}
    for d in r["all"]:
        counts[d.get("pinning", "unknown")] = counts.get(d.get("pinning", "unknown"), 0) + 1
    section("Pinning")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<18} {v}")

    if r["unpinned"]:
        section(f"Unpinned ({len(r['unpinned'])}) — version resolved at install time")
        for d in r["unpinned"][:25]:
            print(f"  {d['name']:<34} {d['manifest']}")

    if r["external_source"]:
        section(f"Installed from outside the registry ({len(r['external_source'])})")
        for d in r["external_source"][:25]:
            print(f"  {d['name']:<28} {d['spec'][:70]}")
        print("  Verify each: these bypass registry integrity checks.")

    if r["install_hooks"]:
        section("Install hooks")
        for h in r["install_hooks"]:
            print(f"  {h['manifest']} [{h['hook']}]: {str(h['command'])[:90]}")

    if r["ci_actions"]:
        section(f"CI actions ({len(r['ci_actions'])}, unpinned: {len(r['ci_unpinned'])})")
        for a in r["ci_unpinned"][:25]:
            print(f"  {a['action'] + '@' + a['ref']:<44} {a['file']}:{a['line']}")
        if not r["ci_unpinned"]:
            print("  all pinned to commit SHA")

    if r["runtime_constraints"]:
        section("Declared runtime")
        for rc in r["runtime_constraints"]:
            print(f"  {rc['manifest']}: {rc['engines']}")

    if r["floating"]:
        section(f"Floating version ranges ({len(r['floating'])})")
        print("  Normal in npm when a committed lockfile exists. Without one, two")
        print("  installs of the same commit can produce different trees.")
        for d in r["floating"][:12]:
            print(f"  {d['name']:<34} {d['spec']}")
        if len(r["floating"]) > 12:
            print(f"  ... and {len(r['floating']) - 12} more")

    if r["errors"]:
        section("Parse errors")
        for e in r["errors"]:
            print(f"  {e}")

    section("Notes")
    for n in r["notes"]:
        print(f"  - {n}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Inventory dependencies, lockfiles and pinning.")
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    root = Path(args.path)
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    r = scan(root)
    print(json.dumps(r, indent=2)) if args.json else print_report(r)
    return 0


def _self_check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "package.json").write_text(json.dumps({
            "dependencies": {"next": "^15.0.0", "pinned-lib": "1.2.3",
                             "forked": "github:someone/forked#main"},
            "scripts": {"postinstall": "node ./setup.js"},
        }), encoding="utf-8")
        (root / "requirements.txt").write_text(
            "requests\nflask==3.0.0\nurllib3>=2.0\n", encoding="utf-8")
        wf = root / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(
            "jobs:\n  b:\n    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - uses: actions/setup-node@" + "a" * 40 + "\n",
            encoding="utf-8")

        r = scan(root)
        names = {d["name"]: d for d in r["all"]}

        assert names["next"]["pinning"] == "floating", names["next"]
        assert names["pinned-lib"]["pinning"] == "exact", names["pinned-lib"]
        assert any(d["name"] == "forked" for d in r["external_source"]), r["external_source"]
        assert names["requests"]["pinning"] == "unpinned", names["requests"]
        assert names["flask"]["pinning"] == "exact", names["flask"]
        assert names["urllib3"]["pinning"] == "floating", names["urllib3"]

        assert any(h["hook"] == "postinstall" for h in r["install_hooks"])
        assert any(m["manifest"] == "package.json" for m in r["missing_lockfiles"])

        # Only the tag-referenced action is unpinned; the SHA one is fine.
        assert len(r["ci_actions"]) == 2, r["ci_actions"]
        assert len(r["ci_unpinned"]) == 1, r["ci_unpinned"]
        assert r["ci_unpinned"][0]["action"] == "actions/checkout"

        # Missing engines must be reported, since it lets environments drift.
        assert any("engines" in n for n in r["notes"]), r["notes"]

    print("dependency_inventory self-check: OK")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        sys.exit(main())
