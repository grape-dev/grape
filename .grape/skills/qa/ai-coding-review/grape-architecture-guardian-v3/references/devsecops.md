# Supply Chain and DevSecOps

`A03:2025` Software Supply Chain Failures moved up the OWASP Top 10 and broadened beyond "vulnerable and outdated components". The dependency tree, the build pipeline and the CI configuration are all part of the attack surface.

Reference NIST SSDF (SP 800-218 v1.1, final) for lifecycle practices. See `references/standards.md`.

## Dependencies

Check: a lockfile exists and is committed, dependencies are pinned appropriately for the ecosystem, known CVEs in direct and transitive dependencies, dependencies that are unmaintained (last release, open issue backlog, single maintainer), the transitive tree depth, package integrity verification, typosquatting risk in recently added packages, private registry configuration where used, and packages that are installed but unused.

Run `scripts/dependency_inventory.py` for the mechanical part, then judge what it surfaces.

Unused dependencies are worth removing: each one is attack surface, install time and a future upgrade obligation, in exchange for nothing.

A lockfile that is generated but gitignored provides no reproducibility. Check the actual git status rather than the file's presence on disk.

## Build pipeline

The pipeline has production credentials, which makes it production infrastructure regardless of how it is treated organisationally.

Check: CI actions and workflows pinned to a commit SHA rather than a mutable tag, since a tag can be moved to point at new code; the permissions granted to the CI token, which frequently default to more than the job needs; whether secrets are exposed to workflows triggered by external contributors, which is the standard exfiltration path in public repositories; whether build steps execute arbitrary code from dependencies during install; and whether the build is reproducible.

Container images: check the base image, its age and patch level, whether a specific digest is pinned, whether the container runs as root, whether build secrets leak into image layers, and the final image size as a proxy for unnecessary surface.

## Secrets in CI

Check: secrets stored in the platform's secret store rather than in workflow files, scoping per environment, whether pull requests from forks can reach them, rotation, and whether secrets appear in build logs. Masking is not a substitute for not printing them.

## Security gates

Which automated controls exist. Justify each gate by risk profile rather than adding a uniform pipeline to every project.

| Gate | Purpose |
|---|---|
| Unit and integration tests | Correctness regression |
| Contract tests | Interface compatibility across services |
| SAST | Code-level vulnerability patterns |
| SCA | Known vulnerable dependencies |
| Secret scanning | Credentials committed, including history |
| IaC scanning | Misconfigured infrastructure before it is applied |
| Container scanning | Vulnerable base images and packages |
| Migration verification | Schema change applies and rolls back |
| Build verification | Artifact builds reproducibly |

Do not mandate a specific vendor when the existing stack already provides the capability. Where a scanner exists but its findings are never acted on, the gate is decorative, and that is the finding worth reporting.

Consider SBOM generation and build provenance where the distribution model justifies it, in particular for software shipped to others.

## Infrastructure as code

Where infrastructure exists, check whether it is described reproducibly: Terraform, Pulumi, CloudFormation, CDK, Kubernetes manifests, Helm, Docker Compose, or provider configuration.

Check for: publicly exposed ports and services, storage buckets with public access, over-broad IAM policies and wildcard permissions, permissive security groups and network policies, encryption at rest and in transit, logging enabled, backups configured, secrets committed into IaC files or state, mutable resources where immutable would be safer, and drift between the declared state and what actually runs.

Drift is the quiet one. Infrastructure code that no longer matches reality gives false assurance to everyone reading it, including this audit. Check when it was last applied.

Where infrastructure is configured entirely by hand through a control panel, say so plainly. It is not automatically wrong for a small system, but it means there is no reviewable record of the configuration, rebuilding after loss depends on memory, and this audit cannot verify infrastructure claims from the repository at all.

## Least privilege

For human users, applications and machine identities, ask: which permissions are necessary, which are actually granted, are wildcard permissions present, are root or admin credentials used for routine operation, are service accounts separated per workload, and are production and development identities separated.

A single credential shared by application, migrations, CI and analytics means every one of them holds the highest privilege any of them needs.

## Multi-tenancy

Where multiple tenants exist, tenant isolation must be checked explicitly and separately, in: database queries, cache keys, object storage paths, search indexes, queues, background jobs, analytics, logs and admin APIs.

A missing tenant filter is treated as potentially `Critical`. Verify isolation is enforced at a level that cannot be forgotten, such as row level security or a repository layer that requires the tenant scope, rather than by every developer remembering to add a `WHERE` clause.

Background jobs are the usual gap: the request path carries tenant context and the worker does not.

## Third-party APIs

Third-party APIs are untrusted dependencies. Check authentication, authorization, input validation on data received, output validation, timeouts, retries, webhook signature verification, replay protection on webhooks, SSRF exposure where a URL is user-supplied, provider outage handling, rate limits, and how breaking version changes are detected.

Webhook endpoints deserve specific attention: they are unauthenticated by default, publicly reachable, and frequently trusted implicitly. Verify the signature check exists, is applied before processing, uses a constant-time comparison, and validates a timestamp to prevent replay.

Provider limits must come from that provider's official documentation, with the plan and region recorded, since both change the limit.
