#!/usr/bin/env python3
"""Find configuration numbers in a codebase that have no documented origin.

Read-only. Standard library only.

This implements the mechanical half of the Numerical Evidence Gate. It cannot
decide whether a value is correct: it finds the candidates and reports whether
anything nearby explains where the number came from. Judging each hit is the
audit's job, and a hit is a question, not a verdict.

Usage:
    python magic_number_audit.py [path] [--json] [--all] [--min-severity high]
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
    ".idea", ".vscode", "coverage", ".pytest_cache", ".mypy_cache",
    "bower_components", ".terraform", "out", ".cache", ".svelte-kit",
    ".angular", "Pods", ".dart_tool", ".serverless", "site-packages",
    ".netlify", ".vercel", ".output", ".turbo", ".parcel-cache", "migrations",
}

CODE_EXT = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rs", ".rb",
    ".php", ".java", ".kt", ".cs", ".ex", ".exs", ".dart", ".scala", ".swift",
    ".yml", ".yaml", ".toml", ".ini", ".conf", ".env", ".tf", ".tfvars",
    ".json", ".properties", ".sh",
}

CONFIG_FILENAMES = {
    "dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml",
    "compose.yaml", "nginx.conf", "caddyfile", ".env.example", ".env.sample",
}

# Categories of configuration value, with the concrete risk of getting them wrong.
# Ordered most to least severe so the first match wins.
CATEGORIES = [
    ("connection_pool", r"(pool[_.]?(size|max|min)|max[_.]?connections|min[_.]?connections|"
                       r"connection[_.]?limit|maxpoolsize|max_overflow|pool_recycle)",
     "high", "Pool exhaustion under load, or the database refusing connections during a rolling deploy"),

    ("timeout", r"(timeout|deadline|ttl_?ms|request[_.]?time|read[_.]?timeout|write[_.]?timeout|"
                r"connect[_.]?timeout|idle[_.]?timeout|statement[_.]?timeout|lock[_.]?timeout|"
                r"query[_.]?timeout|visibility[_.]?timeout|socket[_.]?timeout)",
     "high", "Too low causes spurious failures, too high converts a slow dependency into an outage"),

    ("cache_ttl", r"(cache[_.]?ttl|ttl|max[_.]?age|maxage|expires?[_.]?in|expiry|expiration|"
                  r"s_maxage|stale[_.]?while[_.]?revalidate|revalidate)",
     "high", "Stale data served, or a cache that never helps"),

    ("retry", r"(retry|retries|max[_.]?attempts|attempts|backoff|jitter|redelivery)",
     "high", "Retry storms against a failing dependency, or giving up on a recoverable error"),

    ("rate_limit", r"(rate[_.]?limit|ratelimit|requests?[_.]?per|max[_.]?requests|throttle|"
                   r"burst|quota|points|duration_?window)",
     "high", "Legitimate traffic rejected, or no protection against abuse"),

    ("auth_lifetime", r"(session[_.]?(ttl|timeout|max|duration|lifetime)|token[_.]?(ttl|expiry|expires|lifetime)|"
                      r"jwt[_.]?expir|access[_.]?token[_.]?ttl|refresh[_.]?token[_.]?ttl|cookie[_.]?max)",
     "high", "Sessions that outlive their revocation window, or users logged out mid-task"),

    ("concurrency", r"(concurrency|max[_.]?workers?|worker[_.]?count|num[_.]?workers?|threads?|"
                    r"thread[_.]?pool|process(es)?[_.]?count|parallel|prefetch|max[_.]?parallel)",
     "high", "Downstream saturation, most often the database, or idle capacity"),

    ("memory_cpu", r"(memory[_.]?limit|mem[_.]?limit|cpu[_.]?limit|max[_.]?memory|max[_.]?old[_.]?space|"
                   r"heap|xmx|xms|memory:|cpus?:)",
     "high", "OOM kills under real load, or paying for headroom that is never used"),

    ("scaling", r"(min[_.]?instances?|max[_.]?instances?|min[_.]?replicas?|max[_.]?replicas?|"
                r"target[_.]?cpu|scale[_.]?(up|down)|desired[_.]?count|autoscal)",
     "high", "Scaling that reacts too late, or cost with no traffic"),

    ("payload_limit", r"(max[_.]?(body|payload|file|upload|request)[_.]?size|body[_.]?limit|"
                      r"client_max_body|upload[_.]?limit|max[_.]?size|maxfilesize)",
     "medium", "Legitimate uploads rejected, or memory exhaustion from oversized requests"),

    ("pagination", r"(page[_.]?size|per[_.]?page|limit[_.]?default|max[_.]?results|batch[_.]?size|"
                   r"chunk[_.]?size|max[_.]?items|take|top)",
     "medium", "Unbounded result sets, or excessive round trips"),

    ("health_check", r"(health[_.]?check|liveness|readiness|probe|heartbeat|ping[_.]?interval|"
                     r"initial[_.]?delay|period[_.]?seconds|failure[_.]?threshold)",
     "medium", "Restart loops during a dependency blip, or failures detected too late"),

    ("circuit_breaker", r"(circuit[_.]?breaker|error[_.]?threshold|failure[_.]?rate|half[_.]?open|"
                        r"reset[_.]?timeout|trip[_.]?threshold)",
     "medium", "Breaker that never trips, or one that trips on normal variance"),

    ("retention", r"(retention|keep[_.]?days|max[_.]?age[_.]?days|archive[_.]?after|purge[_.]?after|"
                  r"backup[_.]?interval|log[_.]?retention)",
     "medium", "Regulatory exposure, or unbounded storage growth"),

    ("queue", r"(queue[_.]?(size|depth|limit)|max[_.]?queue|buffer[_.]?size|max[_.]?messages|"
              r"dead[_.]?letter|max[_.]?receive)",
     "medium", "Silent message loss, or unbounded memory growth"),
]

# Values that carry their own meaning. Only filtered for lower-severity
# categories: 100 is a perfectly ordinary rate limit and must not be dropped.
BENIGN_VALUES = {"0", "1", "-1", "true", "false"}

# Identifiers that are the whole key and mean something other than a tuning
# decision. Compared against the key with separators stripped, NOT as a
# substring: "port" inside "EXPORT_TIMEOUT" and "id" inside "idleTimeout" are
# exactly the false negatives that let real timeouts through unnoticed.
BENIGN_EXACT = {
    "port", "status", "statuscode", "code", "httpcode", "version", "index",
    "id", "width", "height", "zindex", "opacity", "radius", "margin",
    "padding", "fontsize", "color", "rgb", "left", "top", "right", "bottom",
}

# Contexts where a number is scaffolding rather than production configuration.
# Word-boundary anchored, and also matched against camelCase word splits.
BENIGN_WORDS = re.compile(
    r"\b(test|tests|spec|mock|stub|fixture|example|sample|demo|seed|"
    r"placeholder|dummy|fake)\b",
    re.I,
)

# A number is considered explained if any of these appear near it.
EVIDENCE_HINTS = re.compile(
    r"(https?://|docs?\.|per\s+(the\s+)?docs|vendor\s+limit|rfc\s?\d|"
    r"measured|benchmark|load\s?test|p9[59]|SLO|SLA|rationale|"
    r"see\s+ADR|ADR-|requirement|limit\s+is|max(imum)?\s+allowed|"
    r"gemessen|laut\s+doku|quelle)",
    re.I,
)

# Phrases that cancel an apparent citation. A comment saying the value is
# undocumented, or that someone should look it up later, is the absence of
# evidence, and counting it as evidence inverts the whole point of the gate.
EVIDENCE_NEGATION = re.compile(
    r"(TODO|FIXME|XXX|HACK|no\s+documentation|not\s+documented|undocumented|"
    r"unclear|unknown|not\s+sure|guess|arbitrary|random|magic|why\?|"
    r"keine\s+doku|unklar|geraten|willkürlich)",
    re.I,
)


def split_words(key: str):
    """Split an identifier into words across camelCase, snake_case and kebab-case."""
    parts = re.split(r"[_.\-]+", key)
    words = []
    for p in parts:
        words.extend(re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+", p) or [p])
    return [w.lower() for w in words if w]

ASSIGN = re.compile(
    r"""(?P<key>[A-Za-z_][A-Za-z0-9_.\-]{2,60})       # identifier
        \s*(?::|=|=>|:=)\s*                           # assignment operator
        (?P<value>\d+(?:\.\d+)?)                      # a bare number
        (?P<unit>(?:\s*\*\s*\d+)+|[a-zA-Z]{1,4})?     # full multiplier chain, or a unit suffix
        \b""",
    re.X,
)

# Bare numeric arguments in calls that are almost always durations.
CALL_ARG = re.compile(
    r"\b(setTimeout|setInterval|sleep|delay|wait|time\.sleep|asyncio\.sleep)\s*\(\s*[^,)]*,?\s*(\d{3,})",
    re.I,
)

# An environment variable with a hard-coded literal fallback. This is where
# configuration values hide most often: the value looks externalised, but the
# number after the fallback operator is the one that actually runs whenever the
# variable is unset, which in practice is most environments.
ENV_FALLBACK = re.compile(
    r"""(?:process\.env\.(?P<k1>[A-Za-z0-9_]+)
        |process\.env\[['"](?P<k2>[A-Za-z0-9_]+)['"]\]
        |os\.environ\.get\(\s*['"](?P<k3>[A-Za-z0-9_]+)['"]
        |os\.getenv\(\s*['"](?P<k4>[A-Za-z0-9_]+)['"]
        |ENV\[['"](?P<k5>[A-Za-z0-9_]+)['"]\]
        )
        [^\n]{0,40}?
        (?:\|\||\?\?|,|\bor\b|\|\s*default\()\s*
        (?P<value>\d+(?:\.\d+)?)\b""",
    re.X,
)


def walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() in CODE_EXT or p.name.lower() in CONFIG_FILENAMES:
                yield p


def categorize(key: str, line: str = ""):
    """Classify a configuration key, using the surrounding line for context.

    The line matters for short generic keys: `max` means nothing alone, but
    `max` inside a pool literal is a connection pool size.
    """
    k = key.lower()
    for name, pattern, severity, risk in CATEGORIES:
        if re.search(pattern, k):
            return name, severity, risk

    if line and k in {"max", "min", "size", "limit", "count"}:
        # Substring rather than word-boundary matching: pool variables are
        # routinely named dbPool, pgPool or connectionPool, and requiring a
        # boundary before "pool" misses all of them.
        ctx = line.lower()
        if re.search(r"pool|connection", ctx):
            return ("connection_pool", "high",
                    "Pool exhaustion under load, or the database refusing connections "
                    "during a rolling deploy")
        if re.search(r"queue|worker", ctx):
            return ("queue", "medium",
                    "Silent message loss, or unbounded memory growth")
        if re.search(r"rate|throttl", ctx):
            return ("rate_limit", "high",
                    "Legitimate traffic rejected, or no protection against abuse")
    return None, None, None


def is_benign_key(key: str) -> bool:
    """Filter noise without swallowing real configuration.

    Matches only whole words, never substrings, so EXPORT_TIMEOUT and
    idleTimeoutMillis survive.
    """
    flat = re.sub(r"[_.\-]", "", key).lower()
    if flat in BENIGN_EXACT:
        return True
    words = split_words(key)
    if words and BENIGN_WORDS.search(" ".join(words)):
        return True
    # A key that is only a benign word plus a qualifier, such as "defaultPort".
    return bool(words) and all(w in BENIGN_EXACT or w in {"default", "the", "a"} for w in words)


def has_evidence(lines, idx: int) -> str:
    """Look for a source or rationale on the line itself and the two above it.

    A nearby TODO or an admission that the value is undocumented cancels the
    match: that text is evidence of absence, not a citation.
    """
    window = lines[max(0, idx - 2): idx + 1]
    for w in window:
        if EVIDENCE_NEGATION.search(w):
            return ""
    for w in window:
        if EVIDENCE_HINTS.search(w):
            return w.strip()[:160]
    return ""


def is_env_backed(line: str) -> bool:
    """A value read from the environment with a literal fallback is still a
    decision, but a value read from the environment with no fallback is not a
    magic number: it has been externalised."""
    return bool(re.search(r"(process\.env|os\.environ|os\.getenv|System\.getenv|ENV\[)", line)) \
        and not re.search(r"(\|\||\?\?|,\s*\d|or\s+\d|default\s*[=:]\s*\d)", line)


def scan_file(path: Path, root: Path, include_all: bool):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if "\x00" in text[:1024]:
        return []

    lines = text.splitlines()
    rel = str(path.relative_to(root)).replace("\\", "/")
    hits = []

    for i, line in enumerate(lines):
        if len(line) > 500:
            continue
        stripped = line.strip()
        if stripped.startswith(("//", "#", "*", "<!--")) and not include_all:
            continue

        for m in ASSIGN.finditer(line):
            key, value = m.group("key"), m.group("value")
            if is_benign_key(key) and not include_all:
                continue
            cat, severity, risk = categorize(key, line)
            if cat is None:
                continue
            # Only drop trivially self-explaining values, and never for the
            # high-severity categories where 0 or 1 is itself a real decision.
            if value in BENIGN_VALUES and severity != "high" and not include_all:
                continue
            if is_env_backed(line):
                continue
            # Normalise whitespace inside a multiplier chain so the reported value
            # reads as written. A truncated or mangled value defeats the purpose.
            unit = re.sub(r"\s*\*\s*", " * ", (m.group("unit") or "").strip())
            hits.append({
                "file": rel,
                "line": i + 1,
                "key": key,
                "value": (value + (" " + unit if unit.startswith("*") else unit)).strip(),
                "category": cat,
                "severity": severity,
                "risk": risk,
                "evidence": has_evidence(lines, i),
                "source_line": stripped[:200],
            })

        for m in CALL_ARG.finditer(line):
            hits.append({
                "file": rel,
                "line": i + 1,
                "key": m.group(1),
                "value": m.group(2),
                "category": "timeout",
                "severity": "medium",
                "risk": "Hard-coded duration in a call, with no name and no stated basis",
                "evidence": has_evidence(lines, i),
                "source_line": stripped[:200],
            })

        for m in ENV_FALLBACK.finditer(line):
            key = next(g for g in (m.group("k1"), m.group("k2"), m.group("k3"),
                                   m.group("k4"), m.group("k5")) if g)
            value = m.group("value")
            cat, severity, risk = categorize(key, line)
            if value in BENIGN_VALUES and severity != "high" and not include_all:
                continue
            if cat is None:
                cat, severity = "env_fallback", "medium"
                risk = ("Literal fallback for an environment variable. This value runs "
                        "wherever the variable is unset, which is easy to overlook")
            hits.append({
                "file": rel,
                "line": i + 1,
                "key": key,
                "value": value,
                "category": cat,
                "severity": severity,
                "risk": risk,
                "evidence": has_evidence(lines, i),
                "source_line": stripped[:200],
            })

    return hits


def scan(root: Path, include_all: bool = False):
    root = root.resolve()
    all_hits = []
    for f in walk(root):
        all_hits.extend(scan_file(f, root, include_all))

    # Same key and value repeated across files is one decision, not many.
    seen = {}
    for h in all_hits:
        sig = (h["key"].lower(), h["value"], h["category"])
        seen.setdefault(sig, []).append(h)

    grouped = []
    for sig, group in seen.items():
        first = dict(group[0])
        first["occurrences"] = len(group)
        first["other_locations"] = [f"{g['file']}:{g['line']}" for g in group[1:6]]
        grouped.append(first)

    order = {"high": 0, "medium": 1, "low": 2}
    grouped.sort(key=lambda h: (order.get(h["severity"], 3), not h["evidence"] and -1 or 0, h["category"]))
    return grouped


def print_report(hits, root: Path, min_sev: str) -> None:
    order = {"high": 0, "medium": 1, "low": 2}
    threshold = order.get(min_sev, 2)
    shown = [h for h in hits if order.get(h["severity"], 3) <= threshold]

    undocumented = [h for h in shown if not h["evidence"]]
    documented = [h for h in shown if h["evidence"]]

    print(f"MAGIC NUMBER AUDIT: {root}")
    print(f"\nCandidates found: {len(hits)}   shown at severity <= {min_sev}: {len(shown)}")
    print(f"  without any nearby source or rationale: {len(undocumented)}")
    print(f"  with something that looks like a source: {len(documented)}")

    if not shown:
        print("\nNo configuration numbers matched. Verify manually that configuration")
        print("is not held somewhere this script does not read, such as a dashboard,")
        print("a platform UI, or a secret store.")
        return

    print("\n" + "=" * 72)
    print("UNSOURCED  (no documentation or measurement found near the value)")
    print("=" * 72)
    for h in undocumented:
        loc = f"{h['file']}:{h['line']}"
        print(f"\n[{h['severity'].upper():<6}] {h['category']:<16} {h['key']} = {h['value']}")
        print(f"  {loc}")
        print(f"  > {h['source_line']}")
        print(f"  Risk if wrong: {h['risk']}")
        if h["occurrences"] > 1:
            print(f"  Also at: {', '.join(h['other_locations'])}"
                  + (f" (+{h['occurrences'] - 1 - len(h['other_locations'])} more)"
                     if h["occurrences"] - 1 > len(h["other_locations"]) else ""))

    if documented:
        print("\n" + "=" * 72)
        print("POSSIBLY SOURCED  (verify the citation actually supports the value)")
        print("=" * 72)
        for h in documented:
            print(f"\n[{h['severity'].upper():<6}] {h['category']:<16} {h['key']} = {h['value']}"
                  f"   {h['file']}:{h['line']}")
            print(f"  claimed basis: {h['evidence']}")

    print("\n" + "-" * 72)
    print("For each entry above, the audit must answer four questions:")
    print("  1. Where did this value come from?")
    print("  2. Is there documentation for it?")
    print("  3. Was it ever measured?")
    print("  4. Is it still valid for the current deployment?")
    print("'Unknown' is a legitimate answer and is itself the finding.")
    print("Carry this into the 'Unsupported Numbers Found' section of the report.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Find configuration numbers with no documented origin.")
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="include comments and benign values")
    ap.add_argument("--min-severity", default="medium", choices=["high", "medium", "low"])
    args = ap.parse_args()

    root = Path(args.path)
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    hits = scan(root, include_all=args.all)
    if args.json:
        print(json.dumps({"root": str(root.resolve()), "findings": hits}, indent=2))
    else:
        print_report(hits, root.resolve(), args.min_severity)
    return 0


def _self_check() -> None:
    import tempfile

    sample = """
const config = {
  poolMax: 20,
  requestTimeout: 30000,
  // p99 of the payment API is 800ms, measured 2026-01-05
  paymentTimeout: 2500,
  maxRetries: 5,
  port: 3000,
  statusCode: 404,
};
const EXPORT_TIMEOUT = 120000;
const dbPool = { max: 20, idleTimeoutMillis: 30000 };
const RATE_LIMIT_MAX = 100;
// TODO: no documentation for this value, look it up
const guessedTtl = 900;
const testTimeout = 45000;
const dbUrl = process.env.DATABASE_URL;
const ttl = process.env.CACHE_TTL || 300;
setTimeout(doThing, 5000);
"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "config.ts").write_text(sample, encoding="utf-8")
        hits = scan(root)
        by_key = {h["key"]: h for h in hits}

        # Real configuration decisions must be caught.
        assert "poolMax" in by_key, by_key.keys()
        assert by_key["poolMax"]["category"] == "connection_pool"
        assert by_key["poolMax"]["severity"] == "high"
        assert "requestTimeout" in by_key
        assert "maxRetries" in by_key

        # A nearby measurement note counts as evidence.
        assert by_key["paymentTimeout"]["evidence"], "measurement comment not detected"
        assert not by_key["requestTimeout"]["evidence"], "false evidence attributed"

        # Noise must stay out: ports and HTTP status codes are not decisions.
        assert "port" not in by_key, "port should be filtered as benign context"
        assert "statusCode" not in by_key, "status code should be filtered"

        # Regression: substring filtering used to swallow these. "port" appears
        # inside EXPORT_TIMEOUT and "id" inside idleTimeoutMillis. Both are real
        # timeouts and missing them defeats the purpose of the tool.
        assert "EXPORT_TIMEOUT" in by_key, "substring filter swallowed a real timeout"
        assert "idleTimeoutMillis" in by_key, "substring filter swallowed a real timeout"

        # Regression: 100 was treated as benign, which dropped every rate limit
        # set to the single most common rate limit value.
        assert "RATE_LIMIT_MAX" in by_key, "common value 100 was filtered as benign"

        # A generic key gains meaning from its line: `max` inside a pool literal.
        assert by_key["max"]["category"] == "connection_pool", by_key.get("max")

        # A TODO admitting the value is undocumented is not a citation.
        assert not by_key["guessedTtl"]["evidence"], "TODO counted as evidence"

        # Test scaffolding stays out.
        assert "testTimeout" not in by_key, "test scaffolding should be filtered"

        # An env var with a literal fallback is still a decision and must be caught,
        # under the environment variable's own name.
        assert "CACHE_TTL" in by_key, f"env fallback literal missed: {sorted(by_key)}"
        assert by_key["CACHE_TTL"]["value"] == "300"

        # A plain env read with no literal fallback is externalised, not a magic number.
        assert "DATABASE_URL" not in by_key

        # Bare durations in timer calls must be caught.
        assert any(h["key"] == "setTimeout" for h in hits), "setTimeout literal missed"

    print("magic_number_audit self-check: OK")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        sys.exit(main())
