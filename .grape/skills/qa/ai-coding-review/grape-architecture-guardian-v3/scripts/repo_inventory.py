#!/usr/bin/env python3
"""Detect the actual technology stack of a repository.

Read-only. Standard library only. Prints a structured inventory so the audit
starts from what is in the repository rather than from what the user remembers.

Usage:
    python repo_inventory.py [path] [--json]

The point of this script is Discovery phase A: never ask the user a stack
question that the repository already answers.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Directories that are never part of the audited source.
SKIP_DIRS = {
    ".git", "node_modules", "vendor", "dist", "build", ".next", ".nuxt",
    "__pycache__", ".venv", "venv", "env", ".tox", "target", ".gradle",
    ".idea", ".vscode", "coverage", ".pytest_cache", ".mypy_cache",
    "bower_components", ".terraform", "out", ".cache", ".svelte-kit",
    ".angular", "Pods", ".dart_tool", ".serverless", "site-packages",
    ".netlify", ".vercel", ".output", ".turbo", ".parcel-cache",
}

MANIFESTS = {
    "package.json": "Node.js / JavaScript",
    "requirements.txt": "Python",
    "pyproject.toml": "Python",
    "Pipfile": "Python",
    "setup.py": "Python",
    "go.mod": "Go",
    "Cargo.toml": "Rust",
    "composer.json": "PHP",
    "Gemfile": "Ruby",
    "pom.xml": "Java (Maven)",
    "build.gradle": "Java/Kotlin (Gradle)",
    "build.gradle.kts": "Kotlin (Gradle)",
    "pubspec.yaml": "Dart/Flutter",
    "mix.exs": "Elixir",
    "*.csproj": ".NET",
    "*.sln": ".NET",
}

LOCKFILES = [
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "poetry.lock", "Pipfile.lock", "uv.lock", "requirements.lock",
    "go.sum", "Cargo.lock", "composer.lock", "Gemfile.lock",
    "pubspec.lock", "mix.lock", "packages.lock.json",
]

# Signals keyed by category. Matched against dependency names and file paths.
DEP_SIGNALS = {
    "framework": {
        "next": "Next.js", "react": "React", "vue": "Vue", "svelte": "Svelte",
        "@angular/core": "Angular", "astro": "Astro", "nuxt": "Nuxt",
        "express": "Express", "fastify": "Fastify", "@nestjs/core": "NestJS",
        "koa": "Koa", "hapi": "hapi", "hono": "Hono",
        "django": "Django", "flask": "Flask", "fastapi": "FastAPI",
        "starlette": "Starlette", "tornado": "Tornado", "pyramid": "Pyramid",
        "rails": "Ruby on Rails", "sinatra": "Sinatra",
        "laravel/framework": "Laravel", "symfony/framework-bundle": "Symfony",
        "gin-gonic/gin": "Gin", "labstack/echo": "Echo", "fiber": "Fiber",
        "spring-boot": "Spring Boot", "quarkus": "Quarkus",
        "actix-web": "Actix Web", "axum": "Axum", "rocket": "Rocket",
        "phoenix": "Phoenix", "remix": "Remix", "solid-js": "SolidJS",
    },
    "database": {
        "pg": "PostgreSQL", "postgres": "PostgreSQL", "psycopg": "PostgreSQL",
        "psycopg2": "PostgreSQL", "asyncpg": "PostgreSQL", "lib/pq": "PostgreSQL",
        "mysql": "MySQL", "mysql2": "MySQL", "pymysql": "MySQL",
        "mariadb": "MariaDB",
        "sqlite3": "SQLite", "better-sqlite3": "SQLite",
        "mongodb": "MongoDB", "mongoose": "MongoDB", "pymongo": "MongoDB",
        "@supabase/supabase-js": "Supabase", "supabase": "Supabase",
        "firebase": "Firebase", "firebase-admin": "Firebase",
        "@planetscale/database": "PlanetScale", "@neondatabase/serverless": "Neon",
        "cassandra-driver": "Cassandra", "elasticsearch": "Elasticsearch",
        "@elastic/elasticsearch": "Elasticsearch", "clickhouse": "ClickHouse",
        "duckdb": "DuckDB", "libsql": "libSQL/Turso",
    },
    "orm": {
        "prisma": "Prisma", "@prisma/client": "Prisma",
        "typeorm": "TypeORM", "sequelize": "Sequelize", "knex": "Knex",
        "drizzle-orm": "Drizzle", "mikro-orm": "MikroORM", "objection": "Objection",
        "sqlalchemy": "SQLAlchemy", "alembic": "Alembic (migrations)",
        "django": "Django ORM", "peewee": "Peewee", "tortoise-orm": "Tortoise",
        "activerecord": "ActiveRecord", "gorm.io/gorm": "GORM",
        "diesel": "Diesel", "sqlx": "SQLx", "doctrine/orm": "Doctrine",
        "ecto": "Ecto", "hibernate": "Hibernate",
    },
    "cache": {
        "redis": "Redis", "ioredis": "Redis", "node-redis": "Redis",
        "@upstash/redis": "Upstash Redis", "memcached": "Memcached",
        "pymemcache": "Memcached", "django-redis": "Redis (Django)",
        "lru-cache": "in-process LRU", "cachetools": "in-process cache",
        "node-cache": "in-process cache",
    },
    "queue": {
        "bullmq": "BullMQ", "bull": "Bull", "agenda": "Agenda",
        "celery": "Celery", "rq": "RQ", "dramatiq": "Dramatiq",
        "kafkajs": "Kafka", "kafka-python": "Kafka", "confluent-kafka": "Kafka",
        "amqplib": "RabbitMQ/AMQP", "pika": "RabbitMQ", "kombu": "AMQP",
        "@aws-sdk/client-sqs": "AWS SQS", "boto3": "AWS SDK (check SQS/SNS)",
        "sidekiq": "Sidekiq", "resque": "Resque", "temporal": "Temporal",
        "inngest": "Inngest", "trigger.dev": "Trigger.dev", "graphile-worker": "Graphile Worker",
        "oban": "Oban", "asynq": "Asynq", "machinery": "Machinery",
    },
    "auth": {
        "passport": "Passport", "next-auth": "NextAuth", "@auth/core": "Auth.js",
        "jsonwebtoken": "JWT", "jose": "JOSE (JWT)", "pyjwt": "JWT",
        "python-jose": "JWT", "bcrypt": "bcrypt", "bcryptjs": "bcrypt",
        "argon2": "Argon2", "argon2-cffi": "Argon2", "scrypt": "scrypt",
        "@clerk/nextjs": "Clerk", "@auth0/auth0-react": "Auth0",
        "auth0": "Auth0", "@supabase/auth-helpers-nextjs": "Supabase Auth",
        "lucia": "Lucia", "authlib": "Authlib", "django-allauth": "django-allauth",
        "devise": "Devise", "better-auth": "Better Auth",
        "@simplewebauthn/server": "WebAuthn/Passkeys",
    },
    "storage": {
        "@aws-sdk/client-s3": "AWS S3", "aws-sdk": "AWS SDK",
        "minio": "MinIO", "@google-cloud/storage": "GCS",
        "@azure/storage-blob": "Azure Blob", "cloudinary": "Cloudinary",
        "uploadthing": "UploadThing", "multer": "multer (local uploads)",
        "boto3": "AWS SDK (check S3)",
    },
    "observability": {
        "@sentry/node": "Sentry", "@sentry/nextjs": "Sentry", "sentry-sdk": "Sentry",
        "winston": "Winston", "pino": "Pino", "bunyan": "Bunyan",
        "structlog": "structlog", "loguru": "loguru",
        "@opentelemetry/api": "OpenTelemetry", "opentelemetry-api": "OpenTelemetry",
        "prom-client": "Prometheus", "prometheus-client": "Prometheus",
        "datadog": "Datadog", "dd-trace": "Datadog", "newrelic": "New Relic",
        "elastic-apm-node": "Elastic APM",
    },
    "validation": {
        "zod": "Zod", "yup": "Yup", "joi": "Joi", "ajv": "Ajv",
        "class-validator": "class-validator", "valibot": "Valibot",
        "pydantic": "Pydantic", "marshmallow": "Marshmallow",
        "cerberus": "Cerberus", "voluptuous": "Voluptuous",
    },
    "testing": {
        "jest": "Jest", "vitest": "Vitest", "mocha": "Mocha", "ava": "AVA",
        "@playwright/test": "Playwright", "playwright": "Playwright",
        "cypress": "Cypress", "puppeteer": "Puppeteer",
        "pytest": "pytest", "unittest2": "unittest", "nose": "nose",
        "rspec": "RSpec", "minitest": "Minitest", "testify": "Testify",
        "supertest": "supertest", "k6": "k6 (load testing)",
        "locust": "Locust (load testing)", "artillery": "Artillery (load testing)",
    },
}

INFRA_FILES = {
    "Dockerfile": "Docker",
    "docker-compose.yml": "Docker Compose",
    "docker-compose.yaml": "Docker Compose",
    "compose.yml": "Docker Compose",
    "compose.yaml": "Docker Compose",
    "kubernetes": "Kubernetes",
    "k8s": "Kubernetes",
    "Chart.yaml": "Helm",
    "main.tf": "Terraform",
    "terraform.tf": "Terraform",
    "Pulumi.yaml": "Pulumi",
    "template.yaml": "AWS SAM / CloudFormation",
    "serverless.yml": "Serverless Framework",
    "vercel.json": "Vercel",
    "netlify.toml": "Netlify",
    "fly.toml": "Fly.io",
    "railway.json": "Railway",
    "render.yaml": "Render",
    "app.yaml": "Google App Engine",
    "Procfile": "Heroku-style process model",
    "wrangler.toml": "Cloudflare Workers",
    "captain-definition": "CapRover",
    "ecosystem.config.js": "PM2",
    "nginx.conf": "nginx",
    "Caddyfile": "Caddy",
}

CI_PATHS = {
    ".github/workflows": "GitHub Actions",
    ".gitlab-ci.yml": "GitLab CI",
    ".circleci/config.yml": "CircleCI",
    "Jenkinsfile": "Jenkins",
    ".drone.yml": "Drone",
    "azure-pipelines.yml": "Azure Pipelines",
    "bitbucket-pipelines.yml": "Bitbucket Pipelines",
    ".woodpecker.yml": "Woodpecker",
}

MIGRATION_HINTS = ["migrations", "migrate", "alembic", "prisma/migrations", "db/migrate"]


def walk(root: Path):
    """Yield files, skipping vendor and build directories."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git")]
        for fn in filenames:
            yield Path(dirpath) / fn


def read_text(path: Path, limit: int = 400_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except (OSError, UnicodeDecodeError):
        return ""


def collect_dependencies(root: Path) -> dict:
    """Extract declared dependency names from whichever manifests exist."""
    deps = {}

    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(read_text(pkg) or "{}")
            for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                for name, ver in (data.get(section) or {}).items():
                    deps[name] = f"{ver} ({section})"
        except json.JSONDecodeError:
            pass

    for req in list(root.glob("requirements*.txt")) + list(root.glob("*/requirements*.txt")):
        for line in read_text(req).splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "-")):
                continue
            m = re.match(r"^([A-Za-z0-9._\-\[\]]+)\s*([<>=!~^].*)?$", line)
            if m:
                deps[m.group(1).lower().split("[")[0]] = (m.group(2) or "unpinned").strip()

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = read_text(pyproject)
        for m in re.finditer(r'^\s*"?([A-Za-z0-9._\-]+)"?\s*[>=<~^]{1,2}\s*"?([0-9][^",\s]*)', text, re.M):
            deps.setdefault(m.group(1).lower(), m.group(2))
        for m in re.finditer(r'^\s*([A-Za-z0-9._\-]+)\s*=\s*"([^"]+)"', text, re.M):
            deps.setdefault(m.group(1).lower(), m.group(2))

    for name, pattern in (("go.mod", r"^\s+([a-zA-Z0-9./\-]+)\s+v([0-9][^\s]*)"),
                          ("Cargo.toml", r'^\s*([A-Za-z0-9._\-]+)\s*=\s*[",{]'),
                          ("composer.json", None),
                          ("Gemfile", r"^\s*gem\s+['\"]([^'\"]+)['\"]")):
        f = root / name
        if not f.is_file():
            continue
        if name == "composer.json":
            try:
                data = json.loads(read_text(f) or "{}")
                for section in ("require", "require-dev"):
                    for k, v in (data.get(section) or {}).items():
                        deps[k] = str(v)
            except json.JSONDecodeError:
                pass
        else:
            for m in re.finditer(pattern, read_text(f), re.M):
                deps[m.group(1)] = m.group(2) if m.lastindex and m.lastindex > 1 else "see manifest"

    return deps


def match_signals(deps: dict) -> dict:
    """Map declared dependencies onto stack categories.

    Exact name matches win over scoped-package matches, so that a dependency
    like "@ai-sdk/react" does not get reported as the evidence for React when
    "react" itself is declared. Attributing a finding to the wrong package
    sends the reader to the wrong file.
    """
    found = {cat: {} for cat in DEP_SIGNALS}
    lowered = {d.lower(): d for d in deps}
    for cat, table in DEP_SIGNALS.items():
        for needle, label in table.items():
            exact = lowered.get(needle)
            if exact:
                found[cat][label] = f"{exact} {deps[exact]}"
                continue
            for dep_lower, dep_orig in lowered.items():
                if dep_lower.endswith("/" + needle) or needle in dep_lower.split("/"):
                    found[cat][label] = f"{dep_orig} {deps[dep_orig]}"
                    break
    return found


def scan_repo(root: Path) -> dict:
    root = root.resolve()
    result = {
        "root": str(root),
        "languages": {},
        "manifests": [],
        "lockfiles": [],
        "missing_lockfiles": [],
        "infrastructure": {},
        "ci": {},
        "env_examples": [],
        "migrations": [],
        "iac_files": [],
        "test_dirs": [],
        "dependency_count": 0,
        "stack": {},
        "notes": [],
    }

    ext_counts = {}
    ext_lang = {
        ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
        ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
        ".py": "Python", ".go": "Go", ".rs": "Rust", ".rb": "Ruby",
        ".php": "PHP", ".java": "Java", ".kt": "Kotlin", ".cs": "C#",
        ".ex": "Elixir", ".exs": "Elixir", ".dart": "Dart", ".swift": "Swift",
        ".scala": "Scala", ".vue": "Vue SFC", ".svelte": "Svelte",
        ".sql": "SQL", ".sh": "Shell",
    }

    all_files = list(walk(root))

    for f in all_files:
        ext = f.suffix.lower()
        if ext in ext_lang:
            ext_counts[ext_lang[ext]] = ext_counts.get(ext_lang[ext], 0) + 1

        rel = str(f.relative_to(root)).replace("\\", "/")
        name = f.name

        if name in MANIFESTS:
            result["manifests"].append({"file": rel, "ecosystem": MANIFESTS[name]})
        elif ext in (".csproj", ".sln"):
            result["manifests"].append({"file": rel, "ecosystem": ".NET"})

        if name in LOCKFILES:
            result["lockfiles"].append(rel)

        if name in INFRA_FILES:
            result["infrastructure"].setdefault(INFRA_FILES[name], []).append(rel)

        if name.lower() in (".env.example", ".env.sample", ".env.template", "env.example"):
            result["env_examples"].append(rel)

        if ext in (".tf", ".tfvars") or name in ("Pulumi.yaml", "Chart.yaml"):
            result["iac_files"].append(rel)

        if any(h in rel.lower() for h in MIGRATION_HINTS) and ext in (".sql", ".py", ".js", ".ts", ".rb", ".go"):
            result["migrations"].append(rel)

    result["languages"] = dict(sorted(ext_counts.items(), key=lambda kv: -kv[1]))

    for ci_path, label in CI_PATHS.items():
        p = root / ci_path
        if p.exists():
            if p.is_dir():
                files = [str(x.relative_to(root)).replace("\\", "/") for x in p.glob("*.y*ml")]
                if files:
                    result["ci"][label] = files
            else:
                result["ci"][label] = [ci_path]

    for d in ("test", "tests", "__tests__", "spec", "e2e", "cypress"):
        p = root / d
        if p.is_dir():
            result["test_dirs"].append(d)

    deps = collect_dependencies(root)
    result["dependency_count"] = len(deps)
    result["stack"] = {k: v for k, v in match_signals(deps).items() if v}

    # A manifest without its lockfile means builds are not reproducible.
    lock_for = {
        "package.json": ["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb"],
        "pyproject.toml": ["poetry.lock", "uv.lock", "requirements.lock", "Pipfile.lock"],
        "Gemfile": ["Gemfile.lock"],
        "composer.json": ["composer.lock"],
        "Cargo.toml": ["Cargo.lock"],
        "go.mod": ["go.sum"],
    }
    present_locks = {Path(p).name for p in result["lockfiles"]}
    for manifest, accepted in lock_for.items():
        if (root / manifest).is_file() and not (present_locks & set(accepted)):
            result["missing_lockfiles"].append(
                f"{manifest} present but none of {accepted} found"
            )

    if result["migrations"]:
        result["migrations"] = sorted(result["migrations"])[:40]

    # Observations, not conclusions. The audit decides what these mean.
    if not result["ci"]:
        result["notes"].append("No CI configuration detected. Verify how deployments happen.")
    if not result["test_dirs"] and "testing" not in result["stack"]:
        result["notes"].append("No test directory or test framework detected.")
    if not result["infrastructure"] and not result["iac_files"]:
        result["notes"].append(
            "No container, IaC or platform config detected. Infrastructure may be configured by hand, "
            "which means it cannot be verified from this repository."
        )
    if "observability" not in result["stack"]:
        result["notes"].append(
            "No logging, metrics or error-tracking library detected. Performance findings can only be "
            "labelled 'plausible' without instrumentation."
        )
    if result["missing_lockfiles"]:
        result["notes"].append("Manifest without lockfile: builds are not reproducible.")

    return result


def print_report(r: dict) -> None:
    def section(title):
        print(f"\n{title}\n{'-' * len(title)}")

    print(f"REPOSITORY INVENTORY: {r['root']}")

    section("Languages (by file count)")
    if r["languages"]:
        for lang, n in list(r["languages"].items())[:12]:
            print(f"  {lang:<16} {n}")
    else:
        print("  none detected")

    section("Manifests")
    for m in r["manifests"] or []:
        print(f"  {m['file']:<40} {m['ecosystem']}")
    if not r["manifests"]:
        print("  none detected")
    print(f"  declared dependencies: {r['dependency_count']}")

    section("Lockfiles")
    for l in r["lockfiles"] or ["  none detected"]:
        print(f"  {l}" if r["lockfiles"] else l)
    for w in r["missing_lockfiles"]:
        print(f"  WARNING: {w}")

    section("Detected stack")
    if r["stack"]:
        for cat, items in r["stack"].items():
            print(f"  {cat}:")
            for label, src in items.items():
                print(f"    - {label}  [{src}]")
    else:
        print("  no framework or infrastructure libraries matched")

    section("Infrastructure")
    for label, files in (r["infrastructure"] or {}).items():
        print(f"  {label}: {', '.join(files[:5])}")
    if r["iac_files"]:
        print(f"  IaC files: {len(r['iac_files'])} (first: {r['iac_files'][:3]})")
    if not r["infrastructure"] and not r["iac_files"]:
        print("  none detected")

    section("CI/CD")
    for label, files in (r["ci"] or {}).items():
        print(f"  {label}: {', '.join(files[:5])}")
    if not r["ci"]:
        print("  none detected")

    section("Other")
    print(f"  env examples: {r['env_examples'] or 'none'}")
    print(f"  test dirs:    {r['test_dirs'] or 'none'}")
    print(f"  migrations:   {len(r['migrations'])} file(s)")

    if r["notes"]:
        section("Observations (verify, do not assume)")
        for n in r["notes"]:
            print(f"  - {n}")

    print("\nNext: read the files this points at. Do not ask the user what the stack is.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Detect the technology stack of a repository.")
    ap.add_argument("path", nargs="?", default=".", help="repository root (default: current directory)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    args = ap.parse_args()

    root = Path(args.path)
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    result = scan_repo(root)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(result)
    return 0


def _self_check() -> None:
    """Minimal check: build a fake repo in a temp dir and assert detection."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "package.json").write_text(json.dumps({
            "dependencies": {"next": "15.0.0", "pg": "^8.11.0", "ioredis": "^5.3.0"},
            "devDependencies": {"vitest": "^2.0.0"},
        }), encoding="utf-8")
        (root / "Dockerfile").write_text("FROM node:22\n", encoding="utf-8")
        (root / "src").mkdir()
        (root / "src" / "index.ts").write_text("export {}\n", encoding="utf-8")

        r = scan_repo(root)
        assert "Next.js" in r["stack"]["framework"], r["stack"]
        assert "PostgreSQL" in r["stack"]["database"], r["stack"]
        assert "Redis" in r["stack"]["cache"], r["stack"]
        assert "Vitest" in r["stack"]["testing"], r["stack"]
        assert "Docker" in r["infrastructure"], r["infrastructure"]
        assert "TypeScript" in r["languages"], r["languages"]
        assert r["dependency_count"] == 4, r["dependency_count"]
        # package.json without a lockfile must be reported.
        assert any("package.json" in w for w in r["missing_lockfiles"]), r["missing_lockfiles"]
    print("repo_inventory self-check: OK")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        sys.exit(main())
