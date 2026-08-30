#!/usr/bin/env bash
# =============================================================================
# Adapter Interface Contract
# =============================================================================
#
# This file documents the contract that every adapter MUST implement.
# It is NOT executable logic — it serves as a reference specification.
#
# -----------------------------------------------------------------------------
# adapter_run()
# -----------------------------------------------------------------------------
#
# Every adapter MUST define a function named `adapter_run`.
#
#   adapter_run() {
#       local skill_path="$1"   # Absolute path to the skill directory (contains SKILL.md)
#       local session_dir="$2"  # Absolute path to the isolated session directory
#       ...
#   }
#
# Parameters:
#   $1  skill_path  — Absolute path to the skill directory that contains SKILL.md.
#   $2  session_dir — Absolute path to the session directory created by session.sh.
#                      All output and artifacts MUST be written here.
#
# Available Environment Variables:
#   ADAPTER_INPUT   — The user prompt / input string to pass to the agent.
#                      May be empty if no prompt is provided.
#   ADAPTER_TIMEOUT — Maximum seconds the agent is allowed to run.
#                      Adapters SHOULD enforce this timeout if possible.
#
# Contract:
#   1. MUST write combined stdout + stderr to  $session_dir/output.log
#   2. MUST return the agent's exit code (0 = success, non-zero = failure)
#   3. MUST NOT modify any files outside of $session_dir
#   4. SHOULD respect ADAPTER_TIMEOUT and terminate the agent if exceeded
#   5. SHOULD validate that $skill_path/SKILL.md exists before running
#
# -----------------------------------------------------------------------------
# How to Add a New Agent
# -----------------------------------------------------------------------------
#
# 1. Create a new file:  scripts/adapters/<agent_name>.sh
# 2. Implement the `adapter_run()` function following the contract above.
# 3. Users select the adapter via:  run-with-session.sh --agent <agent_name>
# 4. The runner sources the adapter file and calls adapter_run().
#
# =============================================================================
