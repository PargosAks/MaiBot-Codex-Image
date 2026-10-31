"""I5 original image storage and one-claim registration ledger, entirely offline."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier
import hashlib
import sqlite3

from PIL import Image
import pytest

from image_plugin_under_test import store as store_module
from image_plugin_under_test.assets import reference_prompt
from image_plugin_under_test.policy import Delegation, Limits, Scope, StoreError
from image_plugin_under_test.store import ArtistStore
from test_assets_store import BRIEF, make_schema_4, reviewed


SCOPE = Scope("test", "reference-stream", "reference-owner")
OTHER = Scope("test", "other-stream", "other-owner")
CONSENT = "explicit-current-image-consent"


def error(code="INVALID_ARGUMENT"):
    return pytest.raises(StoreError, check=lambda exc: exc.code == code)


def picture(format="PNG", *, color="blue", size=(3, 2)):
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format=format)
    return buffer.getvalue()


def policy(db, enabled=True, consent=CONSENT, auth=1):
    return db.configure_reference_policy("s", SCOPE, enabled=enabled, consent_ref=consent,
                                         expected_auth_version=auth)


def import_image(db, event="event", index=0, *, data=None, mime="image/png", consent=CONSENT, auth=1, **changes):
    arguments = {"event_id": event, "message_index": index, "data": picture() if data is None else data,
                 "mime": mime, "roles": ["style", "palette"], "tags": ["host-image"],
                 "description": "Host current-message image metadata; no image analysis", "consent_ref": consent,
                 "expected_auth_version": auth}
    arguments.update(changes)
    return db.import_asset("s", SCOPE, **arguments)


def selection(asset):
    return [{"asset_id": asset["asset_id"], "role": "style", "reason": "use the supplied reference"}]


def plan(db, asset, *, work="w", usage="image_input"):
    return db.plan_work("s", SCOPE, work_id=work, brief=BRIEF, inspiration_keys=[], references=selection(asset),
                        reference_usage=usage, expected_auth_version=1)


def receipt(asset, remote="remote-asset"):
    return {"asset_id": remote, **{key: asset[key] for key in ("sha256", "mime", "size_bytes")}}


@pytest.fixture
def ledger(tmp_path):
    db = ArtistStore(tmp_path / "references.sqlite3")
    db.create_space("s", SCOPE, binding_id="reference-service")
    db.grant("s", SCOPE, Delegation("continuous-creative-consent", "host-admin", continuous=True))
    yield db
    db.close()


def test_reference_policy_is_explicit_versioned_idempotent_and_not_creative_authority(ledger):
    with error("REFERENCE_AUTH_REQUIRED"):
        ledger.validate_reference_authorization("s", SCOPE, expected_auth_version=1)
    for enabled, consent in ((True, ""), (True, " "), (1, CONSENT), (True, "x" * 501)):
        with error():
            ledger.configure_reference_policy("s", SCOPE, enabled=enabled, consent_ref=consent,
                                               expected_auth_version=1)
    first = policy(ledger)
    assert first == {"enabled": True, "consent_ref": CONSENT, "version": 1, "auth_version": 1}
    assert policy(ledger) == first
    assert ledger.validate_reference_authorization("s", SCOPE, expected_auth_version=1) == first
    assert policy(ledger, False, "")["version"] == 2
    with error("REFERENCE_AUTH_REQUIRED"):
        ledger.validate_reference_authorization("s", SCOPE, expected_auth_version=1)
    assert policy(ledger)["version"] == 3
    assert len(ledger.audit_authorizations("s", SCOPE)) == 1
    assert ledger.audit_operations("s", SCOPE) == []
    assert ledger.db.execute("SELECT count(*) FROM limits").fetchone()[0] == 0


@pytest.mark.parametrize("epoch", [None, True, -1, "1"])
def test_reference_policy_requires_an_explicit_integer_authorization_epoch(ledger, epoch):
    policy(ledger)
    with error():
        ledger.validate_reference_authorization("s", SCOPE, expected_auth_version=epoch)
    with error():
        ledger.configure_reference_policy("s", SCOPE, enabled=True, consent_ref=CONSENT,
                                           expected_auth_version=epoch)


def test_import_is_default_denied_without_an_explicit_reference_delegation(ledger):
    with error("REFERENCE_AUTH_REQUIRED"):
        import_image(ledger)
    assert ledger.db.execute("SELECT count(*) FROM asset_catalog").fetchone()[0] == 0


@pytest.mark.parametrize("format,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_external_import_keeps_valid_original_bytes_and_genuinely_null_work_lineage(ledger, format, mime):
    policy(ledger)
    data = picture(format)
    asset = import_image(ledger, data=data, mime=mime)
    assert asset["source_kind"] == "authorized_chat"
    assert all(asset[key] is None for key in ("source_version_id", "work_id", "job_id", "review_request_id"))
    assert "source_metadata" in asset["summary"] and "未进行图像分析" in asset["summary"]
    assert ledger.get_asset_image("s", SCOPE, asset["asset_id"]) == {
        "data": data, "mime": mime, "sha256": hashlib.sha256(data).hexdigest(),
    }
    assert ledger.db.execute("SELECT count(*) FROM versions").fetchone()[0] == 0
    assert ledger.db.execute("SELECT count(*) FROM works").fetchone()[0] == 0
    assert ledger.audit_operations("s", SCOPE) == []
    assert import_image(ledger, data=data, mime=mime) == asset
    second = import_image(ledger, index=1, data=data, mime=mime)
    assert second["asset_id"] != asset["asset_id"]


def test_event_identity_cannot_be_rebound_to_other_bytes_mime_or_import_metadata(ledger):
    policy(ledger)
    asset = import_image(ledger)
    for changes in ({"data": picture(color="red")}, {"description": "changed description"},
                    {"roles": ["character"]}, {"tags": ["different"]}):
        with error("REQUEST_CONFLICT"):
            import_image(ledger, **changes)
    assert ledger.get_asset("s", SCOPE, asset["asset_id"]) == asset
    assert ledger.db.execute("SELECT count(*) FROM external_assets").fetchone()[0] == 1


def test_external_description_is_bounded_but_full_original_metadata_remains_immutable(ledger):
    policy(ledger)
    asset = import_image(ledger, description="x" * 1000)
    assert len(asset["description"]) <= 1000 and len(asset["summary"]) <= 1000
    assert asset["description"].startswith("来源元数据 source_metadata")
    assert ledger.db.execute("SELECT source_description FROM external_assets").fetchone()[0] == "x" * 1000
    with error("REQUEST_CONFLICT"):
        import_image(ledger, description="x" * 999 + "y")


@pytest.mark.parametrize("data,mime,code", [
    (b"", "image/png", "INVALID_ARGUMENT"), (b"not an image", "image/png", "MEDIA_FORMAT_INVALID"),
    (b"x" * (8 * 1024 * 1024 + 1), "image/png", "INVALID_ARGUMENT"),
    (picture("PNG"), "image/jpeg", "ARTIFACT_INVALID"), (picture("GIF"), "image/png", "MEDIA_FORMAT_INVALID"),
    (picture("JPEG")[:-10], "image/jpeg", "MEDIA_FORMAT_INVALID"),
], ids=["empty", "bad-format", "over-size", "wrong-mime", "gif", "truncated-jpeg"])
def test_import_rejects_bad_original_format_mime_and_size(ledger, data, mime, code):
    policy(ledger)
    with error(code):
        import_image(ledger, data=data, mime=mime)
    assert ledger.list_assets("s", SCOPE) == []


def test_import_rejects_animation_and_over_64_megapixel_images(ledger):
    policy(ledger)
    buffer = BytesIO()
    Image.new("RGB", (3, 2), "blue").save(buffer, format="PNG", save_all=True,
                                          append_images=[Image.new("RGB", (3, 2), "red")], duration=100)
    with error("MEDIA_ANIMATION_UNSUPPORTED"):
        import_image(ledger, data=buffer.getvalue())
    buffer = BytesIO()
    Image.new("1", (8001, 8000)).save(buffer, format="PNG")
    with error("MEDIA_PIXELS_EXCEEDED"):
        import_image(ledger, data=buffer.getvalue())


def test_import_rechecks_current_policy_after_image_decoder_finishes(ledger, monkeypatch):
    policy(ledger)
    validate = store_module.validate_asset_image

    def change_consent(data, mime):
        image = validate(data, mime)
        policy(ledger, consent="new-consent")
        return image

    monkeypatch.setattr(store_module, "validate_asset_image", change_consent)
    with error("REFERENCE_AUTH_CHANGED"):
        import_image(ledger)
    assert ledger.db.execute("SELECT count(*) FROM external_assets").fetchone()[0] == 0


def test_reference_modes_freeze_policy_and_keep_old_text_snapshot_semantics(ledger):
    policy(ledger)
    asset = import_image(ledger)
    work = plan(ledger, asset)
    frozen = work["brief"]["references"]
    assert frozen[0]["source_version_id"] is None
    assert (frozen[0]["usage"], frozen[0]["policy_version"], frozen[0]["consent_ref"]) == ("image_input", 1, CONSENT)
    prompt = reference_prompt(BRIEF["prompt"], frozen)
    assert "实际参考图片输入" in prompt and "尚未审阅图片内容" in prompt
    assert "评审摘要：" not in prompt
    with error("REQUEST_CONFLICT"):
        plan(ledger, asset, usage="text_context")
    policy(ledger, False, "")
    assert plan(ledger, asset) == work  # Historical inspection is not dispatch.
    with error("REFERENCE_AUTH_REQUIRED"):
        ledger.validate_reference_authorization("s", SCOPE, expected_auth_version=1)
    ledger.update_artist_work("s", SCOPE, "w", changes={"status": "finished"})
    with error("REFERENCE_AUTH_REQUIRED"):
        plan(ledger, asset, work="blocked")
    policy(ledger)
    text = plan(ledger, asset, work="text", usage="text_context")["brief"]["references"]
    assert text[0]["usage"] == "text_context" and "policy_version" not in text[0]
    assert "仅文字灵感，未输入参考图" in reference_prompt(BRIEF["prompt"], text)


def test_new_consent_cannot_silently_export_an_old_external_image(ledger):
    policy(ledger)
    asset = import_image(ledger)
    policy(ledger, consent="new-consent")
    for usage in ("text_context", "image_input"):
        with error("REFERENCE_AUTH_CHANGED"):
            plan(ledger, asset, usage=usage)
    with error("REFERENCE_AUTH_CHANGED"):
        ledger.reserve_asset_registration("s", SCOPE, asset_id=asset["asset_id"], expected_auth_version=1)
    assert ledger.get_asset_image("s", SCOPE, asset["asset_id"])["data"] == picture()


def test_registration_has_one_claim_fixed_request_and_no_model_render_or_budget_operations(ledger):
    policy(ledger)
    asset = import_image(ledger)
    first = ledger.reserve_asset_registration("s", SCOPE, asset_id=asset["asset_id"], expected_auth_version=1)
    assert first["state"] == "reserved" and first["binding_id"] == "reference-service"
    assert first["sha256"] == asset["sha256"] and first["size_bytes"] == len(picture())
    assert ledger.reserve_asset_registration("s", SCOPE, asset_id=asset["asset_id"], expected_auth_version=1) == first
    with error("INVALID_STATE"):
        ledger.complete_asset_registration("s", SCOPE, asset["asset_id"], receipt(asset))
    with error("INVALID_STATE"):
        ledger.mark_asset_registration_unknown("s", SCOPE, asset["asset_id"], "not dispatched")
    claimed = ledger.claim_asset_registration("s", SCOPE, asset["asset_id"], expected_auth_version=1)
    assert claimed["state"] == "submitting" and claimed["request_id"] == first["request_id"]
    with error("INVALID_STATE"):
        ledger.claim_asset_registration("s", SCOPE, asset["asset_id"], expected_auth_version=1)
    assert ledger.audit_operations("s", SCOPE) == []
    assert ledger.db.execute("SELECT count(*) FROM limits").fetchone()[0] == 0


@pytest.mark.parametrize("restart_state", ["submitting", "unknown"])
def test_restart_and_reauthorization_preserve_original_uncertain_registration(ledger, restart_state):
    policy(ledger)
    asset = import_image(ledger)
    ledger.reserve_asset_registration("s", SCOPE, asset_id=asset["asset_id"], expected_auth_version=1)
    ledger.claim_asset_registration("s", SCOPE, asset["asset_id"], expected_auth_version=1)
    if restart_state == "unknown":
        ledger.mark_asset_registration_unknown("s", SCOPE, asset["asset_id"], "response lost")
    first = ledger.get_asset_registration("s", SCOPE, asset["asset_id"])
    ledger.revoke("s", SCOPE, source_ref="withdrawn", granted_by="admin")
    ledger.grant("s", SCOPE, Delegation("new-creative-consent", "admin", continuous=True))
    policy(ledger, auth=3)
    reopened = ArtistStore(ledger.path)
    try:
        assert reopened.get_asset_registration("s", SCOPE, asset["asset_id"]) == first
        assert reopened.reserve_asset_registration("s", SCOPE, asset_id=asset["asset_id"], expected_auth_version=3) == first
        with error("INVALID_STATE"):
            reopened.claim_asset_registration("s", SCOPE, asset["asset_id"], expected_auth_version=3)
        result = reopened.complete_asset_registration("s", SCOPE, asset["asset_id"], receipt(asset))
        assert result["state"] == "succeeded" and result["auth_version"] == 1
        assert result["request_id"] == first["request_id"]
    finally:
        reopened.close()


def test_old_reserved_registration_cannot_claim_after_policy_or_authorization_changes(ledger):
    policy(ledger)
    asset = import_image(ledger)
    reserved = ledger.reserve_asset_registration("s", SCOPE, asset_id=asset["asset_id"], expected_auth_version=1)
    policy(ledger, False, "")
    policy(ledger)
    with error("REFERENCE_AUTH_CHANGED"):
        ledger.claim_asset_registration("s", SCOPE, asset["asset_id"], expected_auth_version=1)
    assert ledger.get_asset_registration("s", SCOPE, asset["asset_id"]) == reserved
    ledger.revoke("s", SCOPE, source_ref="withdrawn", granted_by="admin")
    ledger.grant("s", SCOPE, Delegation("new-creative-consent", "admin", continuous=True))
    with error("AUTH_CHANGED"):
        ledger.validate_reference_authorization("s", SCOPE, expected_auth_version=3)
    policy(ledger, auth=3)
    with error("AUTH_CHANGED"):
        ledger.claim_asset_registration("s", SCOPE, asset["asset_id"], expected_auth_version=3)


def test_completion_is_immutable_recovery_after_revocation_and_never_downgrades_terminal_state(ledger):
    policy(ledger)
    asset = import_image(ledger)
    ledger.reserve_asset_registration("s", SCOPE, asset_id=asset["asset_id"], expected_auth_version=1)
    ledger.claim_asset_registration("s", SCOPE, asset["asset_id"], expected_auth_version=1)
    first = ledger.mark_asset_registration_unknown("s", SCOPE, asset["asset_id"], "lost response")
    assert ledger.mark_asset_registration_unknown("s", SCOPE, asset["asset_id"], "still lost") == first
    ledger.deactivate_asset("s", SCOPE, asset["asset_id"], expected_auth_version=1)
    ledger.revoke("s", SCOPE, source_ref="withdrawn", granted_by="admin")
    policy_row = ledger.db.execute("SELECT * FROM reference_policy").fetchone()[:]
    assert ledger.get_asset_image("s", SCOPE, asset["asset_id"])["data"] == picture()
    completed = ledger.complete_asset_registration("s", SCOPE, asset["asset_id"], receipt(asset))
    assert completed["state"] == "succeeded" and completed["receipt"] == receipt(asset)
    assert ledger.complete_asset_registration("s", SCOPE, asset["asset_id"], receipt(asset)) == completed
    assert ledger.mark_asset_registration_unknown("s", SCOPE, asset["asset_id"], "late timeout") == completed
    with error("REQUEST_CONFLICT"):
        ledger.complete_asset_registration("s", SCOPE, asset["asset_id"], receipt(asset, "different-remote"))
    assert ledger.db.execute("SELECT * FROM reference_policy").fetchone()[:] == policy_row
    for call in (
        lambda: ledger.reserve_asset_registration("s", SCOPE, asset_id=asset["asset_id"], expected_auth_version=2),
        lambda: ledger.validate_reference_authorization("s", SCOPE, expected_auth_version=2),
        lambda: import_image(ledger, auth=2),
    ):
        with error("AUTH_REQUIRED"):
            call()


@pytest.mark.parametrize("change,code", [
    ({"sha256": "0" * 64}, "REQUEST_CONFLICT"), ({"mime": "image/jpeg"}, "REQUEST_CONFLICT"),
    ({"size_bytes": 1}, "REQUEST_CONFLICT"), ({"size_bytes": True}, "INVALID_ARGUMENT"),
    ({"asset_id": ""}, "INVALID_ARGUMENT"), ({"request_id": "forged"}, "INVALID_ARGUMENT"),
])
def test_receipt_mismatch_cannot_complete_original_registration(ledger, change, code):
    policy(ledger)
    asset = import_image(ledger)
    ledger.reserve_asset_registration("s", SCOPE, asset_id=asset["asset_id"], expected_auth_version=1)
    ledger.claim_asset_registration("s", SCOPE, asset["asset_id"], expected_auth_version=1)
    with error(code):
        ledger.complete_asset_registration("s", SCOPE, asset["asset_id"], {**receipt(asset), **change})
    assert ledger.get_asset_registration("s", SCOPE, asset["asset_id"])["state"] == "submitting"


def test_scoped_history_image_and_ledger_are_inaccessible_to_other_owner(ledger):
    policy(ledger)
    asset = import_image(ledger)
    ledger.reserve_asset_registration("s", SCOPE, asset_id=asset["asset_id"], expected_auth_version=1)
    ledger.create_space("other", OTHER, binding_id="reference-service")
    ledger.grant("other", OTHER, Delegation("other-consent", "admin", continuous=True))
    for call in (
        lambda: ledger.get_asset_image("s", OTHER, asset["asset_id"]),
        lambda: ledger.get_asset_image("other", OTHER, asset["asset_id"]),
        lambda: ledger.get_asset_registration("s", OTHER, asset["asset_id"]),
        lambda: ledger.complete_asset_registration("other", OTHER, asset["asset_id"], receipt(asset)),
        lambda: ledger.configure_reference_policy("s", OTHER, enabled=True, consent_ref=CONSENT, expected_auth_version=1),
    ):
        with error("NOT_FOUND"):
            call()
    assert ledger.get_asset_registration("other", OTHER, asset["asset_id"]) is None


def test_original_image_tampering_fails_read_claim_and_completion_without_replay(ledger):
    policy(ledger)
    asset = import_image(ledger)
    ledger.reserve_asset_registration("s", SCOPE, asset_id=asset["asset_id"], expected_auth_version=1)
    ledger.db.execute("UPDATE external_assets SET data=?", (picture(color="red"),))
    for call in (
        lambda: ledger.get_asset_image("s", SCOPE, asset["asset_id"]),
        lambda: ledger.claim_asset_registration("s", SCOPE, asset["asset_id"], expected_auth_version=1),
    ):
        with error("ARTIFACT_INVALID"):
            call()
    assert ledger.get_asset_registration("s", SCOPE, asset["asset_id"])["state"] == "reserved"


def test_registered_attempt_cannot_complete_after_original_byte_tampering(ledger):
    policy(ledger)
    asset = import_image(ledger)
    ledger.reserve_asset_registration("s", SCOPE, asset_id=asset["asset_id"], expected_auth_version=1)
    ledger.claim_asset_registration("s", SCOPE, asset["asset_id"], expected_auth_version=1)
    ledger.db.execute("UPDATE external_assets SET data=?", (picture(color="red"),))
    with error("ARTIFACT_INVALID"):
        ledger.complete_asset_registration("s", SCOPE, asset["asset_id"], receipt(asset))
    assert ledger.get_asset_registration("s", SCOPE, asset["asset_id"])["state"] == "submitting"


def test_registration_claim_rechecks_service_binding(ledger):
    policy(ledger)
    asset = import_image(ledger)
    ledger.reserve_asset_registration("s", SCOPE, asset_id=asset["asset_id"], expected_auth_version=1)
    ledger.db.execute("UPDATE asset_registrations SET binding_id='different-service'")
    with error("REQUEST_CONFLICT"):
        ledger.claim_asset_registration("s", SCOPE, asset["asset_id"], expected_auth_version=1)
    assert ledger.get_asset_registration("s", SCOPE, asset["asset_id"])["state"] == "reserved"


def test_concurrent_claim_has_one_winner(ledger):
    policy(ledger)
    asset = import_image(ledger)
    ledger.reserve_asset_registration("s", SCOPE, asset_id=asset["asset_id"], expected_auth_version=1)
    barrier = Barrier(2)

    def worker(_):
        connection = ArtistStore(ledger.path)
        try:
            barrier.wait(timeout=5)
            try:
                return connection.claim_asset_registration("s", SCOPE, asset["asset_id"], expected_auth_version=1)["state"]
            except StoreError as exc:
                return exc.code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(worker, range(2))) == ["INVALID_STATE", "submitting"]


def test_quota_is_shared_with_reviewed_work_and_inactive_external_assets(ledger, monkeypatch):
    limits = Limits(think=10, render=10, share=0, max_inflight=10)
    ledger.set_global_limits(limits)
    ledger.set_limits("s", SCOPE, limits)
    reviewed(ledger, "reviewed", data=picture(), space="s", scope=SCOPE)
    reviewed_asset = ledger.archive_version("s", SCOPE, version_id="reviewed", roles=["style"], tags=[],
                                            reason="reviewed", expected_auth_version=1)
    policy(ledger)
    external = import_image(ledger)
    ledger.deactivate_asset("s", SCOPE, external["asset_id"], expected_auth_version=1)
    monkeypatch.setattr(store_module, "MAX_ASSETS", 2)
    with error("ASSET_QUOTA"):
        import_image(ledger, event="third")
    monkeypatch.setattr(store_module, "MAX_ASSETS", 100)
    monkeypatch.setattr(store_module, "MAX_ASSET_BYTES", reviewed_asset["size_bytes"] + external["size_bytes"])
    with error("ASSET_QUOTA"):
        import_image(ledger, event="one-more-byte")
    assert ledger.db.execute("SELECT count(*) FROM asset_catalog").fetchone()[0] == 2


def schema_5(path):
    before = make_schema_4(path)
    db = sqlite3.connect(path)
    for statement in store_module._SCHEMA_5:
        db.execute(statement)
    db.execute("""INSERT INTO asset_catalog VALUES('s','old-asset','v','w','job',?,'image/png',5,
               '["style"]','[]','archived','original summary','original description','reviewed_work','succeeded',1,1,'then')""",
               (hashlib.sha256(b"image").hexdigest(),))
    db.execute("INSERT INTO asset_selection VALUES('s','old-asset',0,1,1)")
    db.execute("PRAGMA user_version=5")
    db.commit()
    for table in ("asset_catalog", "asset_selection"):
        before[table] = db.execute(f"SELECT * FROM {table}").fetchall()
    db.close()
    return before


def test_schema_5_rebuild_preserves_every_old_row_selection_and_foreign_key(tmp_path):
    path = tmp_path / "v5.sqlite3"
    before = schema_5(path)
    for _ in range(2):
        db = ArtistStore(path)
        try:
            assert db.db.execute("PRAGMA user_version").fetchone()[0] == 6
            for table, rows in before.items():
                assert [tuple(row) for row in db.db.execute(f"SELECT * FROM {table}")] == rows
            assert db.db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert db.db.execute("SELECT count(*) FROM external_assets").fetchone()[0] == 0
            assert db.db.execute("SELECT count(*) FROM asset_registrations").fetchone()[0] == 0
        finally:
            db.close()


def test_schema_6_rebuild_failure_restores_original_parent_child_tables_and_version(tmp_path, monkeypatch):
    path = tmp_path / "v5.sqlite3"
    before = schema_5(path)
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_SCHEMA_6", (*store_module._SCHEMA_6, "CREATE TABLE works(bad TEXT)"))
        with pytest.raises(sqlite3.DatabaseError):
            ArtistStore(path)
    db = sqlite3.connect(path)
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == set(before)
        for table, rows in before.items():
            assert db.execute(f"SELECT * FROM {table}").fetchall() == rows
    finally:
        db.close()


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_older_schemas_upgrade_directly_to_6_without_changing_existing_rows(tmp_path, version):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    for statements in (store_module._SCHEMA, store_module._SCHEMA_2, store_module._SCHEMA_3,
                       store_module._SCHEMA_4)[:version]:
        for statement in statements:
            db.execute(statement)
    db.execute("INSERT INTO spaces VALUES('s',?,'old-binding',1,1)", (SCOPE.snapshot(),))
    db.execute("PRAGMA user_version=" + str(version))
    db.commit()
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    db.close()
    upgraded = ArtistStore(path)
    try:
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert upgraded.db.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, rows in before.items():
            assert [tuple(row) for row in upgraded.db.execute(f"SELECT * FROM {table}")] == rows
    finally:
        upgraded.close()


def test_older_schema_client_rejects_v6_without_writes_and_readonly_history_stays_available(ledger, monkeypatch):
    policy(ledger)
    asset = import_image(ledger)
    before = ledger.path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "SCHEMA_VERSION", 5)
        with error("SCHEMA_UNSUPPORTED"):
            ArtistStore(ledger.path)
    assert ledger.path.read_bytes() == before
    readonly = sqlite3.connect(ledger.path.as_uri() + "?mode=ro", uri=True)
    try:
        assert readonly.execute("SELECT source_kind FROM asset_catalog").fetchone()[0] == "authorized_chat"
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute("DELETE FROM asset_catalog")
    finally:
        readonly.close()
    assert ledger.get_asset_image("s", SCOPE, asset["asset_id"])["data"] == picture()


def test_render_reservation_keeps_reference_transport_params_and_old_owner_validation(ledger):
    policy(ledger)
    asset = import_image(ledger)
    limits = Limits(render=10, max_inflight=10)
    ledger.set_global_limits(limits)
    ledger.set_limits("s", SCOPE, limits)
    ledger.create_work("s", SCOPE, "w")
    params = {"prompt": "reference render", "transport_operation": "render",
              "reference_inputs": [{"asset_id": "remote", "sha256": asset["sha256"], "role": "style"}]}
    operation = ledger.reserve("s", SCOPE, request_id="render", action="submit", work_id="w", params=params)
    assert operation["category"] == "render" and operation["params"] == params
    with error():
        ledger.reserve("s", SCOPE, request_id="forged", action="submit", work_id="w", params={**params, "owner": "forged"})
