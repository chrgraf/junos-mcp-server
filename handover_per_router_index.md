# Handover: Add Per-Router Index to RedisArtifactStore

## Context

The enhanced JMCP server ([`artifact_store.py`](artifact_store.py)) currently
maintains two Redis index structures when writing an artifact:

| Key | Type | Purpose |
|---|---|---|
| `{prefix}:index:time` | Sorted set | All `run_id`s ordered by ms timestamp |
| `{prefix}:index:tool:{tool_name}` | Sorted set | Per-tool `run_id`s ordered by ms timestamp |

There is **no per-router index**. If a user wants the latest `run_id` that
contains data for a specific router (e.g. R1), they currently have to either:
- Call `list_artifacts(limit: N)` and scan labels, or
- Load the full artifact blob and inspect its `routers[].router_name` fields.

The manual Redis pattern (documented in `blog/context_blog5.md`) solves the
equivalent problem with `isis:routers:latest` — a HASH that maps each router
name to its most recent data timestamp. We want the same lookup capability in
the enhanced JMCP.

---

## Goal

Add a third index structure:

```
{prefix}:index:router:latest
```

**Type:** Redis HASH  
**Content:** `{router_name} → {run_id}` (the most recent `run_id` that included
that router)

This matches the conceptual role of `isis:routers:latest` in the manual pattern.
It enables O(1) lookup: "which `run_id` is the latest one containing R1?" without
loading any blob.

---

## Files to Change

### 1. `artifact_store.py` — `RedisArtifactStore` class

**Add a key-builder helper** (alongside the existing `_index_time_key`,
`_index_tool_key` helpers, around line 240):

```python
def _index_router_latest_key(self) -> str:
    return self._k("index:router:latest")
```

**Extract router names from the payload** inside `write_json`, after the
`enriched` dict is built (around line 323, before the pipeline block):

```python
# Extract router names for per-router index.
# Payload may use 'routers' (multi-command batch) or 'results' (single-command
# batch). Both contain dicts with a 'router_name' key.
_router_names: list[str] = []
for _rtr in enriched.get("routers") or []:
    _rname = _rtr.get("router_name") if isinstance(_rtr, dict) else None
    if isinstance(_rname, str) and _rname:
        _router_names.append(_rname)
if not _router_names:
    for _r in enriched.get("results") or []:
        _rname = _r.get("router_name") if isinstance(_r, dict) else None
        if isinstance(_rname, str) and _rname:
            _router_names.append(_rname)
```

**Add to the pipeline** (after line 352, `pipe.zadd(self._index_tool_key(...))`,
still inside the `pipe` block):

```python
if _router_names:
    router_latest_key = self._index_router_latest_key()
    pipe.hset(
        router_latest_key,
        mapping={rn.encode("utf-8"): run_id.encode("utf-8") for rn in _router_names},
    )
    if self._ttl_seconds > 0:
        pipe.expire(router_latest_key, self._ttl_seconds)
```

**Note:** the `expire` on the router-latest HASH needs thought — see the TTL
section below.

---

## TTL Considerations

The manual pattern deliberately puts **no TTL on `isis:routers:latest`** because
the HASH is just a pointer; the actual data key has the TTL. If the HASH expired,
you'd lose the ability to look up which key to read even before the data is gone.

For the JMCP per-router index HASH the same reasoning applies:

- **Preferred:** no TTL on `{prefix}:index:router:latest` (remove the
  `pipe.expire(router_latest_key, ...)` call above), so the index survives even
  after individual artifact blobs expire.
- **Alternative:** set a longer TTL than the blob TTL (e.g. `ttl_seconds * 2`)
  to bound memory use while keeping the HASH alive longer than the data it points
  to.

The existing code sets TTL on `index:time` and `index:tool:{tool}` — that's
already slightly inconsistent with the "no TTL on the index" principle from the
manual pattern. Follow the project's existing convention for now (same TTL),
but document the trade-off.

---

## `list()` Method — Optional Extension

Once the index exists, `list()` can gain a `router_contains` parameter to filter
artifacts by router name without loading blobs:

```python
def list(
    self,
    *,
    tool: str | None = None,
    label_contains: str | None = None,
    router_contains: str | None = None,   # NEW
    since_epoch: float | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
```

Implementation sketch: if `router_contains` is set, do
`HGETALL {prefix}:index:router:latest`, filter keys that contain the substring,
collect the matching `run_id` values, then fetch meta HASHes only for those IDs.

This is optional for the first pass — the index alone is already useful even
without the filter parameter.

---

## Testing Checklist

- [ ] Single-command batch (`payload["results"]` path): router index updated
- [ ] Multi-command batch (`payload["routers"]` path): router index updated
- [ ] Payload with neither key (edge case): no crash, index not written
- [ ] `HGETALL {prefix}:index:router:latest` returns correct `router → run_id` mapping after a write
- [ ] After a second write for the same router, index points to the newer `run_id`
- [ ] TTL behaviour: confirm index key TTL matches project convention
- [ ] `DualArtifactStore` and `DiskArtifactStore` unaffected (disk store has no
  equivalent index and does not need one)

---

## Related Reading

- `blog/context_blog5.md` — "Redis Storage: Master Index and TTL" section
  explains the manual pattern this mirrors, including why `setex + hset` must
  be a paired atomic operation and why the index carries no TTL.
- Commit context: this feature was identified during an editorial review of
  Part 5 when verifying the accuracy of the "Enhanced JMCP users" callout in
  that section. The callout currently states that "per-router staleness cannot
  occur because every artifact is a single barrier-synced batch snapshot" —
  which remains true. The per-router index is a convenience lookup, not a
  staleness fix.
