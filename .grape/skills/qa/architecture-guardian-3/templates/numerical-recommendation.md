# Numerical Recommendation

Use this block for every number that ends up in a configuration, a limit, or a technical recommendation. If the block cannot be completed, the number may not be written. Emit `INSUFFICIENT DATA TO VERIFY` and the required measurements instead.

---

### Numerical Recommendation: {parameter name}

**Proposed value**
{value with unit}

**Kind of value**
Vendor limit / Standard / Measured current value / Derived from load test / Business requirement / Calculated from other sourced values

**Technical source or basis of measurement**
{primary source, with URL, or the measurement and how it was taken}

**Document version or date**
{version, or publication date, or date of measurement}

**Applicability to this project**
{why this source governs here: the plan actually in use, the deployed version, the region, the runtime}

**Empirical project data**
{measured values from this system, or "none available"}

**Calculation**
{show the arithmetic where the value is derived, including every input and its source}

**Risk if set lower**
{concrete technical consequence}

**Risk if set higher**
{concrete technical consequence}

**Confidence**
verified / measured / provisional / insufficient data

---

## Worked example: database connection pool

### Numerical Recommendation: `DB_POOL_MAX` per application instance

**Proposed value**
`INSUFFICIENT DATA TO VERIFY` pending one input, see below. Provisional structure given.

**Kind of value**
Calculated from a vendor limit and deployment topology.

**Technical source or basis of measurement**
Provider documentation for the database plan in use, stating maximum concurrent connections. Application instance count from the deployment configuration.

**Document version or date**
Read on {date}, for the {plan name} plan in region {region}.

**Applicability to this project**
The limit applies per database instance and is shared by all clients: application instances, workers, migrations, monitoring and any interactive session.

**Empirical project data**
Peak observed concurrent connections: {value or "not instrumented"}.

**Calculation**

```
database_max_connections            = {A, from provider docs}
reserved_admin_monitoring_migration = {B}
available_to_application            = A - B

max_app_instances_during_deploy     = {C, includes overlap during rolling deploy}
worker_processes                    = {D}
worker_pool_each                    = {E}

pool_max_per_app_instance = (available_to_application - (D * E)) / C
```

**Risk if set lower**
Requests queue waiting for a connection under load, which appears as latency rather than as a connection error and is therefore commonly misdiagnosed as a slow query.

**Risk if set higher**
The database refuses connections once its limit is reached. During a rolling deploy, when old and new instances run simultaneously, total demand can double and exhaust the limit, taking down a system that was stable moments earlier.

**Confidence**
insufficient data, pending {the missing input}

---

## What this template prevents

A number copied from a tutorial has none of the fields above. It cannot be checked, it does not survive a plan change, and it silently encodes assumptions from someone else's system. The block is not bureaucracy: filling it in is what turns a guess into a decision, and failing to fill it in is the signal that the decision has not actually been made.
