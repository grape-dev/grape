---
name: huaweicloud-capability-discovery
description: Discover Huawei Cloud capabilities for an agent task. Use when the user asks what Huawei Cloud service, Skill, CLI command, API, SDK, or document should be used, or when a request is scenario-shaped and needs capability selection before implementation.
---

# Huawei Cloud Capability Discovery


**STOP - Do not answer from general knowledge.** Follow the procedure below.

Use this skill to turn vague developer intent into a precise Huawei Cloud capability path.

## Workflow

1. Classify the goal: application hosting, compute, container, serverless, storage, database, network, security, observability, AI, data, migration, cost, or troubleshooting.
2. Search Huawei Cloud Skills first when the task is scenario-based or operational.
3. Use official docs for exact service limits, API request/response fields, regions, and pricing-sensitive behavior.
4. Prefer KooCLI for local read-only discovery after auth is configured.
5. Before using KooCLI, discover exact operation names with `hcloud <Service> --help`; do not guess from intuition. Examples: list ECS instances with `ECS ListServersDetails`, create ECS with `ECS CreateServers`, inspect images through `IMS GlanceShowImage`.
6. Prefer SDK docs when the deliverable is application code.
7. Prefer MCP only when an approved Huawei Cloud MCP tool exists for the needed operation.
8. Treat Terraform as a secondary V1 path for reviewed IaC, not the default.
9. When no built-in devkit skill matches, browse the community skill marketplace at https://github.com/huaweicloud/huaweicloud-skills. Fetch the index from https://raw.githubusercontent.com/huaweicloud/huaweicloud-skills/master/skills-index/index.json.

## Region Intent

- Extract region intent from the user's words before querying. Examples: Singapore -> `ap-southeast-3` first, Hong Kong -> `ap-southeast-2`, Beijing -> `cn-north-4`, Shanghai -> `cn-east-3`.
- Target operation region wins over reference-resource uncertainty. If the user says "buy ECS in Singapore, reference SSY", inspect Singapore first.
- No blind all-region scans. If the reference resource location is unknown and the target region is not enough, ask the user for the region instead of scanning unrelated regions.
- For unfamiliar names, check Huawei Cloud API Explorer or official endpoint documentation for the region and service endpoint mapping.

## Output Format

When routing a task, return:

- `intent`: one concise sentence
- `recommended_path`: Skills, CLI, API, SDK, MCP, or Terraform
- `why`: why this path is shortest and safest
- `next_action`: exact next command, document lookup, or code file to inspect
- `risk`: read-only, write, secret, permission, cost, or public exposure

## Avoid

- Do not guess a service when Huawei Cloud Skills or official docs should be checked.
- Do not load many service references. Pick the next one that matches the intent.
- Do not propose write operations before the user has seen the plan.
- Do not query every region just to find a reference resource.
