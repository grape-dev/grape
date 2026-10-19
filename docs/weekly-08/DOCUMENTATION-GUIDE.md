# docs/weekly-08 Design-Doc Writing Guide (Security & Sensitive-Data Red Lines)

> Purpose: keep every design doc under `docs/weekly-08/` safe and high-quality.
> **No design doc may contain security/sensitive data** — once it lands in git history it is
> extremely hard to scrub (git-filter-repo is costly and still leaves exposure surface).
> This is a hard rule, not a suggestion.

---

## 1. Red-line categories (placeholders only — never quote real values, even in "wrong example" columns)

| Category | Canonical placeholder |
|----------|----------------------|
| Server IP / port combo | `<server_ip>:<port>` |
| Domain name | `<domain>` / `example.com` |
| SSH / OS password | `<ssh_password>` |
| Database password | `<db_password>` |
| API key / token | `<api_key>` |
| GitCode / GitHub credential | `<vcs_token>` |
| Internal account / email / user ID | `<user>` / `<account>` |
| Repository name | `<owner>/<repo>` |
| Intranet CIDR | `<intranet_cidr>` |
| Real env-var value | variable name only |

> **Critical**: even a `❌ vs ✅` comparison must not contain real values. When presenting
> "wrong vs correct", the wrong side uses shapes like `<real-ip>:<real-port>` — never paste a real
> IP / password / domain / account.
>
> Rule of thumb: **if a value looks like a real credential / address / account, it must not appear.**
> When in doubt, redact.

---

## 2. Redaction conventions

1. **Uniform `<xxx>` placeholders** carrying meaning: `<repo_key>`, `<owner>/<repo>`, `<im_chat_id>`, `<task_id>`.
2. **Code blocks follow the same rule**: SQL / curl / config examples use placeholders, never real values.
   ```sql
   -- ❌ WHERE repo_key='gitcode:<real-owner>/<real-repo>'
   -- ✅ WHERE repo_key='<repo_key>'
   ```
3. **Source-code anchors may stay**: `src/xxx.py:<line>` is a code location (no sensitive value).
4. **Describe scope generically**: "the repo", "the current account", "the cluster master node" —
   never name real assets.

---

## 3. Pre-commit checklist (all must pass)

- [ ] No IP pattern anywhere: `(12[0-9]|1[01][0-9]|2[0-4][0-9]|25[0-5])\.` , `10\.`, `192\.168\.`, `172\.(1[6-9]|2[0-9]|3[01])\.`
- [ ] No `@` email; no `password`/`passwd`/`secret` adjacent to a real value
- [ ] No `token=` / `key=` / `Authorization: Bearer` followed by a real string
- [ ] No real domain suffix; no real port enumeration (`:8080` / `:3306` / `:3000`)
- [ ] Uniform `<xxx>` placeholder style
- [ ] Branch/commit references may use hashes or branch names (not sensitive); never environment-specific secrets

---

## 5. TDD requirement for every doc-driven implementation

> **Any plan doc under this repo must be implemented via Test-Driven-Development (TDD)** — red → green.
> This applies to every feature/spec doc that gets implemented, not just `docs/weekly-08`.

Mandatory workflow:

1. **Write the failing tests first** (`red`): before writing feature code, add the test cases that
   the design doc's "Test case design" section implies. Run them and confirm they **all fail** —
   this proves the tests actually guard the new behavior.
2. **Implement the code** to turn them green: then run the tests and confirm they **all pass**.
3. **Keep it green**: after the implementation, the related suite stays green. If old tests contradict
   the new, doc-approved behavior, update those old assertions deliberately (with a comment explaining
   the intended semantic change) — never silently delete tests.

Checklist before an implementation is considered "done":
- [ ] New tests written BEFORE the feature code
- [ ] New tests observed to **fail** in the red phase (note this in the commit/PR description)
- [ ] After the code, the new tests **pass**
- [ ] Related existing tests still pass (or intentionally updated per doc semantics)

---

## 6. Suggested document skeleton

```
# Title (one-sentence problem/goal)

> Status / Branch / Related
> Sensitive info: this doc contains no real IP, domain, password, account or token (all `<xxx>` placeholders)

## 〇、Problem / Observed behavior
## 一、Target semantics
## 二、Plan (Step N: ...)
## 三、Module changes
## 四、Key implementation notes
## 五、Boundaries / Risks
## 六、Test case design
## References
```

> A header declaring "no sensitive info" + placeholders throughout constrains the author and lets
> reviewers run a fast security pass.

---

## 7. Review & history handling

- New docs must be scanned (manually or via tooling) before merge.
- If a sensitive value is found, **rewrite the whole doc** — do not patch fragments (avoids leftovers).
- Historical docs that already contain secrets: run a scrub flow, or move them to a non-git local
  reference dir (e.g. `temp/`) instead of keeping them in `docs/` history.

---

## Appendix: quick scan commands (local; expect NO output)

```bash
# IP / intranet CIDR
rg -n -E '(192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.|[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})' docs/weekly-08/
# token / key / password
rg -n -iE '(token|secret|password|passwd|authorization)' docs/weekly-08/
# email
rg -n -iE '([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})' docs/weekly-08/
```

> All three must produce **no output** before committing any new doc under `docs/weekly-08/`.