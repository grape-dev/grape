# Human Installation Guide

This file covers the **manual step-by-step install**: concrete steps for each environment (Windows / macOS / Linux / Docker), mirroring what the install script does.

**Install and start with one command:**

**GitHub**

```bash
git clone https://github.com/grape-dev/grape.git && python grape/quickstart.py
```

**GitCode**

```bash
git clone https://gitcode.com/grape-dev/grape.git && python grape/quickstart.py
```

> The manual steps below use the GitHub URL; for the GitCode mirror just swap `https://github.com/grape-dev/grape.git` for `https://gitcode.com/grape-dev/grape.git`.

## Prerequisites

| Component | Requirement | Notes |
| --- | --- | --- |
| **Python** | >= 3.10 (required) | Main service and devbox sandbox both require Python 3.10+ (new-style type annotations/syntax); startup exits with an explicit message below 3.10 |
| **Node.js** | >= 18 (for Agent CLI) | `npm install -g opencode` |
| **Git** | any recent version | |

## Windows (PowerShell)

```powershell
git clone https://github.com/grape-dev/grape.git
cd grape

# One-click setup: venv + deps + .env + preflight check + start
.\start.ps1                    # Local dev (minimal deps, SQLite)
.\start.ps1 -Full              # Full deps (OBS/MySQL/Feishu)
.\start.ps1 -SkipDoctor        # Skip preflight, just start

# or manually:
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
New-Item -ItemType Directory -Force -Path data
python -m src.api.run --dev    # Dev mode, opens http://localhost:3000
```

**Windows notes**

- If execution policy blocks the script, run first: `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

**`git clone` fails with `not found`, URL shows a proxy domain like `gh-proxy`**

GitHub accelerators install a git `insteadOf` rule that rewrites `https://github.com/` to a (possibly broken) proxy — a wrong host such as `github.ab.com` makes every clone fail. Inspect the rules and where they come from:

```powershell
git config --list --show-origin | Select-String insteadOf
```

Remove the broken rule and re-run the command above:

```powershell
git config --global --unset-all "url.https://gh-proxy.com/https://github.ab.com/.insteadOf"
```

If the rule lives in `--system`, use `--system` instead (elevated PowerShell); if it lives in a repo's `.git/config`, run `git config --unset-all` inside that repo without a scope.

## macOS / Linux

**Or manually** (same steps as the script):

```bash
git clone https://github.com/grape-dev/grape.git && cd grape

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt                      # full set (OBS/MySQL/Feishu included)

cp .env.example .env && mkdir -p data
python -m src.api.run --dev                          # http://localhost:3000
```

**macOS / Linux notes**

Behind a restricted network, use a mirror:

```bash
# PyPI Aliyun mirror
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
# or permanently:
pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/
```

Occasional HTTP/2 frame errors or timeouts during `git fetch` are network jitter — the fetch path retries up to 3 times with backoff; if it still fails, fall back to HTTP/1.1 or route through a mirror:

```bash
git config --global http.version HTTP/1.1
git config --global url."https://<your-mirror>/https://github.com/".insteadOf "https://github.com/"
```

If the machine has no Python >= 3.10, the script automatically re-runs itself with a newer interpreter found on `PATH` (`python3.11` / `python3.12`, …); when none exists it prints the uv commands to install one — no root, no changes to the system Python.

## Docker (optional, arch layout)

`python quickstart.py --docker` lets the script generate `JWT_SECRET` and start the containers (it locates `deploy/docker-compose.yml` automatically).

```bash
export JWT_SECRET=$(openssl rand -hex 32)
# deploy/docker-compose.yml declares ${JWT_SECRET:?JWT_SECRET required}; the containers exit immediately without it
docker compose -f deploy/docker-compose.yml up -d
```

**Docker notes**

- Orchestration and image live under `deploy/`: `deploy/docker-compose.yml` (grape-server / event-server / worker-server / insight-server plus nginx) and `deploy/Dockerfile`
- Without `JWT_SECRET` the containers exit immediately; export it even for a local trial
- nginx (port 80) is the entry point: WebUI and API are both at `http://<host>/`; self-check `curl -fsS http://127.0.0.1/api/health`
- Process ports: 38080 (Mgmt / user-facing API, IM WS, frontend), 38081 (Event / webhook, A2A), 38082 (Worker / runtime protocol, tasks, chat), 38083 (Insight / observability, dashboards)

## First sign-in and zero configuration

A local start needs **no `configs/` setup at all**: clone, start, done — then sign up with a username and password in the WebUI:

- With no email service configured, registration skips the verification code (put any placeholder in the email field — it is not validated), and you are signed in as soon as the account is created
- With no GitCode / GitHub OAuth configured, the login page says "register and sign in with username + password"; third-party buttons are greyed out but local accounts work normally
- Project templates take care of themselves: `examples/` ships with only a README, and **on first start the platform pulls the template library from [grape-template](https://github.com/grape-dev/grape-template)** (watch for `Templates: ... verified OK` in the startup log)

## Remaining configuration and deployment

| Item | Where |
| --- | --- |
| Execution engine and model configuration (runtime prerequisite) | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Platform-facing configuration (public base URL / OAuth / bot credentials) | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Deployment (start methods / database / configuration) | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) |

## Updating and maintenance

```bash
cd grape
git pull --ff-only                  # pull the latest code
pip install -r requirements.txt     # reinstall when dependencies changed
```

**Mainland China**: clone from the GitCode mirror `git clone https://gitcode.com/grape-dev/grape.git` and point pip at a domestic index.
