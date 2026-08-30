#!/usr/bin/env bash
# Dummy adapter: extracts and runs bash code blocks from SKILL.md
# Useful for testing the pipeline without opencode

adapter_run() {
    local skill_path="$1"
    local session_dir="$2"
    local skill_md="${skill_path}/SKILL.md"

    if [[ ! -f "$skill_md" ]]; then
        echo "Error: SKILL.md not found at $skill_md" > "${session_dir}/output.log"
        return 1
    fi

    echo "==> Dummy adapter: executing bash blocks from SKILL.md" >> "${session_dir}/output.log"

    local in_block=false
    local block_lang=""
    while IFS= read -r line; do
        if [[ "$line" =~ ^'```'([a-zA-Z]*) ]]; then
            if [[ "$in_block" == "true" ]]; then
                in_block=false
            else
                in_block=true
                block_lang="${BASH_REMATCH[1]}"
            fi
            continue
        fi
        if [[ "$in_block" == "true" && "$block_lang" == "bash" ]]; then
            echo "$ $line" >> "${session_dir}/output.log"
            eval "$line" >> "${session_dir}/output.log" 2>&1 || echo "    (exit: $?)" >> "${session_dir}/output.log"
        fi
    done < "$skill_md"

    echo "==> Dummy adapter: done" >> "${session_dir}/output.log"
    return 0
}
