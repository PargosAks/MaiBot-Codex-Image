"""Exact, read-only inspiration receipts over isolated synthetic SQLite state."""

from contextlib import contextmanager
import hashlib
import sqlite3

import pytest

from image_plugin_under_test.policy import Delegation, Scope, StoreError
from image_plugin_under_test.store import ArtistStore, _SCHEMA
from image_plugin_under_test.studio import Studio


OWNER = Scope("synthetic", "private-stream", "owner")
OTHER = Scope("synthetic", "other-stream", "other-owner")
MESSAGE = "host-message"


@pytest.fixture
def ledger(tmp_path):
    store = ArtistStore(tmp_path / "artist.sqlite3")
    for key, scope in (("a", OWNER), ("b", OTHER)):
        store.create_space(key, scope, binding_id="synthetic-binding")
        store.grant(key, scope, Delegation("explicit-synthetic-consent", "synthetic-admin", True))
    yield store
    store.close()


def enqueue(store, space="a", scope=OWNER, message=MESSAGE):
    return store.enqueue_wake(space, scope, event_id="chat:" + message, kind="chat",
                              source_ref=message, text="private synthetic body must never be read")


def view(store, **kwargs):
    return Studio(store.path.parent, scopes={"a": OWNER}, **kwargs)


def error(code):
    return pytest.raises(StoreError, check=lambda exc: exc.code == code)


def snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def test_exact_minimal_receipt_is_idempotent_and_changes_no_bytes(ledger):
    enqueue(ledger)
    before = snapshot(ledger.path.parent)
    reader = view(ledger)
    expected = {"space_id": "a", "read_only": True,
                "authorization": {"authorized": True, "version": 1}, "inspiration_received": True}
    assert reader.inspiration_receipt("a", MESSAGE, expected_auth_version=1) == expected
    assert reader.inspiration_receipt("a", MESSAGE, expected_auth_version=1) == expected
    assert enqueue(ledger) is False  # duplicate Host ingress is not a new receipt
    assert snapshot(ledger.path.parent) == before


@pytest.mark.parametrize("wrong_scope", [OTHER, Scope("foreign", OWNER.stream_id, OWNER.user_id),
                                        Scope(OWNER.platform, "foreign-stream", OWNER.user_id),
                                        Scope(OWNER.platform, OWNER.stream_id, "foreign-owner")])
def test_persisted_scope_must_match_all_identity_fields(ledger, wrong_scope):
    enqueue(ledger)
    with error("NOT_FOUND"):
        Studio(ledger.path.parent, scopes={"a": wrong_scope}).inspiration_receipt(
            "a", MESSAGE, expected_auth_version=1)


def test_foreign_space_and_same_message_in_foreign_space_are_not_receipts(ledger):
    enqueue(ledger, "b", OTHER)
    with error("NOT_FOUND"):
        view(ledger).inspiration_receipt("b", MESSAGE, expected_auth_version=1)
    with error("NOT_FOUND"):
        view(ledger).inspiration_receipt("a", MESSAGE, expected_auth_version=1)
    enqueue(ledger)
    receipt = view(ledger).inspiration_receipt("a", MESSAGE, expected_auth_version=1)
    assert receipt["space_id"] == "a" and "other-owner" not in str(receipt)


@pytest.mark.parametrize("kind,event_id,source", [
    ("idle", "chat:" + MESSAGE, MESSAGE),
    ("completion", "chat:" + MESSAGE, MESSAGE),
    ("chat", MESSAGE, MESSAGE),
    ("chat", "chat:" + MESSAGE + "-suffix", MESSAGE),
    ("chat", "CHAT:" + MESSAGE, MESSAGE),
    ("Chat", "chat:" + MESSAGE, MESSAGE),
    ("chat", "chat:" + MESSAGE, "other-message"),
    ("chat", "chat:" + MESSAGE, MESSAGE + " "),
    ("chat", "chat:" + MESSAGE, None),
])
def test_kind_event_key_and_source_reference_must_all_match(ledger, kind, event_id, source):
    ledger.db.execute("INSERT INTO artist_wakes(space_id,event_id,kind,text,source_ref) VALUES(?,?,?,?,?)",
                      ("a", event_id, kind, "do not read this body", source))
    with error("NOT_FOUND"):
        view(ledger).inspiration_receipt("a", MESSAGE, expected_auth_version=1)


def test_matching_fields_on_separate_rows_cannot_be_combined(ledger):
    ledger.db.executemany("INSERT INTO artist_wakes(space_id,event_id,kind,text,source_ref) VALUES(?,?,?,?,?)", [
        ("a", "chat:" + MESSAGE, "idle", "private", MESSAGE),
        ("a", "chat:another", "chat", "private", MESSAGE),
    ])
    with error("NOT_FOUND"):
        view(ledger).inspiration_receipt("a", MESSAGE, expected_auth_version=1)


@pytest.mark.parametrize("message", [None, 1, True, b"host-message", "", "   ", "x" * 196])
def test_message_id_type_and_bound_are_checked(ledger, message):
    with error("INVALID_ARGUMENT"):
        view(ledger).inspiration_receipt("a", message, expected_auth_version=1)


@pytest.mark.parametrize("message", ["x" * 195, "中文 Host 消息", "quote' OR 1=1 --", " spaced ", "chat:already-prefixed"])
def test_message_keys_are_bound_exactly_without_normalization_or_sql_interpolation(ledger, message):
    enqueue(ledger, message=message)
    assert view(ledger).inspiration_receipt("a", message, expected_auth_version=1)["inspiration_received"] is True
    with error("NOT_FOUND"):
        view(ledger).inspiration_receipt(
            "a", message + "x" if len(message) < 195 else message[:-1], expected_auth_version=1)


@pytest.mark.parametrize("expected_version", [1, 2])
def test_revocation_rejects_existing_receipt(ledger, expected_version):
    enqueue(ledger)
    ledger.revoke("a", OWNER, source_ref="synthetic-revoke", granted_by="synthetic-admin")
    with error("AUTH_REQUIRED"):
        view(ledger).inspiration_receipt("a", MESSAGE, expected_auth_version=expected_version)


@pytest.mark.parametrize("revoke_first", [False, True])
def test_reauthorization_allows_genuinely_new_ingress_with_current_version(ledger, revoke_first):
    enqueue(ledger)
    if revoke_first:
        ledger.revoke("a", OWNER, source_ref="synthetic-revoke", granted_by="synthetic-admin")
    version = ledger.grant("a", OWNER, Delegation("fresh-consent", "synthetic-admin", True))
    assert enqueue(ledger) is False
    assert enqueue(ledger, message="new-after-regrant") is True
    before = snapshot(ledger.path.parent)
    reader = view(ledger)
    # The plugin only requests receipts for newly committed ingress; freshness
    # is not inferred from the legacy wake schema by this read model.
    expected = {"space_id": "a", "read_only": True,
                "authorization": {"authorized": True, "version": version}, "inspiration_received": True}
    assert reader.inspiration_receipt("a", "new-after-regrant", expected_auth_version=version) == expected
    assert reader.inspiration_receipt("a", "new-after-regrant", expected_auth_version=version) == expected
    for message in (MESSAGE, "new-after-regrant"):
        with error("AUTH_CHANGED"):
            reader.inspiration_receipt("a", message, expected_auth_version=1)
    assert snapshot(ledger.path.parent) == before


@pytest.mark.parametrize("current_version,expected_version", [(1, 2), (2, 1), (2, 3), (3, 1), (3, 2), (3, 4)])
def test_expected_version_must_equal_current_grant(ledger, current_version, expected_version):
    for _ in range(1, current_version):
        ledger.grant("a", OWNER, Delegation("renewed-consent", "synthetic-admin", True))
    enqueue(ledger)
    before = snapshot(ledger.path.parent)
    with error("AUTH_CHANGED"):
        view(ledger).inspiration_receipt("a", MESSAGE, expected_auth_version=expected_version)
    assert snapshot(ledger.path.parent) == before


@pytest.mark.parametrize("version", [None, True, False, "1", 1.0, 0, -1])
def test_expected_version_requires_a_positive_integer(ledger, version):
    enqueue(ledger)
    with error("INVALID_ARGUMENT"):
        view(ledger).inspiration_receipt("a", MESSAGE, expected_auth_version=version)


def test_expected_version_is_required_and_keyword_only(ledger):
    reader = view(ledger)
    with pytest.raises(TypeError):
        reader.inspiration_receipt("a", MESSAGE)
    with pytest.raises(TypeError):
        reader.inspiration_receipt("a", MESSAGE, 1)


@pytest.mark.parametrize("state", ["disabled", "never-granted", "missing-grant", "revoked-audit", "invalid-version",
                                 "missing-current-grant"])
def test_active_continuous_delegation_is_required(ledger, state):
    enqueue(ledger)
    if state == "never-granted":
        ledger.db.execute("UPDATE spaces SET authorized=0,auth_version=0 WHERE space_id='a'")
    elif state == "missing-grant":
        ledger.db.execute("DELETE FROM grants WHERE space_id='a'")
    elif state == "revoked-audit":
        ledger.db.execute("UPDATE grants SET authorized=0 WHERE space_id='a'")
    elif state == "invalid-version":
        ledger.db.execute("UPDATE spaces SET auth_version=0 WHERE space_id='a'")
    elif state == "missing-current-grant":
        ledger.db.execute("UPDATE spaces SET auth_version=2 WHERE space_id='a'")
    expected_version = 2 if state == "missing-current-grant" else 1
    before = snapshot(ledger.path.parent)
    with error("AUTH_REQUIRED"):
        view(ledger, enabled=False if state == "disabled" else None).inspiration_receipt(
            "a", MESSAGE, expected_auth_version=expected_version)
    assert snapshot(ledger.path.parent) == before


@pytest.mark.parametrize("version", [-1, 0, "invalid", 1.5])
def test_invalid_persisted_version_cannot_authorize_a_receipt(ledger, version):
    enqueue(ledger)
    ledger.db.execute("UPDATE spaces SET auth_version=? WHERE space_id='a'", (version,))
    with error("AUTH_REQUIRED"):
        view(ledger).inspiration_receipt("a", MESSAGE, expected_auth_version=1)


def test_absent_database_is_not_created(tmp_path):
    root = tmp_path / "absent"
    with error("NOT_FOUND"):
        Studio(root, scopes={"a": OWNER}).inspiration_receipt("a", MESSAGE, expected_auth_version=1)
    assert not root.exists()


def test_schema_without_wakes_is_not_migrated(tmp_path):
    db = sqlite3.connect(tmp_path / "artist.sqlite3")
    for statement in _SCHEMA:
        db.execute(statement)
    db.execute("INSERT INTO spaces VALUES(?,?,?,?,?)", ("a", OWNER.snapshot(), "synthetic-binding", 1, 1))
    db.execute("INSERT INTO grants VALUES(?,?,?,?,?,?)", ("a", 1, 1, "consent", "admin", "synthetic-time"))
    db.execute("PRAGMA user_version=1")
    db.commit()
    db.close()
    before = snapshot(tmp_path)
    with error("NOT_FOUND"):
        Studio(tmp_path, scopes={"a": OWNER}).inspiration_receipt("a", MESSAGE, expected_auth_version=1)
    assert snapshot(tmp_path) == before


def test_receipt_does_not_require_unprocessed_work_or_model_result(ledger):
    enqueue(ledger)
    wake_id = ledger.db.execute("SELECT id FROM artist_wakes WHERE space_id='a' AND event_id=?",
                                ("chat:" + MESSAGE,)).fetchone()[0]
    ledger.db.execute("INSERT INTO artist_state(space_id,acked_through) VALUES(?,?)", ("a", wake_id))
    assert view(ledger).inspiration_receipt("a", MESSAGE, expected_auth_version=1)["inspiration_received"] is True


@pytest.mark.parametrize("current_version", [1, 2, 3])
def test_receipt_uses_only_indexed_metadata_and_one_read_transaction(ledger, current_version):
    for _ in range(1, current_version):
        ledger.grant("a", OWNER, Delegation("renewed-consent", "synthetic-admin", True))
    enqueue(ledger)
    traces, reads = [], []
    forbidden_tables = {"works", "artist_works", "operations", "inspirations", "versions", "reviews",
                        "artist_state", "artist_claims", "external_assets"}

    class GuardedStudio(Studio):
        @contextmanager
        def _open(self):
            with super()._open() as db:
                db.set_trace_callback(traces.append)

                def authorize(action, table, column, database, trigger):
                    if action == sqlite3.SQLITE_READ:
                        reads.append((table, column))
                        if table in forbidden_tables or (table == "artist_wakes" and column == "text"):
                            return sqlite3.SQLITE_DENY
                    if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_ATTACH}:
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                db.set_authorizer(authorize)
                assert db.in_transaction and db.execute("PRAGMA query_only").fetchone()[0] == 1
                yield db

    receipt = GuardedStudio(ledger.path.parent, scopes={"a": OWNER}).inspiration_receipt(
        "a", MESSAGE, expected_auth_version=current_version)
    assert receipt["inspiration_received"] is True
    assert ("artist_wakes", "text") not in reads
    assert not any("SELECT *" in sql.upper() or "ATTACH" in sql.upper() for sql in traces)
    plan = ledger.db.execute("""EXPLAIN QUERY PLAN SELECT 1 FROM artist_wakes
        WHERE space_id=? AND event_id=? AND kind='chat' AND source_ref=? LIMIT 1""",
                             ("a", "chat:" + MESSAGE, MESSAGE)).fetchall()
    assert any("SEARCH artist_wakes USING INDEX" in row[3] for row in plan)
    assert not any("SCAN artist_wakes" in row[3] for row in plan)


@pytest.mark.parametrize("change", ["revoke", "renew"])
def test_authority_and_receipt_share_one_snapshot_during_concurrent_authorization_change(ledger, change):
    enqueue(ledger)
    ledger.db.execute("PRAGMA journal_mode=WAL")

    class InterleavedStudio(Studio):
        def _space(self, db, space_id):
            space = super()._space(db, space_id)
            if change == "revoke":
                ledger.revoke("a", OWNER, source_ref="concurrent-revoke", granted_by="synthetic-admin")
            else:
                ledger.grant("a", OWNER, Delegation("concurrent-consent", "synthetic-admin", True))
            return space

    receipt = InterleavedStudio(ledger.path.parent, scopes={"a": OWNER}).inspiration_receipt(
        "a", MESSAGE, expected_auth_version=1)
    assert receipt["authorization"] == {"authorized": True, "version": 1}
    with error("AUTH_REQUIRED" if change == "revoke" else "AUTH_CHANGED"):
        view(ledger).inspiration_receipt("a", MESSAGE, expected_auth_version=1)
