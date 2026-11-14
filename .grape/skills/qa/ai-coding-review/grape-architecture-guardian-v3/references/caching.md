# Caching

## The rule this replaces

"Every database query must be cached" is not an architectural rule. It is a reflex that adds an invalidation problem, a consistency problem, a memory cost and an extra failure domain to queries that were never slow.

A cache must have a demonstrated purpose. Absent one, the recommendation is no cache.

## Decision inputs

Before recommending a cache, establish:

- **Access frequency**: how often the same value is actually requested
- **Query cost**: what the uncached path costs, measured rather than assumed
- **Change frequency**: how often the underlying data changes
- **Staleness tolerance**: how stale this value may be before it is wrong, which is a product decision
- **Cardinality**: how many distinct keys exist, since a cache with a key per user per request parameter caches nothing and costs memory
- **Memory footprint** and the eviction policy that follows from it
- **Privacy**: whether the value is personal data, and what that means for storing it in a shared cache
- **Tenant isolation**: whether the key can possibly collide across tenants

A cache is justified when the value is read far more often than it changes, the uncached cost is material, and staleness within the TTL is acceptable to the product. When change frequency approaches read frequency, the cache is overhead.

## Required definitions

For every cache introduced or reviewed, all of these must be defined:

| Aspect | Question |
|---|---|
| Key | Exact composition, including every input that changes the value |
| Namespace | Prefix and versioning, so a schema change can invalidate wholesale |
| Tenant isolation | Tenant identifier inside the key, verified |
| Serialization | Format, and what happens when it changes |
| Invalidation | Event-driven, TTL, or both |
| Cache miss | Behaviour and cost |
| Cache failure | Behaviour when the cache is unreachable |
| Database failure | Whether stale cache is served, and whether that is acceptable |
| Observability | Hit ratio, latency, eviction rate, error rate |

A cache key missing an input that affects the result serves the wrong data to someone. A cache key missing the tenant serves one tenant's data to another, which is a `Critical` finding under `A01:2025`.

## Failure behaviour

Decide deliberately whether the cache is fail-open or fail-closed. Fail-open means a cache outage passes full traffic to the database, which may take the database down as well. Fail-closed means a cache outage becomes an application outage.

Neither is universally right. What is always wrong is not having decided, since the behaviour then emerges from library defaults during the incident.

## Stampede, penetration, poisoning

**Stampede**: many concurrent misses for the same hot key hit the origin simultaneously when the entry expires. Mitigations are request coalescing, a lock on recompute, or staggered expiry. Relevant whenever a key is hot enough to matter.

**Penetration**: repeated lookups for keys that do not exist bypass the cache entirely and reach the database every time. Mitigation is caching the negative result, with a shorter lifetime.

**Poisoning**: an attacker influences what gets stored. Check that no user-controlled input enters a key without validation, and that no response varying by authorization is stored under an unauthenticated key.

## Patterns

Cache aside, read through, write through and write behind are all legitimate. Choose by fit, not by preference. Write behind trades durability for write latency and requires an explicit answer about what happens to unflushed writes on crash.

## TTL

**A TTL may never be invented.** It is a number under Rule 1.

Derive it from staleness tolerance stated by the product, from the change frequency of the underlying data, or from a vendor-mandated maximum. When none is available:

```
CACHE_TTL_SECONDS=<undecided: requires staleness tolerance from product owner>
```

and state `INSUFFICIENT DATA TO VERIFY` with the measurement needed: hit ratio at candidate TTLs, and the acceptable staleness window.

## CDN and edge

For web applications, assess static assets, images, fonts, JavaScript bundles, CSS and downloadable files for CDN suitability. These are usually straightforward and usually already handled by the platform.

The risk is on the dynamic side. Never cache private or personalized responses in a shared cache. Check `Cache-Control` directives including `private` and `no-store`, the `Vary` header, interaction with `Authorization` headers and cookies, and tenant boundaries.

A personalized response served from a shared CDN cache is a data breach, not a performance issue. Verify the actual response headers rather than the intended configuration.

CDN TTLs are numbers under Rule 1 as well, and the platform usually documents both a default and a maximum.
