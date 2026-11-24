# Runbook 04: Server-Hosted Skills & In-RAM Snapshot SDK (`context_sdk`)

Run deterministic, sub-millisecond institutional lookups inside server-hosted skills (CRM Fund Mapping, GIPS Portfolio Reporting, Cash-Flow Wire Reconciliation) using `context_sdk` over ACL/wall-partitioned `.sqlite.zst` snapshots.

---

## 1. Snapshot Partitioning & Compilation

Snapshots are physically partitioned by `(acl_tier, wall_side)` (e.g. `firm-all__public_side`, `private-credit-deal-team__private_side`) and compiled asynchronously off the event loop (`asyncio.to_thread`) using Python 3.14+ stdlib `compression.zstd`:

- Zero unauthorized rows, zero private memories, zero `unverified_observation` rows, and zero restricted `evidence_uri` / `excerpt` strings exist anywhere in a partition's binary `.sqlite.zst` file.
- Manifest updates write `manifest.json.tmp.<pid>.<tid>` and call `os.replace()` with a Windows NTFS sharing-violation retry loop.

Trigger or inspect snapshots over REST:

```powershell
# Steward compile (async, non-blocking)
curl.exe -s -X POST http://127.0.0.1:8000/api/v1/snapshots/compile `
  -H "X-Mock-User: user:fund-steward" `
  -H "Content-Type: application/json" `
  -d '{"acl_tier":"firm-all","wall_side":"public_side"}'

# Latest manifest for caller's authorized partition
curl.exe -s "http://127.0.0.1:8000/api/v1/snapshots/latest?acl_tier=firm-all&wall_side=public_side" `
  -H "X-Mock-User: user:public-analyst"
```

---

## 2. In-RAM Loading, Ref-Counted Swaps & `pinned_version` Isolation

`context_sdk` loads `v{N}.sqlite.zst` entirely into RAM via `sqlite3.Connection.deserialize()` (**zero temp files on disk, zero Windows WinError 32 file locks**):

```python
from pathlib import Path
from context_sdk import ContextClient

# Live service-hosted skill client (background ETag/epoch polling enabled)
client = ContextClient(
    snapshots_dir=Path("data/runtime/snapshots"),
    acl_tier="firm-all",
    wall_side="public_side",
    poll_interval_seconds=5.0,
)

# Exact deterministic mapping lookup (sub-millisecond in RAM)
res = client.lookup(
    canonical_key="veh:alpha-credit-offshore-lp",
    predicate="has_admin_code",
    context_purpose="legal_vehicle",
)

# Concurrent 0-diff historical replay pinned to an immutable snapshot version
# Background polling NEVER clobbers pinned_version!
replay = client.pinned(version_id=1)
q4_pre = replay.lookup(
    canonical_key="grp:alpha-credit-gips-composite",
    predicate="composite_nav_usd",
    as_of="2025-12-31T00:00:00Z",
    recorded_as_of="2026-01-16T00:00:00Z",
)

client.close()
```

### Concurrency & Safety Guarantees

1. **Ref-Counted `SnapshotHandle`:** Active readers acquire a lightweight refcount before executing SQL. Atomic handle swaps mark the prior handle `_retired = True` and close connections only once `_refcount == 0`, eliminating `Cannot operate on a closed database`.
2. **Strict `pinned_version` Isolation:** Passing `pinned_version=N` or opening `client.pinned(N)` never starts or accepts poller swaps, guaranteeing historical report replay cannot drift mid-run.
3. **Epoch / ETag Fencing:** Multi-worker API caches embed `(data_epoch, policy_epoch, principal_id, groups, wall_side, actor_mode, channel_mode)`, eliminating single-worker cache drift across commits or Entra revocations.

---

## 3. External Observability (LangSmith / External Arize Phoenix)

Every SDK lookup and API/MCP read emits an immutable, SHA-256-sealed `ContextAssemblyReceipt`. When a span is recording, `build_receipt` writes these attributes onto it automatically, and mirrors each as `langsmith.metadata.context_<name>` so LangSmith shows them as filterable run metadata. Tracing, routing and masking are described in [ADR-006](../adr/ADR-006-wall-aware-tracing-and-langsmith.md): only public-side traces are exported to LangSmith, and private-side traces stay on the internal collector. Receipt attributes:

- `context.receipt_id` (`receipt.receipt_id`)
- `context.receipt_sha256` (`receipt.receipt_sha256`)
- `context.snapshot_version` (`receipt.snapshot_version`)
- `context.data_epoch` / `context.policy_epoch`
- `context.as_of` / `context.recorded_as_of`
- `context.wall_side` / `context.actor_mode` / `context.channel_mode`
- `context.redacted_evidence_count` (`len(receipt.redacted_evidence_claim_ids)`)
