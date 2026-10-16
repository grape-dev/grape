#!/bin/bash
echo "=== Running all contract checks ==="
./scripts/check-shared-versions.sh
python3 scripts/check-i18n-keys.py
python3 scripts/check-css-vars.py
python3 scripts/check-health-contract.py
# smoke-admin.sh needs a running server (localhost:3000); run manually after deploy
# ./scripts/smoke-admin.sh
echo "=== Contract checks complete ==="
