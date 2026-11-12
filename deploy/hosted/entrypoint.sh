#!/bin/sh
# Grape · 托管平台启动引导（幂等；平台环境变量是唯一事实来源）
#
# 做四件事，全部可重复执行：
#   1) JWT_SECRET     - 卷上没有就生成一次并落盘（否则每次重部署换密钥，所有人被踢下线）
#   2) 对外基址       - SERVER_BASE_URL > PUBLIC_DOMAIN > 平台注入的域名（RAILWAY_PUBLIC_DOMAIN 是示例，
#                       其他平台提供 PUBLIC_DOMAIN / SERVER_BASE_URL 效果相同）
#   3) 项目模板       - configs/project-templates.json 与 examples/ 不入库，启动时按需从模板仓同步
#   4) 引擎模型配置   - 用 LLM_* 变量生成 $HOME/.config/opencode/opencode.json
# 然后交回原始 CMD。
set -eu

# 托管引导默认惰性：1 = 总是执行，0 = 从不，未设置 = 自动探测。
# 自动探测只看「平台信号」（平台注入的域名 / PUBLIC_DOMAIN / SERVER_BASE_URL），
# **不看 LLM_API_KEY**：自建用户为了跑任务必然要设 LLM_API_KEY，若据此判定托管，
# 会改写自建环境的 HOME、configs/server-config.json 与引擎配置（见 deploy/README 说明）。
# 托管平台若只提供模型 key、不注入域名，请显式设 GRAPE_HOSTED_BOOTSTRAP=1。
HOSTED="${GRAPE_HOSTED_BOOTSTRAP:-auto}"
if [ "$HOSTED" = "auto" ]; then
  if [ -n "${RAILWAY_PUBLIC_DOMAIN:-}" ] || [ -n "${RAILWAY_ENVIRONMENT_NAME:-}" ] \
     || [ -n "${RAILWAY_PROJECT_ID:-}" ] || [ -n "${PUBLIC_DOMAIN:-}" ] \
     || [ -n "${SERVER_BASE_URL:-}" ]; then
    HOSTED=1
  else
    HOSTED=0
  fi
fi
if [ "$HOSTED" != "1" ]; then
  echo "[entrypoint] hosted bootstrap disabled (GRAPE_HOSTED_BOOTSTRAP=$HOSTED) -> exec as-is"
  exec "$@"
fi

# 极简环境（env -i、部分 distroless）可能没有 HOME，兜底给一个可写路径，避免 set -u 直接崩。
: "${HOME:=/root}"; export HOME

# 挂了持久卷时把引擎家目录放到卷上，模型配置才能跨重部署存活；没有卷则不动 HOME。
if [ -d /app/data ]; then
  export HOME="${GRAPE_ENGINE_HOME:-/app/data/engine-home}"
fi

DATA="${DEVOPS_DATA_DIR:-/app/data}"
mkdir -p "$DATA" /app/configs "$HOME/.config/opencode"

# ---------- 1) JWT_SECRET ----------
if [ -z "${JWT_SECRET:-}" ]; then
  if [ -f "$DATA/.jwt_secret" ]; then
    JWT_SECRET=$(cat "$DATA/.jwt_secret")
    JWT_SRC="volume"
  else
    JWT_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
    printf '%s' "$JWT_SECRET" > "$DATA/.jwt_secret"
    chmod 600 "$DATA/.jwt_secret"
    JWT_SRC="generated"
  fi
  export JWT_SECRET
  echo "[entrypoint] JWT_SECRET <- ${JWT_SRC}"
fi

# ---------- 2) 对外基址 ----------
# configs/ 被 .dockerignore 排除，运行时生成；优先级 SERVER_BASE_URL > PUBLIC_DOMAIN > 平台注入域名。
BASE_URL=""
BASE_SRC=""
if [ -n "${SERVER_BASE_URL:-}" ]; then
  BASE_URL="${SERVER_BASE_URL%/}"; BASE_SRC="SERVER_BASE_URL"
elif [ -n "${PUBLIC_DOMAIN:-}" ]; then
  BASE_URL="https://${PUBLIC_DOMAIN}"; BASE_SRC="PUBLIC_DOMAIN"
elif [ -n "${RAILWAY_PUBLIC_DOMAIN:-}" ]; then
  BASE_URL="https://${RAILWAY_PUBLIC_DOMAIN}"; BASE_SRC="RAILWAY_PUBLIC_DOMAIN"
fi
if [ -n "$BASE_URL" ]; then
  # webhook 路由随 git 主机不同（GitCode 是 /webhook/gitcode），其他主机可覆盖。
  WEBHOOK_PATH="${WEBHOOK_PATH:-/webhook/gitcode}"
  printf '{"server_base_url":"%s","webhook_url":"%s%s"}\n' \
    "$BASE_URL" "$BASE_URL" "$WEBHOOK_PATH" > /app/configs/server-config.json
  HOST="${BASE_URL#http://}"; HOST="${HOST#https://}"; HOST="${HOST%%/*}"
  export TRUSTED_HOSTS="${TRUSTED_HOSTS:-$HOST}"
  echo "[entrypoint] server_base_url <- ${BASE_SRC} (${BASE_URL})"
else
  echo "[entrypoint] WARN: neither SERVER_BASE_URL nor PUBLIC_DOMAIN / RAILWAY_PUBLIC_DOMAIN is available - server-config.json not written; OAuth callbacks will point at localhost:3000" >&2
fi

# ---------- 3) 项目模板（幂等） ----------
# configs/project-templates.json 与 examples/<模板>/ 都不入库，缺了 UI 模板页就是空的。
# 同步路径按优先级：脚本（git clone，需要 git）> 显式 TEMPLATES_TARBALL_URL > GitHub codeload 推导。
# 模板仓地址可配置：TEMPLATES_REPO（任意 git 托管）/ TEMPLATES_REF（分支或 tag）。
if [ ! -s /app/configs/project-templates.json ] \
   || [ ! -d /app/examples ] \
   || [ -z "$(find /app/examples -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -1)" ]; then
  TPL_REPO="${TEMPLATES_REPO:-https://gitcode.com/grape-dev/grape-template.git}"
  TPL_REF="${TEMPLATES_REF:-main}"
  echo "[entrypoint] templates missing -> syncing from ${TPL_REPO}@${TPL_REF}"
  TPL_DONE=0

  # 3a) 首选：仓库自带同步脚本（git clone，脚本会把模板铺进 examples/ 与 configs/）
  if command -v git >/dev/null 2>&1 && [ -f /app/scripts/sync_templates.sh ] \
     && timeout "${TEMPLATES_SYNC_TIMEOUT:-120}" env GIT_TERMINAL_PROMPT=0 \
          TEMPLATES_REPO="$TPL_REPO" TEMPLATES_REF="$TPL_REF" bash /app/scripts/sync_templates.sh; then
    echo "[entrypoint] templates synced (script)"
    TPL_DONE=1
  fi

  # 3b) 兜底一：显式归档地址（内网镜像、自建 GitLab、无 git 环境都能用）
  if [ "$TPL_DONE" != "1" ] && [ -n "${TEMPLATES_TARBALL_URL:-}" ]; then
    TPL_STAGE=$(mktemp -d)
    if curl -fsSL --retry 1 --max-time 90 "$TEMPLATES_TARBALL_URL" -o "$TPL_STAGE/t.tgz" \
       && tar xzf "$TPL_STAGE/t.tgz" -C "$TPL_STAGE"; then
      TPL_SRC=$(find "$TPL_STAGE" -mindepth 1 -maxdepth 1 -type d | head -1)
      # 守卫：归档没有顶层目录时 $TPL_SRC 为空，`for d in "$TPL_SRC"/*/` 会退化成遍历系统根目录
      # （/bin、/etc、/proc…），对每个根目录执行 rm -rf + cp -a，最坏是 cp /proc 失败后在 set -e 下
      # 中断启动、或把 /app 递归拷进 /app/examples/app。为空就直接跳过。
      if [ -z "${TPL_SRC:-}" ]; then
        echo "[entrypoint] WARN: archive has no top-level directory; skipping template sync" >&2
      else
        mkdir -p /app/examples /app/configs
        for d in "$TPL_SRC"/*/; do
          n=$(basename "$d"); [ "$n" = ".git" ] && continue
          rm -rf "/app/examples/$n"; cp -a "$d" "/app/examples/$n"
        done
        if [ -f "$TPL_SRC/project-templates.json" ]; then
          cp -a "$TPL_SRC/project-templates.json" /app/configs/project-templates.json
        fi
        echo "[entrypoint] templates synced (TEMPLATES_TARBALL_URL)"
        TPL_DONE=1
      fi
    else
      echo "[entrypoint] WARN: TEMPLATES_TARBALL_URL fetch failed" >&2
    fi
    rm -rf "$TPL_STAGE"
  fi

  # 3c) 兜底二：GitHub 主机可按 codeload 归档推导（GitCode 无归档端点，不猜 URL）
  if [ "$TPL_DONE" != "1" ]; then
    case "$TPL_REPO" in
      *github.com*)
        TPL_SLUG=$(printf '%s' "$TPL_REPO" | sed -e 's#^.*github\.com[/:]##' -e 's#\.git$##')
        TPL_TB="https://codeload.github.com/${TPL_SLUG}/tar.gz/refs/heads/${TPL_REF}"
        TPL_STAGE=$(mktemp -d)
        if curl -fsSL --retry 1 --max-time 90 "$TPL_TB" -o "$TPL_STAGE/t.tgz" \
           && tar xzf "$TPL_STAGE/t.tgz" -C "$TPL_STAGE"; then
          TPL_SRC=$(find "$TPL_STAGE" -mindepth 1 -maxdepth 1 -type d | head -1)
          # 同 3b 的守卫：$TPL_SRC 为空时不能进入复制循环（否则遍历 /*/ 并误删误拷）。
          if [ -z "${TPL_SRC:-}" ]; then
            echo "[entrypoint] WARN: archive has no top-level directory; skipping template sync" >&2
          else
            mkdir -p /app/examples /app/configs
            for d in "$TPL_SRC"/*/; do
              n=$(basename "$d"); [ "$n" = ".git" ] && continue
              rm -rf "/app/examples/$n"; cp -a "$d" "/app/examples/$n"
            done
            if [ -f "$TPL_SRC/project-templates.json" ]; then
              cp -a "$TPL_SRC/project-templates.json" /app/configs/project-templates.json
            fi
            echo "[entrypoint] templates synced (codeload)"
            TPL_DONE=1
          fi
        else
          echo "[entrypoint] WARN: codeload fetch failed" >&2
        fi
        rm -rf "$TPL_STAGE"
        ;;
    esac
  fi

  if [ "$TPL_DONE" != "1" ]; then
    echo "[entrypoint] WARN: template sync unavailable (template page may be empty)." >&2
    echo "[entrypoint]        mirror the template repo and set TEMPLATES_REPO, or set TEMPLATES_TARBALL_URL." >&2
  fi
else
  echo "[entrypoint] templates present (skip sync)"
fi

# ---------- 3b) 官方配置种子（agent 类型 / pipelines / …） ----------
# configs/*.json 默认被 .dockerignore 排除，镜像里可能一个都没有；缺 agent-description.json 时
# agent_type 表无法播种，Agents 页会全部 404（"Agent 类型未配置: vod"）。
# 这里按并集清单兜底：新老两代用到的种子文件都覆盖，缺哪个拉哪个，多拉的文件在镜像里无害。
CFG_BASE="${GRAPE_CONFIG_RAW_BASE:-https://raw.githubusercontent.com/grape-dev/grape/main/configs}"
SEEDS="agent-description.json agent-skill-mapping.json default-scene.json engine-selection.json pipelines.json credential-filter.json onboarding-templates.json scene-precheck.json"
# 刻意不含 global-bot-token.json：它是 .gitignore 忽略的运行时 bot token，不是种子；
# 从远端下载也不成立（raw 基址同样取不到被 gitignore 的文件），应用侧对缺失已有兜底。
# 先探一次源是否可达（8s）：不可达就整体跳过，避免在受限网络里 9 个文件各等 15s 拖住启动。
# 镜像里已按 .dockerignore 白名单带了种子，这里只是缺文件时的兜底。
SEED_NET=1
for _f in $SEEDS; do
  [ -s "/app/configs/$_f" ] && continue
  if ! curl -fsS --max-time 8 -o /dev/null "$CFG_BASE/$_f" 2>/dev/null; then
    SEED_NET=0
  fi
  break
done
if [ "$SEED_NET" = "1" ]; then
  for _f in $SEEDS; do
    [ -s "/app/configs/$_f" ] && continue
    if curl -fsSL --retry 1 --max-time 15 "$CFG_BASE/$_f" -o "/app/configs/$_f" 2>/dev/null; then
      echo "[entrypoint] config seed fetched: $_f"
    else
      rm -f "/app/configs/$_f"
      echo "[entrypoint] WARN: config seed unavailable: $_f" >&2
    fi
  done
else
  echo "[entrypoint] WARN: config seed source unreachable ($CFG_BASE) - using the seeds baked into the image" >&2
fi

# ---------- 4) 引擎模型配置 ----------
# 为什么不用顶层 providers 写法：opencode 1.18.x/1.19.x 只认经典 provider.<id> 块，
# npm 必须是 @ai-sdk/openai-compatible，且 baseURL 放在 options 里；
# 用 providers + package + 顶层 baseURL 会得到 "Provider not found" / "Failed to initialize provider"。
if [ -n "${LLM_API_KEY:-}" ]; then
  CFG="$HOME/.config/opencode/opencode.json"
  mkdir -p "$(dirname "$CFG")"
  PID="${LLM_PROVIDER_ID:-custom}"
  # Provider 预设：只给 LLM_PROVIDER + LLM_API_KEY 就能用。全部是 OpenAI 兼容协议；
  # 其他厂商留空 LLM_PROVIDER 并填 LLM_BASE_URL。
  # 没有 /models 端点的厂商打标记，跳过注定失败的探测调用并要求显式 LLM_MODEL。
  NO_MODEL_LIST=0
  case "${LLM_PROVIDER:-}" in
    openai)     DEF_BASE="https://api.openai.com/v1" ;;
    openrouter) DEF_BASE="https://openrouter.ai/api/v1" ;;
    gemini)     DEF_BASE="https://generativelanguage.googleapis.com/v1beta/openai" ;;
    groq)       DEF_BASE="https://api.groq.com/openai/v1" ;;
    anthropic)  DEF_BASE="https://api.anthropic.com/v1"; NO_MODEL_LIST=1 ;;
    deepseek)   DEF_BASE="https://api.deepseek.com/v1" ;;
    opengw)     DEF_BASE="https://opengw.clouddeveloper.club/v1" ;;
    *)          DEF_BASE="" ;;
  esac
  BASE="${LLM_BASE_URL:-$DEF_BASE}"
  if [ -z "$BASE" ]; then
    echo "[entrypoint] WARN: neither LLM_BASE_URL nor an LLM_PROVIDER preset was given; the engine may not resolve an endpoint" >&2
  fi
  LLM_BASE_URL="$BASE" \
  LLM_MODEL_ID="${LLM_MODEL:-}" \
  LLM_PROVIDER_ID="$PID" \
  LLM_NO_MODEL_LIST="$NO_MODEL_LIST" \
  OPENCODE_CFG="$CFG" \
  python3 - <<'PY'
import json, os, sys, urllib.request

base = (os.environ.get("LLM_BASE_URL") or "").strip()
mid = (os.environ.get("LLM_MODEL_ID") or "").strip()
pid = (os.environ.get("LLM_PROVIDER_ID") or "custom").strip() or "custom"
cfg_path = os.environ["OPENCODE_CFG"]
api_key = os.environ.get("LLM_API_KEY") or ""

# LLM_MODEL 可能是 `custom/<model-id>` 或裸 `<model-id>`；取斜杠后半段
mid = mid.split("/", 1)[1] if "/" in mid else mid

models = {}
def add(m):
    m = (m or "").strip()
    if m and m not in models:
        models[m] = {"name": m}

add(mid)

# 端点若暴露 /models，把返回的模型全部登记，部署者可在 Agent 设置里切换，而不是被钉死一个。
no_list = os.environ.get("LLM_NO_MODEL_LIST") == "1"
if base and not no_list:
    try:
        req = urllib.request.Request(
            base.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        for item in (data.get("data") or []):
            add(item.get("id"))
    except Exception as e:
        print(f"[entrypoint] model discovery skipped: {type(e).__name__}: {e}", file=sys.stderr)

if not models:
    print(
        "[entrypoint] ERROR: no model available - the endpoint returned no model list, or the discovery call failed, and LLM_MODEL is empty.\n"
        "[entrypoint]        set LLM_MODEL explicitly, e.g. claude-sonnet-4-5 for the anthropic preset.",
        file=sys.stderr,
    )
    sys.exit(0)

# 没给模型时优先选主流通用模型，而不是字母序第一个（聚合端点动辄几百个模型，顺序无意义）
PREFERRED = [
    "claude-sonnet-4-5", "claude-sonnet-4", "claude-3-7-sonnet",
    "gpt-5", "gpt-4.1", "gemini-2.5-pro", "deepseek-chat", "deepseek-v3",
    "qwen3-coder", "llama-3.3-70b",
]


def pick_default(available):
    lowered = {m.lower(): m for m in available}
    for pref in PREFERRED:
        for k, m in lowered.items():
            if pref in k:
                return m
    return sorted(available)[0]


default_model = f"{pid}/{mid}" if mid else f"{pid}/{pick_default(models)}"
provider = {
    "npm": "@ai-sdk/openai-compatible",
    "options": {"apiKey": "{env:LLM_API_KEY}"},
    "models": models,
}
if base:
    provider["options"]["baseURL"] = base

cfg = {
    "$schema": "https://opencode.ai/config.json",
    "model": default_model,
    "provider": {pid: provider},
}
with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print(
    f"[entrypoint] engine config -> {cfg_path} "
    f"(provider={pid} default={default_model} models={len(models)} baseURL={base or '<unset>'})"
)
PY
else
  echo "[entrypoint] LLM_API_KEY not provided: skipping engine config; tasks will report \"no available engine\"" >&2
fi

# ---------- 5) 交回原始 CMD ----------
exec "$@"