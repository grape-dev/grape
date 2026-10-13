# Security Policy

## Supported Versions

Grape is under active development. Security fixes are applied to the current release line; older releases are supported on a best-effort basis.

| Version | Supported |
|---------|-----------|
| latest (main-opensource / releases) | ✅ |

## Reporting a Vulnerability

Please do **not** open a public issue for security vulnerabilities. Instead, report privately so the maintainers can respond before details become public.

Preferred channels:

- **GitHub private vulnerability reporting** (if enabled on the public repository): use the repository's "Report a vulnerability" entry under Issues.
- **Maintainer contact**: reach the maintainers via the project's public discussion/issue tracker after sending a brief note; the team will reply with a secure channel.
- **Encrypted disclosure**: if you must email, request a PGP key from a maintainer first.

What to include:

- Affected component / version
- Steps to reproduce (minimal)
- Impact assessment if known
- Suggested fix (optional)

## Handling Process

1. Triage by maintainers within 5 business days.
2. If confirmed, a private branch fix is prepared and a coordinated disclosure is scheduled.
3. A security release is tagged; the issue is disclosed publicly only after the fix ships and users can upgrade.

## Secrets & Credentials

If you discover leaked credentials in the public repository, do **not** use them and do **not** post them. Report the location privately so the affected token can be revoked and the file scrubbed (see the root `temp/opensource-prep-plan.md` for the intended cleanup flow).

## Security Baseline

- Runtime secrets are injected via environment variables only, never committed (see `.env.example` and `configs/*.example.json`).
- API tokens are stored hashed in the DB (see `src/api/auth_token.py`); plaintext never persists.
- HTTP server refuses requests from the public internet unless explicitly exposed by deployment config.