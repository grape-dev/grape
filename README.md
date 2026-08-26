<h1 align="center">Grape</h1>

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

English | [中文](README.zh.md)

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

```bash
git clone https://gitcode.com/Jason7602/grape.git
cd grape
pip install -r requirements.txt

# Install at least one Agent CLI:
npm install -g opencode        # or pip install codearts-cli

cp configs/global-bot-token.example.json configs/global-bot-token.json
# Edit global-bot-token.json with your GitCode Bot Token / OAuth credentials

mkdir -p data
python -m src.api.run --dev    # Dev mode, open http://localhost:3000
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
| `configs/skill-description.json` | Official skill seeds |
| `configs/agent-skill-mapping.json` | Dev scenario → Agent × Skill mapping |
| `configs/project-templates.json` | Project template library |
| `configs/login-whitelist.json` | GitCode login whitelist |

## Development & Testing

```bash
# Install dependencies
pip install -r requirements.txt
pip install pytest pytest-mock

# Run tests
mkdir -p data
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
- **Skills** — Contribute new Agent skills to `skills/` (Markdown only, no backend changes)
- **Templates** — Add new project templates to `configs/project-templates.json`
- **Bug Reports** — Provide clear repro steps, environment info, and logs
- **Test Cases** — Add coverage for edge cases
- **Community** — Answer questions, write tutorials

Every contribution, code or otherwise, makes Grape better.

## Feedback

- **GitCode Issues**: https://gitcode.com/Jason7602/grape/issues
  - Bugs: include environment, repro steps, and logs
  - Feature suggestions: describe the use case and expected behavior
- **Feishu**: feedback directly in your bound group chat after connecting Grape

## License

MIT