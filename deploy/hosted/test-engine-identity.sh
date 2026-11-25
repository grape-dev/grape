#!/usr/bin/env bash
# Behaviour test for the engine-identity derivation chain (explicit env + 3 states).
# Run: bash deploy/hosted/test-engine-identity.sh
set -u

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$_here/engine-identity.sh"

_tmp="$(mktemp -d)"
trap 'rm -rf "$_tmp"' EXIT
_fail=0

_assert_host() {
  local _desc="$1"; local _want="$2"
  if [ "${GRAPE_HOSTNAME:-}" = "$_want" ]; then
    echo "PASS: $_desc -> $GRAPE_HOSTNAME"
  else
    echo "FAIL: $_desc -> want '$_want' got '${GRAPE_HOSTNAME:-}'" >&2
    _fail=1
  fi
}

# State 1: explicit GRAPE_HOSTNAME wins over everything.
GRAPE_HOSTNAME="explicit-id"
DATA="$_tmp/s1"; mkdir -p "$DATA"; printf 'vol-id\n' >"$DATA/instance-id"
RAILWAY_SERVICE_NAME="svc-name"
derive_engine_identity
_assert_host "explicit env wins" "explicit-id"

# State 2: persisted instance-id (mimics a mounted volume) wins over service name.
unset GRAPE_HOSTNAME
DATA="$_tmp/s2"; mkdir -p "$DATA"; printf 'vol-id\n' >"$DATA/instance-id"
RAILWAY_SERVICE_NAME="svc-name"
derive_engine_identity
_assert_host "volume instance-id wins" "vol-id"

# State 3: no instance-id + RAILWAY_SERVICE_NAME -> service name, NO random written.
unset GRAPE_HOSTNAME
DATA="$_tmp/s3"; mkdir -p "$DATA"
RAILWAY_SERVICE_NAME="svc-name"
derive_engine_identity
_assert_host "RAILWAY_SERVICE_NAME fallback (no volume)" "svc-name"
if [ -s "$DATA/instance-id" ]; then
  echo "FAIL: state 3 must not write a random instance-id" >&2; _fail=1
else
  echo "PASS: state 3 wrote no random instance-id"
fi

# State 4: no instance-id + no service name -> generated (last resort).
unset GRAPE_HOSTNAME
unset RAILWAY_SERVICE_NAME
DATA="$_tmp/s4"; mkdir -p "$DATA"
derive_engine_identity
case "${GRAPE_HOSTNAME:-}" in
  inst-*) echo "PASS: last-resort generated -> $GRAPE_HOSTNAME";;
  *) echo "FAIL: expected inst-* got '${GRAPE_HOSTNAME:-}'" >&2; _fail=1;;
esac

echo
if [ "$_fail" != "0" ]; then echo "RESULT: FAIL"; exit 1; else echo "RESULT: PASS"; exit 0; fi
