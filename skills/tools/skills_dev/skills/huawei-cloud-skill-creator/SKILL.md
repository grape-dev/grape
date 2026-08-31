---
name: huawei-cloud-skill-creator
version: 2.1.3
description: |
  1. Six-phase pipeline for creating Huawei Cloud skills — Socratic requirements gathering, CLI→SDK→API research, MD generation, test preparation, detailed testing, and final cleanup & compliance check
  2. Phase-chained dependency: each phase builds on the previous phase's output, no phase may be skipped
  3. Supports CLI, SDK, and REST API execution modes with automatic fallback detection
  4. Generates complete skill directory structure with SKILL.md, references/, scripts/, templates/
  5. Validates against the Huawei Cloud Skill Specification (华为云Skill检查规范)
  Triggers include: "创建华为云Skill","新建华为云Skill","华为云skill创建器","创建 Skill","新建 Skill","skill 创建器","create skill","build skill","new skill","skill creator","scaffold a Huawei Cloud skill","wrap CLI or OpenAPI into a skill","package cloud operations into a skill","帮我创建华为云Skill","帮我新建一个Skill","封装华为云CLI为Skill","华为云Skill脚手架","帮我创建一个skill","我需要一个skill","建一个skill","生成skill","帮我建一个华为云skill".
tags: [huawei-cloud, skill-creator, cli, sdk, devops]
---

# Huawei Cloud Skill Creator v2

> **Six-Phase Strict Pipeline** — Each phase depends on the previous phase's output and cannot be skipped. If any phase is missing, restart from the missing phase.

## Overview

The Huawei Cloud Skill Creator v2 is based on a six-phase strict pipeline: starting with Socratic Q&A requirements analysis, followed by technical research (CLI→SDK→API three-level fallback), document generation, test preparation, detailed testing, resource cleanup and compliance check, ultimately generating a complete skill package that conforms to the Huawei Cloud Skill Specification.

## Prerequisites

1. **hcloud CLI** installed and authenticated — Reference: https://support.huaweicloud.com/qs-hcli/hcli_02_003.html
2. **Python 3.8+** with `huaweicloudsdk` packages available — SDK Reference: https://console.huaweicloud.com/apiexplorer/#/sdkcenter
3. **Node.js + npx** available
4. **Huawei Cloud AK/SK** — 自动扫描所有以 `HUAWEI` / `HW` / `HWC` 开头的环境变量，匹配其中含 `ACCESS_KEY` / `_AK` / `SECRET_KEY` / `_SK` 的键值对
5. **API Reference**: https://console.huaweicloud.com/apiexplorer/#/openapi

### Pre-execution Status Report (Mandatory)

**Before starting Phase 1, report the dev-node status as `processing`.** Run this step as soon as the task begins, so the SKILLS dev-flow pipeline (DB table `t_skills_dev_flow.node_dev_status`) reflects that development is in progress:

```bash
bash ~/.agents/skills/huawei-cloud-skill-creator/scripts/report-dev-processing.sh <multica_issue_id>
```

- `<multica_issue_id>` — the multica issue ID of the current task (e.g. from `multica issue get <id>` or the task context)
- The script updates `t_skills_dev_flow.node_dev_status = 'processing'`, `node_dev_url`, and `node_dev_time` for that issue
- **Do NOT skip this step.** It runs before Phase 1 and is independent of the six-phase pipeline
- If the script reports a warning (e.g. no record found), continue — the pipeline status is best-effort; record the warning in the final report

## Workflow — Six-Phase Strict Pipeline + Webhook Notification

```
Step 0 (Report processing) → Phase 1 (Q&A) → Phase 2 (Tech Research) → Phase 3 (Generate MD)
    → Phase 4 (Test Prep) → Phase 5 (Detailed Testing) → Phase 6 (Cleanup & Report)
    → Step 7a (Report done — direct DB update, MANDATORY) → Phase 7 (Notify Pipeline via Webhook)
```

**Strict Rules:**
- **Step 0**: Before starting Phase 1, run `bash scripts/report-dev-processing.sh <multica_issue_id>` to set `t_skills_dev_flow.node_dev_status = processing`. This step is mandatory and cannot be skipped
- **Step 7a**: After Phase 6 passes, run `bash scripts/report-dev-done.sh <multica_issue_id>` to set `t_skills_dev_flow.node_dev_status = done` directly. This step is mandatory and cannot be skipped — it does not depend on the Phase 7 webhook
- Each phase **must** output a phase summary (phase-N-summary)
- Before starting each phase, **must** verify that the previous phase's summary file exists
- After all 6 phases are completed, perform a **final check** for any missing phases. If any are missing, restart from the missing phase
- Skipping any phase is strictly prohibited

### Phase 1: Requirements Analysis (Socratic Q&A)

**Goal:** Clarify user requirements through question-by-question dialogue.

- Ask **one question** at a time, wait for the user's response
- Cover the following dimensions:
  1. **Target Service** — Which Huawei Cloud service? (ECS, VPC, OBS, RDS, BSS, etc.)
  2. **Feature Scope** — What should the Skill do? (Query, Diagnose, Deploy, Monitor, Manage)
  3. **Execution Mode** — Prefer CLI / SDK / API?
  4. **CLI Operations** — Which operations are involved? (List, Show, Create, Delete, Update)
  5. **Trigger Scenarios** — When would an Agent invoke this? (Daily inspection, troubleshooting, auto-scaling)
- After every 5 questions or covering all dimensions → Display requirements summary table → Wait for user confirmation
- **🛑 Do NOT proceed to Phase 2 until the user has explicitly confirmed**

**Output:** `phase-1-summary.json` — User-confirmed requirements description

### Phase 2: Technical Research (CLI→SDK→API Three-Level Fallback)

**Dependency:** Phase 1 requirements analysis completed (phase-1-summary.json exists)

For each feature point confirmed in Phase 1, research availability in the following order:

| Priority | Research Method | Verification Command | Success Criteria |
|----------|----------------|---------------------|-----------------|
| 1st | **CLI** — hcloud command | `hcloud <Service> <Operation> --cli-region=cn-north-4 --help` | Command exists and parameters are valid |
| 2nd | **SDK** — huaweicloudsdk | `python3 -c "from huaweicloudsdk{service}.v2 import ..."` | SDK package installed and class importable |
| 3rd | **API** — **Only from the following two sources** | See rules below | Endpoint from a trusted source, not inferred |

**Core Rule: API Endpoint Forensics (No Guessing)**

API endpoints are **only allowed** from the following two sources. **Strictly prohibited from inferring through naming patterns**:

| Trusted Source | Method |
|----------------|--------|
| ① **SDK source `_http_info` `resource_path`** | `grep -A8 "_http_info" {service}_client.py` → Read `resource_path` value |
| ② **Huawei Cloud API Explorer** (api-explorer.huaweicloud.com) | User searches and confirms on that website |

**❌ Strictly prohibited actions:**
- Inferring new endpoints based on other API path patterns (e.g., inferring **claim-vouchers** endpoint from **coupons** endpoint)
- Constructing URIs yourself based on documentation descriptions
- Using "common naming patterns" to guess API paths
- If the SDK is available but the corresponding function has no method in `_http_info` → Mark ⛔, do not infer

**Execution Rules:**

```
Research feature point N
  ├── CLI available → Record as CLI mode, record specific command
  ├── CLI unavailable → 🚨 IMMEDIATELY report VOD first (run `bash scripts/report-vod.sh <multica_issue_id> <Service> <API/SDK note> <skill_name>`; idempotent, never blocks) → then Check SDK
  │    ├── SDK available → Record as SDK mode
  │    │    ├── Read all _http_info methods from SDK source to obtain real REST paths
  │    │    │    grep "resource_path" <sdk_path>/{service}_client.py
  │    │    └── Feature point's corresponding method has _http_info in SDK → Record real API endpoint
  │    │         Feature point's corresponding method has no _http_info in SDK → Mark ⛔, do not infer
  │    ├── SDK unavailable → Ask user to confirm endpoint from API Explorer
  │    │    ⏱️ API research time-box: max 5 minutes per feature point. Do NOT loop through repeated searches (API Explorer / hcloud metaRepo / pip index) — repeated hunting burns the iteration budget. If the endpoint is NOT confirmed within 5 minutes → Mark ⛔ API does not exist → go straight to the Phase 2 Failure Gate below
  │    │    ├── User finds endpoint from API Explorer → Record as API mode, note the source
  │    │    ├── User provides endpoint (other source) → Record as API mode, mark ⚠ user-provided
  │    │    └── User cannot provide → Mark ⛔
  │    └── SDK partially available (some methods missing and no corresponding _http_info) → Mark missing features as ⛔
  └── Generate feature point research result (including execution mode + real API path if available)
```

**🛑 Agent is strictly forbidden from guessing/fabricating API paths on its own. If neither the SDK source nor API Explorer has the endpoint, mark it ⛔ — it doesn't exist.**

**Tips for finding SDK client source paths:**
```bash
# Method 1: Find package installation path
python3 -c "import huaweicloudsdk{service}.v2 as m; import os; print(os.path.dirname(m.__file__))"

# Method 2: Find all _http_info methods (show all API endpoints)
grep "_http_info" <path>/{service}_client.py

# Method 3: View API path for a specific method
grep -A8 "_{method}_http_info" <path>/{service}_client.py
# The "resource_path" key in output is the real REST endpoint
```

**Output:** `phase-2-summary.json` — Execution mode (CLI/SDK/API/⛔) and corresponding command/code/API path for each feature point

### 🛑 Phase 2 Failure Gate: No Interface Found → Terminate as Failed

**If the required feature points for the skill CANNOT be found in CLI, SDK, or API (all researched feature points are marked ⛔ — no real endpoint exists in the trusted sources above; or API research exceeded the 5-minute time-box without confirmation), then the task CANNOT proceed. End the work immediately and mark the execution as FAILED:**

1. **Stop the pipeline.** Do NOT continue to Phase 3/4/5/6. Do NOT generate any skill files.
2. **Report dev-node failure.** Run:
   ```bash
   bash ~/.agents/skills/huawei-cloud-skill-creator/scripts/report-dev-failed.sh <multica_issue_id> "<拒绝/失败原因>"
   ```
   - Updates `t_skills_dev_flow.node_dev_status = 'failed'`, `node_dev_url`, `node_dev_time` for that issue
   - Posts the failure/rejection reason as a comment on the linked GitCode issue (if the issue is linked in the DB)
   - If the script warns (e.g. no record found), note it in the failure comment; the DB update is best-effort
3. **Feed back to the multica task that execution failed:**
   - Post a final failure comment explaining that no CLI/SDK/API interface exists for the requested skill:
     ```bash
     multica issue comment add <multica_issue_id> --content "任务执行失败：在 hcloud CLI / huaweicloudsdk SDK / API Explorer 中均未找到可用的接口，无法创建该 skill。<details...>"
     ```
   - Flip the issue status to `blocked`:
     ```bash
     multica issue status <multica_issue_id> blocked
     ```
   - Record the failed outcome in the squad timeline (if running in a squad context):
     ```bash
     multica squad activity <multica_issue_id> failed --reason "No CLI/SDK/API interface found for the requested skill"
     ```
4. **Write `phase-2-summary.json`** listing each feature point and its ⛔ result so the failure is auditable.
5. **End the run.** Do not fabricate commands or endpoints, do not submit a PR.

### Phase 3: Document Generation

**Dependency:** Phase 2 technical research completed (phase-2-summary.json exists)

Generate Skill files based on Phase 2 conclusions:

1. **Name the Skill** — Use `huawei-cloud-{product}-{function}` and make the frontmatter `name` match the directory name.
2. **Language** — Generate SKILL.md in **English** by default. Chinese documentation may be added in `references/` as supplementary. The main SKILL.md must use English for frontmatter description, section titles, command examples, and all explanatory content.
3. **Frontmatter** — Include `name`, `description` with a feature summary and trigger conditions, and no more than five `tags`. Do not generate a `version` field.
4. **Resolve category and create directory structure** — 先运行
   `bash ~/.agents/skills/skill-auto-test-pr/scripts/resolve-skill-category.sh <skill-name>`
   得到 `{category}/{subcategory}`（与 skill-auto-test-pr 共用同一份映射表，保证 dev 创建与测试 PR 落盘路径一致；映射表是分类的唯一权威来源）。
   **强约束: 一级分类必须是 12 个注册分类之一**（computing/container/storage/network/security/monitoring/ai/bigdata/bss/cdi/devtools/solution）；resolve 脚本已内置白名单校验，未注册一级分类（自创如 application、历史遗留 compute）会被强制归入 devtools/common。禁止自创一级分类。
   若输出为 `devtools/common` 且该技能不属于工具类（skill-creator/skill-tester/find-skills/vod 等），说明映射表缺少该服务分支：
   先在 `resolve-skill-category.sh` 中新增 `huawei-cloud-{product}-*) category="{domain}/{service}" ;;` 映射（即新增子分类），再继续。最终目录结构：
   ```text
   skills/{category}/{subcategory}/{skill-name}/
   ├── SKILL.md
   ├── references/
   │   ├── iam-policies.md              (Required)
   │   ├── cli-installation-guide.md    (Required when CLI is used)
   │   ├── verification-method.md       (Recommended)
   │   ├── dataflow-diagram.md          (Recommended)
   │   └── acceptance-criteria.md       (Recommended)
   ├── scripts/
   │   └── test-cli-commands.sh
   └── templates/
       └── test-vars.json
   ```
5. **SKILL.md content generation rules:**

   | Execution Mode | Command Format in SKILL.md |
   |---------------|---------------------------|
   | **CLI** | `hcloud <Service> <Operation> --cli-region={region} [--params]` |
   | **SDK** | Python script example (`python3 -c "..."`) |
   | **API** | curl command + user-provided endpoint (mark as user-provided) |
   | **Unavailable** | Mark `requires manual verification`, do not generate specific commands |

6. **Required sections in SKILL.md:**

   | Section | Severity | Description |
   |---------|----------|-------------|
   | YAML Frontmatter | Critical | `name` + `description` with feature summary and trigger conditions + `tags`; no `version` |
   | Overview | High | Feature overview, architecture, applicable scenarios |
   | Prerequisites | High | CLI version, authentication configuration, IAM permissions |
   | Workflow | High | Skill workflow steps |
   | Core Commands | High | Command examples grouped by function |
   | Parameter Confirmation | High | User-configurable parameter table |
   | Reference Documents | Critical | Links to documents under `references/` |
   | KooCLI Command Format Standard | Low | Required when CLI is involved; service, operation, region, and parameter syntax |

7. **Generate Mermaid data flow diagram** → `references/dataflow-diagram.md`.
8. **Generate IAM policies** → `references/iam-policies.md` using least privilege.
9. **Record API references** — Keep verified API paths in `phase-2-summary.json`. If a generated Skill needs reusable API documentation, add a reference file under `references/` using an allowed kebab-case filename.
10. **Package limits** — Total file content size ≤ 40 MB, total files ≤ 30, and SKILL.md ≤ 500 lines. Split oversized SKILL.md content into `references/`.
11. **File extension allowlist** — Every file must have one of these 46 extensions:
    `.md`, `.mdx`, `.txt`, `.json`, `.json5`, `.yaml`, `.yml`, `.toml`, `.js`, `.cjs`, `.mjs`, `.ts`, `.tsx`, `.jsx`, `.py`, `.sh`, `.ps1`, `.psm1`, `.psd1`, `.r`, `.rb`, `.go`, `.rs`, `.swift`, `.kt`, `.java`, `.cs`, `.cpp`, `.c`, `.h`, `.hpp`, `.sql`, `.csv`, `.tsv`, `.ini`, `.cfg`, `.conf`, `.env`, `.properties`, `.dat`, `.xml`, `.html`, `.css`, `.scss`, `.sass`, `.svg`.
    Files without an extension or outside this allowlist must be removed or renamed.
12. **Change scope** — A pull request must change only one Skill directory. Use `BASE_REF=<base-ref> bash scripts/validate-skill.sh {skill-path}` to validate the PR diff when a base ref is available.

**🛑 Strictly prohibited from generating hallucinated URIs / fabricated API paths. Feature points not verified in Phase 2 must not have specific commands written.**

**Output:** `phase-3-summary.json` — List of generated files and structure validation results

### Phase 4: Test Preparation

**Dependency:** Phase 3 document generation completed (phase-3-summary.json exists)

1. **Generate test cases** — Split test cases based on Phase 2/3 feature points

   | Case Type | Coverage Requirement | Example |
   |-----------|---------------------|---------|
   | CLI cases | One case per hcloud command | `hcloud ECS ListServers --cli-region=cn-north-4 --limit=1` |
   | SDK cases | One case per SDK call | `list_sub_customer_coupons(limit=1)` |
   | API cases | One case per user-provided endpoint | `curl -X GET {endpoint}` |

2. **Save test cases as JSON** → `templates/test-vars.json`

   ```json
   {
     "test_cases": [
       {"id": "TC-01", "name": "...", "command": "...", "expected": "..."},
       ...
     ]
   }
   ```

3. **Show all test cases to the user for confirmation**

4. **Run tests:**
    - Read AK/SK from environment variables: 自动扫描所有以 `HUAWEI` / `HW` / `HWC` 开头的环境变量，匹配其中含 `ACCESS_KEY` / `_AK` / `SECRET_KEY` / `_SK` 的键值对
   - **If not found, must prompt the user to provide AK/SK; if the user does not provide, terminate the process. Strictly prohibited from skipping**
   - Execute test cases one by one
   - **Before executing mutating commands (Create/Update/Delete), must prompt the user and wait for confirmation**

5. **Test verification flow:**

   ```
   Each case → Try CLI execution
     ├── ✅ Success → Record PASS
     └── ❌ Failure → Check syntax issues
          ├── ✅ Syntax issue → Fix and retry
          └── ❌ Non-syntax issue → Fallback to SDK
               ├── ✅ Success → Record PASS (SDK)
               └── ❌ Failure → Fallback to API (user-provided endpoint)
                    ├── ✅ Success → Record PASS (API)
                    └── ❌ Failure → Record FAIL ⛔ requires manual verification
   ```

**Output:** `phase-4-summary.json` — Test case list + per-case execution results

### Phase 5: Detailed Testing

**Dependency:** Phase 4 test preparation completed (phase-4-summary.json exists)

1. **Full regression:** Execute all test cases generated in Phase 4

2. **Resource lifecycle testing** (applicable to Skills involving resource creation/modification/deletion):
   - Create resource → Verify creation succeeded (query to confirm)
   - Runtime query → Verify resource status is correct
   - Destroy resource → Verify resource release
   - Test report outputs information on created/modified/deleted resources
   - **Prompt the user and wait for confirmation before each step**

3. **Management-type Skills**:
   - If CRUD operations are involved → End-to-end full testing
   - If query-only → Output query results to test report

4. **Report generation:**
   - Test results aggregated by case
   - Detailed record of resource changes
   - Detailed error information for failed cases

**Output:** `phase-5-summary.json` — Detailed test results + resource operation records

### Phase 6: Resource Cleanup and Compliance Check

**Dependency:** Phase 5 detailed testing completed (phase-5-summary.json exists)

1. **Resource Cleanup:**
   - Check whether all resources created in Phase 5 have been released
   - Unreleased resources → Prompt user and attempt cleanup
   - Record cleanup results

2. **Huawei Cloud Skill Specification Compliance Check** (against 华为云Skill检查规范):

    | Check Item | Level | Verification Method |
    |-----------|-------|-------------------|
    | SKILL.md exists | Critical | File existence check |
    | Skill directory under skills/ | Low | Path format: skills/{category}/{subcategory}/{skill-name}/ |
    | Skill package naming convention | High | Directory name matches huawei-cloud-{product}-{function} |
    | One PR submits only one Skill | Critical | git diff checks that PR changes only affect a single Skill directory |
    | YAML Frontmatter exists | Critical | `grep '^---$'` |
    | name field exists | Critical | Frontmatter name field exists and matches directory name |
    | description field exists | Critical | Frontmatter description field exists and contains feature summary + trigger words |
    | description includes trigger words | Medium | Accept `Triggers include:`, `Use when`, or equivalent trigger conditions |
    | Should not contain version field | Low | No `version` field in frontmatter |
    | Overview section | High | Match `Overview` or `概述` |
    | Prerequisites section | High | Match `Prerequisites` or `前置条件` |
    | Workflow section | High | Match `Workflow` or `工作流` |
    | Core Commands section | High | Match `Core Commands` or `核心命令` |
    | Parameter Confirmation section | High | Match `Parameter Confirmation` or `参数确认` |
    | Reference Documents section | Critical | Match `Reference Documents`, `References`, or `参考文档` |
    | KooCLI Command Format Standard section | Low | Required when CLI is involved; match the English or Chinese heading |
    | references/cli-installation-guide.md | High | Required when CLI is involved, file existence |
    | references/iam-policies.md | Critical | File existence |
    | references/verification-method.md | Medium | Recommended file existence |
    | references/acceptance-criteria.md | Low | Recommended file existence |
    | Reference document kebab-case naming | Low | File names under references/ are all lowercase kebab-case |
    | Credential hardcoding | Critical | grep for credential hardcoding patterns and CLI credential config |
    | Cross-Skill direct calls | Critical | grep other Skill names |
    | CLI write operations require confirmation | Low | Check whether user confirmation is prompted |
    | Service name requirement | Medium | Every concrete hcloud service matches a KooCLI Service name and starts with uppercase/title case, such as `ECS`, `CloudPond`, or `IAMAccessAnalyzer` |
    | Operation name PascalCase | Medium | Every concrete operation name uses PascalCase |
    | Includes `--cli-region` | Medium | Every concrete CLI command includes the region parameter |
    | Total skill size ≤ 40 MB | Medium | Sum all file content sizes under the Skill directory |
    | Total file count ≤ 30 | Medium | Count SKILL.md and every file in all subdirectories |
    | SKILL.md line count ≤ 500 | Medium | Split excess content into `references/` |
    | File extensions in allowlist | Medium | Reject extensionless files and extensions outside the 46-type allowlist |

   3. **Security Audit:**

   Have the Agent orchestrate the five-tool audit described in `references/security-audit-guide.md`. Do not invoke another named Skill or call scripts from another Skill directory directly.

   The gate combines skillcheck, markdownlint-cli2, the Huawei Cloud specification check, and gitleaks. It must explicitly cover these Critical specification checks:

   | Check | Required coverage |
   |-------|-------------------|
   | Secret leak detection | AK/SK hardcoding, `hcloud configure set`, and report-output masking |
   | Vulnerability pattern detection | Known command injection, reverse shell, dangerous function, and prompt injection patterns |
   | Dependency security detection | Known unsafe dependency versions via `pip audit`, `safety check`, or an equivalent tool |
   | Insecure configuration detection | Insecure protocols, weak passwords, and unsafe defaults |

   ERROR or CRITICAL findings must be fixed and re-audited. WARNING-only results require explicit user acceptance. Record the report path, verdict, findings, and accepted warnings in `phase-6-summary.json`.

4. **Final report:**
   - Merge Phase 1-6 phase summaries
   - Include key conclusions from the security audit report (skill-gate-report)
   - Output complete creation report
   - Mark all incomplete items

5. **Final six-phase completeness check:**

   ```
   Check phase-1-summary.json exists → If missing, restart from Phase 1
   Check phase-2-summary.json exists → If missing, restart from Phase 2
   Check phase-3-summary.json exists → If missing, restart from Phase 3
   Check phase-4-summary.json exists → If missing, restart from Phase 4
   Check phase-5-summary.json exists → If missing, restart from Phase 5
   Check phase-6-summary.json exists → If missing, restart from Phase 6
   ```

   **All phases complete → Creation done. Missing phases → Restart from the missing phase.**

6. **Clean up phase summary files:** After completeness check passes, delete all `phase-*-summary.json` files under the skill directory

   ```bash
    # Execute after final completeness check passes
    # Safety check: ensure skill-path is a legitimate directory under the expected path
    [ -d "{skill-path}" ] && [ -f "{skill-path}/SKILL.md" ] && rm -f {skill-path}/phase-*.json
    echo "✅ phase-1~6-summary.json cleanup complete"
   ```

   **Note:** Only perform cleanup after the completeness check **fully passes**. If there are missing phases, do not clean up; restart from the missing phase.

**Output:** `phase-6-summary.json` — Final creation report + compliance check results + security audit conclusion

### 🛑 Completion Status Report (Mandatory, do not skip)

**After Phase 6 passes (and before or regardless of Phase 7), the agent MUST directly update `node_dev_status = 'done'` in the DB.** Do NOT rely solely on the async webhook pipeline — if the webhook is not triggered or the pipeline is unavailable, the DB status would stay stuck at `processing`.

```bash
bash ~/.agents/skills/huawei-cloud-skill-creator/scripts/report-dev-done.sh <multica_issue_id> <pr_id> <report-file>
```

- `<pr_id>`(可选): PR 编号, 提供后脚本自动 @skills_test 触发测试并写 gitcode_pr_id; `<report-file>`(可选): 结论五件套汇报正文 md, 提供后自动评论携带完整汇报(即完成汇报, 不要再手动发第二条)
- Updates `t_skills_dev_flow.node_dev_status = 'done'`, `node_dev_url`, `node_dev_time` for that issue
- **This step is MANDATORY.** Run it as soon as the skill is created and validated, before posting the final multica issue comment
- If the script warns (e.g. no record found), note it in the final comment; the DB update is best-effort
- This is independent of Phase 7 (webhook). Even if Phase 7 is skipped or fails, this direct DB update guarantees the dev node shows `done`

### 🛑 Output Discipline & Delivery Order (Mandatory, 2026-08-12)

**Purpose:** Prevent output-size-limit truncation that has repeatedly broken final delivery (SKI-49/50/51 real incidents).

1. **Delivery FIRST, report LAST:** `git push` + create PR + post a one-line "PR submitted: <url>" comment **BEFORE** any detailed summary. Even if the final summary is truncated, the PR must already exist.
2. **Final summary must be SHORT (≤600 chars):** state what was fixed (file groups + counts), verification result as "X/Y passed, failures: ...". **NEVER** paste full logs, full compile output, full diffs, or full file lists into the final comment.
3. **Long outputs go to files:** redirect compile/verify output to /tmp/*.log and quote only key lines (grep summaries).
4. **Single text messages ≤2000 chars:** split or compress anything longer.
5. If you hit the output limit anyway, make sure step 1 was already done — the PR is what matters.


6. **NO process narration in text:** never emit "Now let me... / Let me check... / Let me verify..." style narration as text messages — that belongs in thinking. text messages are ONLY for: tool calls, brief one-line intermediate conclusions, and the final delivery report. (SKI-51: 99 narration messages = 8393 chars with 0 delivery report -> auto-publish truncated; without narration the final output is a few hundred chars.)

7. **完成汇报 = report-dev-done.sh 自动评论 (方案A, SKI-55)，禁止再手动发第二条完成汇报评论:** 开发完成(PR 已提交)后运行 `report-dev-done.sh <multica_issue_id> <pr_id> <report-file>` — 脚本更新 DB 并在 issue 自动发布一条评论: REPORT_FILE(结论五件套, 规则9) 完整汇报正文在前 + @skills_test 触发在后, 该评论即完成汇报。**不要再手动 post 第二条完成汇报评论** (SKI-55 教训: 脚本自动评论 + agent 手动汇报 = 双评论刷屏)。**NEVER rely on the run-end automatic output publishing** — it has a size limit; on overflow it posts only a placeholder and the report/trigger is silently lost (SKI-49/50/51 verified). 唯一例外: 需要长报告附件(规则8)时, 补发一条附件评论 `multica issue comment add <issue-id> --content "<一行摘要>" --attachment <file>` (不重复正文)。

8. **Long reports go into a .md ATTACHMENT, not the comment body:** when a detailed report is required (full test output, file-by-file change list, verification logs), write it to `<workdir>/report-<skill>.md` and attach it to the completion comment: `multica issue comment add <issue-id> --content "<short summary>" --attachment <workdir>/report-<skill>.md`. The comment BODY stays short (≤1KB); the attachment carries unlimited detail and renders as a downloadable card (verified 2026-08-13: 990KB .md attachments accepted). This bypasses the auto-publish size limit entirely — the body never approaches the threshold.

9. **Comment body = "conclusion five-piece set" only:** (1) conclusion (done/failed), (2) PR/link, (3) what changed (3-5 bullets), (4) verification result (X/Y passed), (5) status transitions (DB node / issue status). Anything longer → attachment per rule 8.
### Phase 7: Notify Pipeline (Webhook Trigger)

**Dependency:** Phase 6 compliance check passed (phase-6-summary.json exists) and phase summary files cleaned up

**Goal:** Notify the `skill-auto-test-pr` autopilot that a new skill has been created, triggering automated testing + PR submission.

1. **Determine skill_name:** Extract the skill directory name created in Phase 3 (`skills/{skill-name}/`). The `name` field in SKILL.md frontmatter must match the directory name.

2. **Determine multica_issue_id:** The ID of the current task's issue (the issue that requested this skill creation). In the agent runtime it is available as the assigned issue ID (e.g. from `multica issue get <id>` or the task context).

3. **POST webhook:** After all six phases complete and the final completeness check passes, call the autopilot webhook with the skill name AND the multica issue ID:

   ```bash
   curl -s -o /dev/null -w "%{http_code}" -X POST \
     "https://multica.clouddeveloper.club/api/webhooks/autopilots/awt_5ymBLU9qY2Gkzx8kdbBUieX6FJg0W5LV8ltZYPYYMxU" \
     -H "Content-Type: application/json" \
     -d '{"event":"skill-created","skill_name":"<skill-name>","multica_issue_id":"<issue-id>","timestamp":"<UTC ISO8601>"}'
   ```

   - HTTP 2xx → Pipeline triggered successfully
   - Non-2xx → Report the error; the pipeline can still be triggered manually later via `bash ~/.agents/skills/skill-auto-test-pr/scripts/process-skill.sh <skill-name>`

4. **Do NOT** copy/move the skill into `~/.agents/skills/` yourself — the pipeline reads the skill content from the git checkout. Local `~/.agents/skills/` installation is optional and independent of this pipeline.

**Output:** Webhook HTTP status — confirms the `skill-auto-test-pr` pipeline has been triggered for the new skill.

## KooCLI Command Format Standard

```bash
hcloud <Service> <Operation> --cli-region=<region> [--key=value ...]
```

| Feature | Description | Example |
|---------|-------------|---------|
| Service name | Exact KooCLI Service name beginning with uppercase/title case | `ECS`, `VPC`, `CloudPond`, `IAMAccessAnalyzer` |
| Operation name | PascalCase | `ListServers`, `ShowServer` |
| Region parameter | `--cli-region=<value>` | `--cli-region=cn-north-4` |
| Simple parameter | `--key=value` | `--server_id=xxx` |
| Indexed parameter | `--key.1=value1` | `--servers.1.id=xxx` |

## Core Commands

| Command | Purpose |
|---------|---------|
| `bash scripts/validate-skill.sh {path}` | Phase 3/6: Structure and Huawei Cloud specification validation |
| `bash scripts/test-cli-commands.sh {path} --executor {cli\|sdk\|api}` | Phase 4/5: Functional testing |

## Parameter Confirmation

| Parameter | Required | Description | Example |
|-----------|----------|-------------|---------|
| `{skill-path}` | Yes | Target Skill directory path | e.g., huawei-cloud-ecs-manage |
| `{region}` | No | Huawei Cloud region | `cn-north-4` |
| `{executor}` | No | Execution mode (cli/sdk/api) | `cli` |

## Edge Cases

| Scenario | Handling |
|----------|----------|
| User skips questions and says "start" directly | Remind: requirements analysis must be completed first, start from Phase 1 questions |
| AK/SK environment variables not set | Prompt user to provide AK/SK; if user does not provide, terminate process, strictly prohibited from skipping |
| Target service not supported by hcloud CLI | **IMMEDIATELY report VOD** (`bash scripts/report-vod.sh <multica_issue_id> <Service> <API/SDK note> <skill_name>`) → then fallback to SDK → Read SDK source _http_info → If still not found, mark ⛔ |
| API research takes > 5 minutes without confirming an endpoint | **Do NOT loop** (no repeated API Explorer / metaRepo / pip searches). Mark ⛔ API does not exist → Phase 2 Failure Gate (report-dev-failed.sh + multica feedback + blocked) |
| SDK package does not exist | Check package name variants, if still not found, inform user, do not infer API |
| User is unsure of API endpoint | Mark ⛔ requires manual verification, do not fabricate endpoints. If SDK has the method, read _http_info for the real path |
| SDK has method but _http_info has no resource_path | Mark ⛔, this API does not exist in the SDK, do not infer |
| Attempting to infer API via path pattern (e.g., inferring claim-vouchers from coupons) | ❌ Strictly prohibited. It doesn't exist |
| **All feature points marked ⛔ (no CLI/SDK/API interface exists)** | **Phase 2 Failure Gate: terminate immediately, run `report-dev-failed.sh <issue_id>`, post failure comment, set issue to `blocked`, record `squad activity failed`, end run — do NOT generate files or submit a PR** |
| Resource creation test fails | Analyze error cause (permissions/quota/parameters) → Fix and retry |
| Resource release fails | Retry 3 times, if still failing, inform user to clean up manually |
| User refuses resource lifecycle testing | Inform user: resource lifecycle testing is a required step and cannot be skipped; if user still refuses, terminate process |
| Phase 6 finds missing phases | Restart from the missing phase until all 6 phases are complete |
| SDK has method but actual API path unknown | Read SDK source `grep _http_info {service}_client.py` to get real path |
| BSS service SDK initialization fails (GlobalCredentials) | BSS is global and must use `GlobalCredentials` with `with_endpoints`, not `BasicCredentials` with `with_region` |
| list_sub_customer_coupons query returns 400 | BSS limit parameter maximum is 100, not the default 200 |
| Phase 6 security audit FAIL | Fix issues from the audit report, then have the Agent rerun the audit until it passes |
| gitleaks false positive | Add to `.gitleaksignore` file |

## Verification Method

### Specification Compliance Verification
```bash
bash scripts/validate-skill.sh {skill-path}
# Check against 华为云Skill检查规范 item by item
```

### Functional Testing
```bash
bash scripts/test-cli-commands.sh {skill-path} --executor cli   # CLI priority
bash scripts/test-cli-commands.sh {skill-path} --executor sdk   # SDK fallback
bash scripts/test-cli-commands.sh {skill-path} --executor api   # API fallback
```

### Six-Phase Completeness Check
```
Final verification: Check whether phase-1-summary.json ~ phase-6-summary.json exist
All exist ✅ → Creation complete
Missing any ❌ → Restart from the missing phase
```

### Security Audit (Phase 6)

Have the Agent orchestrate the tools listed in `references/security-audit-guide.md`, collect their findings into the Phase 6 report, fix every ERROR/CRITICAL issue, and repeat until the gate passes. Do not call another Skill's scripts directly.

## Reference Documents

- `references/cli-installation-guide.md` — CLI installation and configuration
- `references/iam-policies.md` — Least-privilege IAM policies
- `references/verification-method.md` — Verification method details
- `references/dataflow-diagram.md` — Mermaid data flow diagram
- `references/acceptance-criteria.md` — Acceptance criteria
- `references/related-commands.md` — Command quick reference
- `references/security-audit-guide.md` — Phase 6 five-tool security audit and remediation guide

## Best Practices

- During Phase 1 requirements analysis, try to cover all functional dimensions to avoid rework in later phases
- In Phase 2 technical research, prioritize CLI, then SDK, and API last; do not use SDK when CLI is available
- **If CLI is NOT available for the target service, IMMEDIATELY report VOD** (`bash scripts/report-vod.sh <multica_issue_id> <Service> <API/SDK note> <skill_name>`) BEFORE checking SDK/API — do not defer the report to after research
- **API research is time-boxed to 5 minutes per feature point; on timeout mark ⛔ API does not exist and go to the Phase 2 Failure Gate — never loop repeated API searches** (repeated hunting burned the iteration budget in SKI-28/SKI-35)
- In Phase 2, read SDK `_http_info` to get real API paths; strictly prohibited from inferring
- In Phase 4/5 testing, mutating operations (Create/Update/Delete) must be confirmed by the user before execution
- If Phase 6 compliance check fails, fix the issues first, then re-verify; do not skip

## Notes

- Six-phase pipeline strictly follows sequential order; no phase may be skipped
- API endpoints are only allowed from SDK source `_http_info` or Huawei Cloud API Explorer; strictly prohibited from inferring via naming patterns
- **If CLI is NOT available for the target service, IMMEDIATELY report VOD** (`bash scripts/report-vod.sh <multica_issue_id> <Service> <API/SDK note> <skill_name>`) before checking SDK/API; the script is idempotent (skips if an open issue exists) and never blocks research
- **API research is time-boxed to 5 minutes per feature point; on timeout mark ⛔ API does not exist and go to the Phase 2 Failure Gate — never loop repeated API searches** (repeated hunting burned the iteration budget in SKI-28/SKI-35)
- **If Phase 2 finds NO CLI/SDK/API interface for the required feature points, the task FAILS: terminate immediately, report `node_dev_status=failed` via `report-dev-failed.sh <issue_id>`, post a failure comment, set the issue to `blocked`, record `squad activity failed`, and end the run — do NOT generate skill files or submit a PR**
- **On successful completion (after Phase 6), MANDATORY to run `report-dev-done.sh <issue_id>` to set `node_dev_status=done` directly — do NOT rely on the Phase 7 webhook to update the DB; a skipped/failed webhook must not leave the status stuck at `processing`**
- Credentials (AK/SK) are read from environment variables; hardcoding in scripts or documents is prohibited
- **If AK/SK is not set, must prompt the user to provide them; if the user does not provide, terminate the process. Strictly prohibited from skipping any step that requires credentials**
- BSS service SDK must use GlobalCredentials + with_endpoints; BasicCredentials must not be used
- Resources created during resource lifecycle testing must be cleaned up in Phase 6 to avoid leftovers
- When the Phase 6 security audit fails, CRITICAL/ERROR level issues must be fixed and the Agent must rerun the audit
- The skillPath in skills-lock.json is: skills/devtools/common/huawei-cloud-skill-creator/SKILL.md

## Design Principles

- **Six-Phase Strict Pipeline** — Phases are chain-dependent and cannot be skipped
- **Phase 2 No API Inference** — API endpoints only from SDK source `_http_info` or Huawei Cloud API Explorer; strictly prohibited from guessing via naming patterns
- **Phase 2 Failure Gate** — No CLI/SDK/API interface for the required features (API research > 5 min time-box = API does not exist) → terminate as FAILED: report `node_dev_status=failed` via `report-dev-failed.sh`, feedback failure to multica (comment + status blocked + squad activity failed), never fabricate commands/endpoints or submit a PR
- **Direct DB Reporting** — The agent directly writes `node_dev_status` to the DB (`processing` at Step 0, `done` at completion, `failed` on failure); the Phase 7 webhook is a parallel notification and must not be the only path that updates the dev-node status
- **Phase 3 Generate Based on Facts** — CLI commands / SDK scripts / API endpoints generated per Phase 2 conclusions; no endpoint → mark ⛔
- **Phase 4/5 Real Execution** — Every command must be actually executed and verified; if it fails, fallback or mark
- **Phase 6 Double Check** — Resource cleanup + specification compliance + six-phase completeness
- **Credential Security** — No hardcoded AK/SK, read from environment variables, write operations require user confirmation
- **Credentials Mandatory** — If AK/SK is missing, must prompt the user to provide; if not provided, terminate process. Strictly prohibited from skipping
- **Least Privilege** — iam-policies.md provides least-privilege policy JSON
