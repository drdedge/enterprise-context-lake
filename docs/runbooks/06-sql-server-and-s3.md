# 06: Running the Lake on SQL Server, KMS and S3

How to point the lake at the production SQL Server database, the AWS KMS key and the S3 bucket. Design and reasons: [ADR-010](../adr/ADR-010-sql-server-and-s3-storage.md).

## 1. What the app server needs

- Microsoft ODBC Driver 18 for SQL Server (`msodbcsql18`).
- `pip install -e ".[sqlserver]"` (aioodbc, pyodbc, boto3).
- An IAM role (or the usual AWS credential chain) allowed to:
  - `kms:GenerateDataKey` and `kms:Decrypt` on the lake's key, with encryption context `app=enterprise-context-lake`;
  - `s3:PutObject`, `s3:GetObject` and `s3:ListBucket` on the bucket prefix.
- A SQL Server login with `db_ddladmin`, `db_datareader` and `db_datawriter` on the database. Nothing more is needed.

## 2. Settings

Put these in the service's environment or `.env`. Keep the password in its own variable (or a secret store), not in the URL.

```bash
# SQL Server
LAKE_SQLSERVER_URL="mssql+aioodbc://lake_svc@sql-host.internal:1433/BSPAI?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes"
LAKE_SQLSERVER_PASSWORD="..."          # merged into the URL at start-up
LAKE_SQL_SCHEMA="lake"                 # the lake's own schema (default)
# If creating a schema is refused on the server, use prefixed tables in dbo instead:
# LAKE_SQL_SCHEMA="dbo"
# LAKE_SQL_TABLE_PREFIX="lake_"

# Encryption of free text (required in production)
LAKE_ENCRYPTION="kms"
LAKE_KMS_KEY_ID="alias/context-lake"   # key id, ARN or alias
AWS_REGION="eu-west-2"

# Files
LAKE_S3_BUCKET="..."
LAKE_S3_PREFIX="context-lake/"
# LAKE_S3_KMS_KEY_ID defaults to LAKE_KMS_KEY_ID

ENVIRONMENT="production"               # refuses to start without KMS encryption
```

With `LAKE_SQLSERVER_URL` set, SQL Server becomes the active store and history (the version ledger) lives there too. `LAKE_STORE` overrides the choice.

## 3. First start

1. Start the app as usual (`python scripts/dev.py`, or the FastAPI app under your process manager).
2. On start-up the store:
   - checks the KMS key with a round trip, and stops if it cannot use it;
   - creates the schema (or prefixed tables), tables and indexes that are missing, and records each statement in `lake.schema_log`;
   - never drops or alters anything that exists.
3. The demo fixtures are **not** loaded into SQL Server. To load them into an empty test schema, set `LAKE_SEED_FIXTURES=1` once.
4. Check what was created:

```sql
SELECT applied_at, statement FROM lake.schema_log ORDER BY id;
```

Free-text columns should read as `ecl1:...` to anyone querying the tables directly:

```sql
SELECT TOP 5 claim_id, wall_side, LEFT(object_value, 40) FROM lake.claims;
```

## 4. Moving existing SQLite data across

The store refuses to replace a lake that already holds data. To copy a SQLite lake into a new, empty SQL Server schema, start with both configured and swap the active store once (`PUT /api/v1/config/adapters` with `{"store": "sqlserver_bitemporal"}` as a steward). History from the SQLite ledger is not copied; the swap records a baseline in SQL Server's history. Set `LAKE_SQL_ALLOW_REPLACE=1` only when you intend to overwrite a non-empty SQL Server lake.

## 5. Upgrades

A newer build adds tables, columns or indexes on start-up and logs them. It never removes anything, so rolling back to an older build is safe: it ignores the extra columns.

## 6. Running the SQL Server tests locally

```bash
docker run -d --name ecl-mssql -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD='Choose_A_Password_1' \
  -p 1433:1433 mcr.microsoft.com/mssql/server:2022-latest
MSSQL_SA_PASSWORD='Choose_A_Password_1' python scripts/sqlserver_test_setup.py   # prints the test URL
export LAKE_TEST_SQLSERVER_URL="<printed URL>"

python -m pytest tests/test_sqlserver_store.py tests/test_adapter_conformance.py
LAKE_TEST_API_ON_SQLSERVER=1 python -m pytest tests --ignore=tests/e2e   # every API suite on SQL Server
```

The setup script gives its test account the production rights (`db_ddladmin` plus data read/write) and sets compatibility level 150, so the tests see the same limits. Each test uses its own `lake_test_*` schema and drops it. Never run the setup script against a shared server.

## 7. Before the first production run (not yet verified)

These were tested only against a container and local stand-ins. On BSPAI and the real AWS account, check:

- [ ] `CREATE SCHEMA lake` is allowed for the lake's login (otherwise use the `lake_` prefix);
- [ ] the login also has data read/write, not only `db_ddladmin`;
- [ ] the app server reaches SQL Server with `Encrypt=yes` and a trusted certificate;
- [ ] the IAM role can use the KMS key with the encryption context above, and the bucket prefix;
- [ ] a first start creates the tables, and `schema_log` lists only CREATE and ALTER ... ADD statements;
- [ ] a direct `SELECT` on `lake.claims` by another account shows `ecl1:` values only.
