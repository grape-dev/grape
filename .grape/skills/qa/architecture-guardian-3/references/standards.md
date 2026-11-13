# Standards Register

Every version and identifier below was verified against the primary source on the date given in "Verified". Re-verify before citing a version in a report if that date is more than roughly six months old. Standards move, and a stale citation is exactly the failure mode this skill exists to prevent.

Cite findings against these identifiers. Never invent a requirement ID.

---

## OWASP ASVS

**Application Security Verification Standard 5.0.0**, released 2025-05-30 at Global AppSec EU Barcelona.
Source: https://owasp.org/www-project-application-security-verification-standard/
Verified: 2026-08-08

Requirement ID format is `<chapter>.<section>.<requirement>`, for example `1.2.5` meaning chapter 1 Encoding and Sanitization, section 2 Injection Prevention, requirement 5.

When citing a version, the documented format is `v<version>-<requirement>`, lowercase `v`:

```
OWASP ASVS v5.0.0-1.2.5
```

A "bleeding edge" development version exists on GitHub. It is not a release. Do not cite it as normative.

Never write an ASVS ID that has not been read from the standard. An invented requirement ID is worse than no citation, because it looks authoritative and is unfalsifiable to a reader who does not check.

---

## OWASP Top 10

**OWASP Top 10:2025** is the current released version.
Source: https://owasp.org/Top10/2025/
Verified: 2026-08-08

| ID | Category |
|---|---|
| A01:2025 | Broken Access Control |
| A02:2025 | Security Misconfiguration |
| A03:2025 | Software Supply Chain Failures |
| A04:2025 | Cryptographic Failures |
| A05:2025 | Injection |
| A06:2025 | Insecure Design |
| A07:2025 | Authentication Failures |
| A08:2025 | Software or Data Integrity Failures |
| A09:2025 | Security Logging and Alerting Failures |
| A10:2025 | Mishandling of Exceptional Conditions |

Note the changes against the 2021 edition that commonly cause miscitation: supply chain moved up to A03 and broadened from "Vulnerable and Outdated Components", logging became "Logging and Alerting", and "Mishandling of Exceptional Conditions" is new at A10. Do not cite 2021 IDs against a 2025 finding.

---

## OWASP API Security Top 10

**2023 edition** is the current edition.
Source: https://owasp.org/API-Security/editions/2023/en/0x11-t10/
Verified: 2026-08-08

| ID | Category |
|---|---|
| API1:2023 | Broken Object Level Authorization |
| API2:2023 | Broken Authentication |
| API3:2023 | Broken Object Property Level Authorization |
| API4:2023 | Unrestricted Resource Consumption |
| API5:2023 | Broken Function Level Authorization |
| API6:2023 | Unrestricted Access to Sensitive Business Flows |
| API7:2023 | Server Side Request Forgery |
| API8:2023 | Security Misconfiguration |
| API9:2023 | Improper Inventory Management |
| API10:2023 | Unsafe Consumption of APIs |

API1, API3 and API5 are the authorization triad. Any endpoint taking a user-supplied identifier is an API1 candidate until object ownership is proven in code.

---

## NIST Secure Software Development Framework

**SP 800-218, SSDF Version 1.1** is the current FINAL version, published 2022-02-03.
Source: https://csrc.nist.gov/pubs/sp/800/218/final
Verified: 2026-08-08

**SP 800-218 Rev. 1 (SSDF Version 1.2) is a DRAFT.** Initial public draft published 2025-12-17, public comment period closed 2026-01-30. As of the verification date it has not been finalized.
Source: https://csrc.nist.gov/pubs/sp/800/218/r1/ipd

Use 1.1 as the normative basis. Version 1.2 may be mentioned as forthcoming, and must be labelled `DRAFT` every time it is mentioned. Do not let a draft silently replace the stable version in a compliance argument, and do not tell a user they are non-compliant against a draft.

**SP 800-218A**, Secure Software Development Practices for Generative AI and Dual-Use Foundation Models, is a final SSDF community profile that augments 1.1 for AI model development.
Source: https://csrc.nist.gov/pubs/sp/800/218/a/final

Practice group identifiers in SSDF 1.1 are `PO` (Prepare the Organization), `PS` (Protect the Software), `PW` (Produce Well-Secured Software) and `RV` (Respond to Vulnerabilities), for example `PW.4.1`.

---

## Cloud architecture frameworks

Use the framework of the provider actually deployed. Do not transfer AWS guidance onto Azure, GCP, Cloudflare, Vercel, Supabase, Netlify, Hetzner, Netcup or Hostinger.

| Provider | Framework | Entry point |
|---|---|---|
| AWS | AWS Well-Architected Framework | https://aws.amazon.com/architecture/well-architected/ |
| Azure | Azure Well-Architected Framework | https://learn.microsoft.com/azure/well-architected/ |
| Google Cloud | Google Cloud Architecture Framework | https://cloud.google.com/architecture/framework |

For a plain VPS, a container host, or a PaaS with no first-party framework, there is no vendor framework to cite. Say so, and fall back to standards plus the vendor's own product documentation for concrete limits.

### Google Cloud: Patterns for scalable and resilient apps

Source: https://cloud.google.com/architecture/scalable-and-resilient-apps
Document last updated 2025-05-05 UTC. Verified: 2026-08-08

Three overarching themes: **automation**, **loose coupling**, **data-driven design** (scaling and fault-detection decisions from captured metrics rather than assumptions).

Named practices worth applying beyond Google Cloud, because they are provider-neutral in substance:

- Infrastructure as code, version controlled and deployed through a pipeline
- Immutable infrastructure: rebuild rather than mutate, which removes configuration drift
- Redundancy across zones and regions where the availability requirement justifies it
- Statelessness: no local persistent data in the service, so instances can scale and restart freely
- Service decomposition only where components genuinely have different scaling profiles
- Four golden signals: latency, traffic, errors, saturation
- Use percentiles rather than averages to detect outliers
- Autoscale on application-level metrics, not only infrastructure metrics
- Minimise instance startup time, because slow starts make autoscaling react too late
- Evaluate consistency requirements honestly before assuming a relational database is required everywhere
- Circuit breakers, exponential backoff, graceful degradation, health checks that remove failing instances from rotation

This document gives principles and does not supply numeric thresholds for another system. It does not license an invented autoscaling threshold or latency target. Those still require Rule 1.

---

## Twelve-Factor App

Source: https://12factor.net

Apply the factors that remain relevant to the project rather than the whole list ceremonially. The load-bearing ones for this skill are: stateless processes, configuration externalised into the environment, backing services as attached resources, strict separation of build, release and run, scaling out via the process model, logs as event streams, and dev/prod parity.

---

## Other bodies worth citing

| Body | Use for |
|---|---|
| IETF RFCs | HTTP semantics and status codes, caching, URI, TLS, OAuth, JWT |
| NIST SP 800-63 | Digital identity, authentication assurance, password guidance |
| CNCF | Container, orchestration and observability practice |
| OpenSSF | Supply chain, scorecards, build provenance, SLSA |
| CIS Benchmarks | Concrete hardening baselines per platform |

---

## How to cite in a finding

Good:

> OWASP API Security Top 10 `API1:2023` Broken Object Level Authorization. The handler at `src/api/invoices.ts:42` loads by `req.params.id` without an ownership predicate.

Bad, and specifically forbidden:

> This violates OWASP best practices for API security.

The second version cites nothing, cannot be checked, and is indistinguishable from a guess.
