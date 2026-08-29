#!/usr/bin/env bash
cd "/home/mys/grape"
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$HOME/.local/npm-global/bin:$PATH"
set -a; . "/home/mys/.grape/runtime.env"; set +a
exec "/home/mys/grape/.venv/bin/python" -m src.runtime.agent
