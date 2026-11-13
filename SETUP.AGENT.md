# Agent Installation and Configuration Guide

## Hand it to an agent

Copy the whole block below to your coding agent (Claude Code / Codex / Cursor / opencode …); it will clone, install dependencies, start the service and verify it:

```text
Please clone the repository this SETUP.AGENT.md was read from — https://gitcode.com/grape-dev/grape — and verify that the service is reachable (trial mode: no OAuth, mail service or external database).
Pre-check: Python >= 3.10, Node.js >= 18. If either does not match, stop and report; do not upgrade the system environment.
Startup: run python -m src.api.run --dev in the background (it blocks the foreground, so use a background job) and wait for port 3000 to be ready.
Verification: curl -s -o /dev/null -w '%{http_code}' http://localhost:3000/api/health returning 200 means success.
When the service is up, tell me; I will register the account. Report every step; on failure, stop and quote the command and the full error — do not retry silently and do not switch approaches on your own.
```

> This guide is published on both hosts; use the clone URL of the host you read it from — GitHub `https://github.com/grape-dev/grape` · GitCode `https://gitcode.com/grape-dev/grape`.

Acceptance: `curl -s http://localhost:3000/api/health` returns 200. Account registration is done by a person on the page.

## Host your first repository

| Step | Expected result | Troubleshooting |
| --- | --- | --- |
| Workspace → Repositories → join and host | Hosting confirmation; the repository becomes managed | 403 → use the repository **owner's** credentials, or grant the bot admin rights; 502 → retry later, or use "repair hosting" to self-heal |
| Open the repository workspace → dispatch an issue / task | The agent starts and a task card appears | Check the server log |

Platform credentials can also be filled in the admin console `http://<host>/admin` (separate login; the first deployment account is `admin` / `admin123`, changeable via `GRAPE_ADMIN_PASSWORD` before startup, and the first login prompts a password change) under Config Center → Platform Credentials — saved changes take effect immediately because they are written to the database, which is authoritative; the whitelist lives under User Management → OAuth Login Whitelist and is off by default. The one thing the console cannot control is the OAuth **callback URL**, which comes from `server_base_url` / `webhook_url` in `configs/server-config.json`.

## Preflight check (optional)

```bash
python scripts/doctor.py                  # pre-install check (Python version / deps / config / ports / DB)
curl -s http://localhost:3000/api/health  # health check (includes base-URL configuration state)
```

## Remaining configuration and deployment

| Item | Where |
| --- | --- |
| Execution engine and model configuration (runtime prerequisite) | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Platform-facing configuration (public base URL / OAuth / bot credentials) | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Deployment (start methods / database / configuration) | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) |
