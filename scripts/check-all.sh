#!/bin/bash
echo "=== Running all contract checks ==="
./scripts/check-shared-versions.sh
python3 scripts/check-i18n-keys.py
python3 scripts/check-css-vars.py
python3 scripts/check-health-contract.py
python3 scripts/check-secrets.py
# check-doc-redaction.py 是针对开源清洗分支（weekly-07-clean）的 docs 门禁：
# 内部周分支的 docs 合法保留内部信息，直接跑会整片报错，故只作开源合入前手工门禁：
#   python3 scripts/check-doc-redaction.py
# smoke-admin.sh needs a running server (localhost:3000); run manually after deploy
# ./scripts/smoke-admin.sh
echo "=== Contract checks complete ==="
