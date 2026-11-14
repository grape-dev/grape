# Security Review

Security is not an audit stage appended at the end. It accompanies every architectural decision. Map the system before hunting for bugs, because a vulnerability list without trust boundaries cannot be prioritised.

## Step 1: Map before testing

Establish, from the code and configuration:

- **Assets**: what is worth stealing or corrupting in this system
- **Trust boundaries**: where data crosses from less trusted to more trusted
- **Actors**: anonymous user, authenticated user, tenant admin, staff, service account, CI, operator
- **Entry points**: HTTP routes, webhooks, queue consumers, file imports, scheduled jobs, admin interfaces, database access
- **External systems** and what they are trusted with
- **Sensitive data**: credentials, tokens, personal data, payment data, health data, location data
- **Secrets** and where they live
- **Authentication and authorization flows**, drawn as they actually are

Build a threat model for systems where this is proportionate: what an attacker at each boundary can reach.

## Authorization is the first thing to check

`API1:2023` Broken Object Level Authorization and `A01:2025` Broken Access Control are consistently the highest-yield findings in AI-generated code, because a model writes the happy path and the happy path is the owner reading their own record.

For every data access function taking a user-controlled identifier, verify in code that the current principal is authorized for that specific object. The question is not "is this route authenticated" but "does this row belong to this caller".

Check all four layers:

| Layer | Question |
|---|---|
| Object level | May this principal touch this specific object |
| Function level | May this principal invoke this operation at all |
| Property level | May this principal read or write this specific field |
| Tenant | Is every query, cache key, storage path and index scoped to the tenant |

Unguessable identifiers such as UUIDs raise the cost of enumeration. They are not authorization. A finding that says "IDs are UUIDs so this is fine" is wrong and should be reported as such.

Also check: mass assignment on create and update, admin-only fields writable through a public schema, service-to-service calls trusting a caller-supplied user ID, and authorization implemented in the frontend only.

## Authentication

Do not recommend building authentication where an established identity provider is the sensible choice. Where authentication already exists, evaluate what is actually there rather than replacing it reflexively.

Check as applicable: OIDC and OAuth flows and their grant types, session handling, passkeys and WebAuthn, MFA, password storage and policy, account recovery (usually the weakest path), credential rotation, session revocation, token revocation, and device session management.

JWT is not a prerequisite for a stateless architecture, and it is neither automatically secure nor automatically insecure. Evaluate the actual design: signature algorithm and whether `alg` is pinned, key management and rotation, audience and issuer validation, expiry handling, whether revocation is required by the threat model and whether it is achievable with the chosen model, and where the token is stored in the client.

Token and session lifetimes are numbers. Rule 1 applies. Derive them from the threat model, the revocation capability and any regulatory requirement, or leave them as parameters.

## Input and output

All external data is untrusted. This includes browsers, mobile apps, webhooks, partner APIs, third-party APIs, message queues, uploaded files, data imports, and AI model output.

Check: schema validation, type validation, range validation, allowlisting over denylisting, canonicalisation before comparison, output encoding at the sink, parameterized queries, ORM escape hatches where raw SQL re-enters, command injection, SQL and NoSQL injection, XSS, SSRF, path traversal, template injection, file upload handling, and deserialization of untrusted data.

Validation on the client is a user experience feature. It is never a control.

## Secrets

Forbidden without exception: API keys, passwords, tokens or private keys committed to the repository, secrets in client bundles, secrets in logs, secrets in error responses.

Check: a secret manager or equivalent, environment separation, CI/CD secret handling, rotation capability, least privilege per credential, separate service accounts per workload, workload identity where the platform supports it, and short-lived credentials where available.

Search history as well as the working tree. A rotated key that is still in git history is still leaked.

## Cryptography

Never implement cryptography. Use established libraries and platform primitives.

Algorithm choice, key sizes, cipher suites, hashing, password hashing and TLS versions must come from current standards or the deployed provider's documentation, not from a remembered blog recommendation. Password hashing in particular has parameters (memory, iterations, parallelism) that are numbers under Rule 1 and change with hardware.

## Privacy and regulated data

Identify personal data, sensitive personal data, payment data, credentials, tokens, health data, location data and any other regulated category.

Check data minimisation, purpose limitation, retention, deletion (including from backups, logs and analytics), access, export, auditability, encryption at rest and in transit.

Never assume a compliance regime applies, and never assume it does not. Ask which regimes are in scope when the answer changes the architecture. Where a regime is claimed but not confirmed, mark it as an assumption.

## Logging security

Logs must not contain passwords, session tokens, access tokens, refresh tokens, API keys, `Authorization` headers, cookies, personal data or payment data. Check the error path specifically, since full request dumps on exception are the usual leak.

Separate audit logs from debug logs. Security-relevant events need to remain reconstructable: authentication success and failure, authorization denial, privilege change, secret access, data export, admin action.

`A09:2025` covers alerting as well as logging. A log nobody alerts on does not detect anything.

## AI and LLM features

If the application calls a model, treat model output as untrusted input, and check:

- Prompt injection, direct and indirect through retrieved content
- Tool authorization: what the model is permitted to invoke, and with whose privileges
- Excessive agency: actions taken without a human gate that should have one
- Data exfiltration through prompts, tool calls or rendered output
- Tenant isolation in retrieval, embeddings and caches
- Structured output validation before the output reaches a sink
- Tool input validation, since a model can emit any argument
- Secrets exposure through prompt context
- Cost amplification: unbounded loops, unbounded context growth, retry storms against a metered API

## Severity guidance

`Blocker` and `Critical` are for exploitable access control failures, tenant isolation failures, exposed secrets, data loss risk and remote code execution. Do not inflate severity to create urgency, and do not deflate it because a fix is inconvenient. State the concrete failure scenario instead, and let severity follow from it.
