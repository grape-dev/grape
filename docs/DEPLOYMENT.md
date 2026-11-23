# Deployment

The go-live steps both paths share: start shape, database, configuration.

## Start methods

Decide how the service runs before going live: production uses **two processes** (main API 3000 + Webhook 8080); development uses a **single port with hot reload**.

```bash
python -m src.api.run          # Production: main API 3000 + Webhook 8080
python -m src.api.run --dev    # Development: single port 3000, hot-reload
```

Production deployment examples ship with the code under `deploy/`: nginx configs (`deploy/nginx-grape*.conf`), systemd units (`deploy/systemd/`), and the compose file (`deploy/docker-compose.yml`). The documentation map is `docs/DOCUMENTATION-GUIDE.md`; alert rules ship in `deploy/alerts.yml`.

## Database

**SQLite by default — nothing to install.** Tables are created on first start, the database file is `data/sessions.db` (change the path with `DEVOPS_DB_PATH`); under Docker it lives in the `grape-data` volume, and `docker compose down -v` deletes that volume along with all state.

Backend resolution order: `DEVOPS_DB_BACKEND` env var > `backend` field in `configs/db-backend.json` > `sqlite` (restart required).

For production, switch to **MySQL 8.x** in three steps:

```bash
# 1. Driver (already installed with requirements.txt; run this only to add it separately)
pip install pymysql DBUtils

# 2. Create the database — Grape creates tables, never the database itself; the charset must be utf8mb4 or CJK/emoji text breaks
mysql -h<host> -u<user> -p -e "CREATE DATABASE IF NOT EXISTS grape_db CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;"
```

```json
// 3. configs/db-backend.json (copy from db-backend.example.json)
{
  "backend": "mysql",
  "mysql": {
    "host": "<db-host>",
    "port": 3306,
    "user": "<user>",
    "password": "<password>",
    "database": "grape_db"
  }
}
```

The `DEVOPS_DB_HOST` / `DEVOPS_DB_PORT` / `DEVOPS_DB_USER` / `DEVOPS_DB_PASSWORD` / `DEVOPS_DB_NAME` env vars override that file (preferred for containers and clusters), or use a single URL: `DEVOPS_DB_URL=mysql://<user>:<password>@<host>:3306/grape_db`. Defaults for anything unset: host `127.0.0.1`, port `3306`, user `grape`, database `grape_db`.

**No manual table creation, no migration runs**: startup issues `CREATE TABLE IF NOT EXISTS` plus idempotent `ALTER` migrations; for SQLite the old file is backed up to `data/backups/` before a migration (latest 5 kept, tunable via `DB_BACKUP_KEEP`). Verify with `curl -fsS http://127.0.0.1:3000/api/health` and the startup line `MySQLCompatDB: connection pool initialized (min=2, max_cached=16, max_conn=32)`.

<details markdown="1">
<summary>Production notes and common failures</summary>

- The database account needs `CREATE` / `ALTER` / `INDEX` on that database — table creation and migrations run automatically at startup.
- Pool sizing is env-driven: `DB_POOL_MIN` (2) / `DB_POOL_MAX_CACHED` (16) / `DB_POOL_MAX_CONNECTIONS` (32); `DB_POOL_DISABLED=1` falls back to the legacy per-thread connection.
- MySQL 8 defaults to `caching_sha2_password`: `pymysql` with `cryptography` (already pulled in by `python-jose[cryptography]`) completes the handshake; if it still fails, create the account with `mysql_native_password`.
- The admin console reads its featured-order list from a separate admin database (default `grape_admin_db`, overridable via `ADMIN_DB_*`): if unreachable it degrades silently and does not affect the main flow.
- `deploy/docker-compose.yml` starts the app only, **no MySQL service**; for Docker + MySQL point the config at an external instance (`./configs` is mounted into the container — restart the container after editing).
- Switching backends does not move data: existing SQLite data must be migrated by hand (the repo only ships `scripts/export_mysql_to_sqlite.py`, the MySQL → SQLite direction).
- **Container overlayfs compatibility (Issue #3588)**: some container storage drivers replace SQLite WAL/SHM files (`sessions.db-wal` / `sessions.db-shm`) via copy-up as "delete old file + create a new one", which invalidates the process-held connection and causes persistent `disk I/O error` / HTTP 500s. grape now self-heals at runtime: stale connections are detected and rebuilt with PRAGMA replay (log markers `event=db_stale_detected` / `event=db_reconnect_ok`; `/healthz` exposes `db_status` / `wal_valid`). Deployment recommendations: ① keep the data directory on a **persistent volume** (e.g. the `grape-data` volume, or set `DEVOPS_DB_PATH` to a path inside the volume) so WAL/SHM never live in the container writable layer; ② if staleness keeps recurring, `DEVOPS_DB_JOURNAL_MODE` can switch the journal mode (e.g. `truncate` / `delete`; default remains `wal` — changing it affects write concurrency, evaluate carefully).
- Failure cheat sheet: `ModuleNotFoundError: pymysql` → driver missing; `Access denied` → wrong account, grants or database name; garbled CJK text → database is not `utf8mb4`; data "disappears" after a restart → still on SQLite and the `grape-data` volume was removed by `down -v`.

</details>

## Configuration

> How to fill in OAuth credentials, the public base URL and bot credentials: see **[CONFIGURATION.md](CONFIGURATION.md)**.

`copy` = not in this repo, copy it from the matching `.example.json` before first start. `synced` = produced at deploy time by the sync scripts. `committed` = version-controlled here.

| File | Source | Description |
| --- | --- | --- |
| `configs/server-config.json` | copy | Skippable for a local trial; required when serving others. Public base URL (`server_base_url` / `webhook_url`); without it OAuth callbacks point at localhost |
| `configs/oauth-providers.json` | copy | Optional — only needed for third-party login or repo hosting. Seeds the GitCode / GitHub OAuth credentials into the DB at startup; maintain them in the console (Platform Credentials) afterwards. `redirect_uri` must match the OAuth App exactly |
| `configs/global-bot-token.json` | copy | Global bot token + legacy OAuth fields |
| `configs/login-whitelist.json` | copy | Optional. Seed for the GitCode login whitelist — imported once on first start, only when the table is empty; the console (OAuth Login Whitelist) owns it afterwards |
| `configs/db-backend.json` | copy | Database backend (SQLite by default; MySQL 8.x recommended for production — see Database above) |
| `configs/agent-description.json` | committed | Agent type seeds (event subscriptions, default engine) |
| `configs/agent-skill-mapping.json` | committed | Dev scenario → Agent × Skill mapping |
| `configs/engine-selection.json` | committed | Which engine runs which agent; no silent fallback to `opencode` when unset |
| `configs/skill-description.json` | synced | Skill registry, from [grape-skills](https://gitcode.com/grape-dev/grape-skills) via `scripts/sync_skills.sh` |
| `configs/project-templates.json` | synced | Project template registry, from [grape-template](https://gitcode.com/grape-dev/grape-template) via `scripts/sync_templates.sh`; template dirs land in `examples/` |
