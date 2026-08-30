#!/usr/bin/env bash
set -euo pipefail

SESSION_ROOT="${FETCH_PR_SKILL_SESSION_ROOT:-$HOME/.fetch-pr-skill/sessions}"

require_jq() {
  if ! command -v jq &>/dev/null; then
    echo "error: jq is required but not found" >&2
    exit 1
  fi
}

now_iso() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

generate_session_id() {
  local skill_name="$1"
  local date_stamp
  date_stamp=$(date -u +"%Y%m%d")
  local time_stamp
  time_stamp=$(date -u +"%H%M%S")
  local hex
  hex=$(printf '%04x' $((RANDOM % 65536)))
  echo "${skill_name}_${date_stamp}_${time_stamp}_${hex}"
}

cmd_create() {
  local skill_name="$1"
  local skill_path="$2"
  local agent="$3"
  local input="${4:-}"

  local session_id
  session_id=$(generate_session_id "$skill_name")
  local session_dir="$SESSION_ROOT/$session_id"

  mkdir -p "$session_dir/skill"

  local started_at
  started_at=$(now_iso)

  local started_epoch
  started_epoch=$(date +%s)

  jq -n \
    --arg sid "$session_id" \
    --arg sn "$skill_name" \
    --arg sp "$skill_path" \
    --arg ag "$agent" \
    --arg inp "$input" \
    --arg sa "$started_at" \
    --argjson se "$started_epoch" \
    '{
      session_id: $sid,
      skill_name: $sn,
      skill_path: $sp,
      agent: $ag,
      input: $inp,
      started_at: $sa,
      started_epoch: $se,
      status: "running"
    }' > "$session_dir/meta.json"

  touch "$session_dir/output.log"

  echo "$session_id"
}

cmd_complete() {
  require_jq

  local session_id="$1"
  local status="$2"
  local exit_code="$3"
  local session_dir="$SESSION_ROOT/$session_id"

  if [[ ! -d "$session_dir" ]]; then
    echo "error: session not found: $session_id" >&2
    exit 1
  fi

  local started_at
  started_at=$(jq -r '.started_at' "$session_dir/meta.json")

  local finished_at
  finished_at=$(now_iso)

  local start_epoch finish_epoch duration
  start_epoch=$(jq -r '.started_epoch // 0' "$session_dir/meta.json")
  finish_epoch=$(date +%s)
  duration=$((finish_epoch - start_epoch))

  jq -n \
    --arg sid "$session_id" \
    --arg st "$status" \
    --arg sa "$started_at" \
    --arg fa "$finished_at" \
    --argjson dur "$duration" \
    --argjson ec "$exit_code" \
    '{
      session_id: $sid,
      status: $st,
      started_at: $sa,
      finished_at: $fa,
      duration_seconds: $dur,
      exit_code: $ec
    }' > "$session_dir/result.json"

  local tmp_meta
  tmp_meta=$(jq --arg st "$status" '.status = $st' "$session_dir/meta.json")
  echo "$tmp_meta" > "$session_dir/meta.json"
}

cmd_list() {
  require_jq

  local filter_skill="${1:-}"

  if [[ ! -d "$SESSION_ROOT" ]]; then
    return 0
  fi

  for session_dir in "$SESSION_ROOT"/*/; do
    [[ -d "$session_dir" ]] || continue
    [[ -f "$session_dir/meta.json" ]] || continue

    local meta
    meta=$(cat "$session_dir/meta.json")

    local sn
    sn=$(echo "$meta" | jq -r '.skill_name')

    if [[ -n "$filter_skill" && "$sn" != "$filter_skill" ]]; then
      continue
    fi

    local sid st
    sid=$(echo "$meta" | jq -r '.session_id')
    st=$(echo "$meta" | jq -r '.status')

    printf '%s\t%s\t%s\n' "$sid" "$sn" "$st"
  done
}

cmd_show() {
  require_jq

  local session_id="$1"
  local session_dir="$SESSION_ROOT/$session_id"

  if [[ ! -d "$session_dir" ]]; then
    echo "error: session not found: $session_id" >&2
    exit 1
  fi

  echo "=== meta.json ==="
  jq . "$session_dir/meta.json"

  if [[ -f "$session_dir/result.json" ]]; then
    echo ""
    echo "=== result.json ==="
    jq . "$session_dir/result.json"
  fi

  if [[ -f "$session_dir/output.log" ]]; then
    echo ""
    echo "=== output.log (first 50 lines) ==="
    head -50 "$session_dir/output.log"
  fi
}

usage() {
  echo "usage: session.sh <command> [args...]"
  echo ""
  echo "commands:"
  echo "  create <skill_name> <skill_path> <agent> [input]"
  echo "  complete <session_id> <status> <exit_code>"
  echo "  list [skill_name]"
  echo "  show <session_id>"
  exit 1
}

cmd="${1:-}"
shift || true

case "$cmd" in
  create)
    [[ $# -lt 3 ]] && usage
    cmd_create "$@"
    ;;
  complete)
    [[ $# -lt 3 ]] && usage
    cmd_complete "$@"
    ;;
  list)
    cmd_list "$@"
    ;;
  show)
    [[ $# -lt 1 ]] && usage
    cmd_show "$@"
    ;;
  *)
    usage
    ;;
esac
