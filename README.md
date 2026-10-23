<h1 align="center">Grape</h1>

<p align="center">English | <a href="README.zh.md">中文</a></p>

<p align="center">
  <strong>AI Native DevOps —— Turn Any Git Project into an AI-Driven Development Workflow with One Click</strong>
  <br>
  No IDE required. Design, develop, test, and ship anywhere, anytime — everyone on your team is a shipper.
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="#core-values">Core Values</a> ·
  <a href="#core-capabilities">Core Capabilities</a> ·
  <a href="#deployment">Deployment</a> ·
  <a href="#development--testing">Development & Testing</a> ·
  <a href="#contributing">Contributing</a> ·
  <a href="#feedback">Feedback</a>
</p>

---

## Core Values

Grape redefines the software development workflow — no longer "human-driven with AI assistance", but "AI-driven, human-reviewed".

| # | Core Value |
|---|-----------|
| **1** | **One-Click AI Hosting, Autonomous Evolution** — Connect any Git project to Grape, and the full pipeline from requirements to delivery is driven by Multi-Agent handoff. Issues auto-triage, code auto-implemented, PRs auto-reviewed, builds auto-deployed — iteration velocity **skyrockets**. |
| **2** | **No IDE, Anywhere Full-Stack R&D** — Feishu or WebUI becomes your development interface. Design, develop, test, ship — no editor required. Everyone on the team can directly contribute to delivery. |
| **3** | **Open Source as Templates, Instant Experience & Customization** — Every open-source project in the community is your starting template. One-click creation, auto-deployment for experience, Skill-driven customization — an AI development workflow from 0 to 1, ready instantly. |

---

## What is Grape

Grape is a Multi-Agent DevOps platform where AI Agents autonomously collaborate to handle every repetitive step of the development workflow. Connect a traditional Git project to Grape and it instantly gains a complete AI-native workflow — from requirements clarification, code implementation, review and verification, to build and deployment.

**In one sentence: turn your Git project into an AI-driven autonomous development team.**

## The Problem

Traditional DevOps relies heavily on manual effort across the pipeline: Issues need manual triage, code needs developers to write, PRs need human review, builds and deployments need manual triggers. More projects, more overhead.

Grape's goal is to **AI-ify every one of these steps**, turning developers from executors into decision-makers — you define the direction, AI handles the rest.

| Stage | Traditional | Grape AI Native |
|-------|-----------|-----------------|
| Issue Triage | Manual classification & assignment | VOD / Issue Agent auto-classify & assign |
| Implementation | Developer writes code | Coding Agent auto-implements & submits PR |
| Code Review | Human review | QA Agent auto-review & security scan |
| Build & Deploy | Manual / CI trigger | SRE Agent auto-build, deploy, monitor |
| Requirements | Manual feedback collection | VOD Agent gathers developer voice, clusters insights |

## Core Capabilities

### Multi-Agent Workflow

Once connected, every Issue / PR event is handled by a pipeline of Agents:

```
VOD Agent → Issue Agent → Coding Agent → QA Agent → SRE Agent
 Voice      Requirements    Implementation   Review       Build & Deploy
```

- **VOD Agent** — Collects developer feedback, clusters insights, auto-creates Issues
- **Issue Agent** — Classifies, labels, assigns, triggers Coding
- **Coding Agent** — Code implementation, PR creation, self-review
- **QA Agent** — PR review, automated testing, security checks
- **SRE Agent** — Build & deployment, release management, monitoring
- **Chat Agent** — Personal assistant via Feishu or WebUI

Agents collaborate through the A2A protocol, with each task flowing end-to-end automatically.

### One-Click AI Native

```
Choose a template / existing Git project
  → One-click create & host
  → Auto-apply dev scenario (Agent × Skill mapping)
  → Async onboarding (analyze project, create experience & exploration Issues)
  → Every Issue / PR from that point on is handled by the Multi-Agent pipeline
```

### Skills Library

15+ official skills covering the full development lifecycle. Support for custom skill uploads (official / repo-level / user-level), configurable per repository.

### Supported Entry Points

| Entry | Method | Description |
|-------|--------|-------------|
| GitCode | HTTP Webhook | Issue / PR / comment / build / deploy events |
| Feishu | WebSocket long-lived | Multi-bot isolation, 100+ bots supported |
| WebUI | SPA | Dashboard / Pipeline / Agent detail / Chat / Skills / Settings |

### Other Features

- **Multi-bot Feishu** — 100+ bots isolated in a single process, hot-reload, auto-reconnect
- **GitCode ↔ Feishu User Mapping** — P2P identity confirmation binding
- **Multi-tenant** — GitCode OAuth login with whitelist, per-user credential isolation
- **Event Audit** — Every Agent execution traced (logs, duration, conclusion), Pipeline visualization
- **Multi-backend** — opencode / codearts, pluggable

## Quick Start

### Prerequisites

| Component | Requirement | Notes |
|-----------|-------------|-------|
| **Python** | >= 3.9 (main service) | 3.10+ recommended for devbox sandbox |
| **Node.js** | >= 18 (for Agent CLI) | `npm install -g opencode` |
| **Git** | any recent version | |

### Windows (PowerShell)

```powershell
git clone https://github.com/grape-dev/grape.git
cd grape

# One-click setup: venv + deps + .env + preflight check + start
.\start.ps1                    # Local dev (minimal deps, SQLite)
.\start.ps1 -Full              # Full deps (OBS/MySQL/Feishu)
.\start.ps1 -SkipDoctor        # Skip preflight, just start

# Or manual setup:
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-local.txt
Copy-Item .env.example .env
New-Item -ItemType Directory -Force -Path data
python -m src.api.run --dev    # Dev mode, open http://localhost:3000
```

> If PowerShell blocks script execution: `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

### macOS / Linux

```bash
git clone https://github.com/grape-dev/grape.git
cd grape

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-local.txt     # Minimal (no OBS/MySQL/Feishu)
# pip install -r requirements.txt         # Full deps

cp .env.example .env
mkdir -p data
python -m src.api.run --dev                # Dev mode, open http://localhost:3000
```

### China Mirror (国内镜像加速)

```bash
# PyPI mirror
pip install -r requirements-local.txt -i https://mirrors.aliyun.com/pypi/simple/

# Or configure permanently:
pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/
```

### Install Agent CLI (at least one)

```bash
npm install -g opencode          # Node.js based
# or
pip install codearts-cli         # Python based
```

### Environment Check (Optional)

```bash
python scripts/doctor.py         # Run preflight checks
```

### Copy Bot Token Config

```bash
cp configs/global-bot-token.example.json configs/global-bot-token.json
# Edit with your GitCode Bot Token / OAuth credentials
```

## Deployment

```bash
python -m src.api.run          # Production: main API 3000 + Webhook 8080
python -m src.api.run --dev    # Development: single port 3000, hot-reload
```

Production deployment examples (nginx reverse proxy / systemd / Docker) in `docs/DEPLOYMENT`.

### Configuration

| File | Description |
|------|-------------|
| `configs/global-bot-token.json` | Global Bot token + GitCode OAuth credentials (gitignored) |
| `configs/agent-description.json` | Agent type seeds (event subscriptions, default backend) |
| `configs/skill-description.json` | Official skill seeds (synced from the standalone [grape-skills](https://github.com/grape-dev/grape-skills) repo via `scripts/sync_skills.sh`) |
| `configs/agent-skill-mapping.json` | Dev scenario → Agent × Skill mapping |
| `configs/project-templates.json` | Project template library (synced from the standalone [grape-template](https://github.com/grape-dev/grape-template) repo via `scripts/sync_templates.sh`) |
| `configs/login-whitelist.json` | GitCode login whitelist |

## Development & Testing

```bash
# Install dependencies (local dev minimal set)
python3 -m venv .venv
source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements-local.txt
pip install -r requirements-dev.txt

# Run tests
mkdir -p data                    # Windows: New-Item -ItemType Directory -Force data
python -m pytest tests/ -v
```

Test coverage: event normalization, prompt building, Agent executor (NDJSON parsing / session rebuild), Feishu handler, P2P binding, OAuth whitelist, Pipeline, project templates, repository hosting, Task Center, UI scenarios (Playwright).

## Contributing

1. **Open an Issue** — Describe what you want to do or the problem you've found
2. **Fork & Develop** — Fork the repo, work on a feature branch
3. **Add Tests** — Include pytest tests for new/modified features
4. **Submit a PR** — Describe changes, test results, and linked Issue

### Branch Naming

| Prefix | Purpose |
|--------|---------|
| `feat/` | New features |
| `fix/` | Bug fixes |
| `refactor/` | Code refactoring (no behavior change) |
| `docs/` | Documentation |
| `chore/` | Build, tooling |

### Non-code Contributions Welcome

- **Documentation** — Improve README, supplement design docs, fix typos
- **Skills** — Contribute new Agent skills to the standalone [grape-skills](https://github.com/grape-dev/grape-skills) repo (`skills/<agent_type>/<skill>/`, Markdown only, no backend changes)
- **Templates** — Add new project templates to the standalone [grape-template](https://github.com/grape-dev/grape-template) repo (template directory + `project-templates.json` entry)
- **Bug Reports** — Provide clear repro steps, environment info, and logs
- **Test Cases** — Add coverage for edge cases
- **Community** — Answer questions, write tutorials

Every contribution, code or otherwise, makes Grape better.

## Feedback

- **GitHub Issues**: https://github.com/grape-dev/grape/issues
  - Bugs: include environment, repro steps, and logs
  - Feature suggestions: describe the use case and expected behavior
- **Feishu**: feedback directly in your bound group chat after connecting Grape

## License

MIT