#!/usr/bin/env bash
set -euo pipefail

adapter_run() {
    local skill_path="$1"
    local session_dir="$2"
    local input="${ADAPTER_INPUT:-}"

    local skill_md="${skill_path}/SKILL.md"
    if [[ ! -f "${skill_md}" ]]; then
        echo "Error: SKILL.md not found at ${skill_md}" > "${session_dir}/output.log"
        return 1
    fi

    local skill_name=$(basename "${skill_path}")

    if command -v opencode &>/dev/null; then
        local skill_content
        skill_content=$(cat "${skill_md}")
        local prompt
        if [[ "${skill_name}" == "huawei-cloud-find-skills" ]]; then
            prompt="Execute the following skill. Execute ONLY search and query operations (Steps 0-2). DO NOT execute any install, write, or modify operations (skip Step 3)."
        else
            prompt="Execute the following skill. You MUST execute ALL workflow steps, do not skip any step. Complete every single step from start to finish."
        fi
        prompt="${prompt}

--- SKILL DEFINITION ---
${skill_content}
--- END SKILL DEFINITION ---"
        if [[ -n "${input}" ]]; then
            prompt="${prompt} User request: ${input}"
        fi
        opencode run "${prompt}" \
            --dir "${skill_path}" \
            --auto \
            --print-logs \
            >> "${session_dir}/output.log" 2>&1
        return $?
    else
        echo "Error: 'opencode' CLI not found. Install it first: https://opencode.ai" \
            > "${session_dir}/output.log"
        return 127
    fi
}
