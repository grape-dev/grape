#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    echo "Usage: $0 <PR_URL> [OUTPUT_DIR]"
    echo ""
    echo "  PR_URL      GitCode PR URL, e.g. https://gitcode.com/owner/repo/pull/123"
    echo "  OUTPUT_DIR  Local directory to copy the skill into (default: current directory)"
    echo ""
    echo "Examples:"
    echo "  $0 https://gitcode.com/developer-skill/huaweicloud-skills/pull/277"
    echo "  $0 https://gitcode.com/developer-skill/huaweicloud-skills/pull/277 ./my-skills"
    exit 1
}

if [[ $# -lt 1 ]]; then
    usage
fi

PR_URL="$1"
OUTPUT_DIR="${2:-.}"

if [[ "$PR_URL" =~ ^https://gitcode\.com/([^/]+)/([^/]+)/pull/([0-9]+)$ ]]; then
    OWNER="${BASH_REMATCH[1]}"
    REPO="${BASH_REMATCH[2]}"
    PR_NUM="${BASH_REMATCH[3]}"
elif [[ "$PR_URL" =~ ^https://gitcode\.com/([^/]+)/([^/]+)/merge_requests/([0-9]+)$ ]]; then
    OWNER="${BASH_REMATCH[1]}"
    REPO="${BASH_REMATCH[2]}"
    PR_NUM="${BASH_REMATCH[3]}"
else
    echo "Error: Cannot parse PR URL: $PR_URL"
    echo "Expected format: https://gitcode.com/<owner>/<repo>/pull/<number>"
    exit 1
fi

REPO_URL="https://gitcode.com/${OWNER}/${REPO}.git"
BRANCH_NAME="pr-${PR_NUM}"
WORK_DIR="/tmp/fetch-pr-skill/${OWNER}_${REPO}"

echo "==> Parsed PR URL:"
echo "    Owner:  ${OWNER}"
echo "    Repo:   ${REPO}"
echo "    PR #:   ${PR_NUM}"
echo ""

if [[ -d "${WORK_DIR}" ]]; then
    echo "==> Repo already cloned at ${WORK_DIR}, fetching updates..."
    cd "${WORK_DIR}"
    git fetch origin 2>&1
else
    echo "==> Cloning repo ${REPO_URL}..."
    mkdir -p "$(dirname "${WORK_DIR}")"
    git clone "${REPO_URL}" "${WORK_DIR}" 2>&1
    cd "${WORK_DIR}"
fi

echo ""
echo "==> Fetching PR #${PR_NUM} branch (refs/merge-requests/${PR_NUM}/head)..."
if git branch --list "${BRANCH_NAME}" | grep -q "${BRANCH_NAME}"; then
    git checkout main 2>/dev/null || git checkout master 2>/dev/null
    git branch -D "${BRANCH_NAME}" 2>/dev/null
fi
if git fetch origin "refs/merge-requests/${PR_NUM}/head:${BRANCH_NAME}" 2>&1; then
    echo "    Fetched successfully."
else
    echo "Error: Failed to fetch PR #${PR_NUM}. The PR may not exist or the repo may require authentication."
    exit 1
fi

echo ""
echo "==> Checking out PR branch ${BRANCH_NAME}..."
git checkout "${BRANCH_NAME}" 2>&1

echo ""
echo "==> Detecting changed files in PR..."
MAIN_BRANCH=$(git remote show origin 2>/dev/null | grep 'HEAD branch' | awk '{print $NF}' || echo "main")
if [[ -z "${MAIN_BRANCH}" || "${MAIN_BRANCH}" == "(unknown)" ]]; then
    MAIN_BRANCH="main"
fi

CHANGED_FILES=$(git diff "origin/${MAIN_BRANCH}...HEAD" --name-only 2>/dev/null || git diff "HEAD~1...HEAD" --name-only 2>/dev/null)

if [[ -z "${CHANGED_FILES}" ]]; then
    echo "Warning: No changed files detected via diff. Listing all files in repo..."
    CHANGED_FILES=$(git ls-files)
fi

echo "${CHANGED_FILES}"
echo ""

SKILL_DIRS=""
while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    if [[ "$f" == *"SKILL.md" ]]; then
        SKILL_DIRS="${SKILL_DIRS}${SKILL_DIRS:+$'\n'}$(dirname "$f")"
    fi
done < <(echo "${CHANGED_FILES}")
SKILL_DIRS=$(echo "$SKILL_DIRS" | sort -u)

if [[ -z "${SKILL_DIRS}" ]]; then
    echo "Warning: No SKILL.md directly changed. Inferring skill directories from changed file paths..."
    set +eo pipefail
    SKILL_DIRS=""
    while IFS= read -r f; do
        [[ -z "${f}" ]] && continue
        current="$f"
        while [[ "${current}" != "." && "${current}" != "" ]]; do
            if [[ -f "${WORK_DIR}/${current}/SKILL.md" ]]; then
                SKILL_DIRS="${SKILL_DIRS}${SKILL_DIRS:+$'\n'}${current}"
                break
            fi
            current=$(dirname "${current}")
        done
    done < <(echo "${CHANGED_FILES}")
    SKILL_DIRS=$(echo "$SKILL_DIRS" | sort -u)
    set -eo pipefail
fi

if [[ -z "${SKILL_DIRS}" ]]; then
    echo "Warning: Could not infer skill dirs from changed files. Searching entire repo..."
    SKILL_DIRS=$(find . -name "SKILL.md" -not -path "./.git/*" | while read -r f; do
        dirname "$f" | sed 's|^\./||'
    done | sort -u || true)
fi

if [[ -z "${SKILL_DIRS}" ]]; then
    echo "Error: No skill directories found in this PR/repo."
    exit 1
fi

echo "==> Found skill directories:"
echo "${SKILL_DIRS}" | while read -r d; do echo "    - ${d}"; done
echo ""

mkdir -p "${OUTPUT_DIR}"

COPIED_COUNT=0
while IFS= read -r SKILL_DIR; do
    if [[ -z "${SKILL_DIR}" ]]; then
        continue
    fi

    SKILL_NAME=$(basename "${SKILL_DIR}")
    TARGET_DIR="${OUTPUT_DIR}/${SKILL_NAME}"

    echo "==> Copying complete skill: ${SKILL_DIR} -> ${TARGET_DIR}"
    mkdir -p "${TARGET_DIR}"

    if [[ -d "${WORK_DIR}/${SKILL_DIR}" ]]; then
        cp -r "${WORK_DIR}/${SKILL_DIR}/." "${TARGET_DIR}/"
        FILE_COUNT=$(find "${TARGET_DIR}" -type f | wc -l)
        echo "    Copied ${FILE_COUNT} files."
        COPIED_COUNT=$((COPIED_COUNT + 1))
    else
        echo "    Warning: Directory ${WORK_DIR}/${SKILL_DIR} not found on PR branch."
    fi
done < <(echo "${SKILL_DIRS}")

echo ""
echo "==> Done! Skills copied to: ${OUTPUT_DIR}"
echo ""

RUN_SCRIPT="${SCRIPT_DIR}/run_skill.sh"
if [[ -f "${RUN_SCRIPT}" ]]; then
while IFS= read -r SKILL_DIR; do
    if [[ -z "${SKILL_DIR}" ]]; then
        continue
    fi
    SKILL_NAME=$(basename "${SKILL_DIR}")
    TARGET_DIR="${OUTPUT_DIR}/${SKILL_NAME}"
    echo "==> Auto-running skill: ${SKILL_NAME}"
    bash "${RUN_SCRIPT}" "${TARGET_DIR}" --timeout 300 --pr-url "${PR_URL}" 2>&1 || echo "    Warning: Run failed for ${SKILL_NAME}"
done < <(echo "${SKILL_DIRS}")
else
    echo "    run_skill.sh not found, skipping auto-run"
    echo "    You can manually run: bash ${SCRIPT_DIR}/run_skill.sh <skill_path>"
fi
