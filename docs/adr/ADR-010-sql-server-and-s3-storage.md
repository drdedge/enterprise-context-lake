# ADR-010: SQL Server and S3 Storage

- **Status:** Proposed. Built and tested against SQL Server 2022 (compatibility level 150) in a container, with an account holding the same rights as production, and against local stand-ins for AWS KMS and S3. Not yet run against the production server or AWS account.
- **Scope:** `context_adapters/sqlserver_store.py`, `sqlserver_ledger.py`, `sqlserver_dialect.py`, `sealed.py`, `blob_store.py`, `sqlite_store.py` (column types only), `versioned_store.py`, `context_core/change_service.py`, `context_api/uploads.py`, `context_api/bootstrap.py`
- **Builds on:** ADR-002 (ports and adapters), ADR-003 (barrier), ADR-007 (version control), ADR-008 (access checks in the query)
- **Design review:** "Taxonomy, data model and storage review" (revised recommendation: SQL Server for records, S3 for files, graph and search in memory, no Azure AI Search, no Azure Blob)

## Context

The lake ran on SQLite. Production is a shared SQL Server 2022 Enterprise database on AWS (BSPAI, compatibility level 150) used by about 48 users and service accounts. The lake's account has `db_ddladmin` and data read/write, and nothing more:

- no `ALTER ANY SECURITY POLICY`, so no row-level security;
- no `CONTROL` on what it creates in `dbo`, so it cannot switch off or drop a system-versioned temporal table once created;
- no DBA available to grant more.

The database is broadly readable, so anything the lake stores in clear text is readable by people outside the barrier. Scale target: up to 400 users and tens of thousands of pieces of context.

## Decision

### 1. One adapter, the same queries

`SqlServerBitemporalStore` subclasses `SqliteBitemporalStore`. The schema and queries were already portable SQLAlchemy Core, so the SQL Server store runs the same code, including the in-query access checks of ADR-008 (`acl_sets`), and passes the same conformance suite. What differs is confined to the engine, the dialect and a few overrides:

| Concern | SQL Server behaviour |
|---|---|
| Text columns | `NVARCHAR(n or MAX) COLLATE Latin1_General_100_BIN2`; string parameters sent as NVARCHAR. Unicode survives, comparisons are case-sensitive and ordering is by code point, exactly as on SQLite, and no VARCHAR/NVARCHAR mismatch defeats an index. |
| Large `IN` lists | 50 or more values are sent as one JSON parameter and expanded with `OPENJSON`, so SQL Server's 2,100-parameter limit never bites and one plan serves any list size. |
| `rowid` ordering | replaced by `(recorded_at, id)`. |
| Full-text search | not used. Search and the graph run in memory on the app server, as decided in the review. |
| Read cache | the SQLite store's 2-second scan cache is kept. Writes by this process invalidate it exactly; writes by another app server are picked up within 2 seconds. |

### 2. Where the tables live

In their own schema (`LAKE_SQL_SCHEMA`, default `lake`). A `db_ddladmin` member could create a schema in our test, but if BSPAI refuses it, set `LAKE_SQL_SCHEMA=dbo` and `LAKE_SQL_TABLE_PREFIX=lake_` and every table becomes `dbo.lake_<name>`. The store refuses bare names in `dbo`, so it can never collide with someone else's `dbo.entities`.

### 3. Additive-only migrations

On start-up the store compares its table definitions with the database and creates what is missing: the schema, tables, columns (always NULL-able) and indexes. It never drops, renames or narrows anything, so it is safe to run with `db_ddladmin` on a shared database, and a newer build never damages an older one's data. Every statement it runs is recorded in `schema_log`.

We tested one more lock-down and rejected it: `DENY SELECT ON SCHEMA::lake TO public` keeps other readers out, but on SQL Server 2022 it also applies to the account that owns the schema, so the lake locks itself out.

### 4. Encryption of free text with AWS KMS

Free-text columns are declared `SealedText` in the table definitions: entity names, aliases, descriptions and attributes; claim values, facets and provenance (excerpts, evidence links); lineage summaries; conflict summaries and rationales; note bodies; curator patches; personal memory; receipts; and in the history ledger, every record payload and change-set body. On SQL Server each value is encrypted by the app before it is sent:

- KMS `GenerateDataKey` gives a 256-bit data key under the lake's customer-managed key, with the encryption context `{"app": "enterprise-context-lake", "purpose": "column-encryption"}`.
- Values are encrypted locally with AES-256-GCM. The stored value carries the KMS-wrapped data key, so any app server can read it with `kms:Decrypt` and nothing else; unwrapped keys are cached, so KMS is called once per data key, not per row. Keys rotate every 100,000 values or 24 hours.
- Tampering with a stored value makes decryption fail rather than return altered text. Values written before encryption was switched on still read as plain text.

**Every row is encrypted, not only private-side rows.** The requirement is that private-side text is encrypted. Several tables mix sides in one value (a change set can hold public and private items; a receipt lists what was assembled), and a per-row choice is one forgotten code path away from a leak. Encrypting everything costs a few microseconds per value. Identifiers, wall side, ACL groups, predicates, dates and status stay in clear, because queries filter on them; none of them is free text.

Production refuses to start unless `LAKE_ENCRYPTION=kms`. SQLite and the Markdown vault never attach a cipher and are unchanged.

### 5. History in the same transaction (atomic approvals)

The version ledger moves into SQL Server (`vc_*` tables, same schema or prefix), as plain append-only tables written by the lake, not system-versioned temporal tables (which this account could never switch off or drop).

`unit_of_work()` opens one SQL Server transaction and hands its connection to every store call made inside it. `VersionedStore` opens one around each write, and `ChangeSetService` one around a whole merge. Ledger calls made inside are held with the transaction and inserted on its connection just before it commits. So:

- a record and its history row commit together or not at all;
- an approved change set with several items is applied in full or not at all. On failure everything rolls back and the change set is marked `failed` with nothing applied (on SQLite the old behaviour stands: items applied before the failure stay applied).

The unit of work holds the store's write lock throughout, so another request's write waits rather than interleaving with a half-applied change. Ledger reads run on a small synchronous pool at READ UNCOMMITTED with a 5-second lock timeout: they run on the event loop thread and must never wait for a transaction that needs that loop to finish.

### 6. Files in S3

Uploaded files go to S3 (`LAKE_S3_BUCKET`, under `LAKE_S3_PREFIX`) with server-side encryption under the same KMS key, and claims link to them as `s3://` URIs. Upload records (who uploaded what, its wall side and groups) are kept next to the files, so they survive a restart; previously they lived only in memory. Without `LAKE_S3_BUCKET`, files go to the local folder as before. No Azure Blob.

### 7. Safety around seeding

The demo fixtures are never seeded into SQL Server unless `LAKE_SEED_FIXTURES=1`, and the store refuses any full replace (seed or adapter swap) of a lake that already holds data unless `LAKE_SQL_ALLOW_REPLACE=1`.

## Consequences

- SQL Server becomes the record for entities, claims, topics, vocabularies, notes, memory and history. SQLite stays for local development and tests; the Markdown vault is unchanged.
- The database holds no readable free text, so a reader outside the barrier sees ids, dates and ciphertext. Losing the KMS key means losing the text: the key needs a deletion waiting period and a backup policy.
- The app server's IAM role needs `kms:GenerateDataKey` and `kms:Decrypt` on the key, and `s3:PutObject`, `s3:GetObject`, `s3:ListBucket` on the bucket prefix.
- The ODBC Driver 18 for SQL Server must be installed on the app server (`pip install .[sqlserver]` for the Python side).

### Known limits

- `acl_sets.signature` (ADR-008) is indexed as `NVARCHAR(1024)`. SQL Server caps an index key at 1,700 bytes, so an ACL with roughly 25 or more long group names would be refused. Hashing the signature fixes it if that ever happens.
- Other accounts' long reads of the same tables can make the lake's writes wait (READ COMMITTED with locks). `ALTER DATABASE BSPAI SET READ_COMMITTED_SNAPSHOT ON` removes that, but needs a DBA.
- Cache freshness across several app servers is bounded by the 2-second scan cache; one app server is the plan.

### Optional asks for a DBA later

A dedicated login owning the `lake` schema; `READ_COMMITTED_SNAPSHOT ON`; SQL Server Audit on the `lake` schema; or a separate database for the lake. None is needed to run.

## Verification

- `tests/test_adapter_conformance.py` runs every store test against SQLite, the vault and SQL Server when `LAKE_TEST_SQLSERVER_URL` is set.
- `tests/test_sqlserver_store.py`: failed merge leaves nothing behind; record and history row commit together; text encrypted at rest (including history) and readable through the store; `lake_` prefix mode; case-sensitive ids; Unicode; a 3,000-id query; additive migrations; the replace guard; KMS data keys through a KMS stand-in; S3 uploads with SSE-KMS that survive a restart.
- `LAKE_TEST_API_ON_SQLSERVER=1` runs every API suite with SQL Server as the active store.
- CI job `sqlserver` runs all of this on a SQL Server 2022 service container with an account that has only `db_ddladmin` and data read/write (`scripts/sqlserver_test_setup.py`).
