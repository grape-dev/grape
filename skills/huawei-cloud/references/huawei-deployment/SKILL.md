---
name: huawei-deployment
description: "Use when creating, managing, or running deployment tasks and pipelines on Huawei Cloud CloudDeploy. Triggers: CloudDeploy, deployment, CI/CD, pipeline, release, artifact deployment, deploy task. NOT for: CodeArts Build (build pipeline), SWR container registry."
version: 1
---

# Huawei Cloud CloudDeploy

**STOP - Do not answer from general knowledge.** Follow the procedure below.

Always run `hcloud <Service> <Operation> --help` before constructing commands to discover exact parameter names and requirements.

## Overview

Domain expertise for Huawei Cloud CloudDeploy. Covers application creation, deployment task management, pipeline execution, and artifact configuration.

## Critical Warnings

| Trap | Why |
|------|-----|
| Service name may be `CodeArtsDeploy` | hcloud service name for deployment may be `CodeArtsDeploy` instead of `CloudDeploy`. Run `hcloud --help` to verify |
| Deployment hosts need agent | Install CloudDeploy agent on target hosts first |
| Task must reference application first | Create application before task |
| Artifact source defaults to OBS | Most deployment tasks pull artifacts from OBS. Ensure bucket and object exist |
| Parallel deployments may conflict | Lock resources or use deployment groups |

## Common Workflows

| Task | Operation |
|------|-----------|
| Create application | `CreateApp --name=<n> --platform=<p> --cli-region=<r>` |
| Create deployment task | `CreateTask --name=<n> --app_id=<id> --artifact_source_type=OBS` |
| Start deployment | `StartTask --task_id=<id>` |
| List tasks | `ListTasks --app_id=<id>` |
| Delete task | `DeleteTask --task_id=<id>` |

## Troubleshooting

| Error | Fix |
|-------|-----|
| Agent offline | Check agent service on target host, network connectivity, firewall rules |
| Deployment timeout | Check artifact size, increase task timeout, verify target host resources |
| Artifact not found | Verify OBS bucket and object path, check artifact permissions |
| Permission denied | Verify IAM roles for deployment: `CodeArtsDeploy FullAccess` or custom policy |

## Security

- MUST use IAM roles for deployment permissions
- MUST verify artifact integrity before deployment
- MUST not store credentials in deployment scripts

## Cross-Skill References

- **OBS artifact storage**: See `huawei-obs`
- **ECS deployment target**: See `huawei-ecs`
- **IAM permissions**: See `huawei-iam`
