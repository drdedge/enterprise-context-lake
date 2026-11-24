"""SQL Server store, KMS column encryption and S3 file storage (ADR-010).

Two groups of tests:

* Offline (always run): the cipher, settings validation, the S3 blob store and S3-backed uploads.
  KMS and S3 are stood in by ``moto`` in-process, so nothing leaves the machine.
* Live (run when ``LAKE_TEST_SQLSERVER_URL`` points at a SQL Server 2019+ test database): one
  transaction per approval, history in SQL Server, encryption at rest, the ``lake_`` prefix mode,
  additive migrations, large IN lists, case-sensitive keys and the overwrite guard. Each test uses its
  own throwaway ``lake_test_*`` schema and drops it afterwards.

The adapter conformance suite (tests/test_adapter_conformance.py) also runs against SQL Server when the
same variable is set, and ``LAKE_TEST_API_ON_SQLSERVER=1`` runs every API suite on it.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from conftest import build_principal_from_fixture
from context_adapters.blob_store import LocalBlobStore, S3BlobStore
from context_adapters.sealed import PREFIX, KmsEnvelopeCipher, LocalKeyCipher, SealError
from context_adapters.sqlserver_store import SqlServerSettings

SQLSERVER_TEST_URL = os.environ.get("LAKE_TEST_SQLSERVER_URL", "").strip()
live = pytest.mark.skipif(not SQLSERVER_TEST_URL, reason="LAKE_TEST_SQLSERVER_URL not set")

moto = pytest.importorskip("moto", reason="moto stands in for AWS KMS and S3")


# ---------------------------------------------------------------------------
# Cipher
# ---------------------------------------------------------------------------


def test_local_cipher_round_trip_tamper_and_plaintext_passthrough() -> None:
    cipher = LocalKeyCipher(os.urandom(32))
    sealed = cipher.seal("Project Cobalt: LME settlement at 97.5")
    assert sealed.startswith(PREFIX) and "Cobalt" not in sealed
    assert cipher.open(sealed) == "Project Cobalt: LME settlement at 97.5"
    # Two seals of the same text differ (random nonce)
    assert cipher.seal("x") != cipher.seal("x")
    # Flip one character of the ciphertext: authentication fails instead of returning altered text
    head, ct = sealed.rsplit(":", 1)
    tampered = head + ":" + ("A" if ct[0] != "A" else "B") + ct[1:]
    with pytest.raises(SealError):
        cipher.open(tampered)
    # Another key cannot read it
    with pytest.raises(SealError):
        LocalKeyCipher(os.urandom(32)).open(sealed)


def test_sealed_text_is_plain_on_sqlite_and_needs_a_cipher_to_read() -> None:
    from sqlalchemy.dialects import sqlite

    from context_adapters.sealed import SealedText, attach_cipher

    t = SealedText()
    d = sqlite.dialect()
    assert t.process_bind_param("hello", d) == "hello"  # no cipher: unchanged
    cipher = LocalKeyCipher(os.urandom(32))
    sealed = cipher.seal("hello")
    with pytest.raises(SealError):
        t.process_result_value(sealed, d)
    attach_cipher(d, cipher)
    assert t.process_result_value(t.process_bind_param("hello", d), d) == "hello"
    assert t.process_result_value("written before encryption", d) == "written before encryption"


def test_kms_envelope_cipher_calls_kms_once_per_data_key() -> None:
    import boto3

    with moto.mock_aws():
        kms = boto3.client("kms", region_name="eu-west-2")
        key_id = kms.create_key(Description="lake test")["KeyMetadata"]["KeyId"]
        calls: list[str] = []

        class Counting:
            def generate_data_key(self, **kw: Any) -> Any:
                calls.append("generate")
                assert kw["EncryptionContext"]["app"] == "enterprise-context-lake"
                return kms.generate_data_key(**kw)

            def decrypt(self, **kw: Any) -> Any:
                calls.append("decrypt")
                return kms.decrypt(**kw)

        writer = KmsEnvelopeCipher(key_id, client=Counting())
        values = [writer.seal(f"value {i}") for i in range(200)]
        assert calls == ["generate"]  # one data key for all 200 values

        reader = KmsEnvelopeCipher(key_id, client=Counting())  # e.g. another app server
        assert [reader.open(v) for v in values] == [f"value {i}" for i in range(200)]
        assert calls == ["generate", "decrypt"]  # unwrapped once, then cached

        # Rotation: a new data key after the limit, old values still readable
        rotating = KmsEnvelopeCipher(key_id, client=Counting(), rotate_after_values=2)
        a, b, c = rotating.seal("a"), rotating.seal("b"), rotating.seal("c")
        assert a.split(":")[1] == b.split(":")[1] != c.split(":")[1]
        assert reader.open(c) == "c"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _settings(**kw: Any) -> SqlServerSettings:
    base = {"url": "mssql+aioodbc://u@h/BSPAI?driver=ODBC+Driver+18+for+SQL+Server", "encryption": "off"}
    base.update(kw)
    return SqlServerSettings(**base)


def test_settings_refuse_bare_dbo_unsafe_names_and_plaintext_in_production(monkeypatch: Any) -> None:
    _settings().validate()  # own schema "lake"
    _settings(schema="dbo", table_prefix="lake_").validate()
    with pytest.raises(ValueError, match="own schema or a table prefix"):
        _settings(schema="dbo").validate()
    with pytest.raises(ValueError, match="Invalid SQL Server schema"):
        _settings(schema="lake];DROP TABLE x;--").validate()
    with pytest.raises(ValueError, match="LAKE_KMS_KEY_ID"):
        _settings(encryption="kms").validate()
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(ValueError, match="Production needs LAKE_ENCRYPTION=kms"):
        _settings().validate()


def test_settings_from_env_keep_the_password_out_of_the_url_variable() -> None:
    s = SqlServerSettings.from_env(
        {
            "LAKE_SQLSERVER_URL": "mssql+aioodbc://lake_svc@sql.internal:1433/BSPAI?driver=ODBC+Driver+18+for+SQL+Server",
            "LAKE_SQLSERVER_PASSWORD": "p@ss:w/rd",
            "LAKE_SQL_SCHEMA": "dbo",
            "LAKE_SQL_TABLE_PREFIX": "lake_",
            "LAKE_KMS_KEY_ID": "alias/context-lake",
        }
    )
    from sqlalchemy.engine import make_url

    assert make_url(s.url).password == "p@ss:w/rd"
    assert (s.schema, s.table_prefix, s.encryption, s.kms_key_id) == ("dbo", "lake_", "kms", "alias/context-lake")
    s.validate()


def test_registry_has_no_sql_server_store_unless_configured(tmp_path: Path) -> None:
    from context_adapters import create_default_registry

    reg = create_default_registry(okf_vault_dir=tmp_path / "vault")
    assert "sqlserver_bitemporal" not in reg._stores  # noqa: SLF001


# ---------------------------------------------------------------------------
# S3 file storage
# ---------------------------------------------------------------------------


def test_s3_blob_store_encrypts_with_kms_and_lists_keys() -> None:
    import boto3

    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="eu-west-2")
        s3.create_bucket(Bucket="lake-files", CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
        key_id = boto3.client("kms", region_name="eu-west-2").create_key()["KeyMetadata"]["KeyId"]
        blobs = S3BlobStore("lake-files", prefix="context-lake", kms_key_id=key_id, client=s3)

        uri = blobs.put("upl-1_terms.pdf", b"%PDF-1.7 ...", content_type="application/pdf")
        assert uri == "s3://lake-files/context-lake/upl-1_terms.pdf"
        assert blobs.get("upl-1_terms.pdf") == b"%PDF-1.7 ..."
        head = s3.head_object(Bucket="lake-files", Key="context-lake/upl-1_terms.pdf")
        assert head["ServerSideEncryption"] == "aws:kms" and key_id in head["SSEKMSKeyId"]
        blobs.put("records/upl-1.json", b"{}")
        assert blobs.list_keys("records") == ["records/upl-1.json"]
        # Keys cannot climb out of the prefix
        assert blobs.put("../../etc/passwd", b"x") == "s3://lake-files/context-lake/etc/passwd"


def test_local_blob_store_keeps_the_old_relative_paths(tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path / "data" / "uploads", repo_root=tmp_path)
    assert blobs.put("upl-1_a.md", b"# A") == "data/uploads/upl-1_a.md"
    assert (tmp_path / "data" / "uploads" / "upl-1_a.md").read_bytes() == b"# A"


@pytest.mark.asyncio
async def test_uploads_go_to_s3_and_survive_a_restart(tmp_path: Path) -> None:
    import boto3

    from context_api.uploads import UploadService
    from context_core.policy import PolicyEngine
    from context_core.seeder import seed_store_from_fixtures
    from context_adapters.sqlite_store import SqliteBitemporalStore

    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="eu-west-2")
        s3.create_bucket(Bucket="lake-files", CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
        blobs = S3BlobStore("lake-files", client=s3)
        policy = PolicyEngine()
        store = SqliteBitemporalStore(db_path=tmp_path / "lake.sqlite3", policy=policy)
        await store.initialize()
        await seed_store_from_fixtures(store)
        steward = build_principal_from_fixture("user:fund-steward")
        try:
            svc = UploadService(storage_dir=tmp_path / "uploads", blobs=blobs)
            res = await svc.ingest_upload(
                filename="memo.md",
                content_type="text/markdown",
                raw_bytes=b"---\nsubject: veh:alpha-credit-offshore-lp\n---\nThe fund closed at 450m.\n",
                principal=steward,
                store=store,
                policy=policy,
            )
            assert not (tmp_path / "uploads").exists()  # nothing written locally
            keys = blobs.list_keys("")
            assert any(k.endswith("_memo.md") for k in keys) and f"records/{res.upload_id}.json" in keys

            restarted = UploadService(storage_dir=tmp_path / "uploads", blobs=blobs)
            src = restarted.get_source(res.upload_id, principal=steward)
            assert src is not None and src["relative_path"].startswith("s3://lake-files/context-lake/")
        finally:
            await store.close()


# ---------------------------------------------------------------------------
# Live SQL Server
# ---------------------------------------------------------------------------


def _live_settings(**kw: Any) -> SqlServerSettings:
    base: dict[str, Any] = {
        "url": SQLSERVER_TEST_URL,
        "schema": f"lake_test_{uuid.uuid4().hex[:10]}",
        "encryption": "local",
        "local_master_key": os.urandom(32),
    }
    base.update(kw)
    return SqlServerSettings(**base)


@pytest.fixture
async def mssql_registry(tmp_path: Path):
    from context_adapters import create_default_registry
    from context_core.seeder import seed_store_from_fixtures

    settings = _live_settings()
    reg = create_default_registry(
        okf_vault_dir=tmp_path / "vault", sqlserver=settings, active_store="sqlserver_bitemporal"
    )
    store = reg.store
    await store.initialize()
    await seed_store_from_fixtures(store)
    try:
        yield reg
    finally:
        reg.ledger.close()
        try:
            await store.inner.drop_all_for_tests()
        finally:
            await reg.close_all()


def _raw(settings: SqlServerSettings, sql: str) -> list[Any]:
    """Read with a plain driver connection, as any other account on the shared database would."""
    import pyodbc
    from sqlalchemy.engine import make_url

    u = make_url(settings.url)
    odbc = (
        f"DRIVER={{{u.query.get('driver', 'ODBC Driver 18 for SQL Server')}}};SERVER={u.host},{u.port or 1433};"
        f"DATABASE={u.database};UID={u.username};PWD={u.password};TrustServerCertificate=yes"
    )
    conn = pyodbc.connect(odbc, autocommit=True)
    try:
        return conn.cursor().execute(sql).fetchall()
    finally:
        conn.close()


async def _note_body(reg: Any, steward: Any, item_id: str) -> str:
    items = await reg.store.list_knowledge_items(principal=steward)
    return next(i.body_markdown for i in items if i.item_id == item_id)


@live
@pytest.mark.asyncio
async def test_a_failed_merge_leaves_nothing_behind(mssql_registry: Any, monkeypatch: Any) -> None:
    from context_core.versioning import ChangeItemInput

    reg = mssql_registry
    steward = build_principal_from_fixture("user:fund-steward")
    note = "kf:dag-live-admin-nav"
    before = await _note_body(reg, steward, note)
    versions_before = len(reg.ledger.list_versions("knowledge_item", note))

    svc = reg.versions
    real_apply = svc._apply_item  # noqa: SLF001
    calls = {"n": 0}

    async def flaky_apply(item: Any, **kw: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("second item failed")
        await real_apply(item, **kw)

    monkeypatch.setattr(svc, "_apply_item", flaky_apply)
    items = [
        ChangeItemInput(record_type="knowledge_item", op="upsert", record_id=note, payload={"body_markdown": "First edit"}),
        ChangeItemInput(record_type="topic", op="patch", record_id="topic:cashflows-liquidity", payload={"pref_label": "Cash flows"}),
    ]
    with pytest.raises(RuntimeError, match="second item failed"):
        await svc.propose(title="Two edits", items=items, principal=steward)

    # The first item was applied inside the transaction, then rolled back with it
    assert calls["n"] == 2
    assert await _note_body(reg, steward, note) == before
    assert len(reg.ledger.list_versions("knowledge_item", note)) == versions_before
    failed = [cs for cs in reg.ledger.list_change_sets(statuses=["failed"]) if cs.title == "Two edits"]
    assert len(failed) == 1 and all(i.applied_version is None for i in failed[0].items)

    # And the same change goes through in full once nothing fails
    monkeypatch.setattr(svc, "_apply_item", real_apply)
    cs = await svc.propose(title="One edit", items=items[:1], principal=steward)
    assert cs.status == "merged"
    assert await _note_body(reg, steward, note) == "First edit"
    assert len(reg.ledger.list_versions("knowledge_item", note)) == versions_before + 1


@live
@pytest.mark.asyncio
async def test_record_and_history_row_commit_together(mssql_registry: Any, monkeypatch: Any) -> None:
    from context_adapters.sqlserver_ledger import SqlServerVersionLedger

    reg = mssql_registry
    steward = build_principal_from_fixture("user:fund-steward")
    note = "kf:dag-live-admin-nav"
    before = await _note_body(reg, steward, note)
    item = next(i for i in await reg.store.list_knowledge_items(principal=steward) if i.item_id == note)

    def broken_flush(self: Any, conn: Any, pending: Any) -> None:
        raise RuntimeError("history write failed")

    monkeypatch.setattr(SqlServerVersionLedger, "_write_pending", broken_flush)
    with pytest.raises(RuntimeError, match="history write failed"):
        await reg.store.upsert_knowledge_item(item.model_copy(update={"body_markdown": "Never saved"}))
    monkeypatch.undo()
    # The record write was rolled back with the history row it could not save
    assert await _note_body(reg, steward, note) == before


@live
@pytest.mark.asyncio
async def test_text_is_encrypted_at_rest_including_history(mssql_registry: Any) -> None:
    reg = mssql_registry
    inner = reg.store.inner
    schema = inner.schema_name
    deal = build_principal_from_fixture("user:private-deal-team")

    rows = _raw(inner.settings, f"SELECT object_value, provenance_json FROM [{schema}].[claims] WHERE wall_side = 'private_side'")
    assert rows and all(v.startswith(PREFIX) and p.startswith(PREFIX) for v, p in rows)
    names = _raw(inner.settings, f"SELECT display_name, description FROM [{schema}].[entities]")
    assert all(n.startswith(PREFIX) for n, _ in names)
    history = _raw(inner.settings, f"SELECT payload_json FROM [{schema}].[vc_record_versions] WHERE payload_json IS NOT NULL")
    assert history and all(p.startswith(PREFIX) for (p,) in history)
    bodies = _raw(inner.settings, f"SELECT body_json FROM [{schema}].[vc_change_sets]")
    assert bodies and all(b.startswith(PREFIX) for (b,) in bodies)
    raw_dump = " ".join(str(r) for r in rows + names + history)
    assert "CANARY_MNPI_COBALT_LME_9174" not in raw_dump and "Cobalt" not in raw_dump

    # Through the store, the same rows read back as plain text for an authorised caller
    claims = await reg.store.query_claims(principal=deal, subject_ids=["ent:proj-project-cobalt-lme"])
    assert any("CANARY_MNPI_COBALT_LME_9174" in c.object_value for c in claims)


@live
@pytest.mark.asyncio
async def test_prefix_mode_unicode_case_and_large_in_lists(tmp_path: Path) -> None:
    from context_adapters.sqlserver_store import SqlServerBitemporalStore
    from context_core.policy import PolicyEngine
    from context_core.seeder import seed_store_from_fixtures

    settings = _live_settings(table_prefix="lake_")
    store = SqlServerBitemporalStore(settings, policy=PolicyEngine())
    steward = build_principal_from_fixture("user:fund-steward")
    try:
        await store.initialize()
        await seed_store_from_fixtures(store)
        tables = {r[0] for r in _raw(settings, f"SELECT name FROM sys.tables WHERE schema_id = SCHEMA_ID('{settings.schema}')")}
        assert tables and all(t.startswith("lake_") for t in tables) and "lake_claims" in tables

        ent = await store.get_entity("veh:alpha-credit-offshore-lp", principal=steward)
        assert ent is not None
        # Binary collation: ids compare case-sensitively, as on SQLite (canonical keys are
        # lower-cased by the store on both)
        assert await store.get_entity(ent.entity_id.upper(), principal=steward) is None
        assert await store.get_entity("VEH:ALPHA-CREDIT-OFFSHORE-LP", principal=steward) is not None

        # Unicode survives (NVARCHAR + encryption round trip)
        renamed = ent.model_copy(update={"display_name": "Société Générale – 日本 Ünïcödé"})
        await store.upsert_entity(renamed)
        again = await store.get_entity(ent.entity_id, principal=steward)
        assert again is not None and again.display_name == "Société Générale – 日本 Ünïcödé"

        # 3,000 ids is past SQL Server's 2,100-parameter limit: sent as one OPENJSON parameter
        many = [f"ent:missing-{i}" for i in range(3000)] + [ent.entity_id]
        claims = await store.query_claims(principal=steward, subject_ids=many)
        assert claims and {c.subject_id for c in claims} == {ent.entity_id}
    finally:
        try:
            await store.drop_all_for_tests()
        finally:
            await store.close()


@live
@pytest.mark.asyncio
async def test_migrations_are_additive_and_logged(tmp_path: Path) -> None:
    from context_adapters.sqlserver_store import SqlServerBitemporalStore
    from context_core.exceptions import ValidationError
    from context_core.policy import PolicyEngine
    from context_core.seeder import seed_store_from_fixtures

    settings = _live_settings()
    store = SqlServerBitemporalStore(settings, policy=PolicyEngine())
    try:
        await store.initialize()
        log = await store.schema_log()
        assert any(s["statement"].startswith("CREATE SCHEMA") for s in log)
        assert any("CREATE TABLE" in s["statement"] for s in log)
        assert not any(word in s["statement"].upper() for s in log for word in ("DROP ", "ALTER COLUMN"))
        await seed_store_from_fixtures(store)
        await store.close()

        # Simulate an older database: a column the code now expects is missing. Start-up adds it back
        # (NULL-able) and leaves every existing row alone.
        import pyodbc  # noqa: F401  (the live tests need the driver anyway)

        count = _raw(settings, f"SELECT COUNT(*) FROM [{settings.schema}].[entities]")[0][0]
        _exec(settings, f"DROP INDEX ix_claims_acl_set_id ON [{settings.schema}].[claims]")
        _exec(settings, f"ALTER TABLE [{settings.schema}].[claims] DROP COLUMN acl_set_id")
        store = SqlServerBitemporalStore(settings, policy=PolicyEngine())
        await store.initialize()
        log2 = await store.schema_log()
        added = [s["statement"] for s in log2[len(log) :]]
        assert any("ADD [acl_set_id]" in s or "ADD acl_set_id" in s for s in added), added
        assert any("ix_claims_acl_set_id" in s for s in added), added
        assert _raw(settings, f"SELECT COUNT(*) FROM [{settings.schema}].[entities]")[0][0] == count

        # A second start-up has nothing left to do
        store2 = SqlServerBitemporalStore(settings, policy=PolicyEngine())
        await store2.initialize()
        assert len(await store2.schema_log()) == len(log2)
        await store2.close()

        # Seeding again would replace real data: refused unless explicitly allowed
        with pytest.raises(ValidationError, match="refusing to replace"):
            await seed_store_from_fixtures(store)
    finally:
        try:
            await store.drop_all_for_tests()
        finally:
            await store.close()


def _exec(settings: SqlServerSettings, sql: str) -> None:
    import pyodbc
    from sqlalchemy.engine import make_url

    u = make_url(settings.url)
    odbc = (
        f"DRIVER={{{u.query.get('driver', 'ODBC Driver 18 for SQL Server')}}};SERVER={u.host},{u.port or 1433};"
        f"DATABASE={u.database};UID={u.username};PWD={u.password};TrustServerCertificate=yes"
    )
    conn = pyodbc.connect(odbc, autocommit=True)
    try:
        conn.cursor().execute(sql)
    finally:
        conn.close()


@live
@pytest.mark.asyncio
async def test_kms_encryption_against_a_kms_stand_in(tmp_path: Path) -> None:
    """The KMS path end to end: data keys from (moto) KMS, values in SQL Server, read by a new process."""
    import boto3

    from context_adapters.sqlserver_store import SqlServerBitemporalStore
    from context_core.policy import PolicyEngine
    from context_core.seeder import seed_store_from_fixtures

    with moto.mock_aws():
        kms = boto3.client("kms", region_name="eu-west-2")
        key_id = kms.create_key()["KeyMetadata"]["KeyId"]
        settings = _live_settings(encryption="kms", kms_key_id=key_id, local_master_key=None)
        store = SqlServerBitemporalStore(settings, policy=PolicyEngine(), cipher=KmsEnvelopeCipher(key_id, client=kms))
        deal = build_principal_from_fixture("user:private-deal-team")
        try:
            await store.initialize()
            await seed_store_from_fixtures(store)
            await store.close()
            # A fresh store (another app server) with its own KMS client reads the same rows
            other = SqlServerBitemporalStore(settings, policy=PolicyEngine(), cipher=KmsEnvelopeCipher(key_id, client=kms))
            claims = await other.query_claims(principal=deal, subject_ids=["ent:proj-project-cobalt-lme"])
            assert claims and not claims[0].object_value.startswith(PREFIX)
            await other.close()
        finally:
            try:
                await store.drop_all_for_tests()
            finally:
                await store.close()

