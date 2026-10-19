# docs/ Documentation Security & Sensitive-Data Guide (repo-wide)

> Purpose: set the security & quality bar for **all** design documents under `docs/`
> (including `weekly-*`, `new-version*`, `spec-*`, `issue-*` and every other subdirectory).
> **No document may contain security/sensitive data.** Once a value lands in git history it is
> almost impossible to scrub cleanly (requires git-filter-repo level rewriting) and is a common
> source of leaks / open-source incidents.
> This is a hard rule, not a suggestion. A per-weekly refinement layer may exist
> (e.g. `docs/weekly-08/DOCUMENTATION-GUIDE.md`), but it must never be looser than this file.

---

## 1. Red-line content: NEVER put in a document

Any occurrence below is a security incident; the document must be rewritten before commit/merge:

| Category | Example (❌ wrong vs ✅ correct) |
|----------|----------------------------------|
| Server IP / port combo | ❌ `<a-real-ip>:<a-real-port>` ｜ ✅ `<server_ip>:<port>` |
| Domain name | ❌ any real production / intranet domain ｜ ✅ `<domain>` / `example.com` |
| SSH / OS password | ❌ any plaintext password ｜ ✅ `<ssh_password>` |
| DB password / connection string | ❌ conn string embedding a secret ｜ ✅ `<db_conn>` |
| API key / token / secret | ❌ any `grape_*` / `rta_*` / `ghp_*` / `sk-*` / oauth2 token ｜ ✅ `<api_key>` |
| Account / email / user ID | ❌ real username, email, `user_id` ｜ ✅ `<user>` / `<account>` |
| Real repository name | ❌ real `owner/repo` values (when ownership matters) ｜ ✅ `<owner>/<repo>` |
| Intranet CIDR (any private range — `10/8`, `172.16/12`, `192.168/16`) | ✅ describe as `<intranet_cidr>` |
| Real env/config values | ❌ real value after `KEY=` ｜ ✅ variable name only |
| Certificate / private-key content | ❌ PEM / private-key fragments ｜ ✅ `<cert>` (conceptual mention only) |

> Principle: **if a value looks like a real credential/address/account, it must not appear.**
> When in doubt, redact.

---

## 2. Redaction conventions

1. **Use `<xxx>` placeholders** carrying the meaning: `<repo_key>`, `<owner>/<repo>`, `<task_id>`, `<server_url>`.
2. **Code / SQL / curl / config examples obey the same rule** — placeholders only, never real values.
   ```sql
   -- ❌ WHERE repo_key='gitcode:<real-owner>/<real-repo>'
   -- ✅ WHERE repo_key='<repo_key>'
   ```
3. **Source-code anchors are allowed**: `src/xxx.py:<line>` is a code location (no sensitive value) and may stay.
4. **Describe scope generically**: "the repo", "the current account", "the cluster master node" — never name real assets.

---

## 3. Pre-commit / pre-merge checklist (must all pass)

- [ ] No IP patterns anywhere: `rg '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}'` (also `10\.` `192\.168\.` `172\.(1[6-9]|2[0-9]|3[01])\.`)
- [ ] No `@` email; no `password/passwd/secret` adjacent to a real value
- [ ] No `token=` / `key=` / `Authorization: Bearer` followed by a real string
- [ ] No real domain suffix or real port enumeration (`:8080` `:3306` `:3000`)
- [ ] Uniform `<xxx>` placeholder style
- [ ] Branch/commit references may use hashes or branch names (not sensitive); but never environment-specific secrets
- [ ] Document header explicitly declares: "This doc contains no real IP, domain, password, account or token."

> ⚠️ For **existing** historical docs that may already contain sensitive values: do not add new ones; before
> any public/open-source release, run a dedicated scrub flow, or move them out of the git history.

---

## 4. How to use (authors / agents writing new docs)

1. Before creating any design doc under `docs/` (or a subdirectory), read this file, then follow the
   structure & details in `docs/weekly-08/DOCUMENTATION-GUIDE.md`.
2. Run the scan commands below **before `git add`**; they are expected to produce **no output**.
3. Reviewers run a manual/tool scan as well; if a sensitive value is found, **rewrite the whole
   document** rather than patching fragments (avoids leftovers).

---

## 5. TDD requirement for every doc-driven implementation

> **Any plan doc in this repo, when implemented, MUST be done Test-Driven-Development (TDD)** — red → green.

Mandatory workflow:

1. **Write the failing tests first** (`red`): before writing any feature code, add the test cases
   that the design doc's "Test case design" section implies. Run them and confirm they **all fail**
   (this is the red phase — proving the tests actually guard the new behavior).
2. **Implement the code** to make the tests pass (`green`): then run the tests and confirm they
   **all pass**.
3. **Keep it green**: after the implementation, the full related suite must stay green; if old tests
   contradict the new, doc-approved behavior, update those old assertions deliberately (with a comment
   explaining the intended semantic change) — never just delete the tests.

Why: TDD guarantees the delivered code matches the doc's intent, catches regression early, and
gives reviewers a runnable proof of the plan's assertions rather than trusting prose alone.

Checklist before marking an implementation "done":
- [ ] New tests written BEFORE the feature code
- [ ] The new tests were observed to **fail** in the red phase (recorded in commit/PR description)
- [ ] After code, the new tests **pass**
- [ ] Related existing tests still pass (or were intentionally updated per doc semantics)

---

## 6. Quick scan commands (run before every commit)

```bash
# ① IP / intranet CIDR
rg -n -E '(192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.|[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})' docs/
# ② token / key / password
rg -n -iE '(authorization\s*[:=]\s*\S+|["'"'"'](password|passwd|secret|api[_-]?key|token)["'"'"']\s*[:=]\s*\S+)' docs/
# ③ email
rg -n -iE '([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})' docs/
```

> All three commands must produce **no output** before committing new docs under `docs/`.

---

## 7. Relationship with existing tooling in the repo

- When the repo already ships docs-redaction-guard / check-doc-redaction tooling: **trust the tool**
  as the executive gate; this file is the **norm layer aligned with it** (for quick self-checks where
  no tool is present).
- This file does not replace existing CLI/CI checks — CI enforces, this file guides authors upstream.