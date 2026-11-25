#!/usr/bin/env bash
# Engine/device identity derivation - shared by deploy/hosted/entrypoint.sh and its tests.
#
# host_name is part of the local engine's natural key
# (UNIQUE (credential_id, name, scope, owner_id, host_name); see
# src/domains/runtime/schema_ddl.py). A container hostname changes on every rebuild,
# so an unstable identity leaves a new offline engine row per redeploy. Priority:
#   1. explicit GRAPE_HOSTNAME       - operator-set, always wins
#   2. $DATA/instance-id (volume)    - persistent volume, stable across redeploys
#   3. RAILWAY_SERVICE_NAME          - platform-injected, stable per service
#   4. generated random id           - last resort, non-persistent without a volume
#
# NOTE: level 3 assumes a SINGLE-replica service. For multi-replica, set
# GRAPE_HOSTNAME="service-name-replica-id" explicitly (see the multi-replica
# warning in src/kernel/host_identity.py).

derive_engine_identity() {
  : "${DATA:=/app/data}"
  if [ -n "${GRAPE_HOSTNAME:-}" ]; then
    echo "[entrypoint] engine identity -> GRAPE_HOSTNAME=$GRAPE_HOSTNAME (from env)"
    return 0
  fi
  local _idf="$DATA/instance-id"
  if [ -s "$_idf" ]; then
    GRAPE_HOSTNAME="$(cat "$_idf")"
    export GRAPE_HOSTNAME
    echo "[entrypoint] engine identity -> GRAPE_HOSTNAME=$GRAPE_HOSTNAME (persisted in $_idf)"
    return 0
  fi
  if [ -n "${RAILWAY_SERVICE_NAME:-}" ]; then
    GRAPE_HOSTNAME="${RAILWAY_SERVICE_NAME}"
    export GRAPE_HOSTNAME
    echo "[entrypoint] engine identity -> GRAPE_HOSTNAME=$GRAPE_HOSTNAME (RAILWAY_SERVICE_NAME; assumes single replica)"
    return 0
  fi
  local _gen=""
  if [ -r /dev/urandom ]; then
    _gen="$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n' || true)"
  fi
  [ -n "$_gen" ] || _gen="$(date +%s)"
  if ! printf 'inst-%s\n' "$_gen" >"$_idf" 2>/dev/null; then
    echo "[entrypoint] WARN: cannot persist instance id under $DATA; generated identity will not survive redeploys (set GRAPE_HOSTNAME or mount a volume)" >&2
  fi
  GRAPE_HOSTNAME="inst-${_gen}"
  export GRAPE_HOSTNAME
  echo "[entrypoint] engine identity -> GRAPE_HOSTNAME=$GRAPE_HOSTNAME (generated)"
}
