---
name: runtime-install
description: Install, configure, and register hermes / opencode / codearts runtimes in Grape, including custom API endpoint and model configuration.
version: 1.0.0
author: Nous Research
license: MIT
platforms: [linux]
---

# Runtime Install & Configuration

Install and configure hermes, opencode, and codearts CLI runtimes for Grape, including custom OpenAI-compatible API endpoint and model configuration.

## Prerequisites

- Linux (x86_64)
- Grape installed under `/opt/grape`
- GitCode bot token (`BOT_GITCODE_TOKEN`) available for Agent runtime DB operations

## Quickstart

### 1. Install hermes

```bash
pip install hermes --break-system-packages
```

Verify:

```bash
hermes --version
# Expected: Hermes Agent v0.x.x
```

Locate binary for Grape registration:

```bash
which hermes
# Expected: /usr/local/bin/hermes
```

### 2. Install opencode

```bash
npm install -g @opencode-ai/cli
# Verify
opencode --version
# Expected: 1.x.x
```

### 3. Install codearts (Huawei Cloud fork of opencode)

```bash
sh -c "$(curl -L https://cnnorth4-cloudide-marketplace.obs.cn-north-4.myhuaweicloud.com/codearts/cli_tui/install_script/install.sh)"
export PATH=~/.codeartsdoer/installers:$PATH
```

Symlink for `shutil.which()`:

```bash
ln -sf ~/.codeartsdoer/installers/codearts /usr/local/bin/codearts
```

Verify:

```bash
codearts --version
# Expected: 26.x.x
```

## Model / API Configuration

Configure an OpenAI-compatible API endpoint (e.g. ModelArts, custom LLM gateway) for the CLI engines.

### Configure opencode

Edit `~/.config/opencode/opencode.jsonc`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "<provider-name>": {
      "name": "<Display Name>",
      "api": "openai",
      "options": {
        "baseURL": "https://your-api-endpoint.com/v1",
        "apiKey": "sk-your-api-key"
      },
      "models": {
        "model-name-1": {
          "id": "model-name-1",
          "name": "Model Display Name 1",
          "family": "model-family"
        },
        "model-name-2": {
          "id": "model-name-2",
          "name": "Model Display Name 2",
          "family": "model-family"
        }
      }
    }
  },
  "model": "<provider-name>/<default-model-id>"
}
```

### Configure codearts

codearts shares its config at `~/.codeartsdoer/codearts_cli.json`. Same schema as opencode:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "modelarts": {
      "name": "ModelArts MaaS",
      "api": "openai",
      "options": {
        "baseURL": "https://your-api-endpoint.com/v1",
        "apiKey": "sk-your-api-key"
      },
      "models": {
        "deepseek-v4-flash": {
          "id": "deepseek-v4-flash",
          "name": "DeepSeek V4 Flash",
          "family": "deepseek"
        }
      }
    }
  },
  "model": "modelarts/deepseek-v4-flash"
}
```

### codearts models cache

codearts v26.x has a `ProviderModelsHookPlugin` that fetches model lists from Huawei Cloud xCenter (internal API, returns 503 from public internet). This plugin falls back to a local cache file.

Create the cache at `~/.cache/codeartsdoer/models-cache.json`:

```json
{
  "models": {
    "deepseek-v4-flash": {
      "id": "deepseek-v4-flash",
      "reasoning": true,
      "limit": { "context": 1048576, "output": 128000 },
      "enable_queue": false
    }
  },
  "timestamp": 1700000000000,
  "version": 1
}
```

Required fields: `models` (object) and `timestamp` (millisecond epoch). Each model key's value must include at minimum `id`. Without a valid cache file, `codearts run` fails with `模型列表获取失败，请刷新重试。`.

### codearts sandbox network policy

The codearts sandbox default config blocks external API calls (`network_policy: deny_all`). Update to allow:

**`~/.codeartsdoer/cli-data/storage/sandbox/config.json`:**
```json
{ "network_policy": "allow_all" }
```

**`~/.codeartsdoer/cli-data/storage/permission/config.json`:**
```json
[
  {"permission": "question", "pattern": "*", "action": "allow"},
  {"permission": "webfetch", "pattern": "*", "action": "allow"},
  {"permission": "websearch", "pattern": "*", "action": "allow"},
  {"permission": "bash", "pattern": "*", "action": "allow"},
  {"permission": "browser", "pattern": "*", "action": "allow"},
  {"permission": "read", "pattern": "*", "action": "allow"},
  {"permission": "write", "pattern": "*", "action": "ask"},
  {"permission": "edit", "pattern": "*", "action": "ask"}
]
```

## Register Runtimes in Grape

After installing the CLIs, register them with Grape's `agent_runtimes` table so `pick_engine()` can select them.

### Via Grape API (recommended)

```python
import sys
sys.path.insert(0, "/opt/grape")
from src.runtime.registry import discover_runtimes
from src.user.db import save_agent_runtimes

discovered = discover_runtimes()
installed = [d for d in discovered if d["installed"]]
save_agent_runtimes(installed)
```

### Manual health check

```python
import subprocess, sqlite3

db_path = "/opt/grape/data/sessions.db"
conn = sqlite3.connect(db_path)
c = conn.cursor()

for name, path in [("opencode", "/root/.opencode/bin/opencode"),
                   ("hermes", "/usr/local/bin/hermes"),
                   ("codearts", "/usr/local/bin/codearts")]:
    r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
    healthy = 1 if r.returncode == 0 else 0
    version = r.stdout.strip().split("\n")[0][:60] if healthy else ""
    c.execute("UPDATE agent_runtimes SET healthy=?, version=?, updated_at=datetime('now') WHERE name=?",
              (healthy, version, name))
conn.commit()
conn.close()
```

## Engine Binding

After registration, bind engines to agents via `devops_agent_runtime_binding`:

```python
from src.runtime.engine_select import set_binding

# Bind all three runtimes to VOD agent
set_binding("gitcode:owner/repo", "vod", [1, 2, 3])
# [opencode(id=1), hermes(id=2), codearts(id=3)]
```

`pick_engine()` selects the engine with the lowest `priority` then lowest `runtime_id` among healthy candidates. All three share priority=90 when bound as local engines.

## Runtime Adapter Details

Each CLI has a runtime adapter in `src/runtime/`:

| Engine | Adapter class | File | prompt_via_stdin |
|--------|---------------|------|------------------|
| opencode | `OpencodeRuntime` | `src/runtime/opencode.py` | False |
| hermes | `HermesRuntime` | `src/runtime/hermes.py` | False |
| codearts | `CodeArtsRuntime` | `src/runtime/codearts.py` | **False** (positional arg: stdin triggers sandbox network block) |

### CodeArtsRuntime key env vars

The `get_env()` method must set these to match the codearts startup script:

| Variable | Value | Purpose |
|----------|-------|---------|
| `CODEARTS_CLI_AK` | `dummy` | Bypass Huawei Cloud AK/SK check |
| `CODEARTS_CLI_SK` | `dummy` | Bypass Huawei Cloud AK/SK check |
| `SCENARIO` | `codeartsdoer` | Scenario identifier |
| `KERNEL_DATA_DIR` | `~/.codeartsdoer/cli-data` | **Data isolation** — prevents DB conflict with opencode |
| `KERNEL_CONFIG_DIR` | `~/.codeartsdoer` | Config directory |
| `OPENCODE_CONFIG` | `~/.codeartsdoer/codearts_cli.json` | Custom provider config path |
| `OPENCODE_CONFIG_FILE` | `codearts_cli.json,codearts_cli.jsonc` | Config file names |
| `OPENCODE_MODE` | `tui` | **Critical** — without this `network-interceptor` blocks external API |
| `PLUGIN_ENV` | `hc` | Huawei Cloud plugin environment |
| `OPENCODE_CHANNEL` | `latest` | Update channel |
| `OPENCODE_DISABLE_MODELS_FETCH` | `1` | Skip xCenter model fetch |
| `OPENCODE_DISABLE_AUTOUPDATE` | `true` | Disable auto-update |
| `OPENCODE_ALWAYS_NOTIFY_UPDATE` | `false` | Suppress update notifications |
| `NODE_TLS_REJECT_UNAUTHORIZED` | `0` | Trust self-signed certs |
| `DEVOPS_SKILLS_DIR` | `SKILLS_DIR` | Grape skill directory |
| `DEVOPS_EVENT_TYPE` | `""` | Event type override |

## Verification

### CLI health

```bash
opencode --version      # ≥ 1.18.x
hermes --version        # ≥ 0.19.x
codearts --version      # ≥ 26.8.x
```

### API connectivity

```bash
# opencode (model from config)
opencode run --format json --model <provider>/<model> "连通测试"
# Expected: NDJSON output with step_start/text/step_finish

# hermes
hermes -z "连通测试" --yolo --cli
# Expected: Chinese reply

# codearts (must use startup script or set all env vars)
export CODEARTS_CLI_AK=dummy
export CODEARTS_CLI_SK=dummy
export OPENCODE_MODE=tui
export SCENARIO=codeartsdoer
export KERNEL_DATA_DIR=$HOME/.codeartsdoer/cli-data
export OPENCODE_CONFIG=$HOME/.codeartsdoer/codearts_cli.json
codearts run --format json --model <provider>/<model> "连通测试"
# Expected: NDJSON output with step_start/text/step_finish
```

### Grape pipeline

```python
from src.runtime.engine_select import pick_engine

# Verify engine can be selected
rid = pick_engine("gitcode:owner/repo", "vod")
print(f"Selected engine id: {rid}")
```

## Known Issues

| Issue | Symptom | Fix |
|-------|---------|-----|
| codearts DB conflict | `CREATE TABLE workspace already exists` | Set `KERNEL_DATA_DIR` to codearts's own directory |
| codearts session miss | `Session not found: ses_xxx` | Don't pass `--session` in build_args (cross-engine sessions invisible) |
| codearts network blocked | `Network request blocked: https://...` | Set `OPENCODE_MODE=tui` and all startup script env vars |
| xCenter 503 | `模型列表获取失败，请刷新重试。` | Create `models-cache.json` with correct format |
| opencode not found | `CLI not found in PATH` | Symlink `/usr/local/bin/opencode` or ensure npm global bin in PATH |
| hermes not found | `CLI not found in PATH` | Symlink `/usr/local/bin/hermes` or ensure pip install path in PATH |